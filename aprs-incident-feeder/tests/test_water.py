"""Tests for aprs_water.py: which gauges become objects, and what a failed poll does.

  python3 -m unittest test_water        (from this folder)

Nothing here reaches the real gauge service: the poll tests ask a fake one on 127.0.0.1.
"""
import contextlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import aprs_priority
import aprs_water as W

NOW = datetime(2026, 11, 25, 2, 0, tzinfo=timezone.utc)


def gauge(lid, name, category, level=9.7, minutes_ago=15, state="NJ", lat=40.0817, lon=-74.8697):
    when = (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"lid": lid, "name": name, "state": {"abbreviation": state}, "latitude": lat, "longitude": lon,
            "status": {"observed": {"primary": level, "primaryUnit": "ft", "floodCategory": category, "validTime": when},
                       "forecast": {"primary": 12.0, "floodCategory": "major", "validTime": when}}}


class WhichGaugesShow(unittest.TestCase):
    def test_flooding_and_low_water_show_and_a_normal_gauge_does_not(self):
        for category, want in (("minor", "minor"), ("moderate", "moderate"), ("major", "major"), ("low_threshold", "low"),
                               ("no_flooding", None), ("not_defined", None), ("obs_not_current", None), ("action", None)):
            got = W.condition(gauge("BDRN4", "x", category), NOW)
            self.assertEqual(got and got[0], want, category)

    def test_the_forecast_alone_never_makes_an_object(self):
        self.assertIsNone(W.condition(gauge("BDRN4", "x", "no_flooding"), NOW))       # its forecast says major

    def test_action_stage_and_low_water_are_choices(self):
        self.assertEqual(W.condition(gauge("A", "x", "action"), NOW, min_flood="action")[0], "action")
        self.assertIsNone(W.condition(gauge("A", "x", "minor"), NOW, min_flood="moderate"))
        self.assertIsNone(W.condition(gauge("A", "x", "low_threshold"), NOW, low_water=False))

    def test_an_old_missing_or_broken_reading_does_not_count(self):
        self.assertIsNone(W.condition(gauge("A", "x", "major", minutes_ago=7 * 60), NOW))
        self.assertIsNone(W.condition(gauge("A", "x", "major", level=-999), NOW))
        self.assertIsNone(W.condition(gauge("A", "x", "major", minutes_ago=-300), NOW))   # dated in the future
        self.assertIsNone(W.condition({"lid": "A", "status": {}}, NOW))

    def test_the_text_says_what_how_high_where_and_when_and_fits(self):
        when = NOW - timedelta(minutes=45)                                             # 01:15 UTC = 20:15 Eastern
        self.assertEqual(W.comment_for("minor", 9.7, when, "Delaware River at Burlington (IN MLLW)"),
                         "MINOR FLOOD 9.7ft Delaware River at Burlington @20:15")
        long = W.comment_for("low", -0.32, when, "Delaware River at New Hope-Lambertville Bridge near Somewhere Else", 43)
        self.assertLessEqual(len(long), 43)
        self.assertTrue(long.startswith("LOW WATER -0.3ft Delaware River") and long.endswith("@20:15"), long)

    def test_the_importance_follows_the_flood_category(self):
        for what, want in (("major", "critical"), ("moderate", "high"), ("minor", "medium"), ("action", "low"), ("low", "low")):
            text = W.comment_for(what, 9.7, NOW, "Delaware River at Burlington")
            r = aprs_priority.rate("RIVBDRN4", text, "water")
            self.assertEqual((r["level"], r["rated"]), (want, True), text)


