"""Tests for aprs_radio.py.  Nothing here touches a radio or the real feeder: the feeder is a
fake HTTP server and the TNC a fake KISS server, both on 127.0.0.1, plus a pseudo-terminal.

  python3 -m unittest test_radio        (from this folder)
"""
import http.server
import json
import os
import socketserver
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import aprs_radio as R

ME, FEEDER = "W3XYZ-10", "K3YLE-15"


def rf(name, text="BUILDING FIRE MAIN ST", live=True, stamp="241500z", lat="4007.20N"):
    return f"}}{FEEDER}>APRS,TCPIP,{ME}*:;{name:<9}{'*' if live else '_'}{stamp}{lat}/07520.40Wd{text if live else ''}"


def listing(*packets):
    return [{"rf": p} for p in packets]


class Frames(unittest.TestCase):
    def test_address_bytes(self):
        self.assertEqual(R.ax25_address("APRS", command=True), bytes([0x82, 0xA0, 0xA4, 0xA6, 0x40, 0x40, 0xE0]))
        self.assertEqual(R.ax25_address("W3XYZ-10", last=True), bytes([0xAE, 0x66, 0xB0, 0xB2, 0xB4, 0x40, 0x75]))
        for bad in ("TOOLONGCALL", "W3XYZ-16", "W3 XYZ", ""):
            with self.assertRaises(ValueError):
                R.ax25_address(bad)

    def test_ui_frame_layout(self):
        f = R.ax25_ui_frame("W3XYZ-10", "APZIFD", ["WIDE2-1"], "}hello")
        self.assertEqual(len(f), 7 * 3 + 2 + 6)
        self.assertEqual(f[13] & 1, 0)                       # source is not the last address ...
        self.assertEqual(f[20] & 1, 1)                       # ... the digipeater is
        self.assertEqual(f[21:23], b"\x03\xf0")              # UI frame, no layer 3
        self.assertEqual(f[23:], b"}hello")
        self.assertEqual(R.ax25_ui_frame("W3XYZ-10", "APZIFD", [], "x")[13] & 1, 1)
        with self.assertRaises(ValueError):
            R.ax25_ui_frame("W3XYZ-10", "APZIFD", [], "x" * 257)

    def test_kiss_escapes_its_own_markers(self):
        self.assertEqual(R.kiss_frame(bytes([1, 0xC0, 2, 0xDB, 3])), bytes([0xC0, 0x00, 1, 0xDB, 0xDC, 2, 0xDB, 0xDD, 3, 0xC0]))
        self.assertEqual(R.kiss_frame(b"x", port=1)[1], 0x10)


class Packets(unittest.TestCase):
    def test_reading_a_third_party_object(self):
        start, name, live = R.object_of(rf("FIRE1"))
        self.assertEqual((name, live, rf("FIRE1")[start]), ("FIRE1", True, ";"))
        self.assertEqual(R.object_of(rf("FIRE1", live=False))[2], False)
        for junk in ("", "}K3YLE-15>APRS,TCPIP,W3XYZ-10*:>status", "K3YLE-15>APRS:;FIRE1    *241500z4007.20N/07520.40Wd", "}x:;short"):
            self.assertIsNone(R.object_of(junk))

    def test_kill_keeps_name_time_and_position_and_drops_the_text(self):
        self.assertEqual(R.kill_of(rf("FIRE1")), rf("FIRE1", live=False))

    def test_body_ignores_the_time_stamp(self):
        self.assertEqual(R.body_of(rf("FIRE1", stamp="241500z")), R.body_of(rf("FIRE1", stamp="241502z")))
        self.assertNotEqual(R.body_of(rf("FIRE1")), R.body_of(rf("FIRE1", text="OUT")))