class FakeService(http.server.ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.gauges, self.status, self.asked = [], 200, []
        threading.Thread(target=self.serve_forever, daemon=True).start()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.server.asked.append(self.path)
        body = json.dumps({"gauges": self.server.gauges}).encode()
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Polling(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        old = os.environ.get("APRS_DATA_DIR")
        os.environ["APRS_DATA_DIR"] = self.dir.name
        self.addCleanup(lambda: os.environ.pop("APRS_DATA_DIR") if old is None else os.environ.update(APRS_DATA_DIR=old))
        self.service = FakeService()
        self.addCleanup(self.service.server_close)
        self.addCleanup(self.service.shutdown)
        self.state = os.path.join(self.dir.name, "water_live_state.json")
        self.args = W.parse_args(["--url", f"http://127.0.0.1:{self.service.server_address[1]}/gauges",
                                  "--callsign", "TEST-13", "--state-file", self.state, "--once"])
        self.live = {}

    def saved(self):
        with open(self.state) as f:
            return json.load(f)

    def poll(self, **change):
        for k, v in change.items():
            setattr(self.args, k, v)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            counts = W.run_cycle(self.args, self.live, None, now=NOW)
        return counts, [line.split(":", 1)[1] for line in buf.getvalue().splitlines()]

    def test_only_flooding_and_low_gauges_become_objects_and_a_recovered_one_is_killed(self):
        self.service.gauges = [gauge("BDRN4", "Delaware River at Burlington (IN MLLW)", "minor"),
                               gauge("NHPP1", "Delaware River at New Hope", "low_threshold", level=-0.3, state="PA", lat=40.3647, lon=-74.9489),
                               gauge("PHBP1", "Delaware River at Philadelphia", "no_flooding", state="PA"),
                               gauge("bad lid!", "junk", "major"), "junk"]
        counts, out = self.poll()
        self.assertEqual(counts, (5, 2, 0))
        self.assertEqual(sorted(p[1:10].strip() for p in out), ["RIVBDRN4", "RIVNHPP1"])
        flood = next(p for p in out if p.startswith(";RIVBDRN4"))
        self.assertEqual(flood[10], "*")
        self.assertEqual((flood[26], flood[36]), ("\\", "w"))                          # the flooding symbol
        self.assertTrue(flood.endswith("wMINOR FLOOD 9.7ft Delaware River at Burlington @20:45"), flood)
        self.assertIn("bbox.xmin=-76.2", self.service.asked[0])
        self.assertEqual(sorted(self.saved()["objects"]), ["RIVBDRN4", "RIVNHPP1"])
        self.service.gauges[0] = gauge("BDRN4", "Delaware River at Burlington (IN MLLW)", "no_flooding", level=5.0)
        counts, out = self.poll()
        self.assertEqual(counts, (5, 1, 1))
        kill = next(p for p in out if p.startswith(";RIVBDRN4"))
        self.assertEqual((kill[10], len(kill)), ("_", 37))                             # a kill, without text
        self.assertEqual(list(self.live), ["RIVNHPP1"])

    def test_a_failed_or_empty_answer_kills_nothing(self):
        self.service.gauges = [gauge("BDRN4", "Delaware River at Burlington", "major")]
        self.poll()
        for status, gauges in ((500, self.service.gauges), (200, [])):
            self.service.status, self.service.gauges = status, gauges
            with self.assertRaises((OSError, ValueError)):
                self.poll()
            self.assertEqual(list(self.live), ["RIVBDRN4"])
            self.assertEqual(list(self.saved()["objects"]), ["RIVBDRN4"])

    def test_states_can_be_limited_and_settings_are_checked(self):
        self.service.gauges = [gauge("BDRN4", "Burlington", "minor"), gauge("PHBP1", "Philadelphia", "minor", state="PA")]
        counts, out = self.poll(states={"PA"})
        self.assertEqual([p[1:10].strip() for p in out], ["RIVPHBP1"])
        with contextlib.redirect_stderr(io.StringIO()):
            for bad in (["--box", "1,2,3"], ["--box", "41,-76,40,-74"], ["--url", "ftp://x"], ["--send"], ["--min-flood", "huge"]):
                with self.assertRaises(SystemExit, msg=bad):
                    W.parse_args(bad)
        self.assertEqual(W.parse_args(["--interval", "30"]).interval, 300)


if __name__ == "__main__":
    unittest.main()