class Plan(unittest.TestCase):
    def setUp(self):
        self.c = R.Carried()

    def go(self, packets, now, refresh=1800, max_objects=10):
        plan = self.c.plan(packets, now, refresh, max_objects)
        for reason, name, packet in plan:
            self.c.sent(reason, name, packet, now)
        return [(reason, name) for reason, name, _ in plan]

    def test_new_then_quiet_then_refresh(self):
        self.assertEqual(self.go(listing(rf("FIRE1")), 0), [("new", "FIRE1")])
        self.assertEqual(self.go(listing(rf("FIRE1", stamp="241502z")), 120), [])
        self.assertEqual(self.go(listing(rf("FIRE1")), 1799), [])
        self.assertEqual(self.go(listing(rf("FIRE1")), 1800), [("refresh", "FIRE1")])
        self.assertEqual(self.go(listing(rf("FIRE1")), 1920), [])

    def test_change_is_sent(self):
        self.go(listing(rf("FIRE1")), 0)
        self.assertEqual(self.go(listing(rf("FIRE1", text="FIRE OUT")), 120), [("changed", "FIRE1")])
        self.assertEqual(self.go(listing(rf("FIRE1", lat="4007.30N", text="FIRE OUT")), 240), [("changed", "FIRE1")])

    def test_the_feeders_kill_is_passed_on_once_and_only_for_what_we_sent(self):
        self.go(listing(rf("FIRE1")), 0)
        kills = listing(rf("FIRE1", live=False, stamp="241530z"), rf("OTHER1", live=False))
        plan = self.c.plan(kills, 120, 1800, 10)
        self.assertEqual(plan, [("kill", "FIRE1", rf("FIRE1", live=False, stamp="241530z"))])
        self.assertEqual(self.go(kills, 120), [("kill", "FIRE1")])
        self.assertEqual(self.go(kills, 240), [])

    def test_an_object_that_leaves_the_list_is_killed_from_our_own_copy(self):
        self.go(listing(rf("FIRE1"), rf("FIRE2")), 0)
        plan = self.c.plan(listing(rf("FIRE2")), 120, 1800, 10)
        self.assertEqual(plan, [("dropped", "FIRE1", rf("FIRE1", live=False))])

    def test_only_the_first_few_are_carried_and_kills_go_first(self):
        five = listing(*[rf(f"OBJ{i}") for i in range(5)])
        self.assertEqual([n for _r, n in self.go(five, 0, max_objects=2)], ["OBJ0", "OBJ1"])
        bumped = listing(rf("NEW1"), *[rf(f"OBJ{i}") for i in range(5)])        # something more important arrives
        self.assertEqual(self.go(bumped, 120, max_objects=2), [("dropped", "OBJ1"), ("new", "NEW1")])

    def test_junk_in_the_list_is_ignored(self):
        junk = [None, {}, {"rf": 5}, {"rf": "}x:>status"}, {"rf": rf("LONG", text="x" * 300)}, {"rf": rf("FIRE1")}, {"rf": rf("FIRE1")}]
        self.assertEqual(self.go(junk, 0), [("new", "FIRE1")])

    def test_what_was_sent_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            c = R.Carried(path)
            c.sent("new", "FIRE1", rf("FIRE1"), 1000)
            c.save()
            self.assertEqual(R.Carried(path).plan(listing(rf("FIRE1")), 1100, 1800, 10), [])
            with open(path, "w") as f:
                f.write("not json")
            self.assertEqual(R.Carried(path).objects, {})


class FakeFeeder(http.server.ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), FeederHandler)
        self.packets, self.requests, self.status = [], [], 200
        threading.Thread(target=self.serve_forever, daemon=True).start()


class FeederHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.server.requests.append((self.path, self.headers.get("User-Agent")))
        body = json.dumps({"packets": self.server.packets, "bulletins": getattr(self.server, "bulletins", [])}).encode() if self.server.status == 200 else b"{}"
        self.send_response(self.server.status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeTnc(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), TncHandler)
        self.data = bytearray()
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def frames(self):
        return [bytes(f) for f in self.data.split(b"\xc0") if f]


class TncHandler(socketserver.BaseRequestHandler):
    def handle(self):
        while True:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            self.server.data += chunk


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.feeder, self.tnc = FakeFeeder(), FakeTnc()
        for s in (self.feeder, self.tnc):
            self.addCleanup(s.server_close)
            self.addCleanup(s.shutdown)

    def gateway(self, *extra):
        args = R.parse_args(["--server", f"http://127.0.0.1:{self.feeder.server_address[1]}", "--callsign", ME,
                             "--lat", "40.12", "--lon", "-75.34", "--state-file", os.path.join(self.dir.name, "state.json"),
                             "--kiss-tcp", f"127.0.0.1:{self.tnc.server_address[1]}", *extra])
        host, _, port = args.kiss_tcp.rpartition(":")
        self.waits = []
        return R.Gateway(args, R.KissTcp(host, int(port)), R.Carried(args.state_file), sleep=self.waits.append)

    def wait_frames(self, n):
        end = time.time() + 5
        while len(self.tnc.frames()) < n and time.time() < end:
            time.sleep(0.02)
        return self.tnc.frames()

    def test_asks_the_feeder_with_our_callsign_area_and_level(self):
        self.gateway("--miles", "15", "--min", "high").cycle(1000)
        path, agent = self.feeder.requests[0]
        self.assertEqual(path, "/api/packets?lat=40.12000&lon=-75.34000&miles=15&min=high&via=W3XYZ-10")
        self.assertIn("aprs-radio/", agent)

    def test_without_transmit_nothing_reaches_the_tnc_and_nothing_is_recorded(self):
        self.feeder.packets = listing(rf("FIRE1"))
        self.assertEqual(self.gateway().cycle(1000), 1)
        time.sleep(0.1)
        self.assertEqual(self.tnc.frames(), [])
        self.assertFalse(os.path.exists(os.path.join(self.dir.name, "state.json")))

    def test_transmits_valid_kiss_frames_paced_and_only_once(self):
        self.feeder.packets = listing(rf("FIRE1"), rf("FIRE2"))
        g = self.gateway("--transmit", "--path", "WIDE2-1", "--gap", "5")
        self.assertEqual(g.cycle(1000), 2)
        frames = self.wait_frames(2)
        self.assertEqual(frames[0], R.kiss_frame(R.ax25_ui_frame(ME, "APZIFD", ["WIDE2-1"], rf("FIRE1")))[1:-1])
        self.assertEqual(self.waits, [5.0])                               # one gap between two packets
        self.assertEqual(g.cycle(1120), 0)                                # unchanged: silent
        self.feeder.packets = listing(rf("FIRE1", live=False), rf("FIRE2"))
        self.assertEqual(g.cycle(1240), 1)
        self.assertTrue(self.wait_frames(3)[2].endswith(rf("FIRE1", live=False).encode()))
        self.assertEqual(self.gateway("--transmit").cycle(1360), 0)       # a restart remembers

    def test_no_more_than_the_limit_per_poll_and_the_rest_follow(self):
        self.feeder.packets = listing(*[rf(f"OBJ{i}") for i in range(5)])
        g = self.gateway("--transmit", "--max-per-poll", "2")
        self.assertEqual([g.cycle(1000), g.cycle(1120), g.cycle(1240), g.cycle(1360)], [2, 2, 1, 0])
        self.assertEqual(len(self.wait_frames(5)), 5)

    def test_feeder_trouble_transmits_and_kills_nothing(self):
        self.feeder.packets = listing(rf("FIRE1"))
        g = self.gateway("--transmit")
        g.cycle(1000)
        self.feeder.status = 500
        self.assertEqual(g.cycle(1120), 0)
        self.assertIn("FIRE1", g.carried.objects)
        self.feeder.shutdown()
        self.feeder.server_close()
        self.assertEqual(g.cycle(1240), 0)
        self.assertEqual(len(self.wait_frames(1)), 1)

    def test_tnc_trouble_is_reported_and_tried_again_next_poll(self):
        self.feeder.packets = listing(rf("FIRE1"))
        args_port = self.tnc.server_address[1]
        self.tnc.shutdown()
        self.tnc.server_close()
        g = self.gateway("--transmit")
        self.assertEqual(g.cycle(1000), 0)
        self.assertEqual(g.carried.objects, {})
        self.tnc = FakeTnc.__new__(FakeTnc)
        socketserver.ThreadingTCPServer.__init__(self.tnc, ("127.0.0.1", args_port), TncHandler)
        self.tnc.data = bytearray()
        threading.Thread(target=self.tnc.serve_forever, daemon=True).start()
        self.assertEqual(g.cycle(1120), 1)

    def test_settings_are_checked(self):
        base = ["--server", "http://127.0.0.1:1", "--lat", "40", "--lon", "-75"]
        for bad in (["--callsign", "N0CALL"], ["--callsign", "W3XYZ-99"], ["--callsign", ME, "--path", "NOT A PATH"],
                    ["--callsign", ME, "--min", "urgent"]):
            with self.assertRaises(SystemExit):
                with open(os.devnull, "w") as null:
                    old, sys.stderr = sys.stderr, null
                    try:
                        R.parse_args(base + bad)
                    finally:
                        sys.stderr = old
        a = R.parse_args(base + ["--callsign", ME, "--poll", "5", "--gap", "0", "--refresh", "1"])
        self.assertEqual((a.poll, a.gap, a.refresh), (60, 2.0, 10.0))     # floors that protect the channel


@unittest.skipUnless(hasattr(os, "openpty"), "needs a pseudo-terminal")
class Serial(unittest.TestCase):
    def test_serial_port_is_set_raw_and_carries_the_frame(self):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        tnc = R.KissSerial(os.ttyname(slave), 9600)
        frame = R.kiss_frame(R.ax25_ui_frame(ME, "APZIFD", [], rf("FIRE1") + "\n\r"))    # raw: no line-ending rewriting
        tnc.send(frame)
        got = b""
        while len(got) < len(frame):
            got += os.read(master, 4096)
        tnc.close()
        self.assertEqual(got, frame)


def bln(slot, text="Tornado Warning til 9:15PM"):
    return {"rf": f"}}{FEEDER}>APRS,TCPIP,{ME}*::BLN{slot}     :{text}", "bulletin": True}


class Bulletins(unittest.TestCase):
    def setUp(self):
        self.c = R.Carried()

    go = Plan.go

    def test_a_bulletin_is_sent_when_new_or_changed_and_again_after_the_refresh_time(self):
        self.assertEqual(R.bulletin_of(bln(1)["rf"]), "BLN1")
        self.assertIsNone(R.bulletin_of(rf("FIRE1")))
        self.assertEqual(self.go([bln(1)], 0), [("bulletin", "BLN1")])
        self.assertEqual(self.go([bln(1)], 120), [])
        self.assertEqual(self.go([bln(1, "Tornado Warning til 9:45PM")], 240), [("bulletin", "BLN1")])
        self.assertEqual(self.go([bln(1, "Tornado Warning til 9:45PM")], 240 + 1800), [("bulletin again", "BLN1")])

    def test_bulletins_go_after_kills_and_before_objects_and_end_without_a_kill(self):
        self.go(listing(rf("FIRE1")), 0)
        plan = self.go(listing(rf("FIRE1", live=False), rf("TOR1", "Tornado Warning")) + [bln(1), bln(2, "EXPLOSION OAK ST")], 120)
        self.assertEqual(plan, [("kill", "FIRE1"), ("bulletin", "BLN1"), ("bulletin", "BLN2"), ("new", "TOR1")])
        self.assertEqual(self.go(listing(rf("TOR1", "Tornado Warning")), 240), [])      # the bulletins ended: nothing sent
        self.assertEqual(self.c.bulletins, {})
        self.assertEqual(self.go(listing(rf("TOR1", "Tornado Warning")) + [bln(1)], 360), [("bulletin", "BLN1")])   # back: new again

    def test_bulletins_can_be_left_out_and_junk_is_ignored(self):
        self.assertEqual(self.c.plan([bln(1)], 0, 1800, 10, bulletins=False), [])
        junk = [{"rf": ":BLN1     :not third-party", "bulletin": True}, {"rf": bln(1)["rf"] + "\u00e9", "bulletin": True},
                {"rf": f"}}{FEEDER}>APRS,TCPIP,{ME}*::W3XYZ    :a message to one station", "bulletin": True}]
        self.assertEqual(self.go(junk, 0), [])

    def test_bulletins_survive_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            c = R.Carried(path)
            c.sent("bulletin", "BLN1", bln(1)["rf"], 100.0)
            c.save()
            self.assertEqual(R.Carried(path).bulletins, {"BLN1": {"rf": bln(1)["rf"], "sent": 100.0}})
            self.assertEqual(R.Carried(path).plan([bln(1)], 200, 1800, 10), [])


if __name__ == "__main__":
    unittest.main()
