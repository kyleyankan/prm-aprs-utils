"""Tests for GET /api/packets in aprs_web.py: our own packets within a radius of a point.

  python3 -m unittest test_web        (from this folder; reads temporary files only, no network)
"""
import argparse
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import aprs_priority
import aprs_send
import aprs_web

CALL = "TEST-13"
HOME = (40.12, -75.34)


def obj(name, lat, lon, comment="Fire Main St", live=True):
    return f";{name:<9}{'*' if live else '_'}241500z{lat}/{lon}d{comment if live else ''}"


class PacketsNear(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        old = os.environ.get("APRS_DATA_DIR")
        os.environ["APRS_DATA_DIR"] = self.dir.name
        self.addCleanup(lambda: os.environ.pop("APRS_DATA_DIR") if old is None else os.environ.update(APRS_DATA_DIR=old))
        aprs_web.ARGS = argparse.Namespace(send=True, callsign=CALL, proxied="packets")
        book = aprs_send.Book(CALL, os.path.join(self.dir.name, aprs_send.STATE_FILE))
        for packet in (obj("NEAR1", "4007.20N", "07520.40W"),               # at home
                       obj("NEAR2", "4012.00N", "07520.40W"),               # about 5.5 miles north
                       obj("FAR1", "4100.00N", "07520.40W"),                # about 61 miles north
                       obj("GONE1", "4007.20N", "07521.00W", live=False),   # killed, close by
                       ">a status line"):
            key = aprs_send.split_packet(packet)[0]
            book.offer(packet, "montco", 1000)
            book.mark_sent(key, 1000)
        book.offer(obj("WAIT1", "4007.20N", "07520.40W"), "montco", 1000)   # never sent yet
        book.save()

    def names(self, **q):
        code, payload = aprs_web.packets_near({"lat": str(HOME[0]), "lon": str(HOME[1]), **q})
        self.assertEqual(code, 200)
        return [p["name"] for p in payload["packets"]]

    def test_only_what_is_inside_the_radius_kills_first_then_nearest(self):
        self.assertEqual(self.names(miles="10"), ["GONE1", "NEAR1", "NEAR2"])
        self.assertEqual(self.names(miles="3"), ["GONE1", "NEAR1"])
        self.assertEqual(self.names(miles="100"), ["GONE1", "NEAR1", "NEAR2", "FAR1"])

    def test_default_radius_is_25_miles(self):
        self.assertEqual(self.names(), ["GONE1", "NEAR1", "NEAR2"])

    def test_kills_can_be_left_out_and_the_list_cut_short(self):
        self.assertEqual(self.names(miles="10", kills="0"), ["NEAR1", "NEAR2"])
        self.assertEqual(self.names(miles="10", limit="2"), ["GONE1", "NEAR1"])

    def test_since_keeps_only_what_changed_after_that_time(self):
        self.assertEqual(self.names(miles="10", since="1001"), [])
        self.assertEqual(len(self.names(miles="10", since="1000")), 3)

    def test_each_packet_is_given_as_sent_and_ready_for_a_gateway(self):
        _code, payload = aprs_web.packets_near({"lat": "40.12", "lon": "-75.34", "miles": "3", "via": "w3xyz-10"})
        live = [p for p in payload["packets"] if p["live"]][0]
        packet = obj("NEAR1", "4007.20N", "07520.40W")
        self.assertEqual(live["info"], packet)
        self.assertEqual(live["tnc2"], f"{CALL}>APRS,TCPIP*:{packet}")
        self.assertEqual(live["rf"], f"}}{CALL}>APRS,TCPIP,W3XYZ-10*:{packet}")
        self.assertEqual((live["miles"], live["source"], live["sent"]), (0.0, "montco", 1000))
        self.assertNotIn("rf", aprs_web.packets_near({"lat": "40.12", "lon": "-75.34"})[1]["packets"][0])

    def test_min_keeps_the_important_ones_and_they_come_first(self):
        book = aprs_send.Book(CALL, os.path.join(self.dir.name, aprs_send.STATE_FILE))
        for name, text in (("FIRE1", "BUILDING FIRE MAIN ST"), ("FIRE2", "FIRE ALARM OAK ST"),
                           ("TORPHI123", "TORNADO WRN til 3PM"), ("WORK1", "roadwork US 202")):
            packet = obj(name, "4008.00N", "07520.40W", text)
            book.offer(packet, "montco", 1000)
            book.mark_sent(aprs_send.split_packet(packet)[0], 1000)
        book.save()
        self.assertEqual(self.names(miles="10")[:4], ["GONE1", "TORPHI123", "FIRE1", "NEAR1"])
        self.assertEqual(self.names(miles="10", min="high"), ["GONE1", "TORPHI123", "FIRE1"])
        self.assertEqual(self.names(miles="10", min="critical", kills="0"), ["TORPHI123"])
        _code, payload = aprs_web.packets_near({"lat": "40.12", "lon": "-75.34", "miles": "10"})
        self.assertEqual(payload["levels"], {"low": 2, "medium": 2, "high": 1, "critical": 1})
        tor = [p for p in payload["packets"] if p["name"] == "TORPHI123"][0]
        self.assertEqual((tor["level"], tor["reasons"]), ("critical", ["tornado warning"]))
        self.assertEqual(aprs_web.packets_near({"lat": "40", "lon": "-75", "min": "urgent"})[0], 400)

    def test_the_object_list_counts_each_level(self):
        aprs_web.OWN_TRAILS = None
        with open(os.path.join(self.dir.name, "aprs_live_state.json"), "w") as f:
            json.dump({"callsign": CALL, "objects": {
                "FIRE1": {"lat": 40.12, "lon": -75.34, "table": "/", "code": "d", "comment": "BUILDING FIRE MAIN ST"},
                "FIRE2": {"lat": 40.12, "lon": -75.34, "table": "/", "code": "d", "comment": "FIRE ALARM OAK ST"},
                "WORK1": {"lat": 40.12, "lon": -75.34, "table": "/", "code": ".", "comment": "roadwork US 202"}}}, f)
        snap = aprs_web.snapshot()
        self.assertEqual(snap["levels"], {"low": 2, "medium": 0, "high": 1, "critical": 0})
        self.assertEqual(snap["min_level"], "low")
        self.assertEqual({o["name"]: o["level"] for o in snap["objects"]}, {"FIRE1": "high", "FIRE2": "low", "WORK1": "low"})

    def test_bad_requests_are_refused(self):
        for q in ({}, {"lat": "40.12"}, {"lat": "x", "lon": "-75"}, {"lat": "95", "lon": "-75"},
                  {"lat": "40", "lon": "-75", "miles": "0"}, {"lat": "40", "lon": "-75", "miles": "5000"},
                  {"lat": "40", "lon": "-75", "via": "not a call"}):
            self.assertEqual(aprs_web.packets_near(q)[0], 400, q)

    def test_print_only_mode_builds_packets_from_the_scripts_state_files(self):
        aprs_web.ARGS = argparse.Namespace(send=False, callsign=CALL, proxied="packets")
        with open(os.path.join(self.dir.name, "aprs_live_state.json"), "w") as f:
            json.dump({"callsign": CALL, "objects": {
                "FIRE1": {"lat": 40.12, "lon": -75.34, "table": "/", "code": "d", "comment": "Fire Main St"},
                "FIRE2": {"lat": 41.5, "lon": -75.34, "table": "/", "code": "d", "comment": "Far away"}}}, f)
        code, payload = aprs_web.packets_near({"lat": "40.12", "lon": "-75.34", "miles": "10"})
        self.assertEqual((code, [p["name"] for p in payload["packets"]]), (200, ["FIRE1"]))
        self.assertRegex(payload["packets"][0]["info"], r"^;FIRE1    \*\d{6}z4007\.20N/07520\.40WdFire Main St$")


class Gateways(unittest.TestCase):
    """Who asks /api/packets, and what a request through a reverse proxy may reach."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        old = os.environ.get("APRS_DATA_DIR")
        os.environ["APRS_DATA_DIR"] = self.dir.name
        self.addCleanup(lambda: os.environ.pop("APRS_DATA_DIR") if old is None else os.environ.update(APRS_DATA_DIR=old))
        aprs_web.ARGS = argparse.Namespace(send=False, callsign=CALL, proxied="packets")
        aprs_web.CLIENTS.clear()
        aprs_web.OWN_TRAILS = aprs_web.LISTENER = None
        self.server = aprs_web.ThreadingHTTPServer(("127.0.0.1", 0), aprs_web.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self, path, proxied=False, method="GET", body=None):
        headers = {"X-Forwarded-For": "203.0.113.9", "User-Agent": "aprs-radio/1.0"} if proxied else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, headers=headers, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_each_station_is_remembered_with_its_point_radius_and_times(self):
        self.get("/api/packets?lat=40.12&lon=-75.34&miles=15&min=high&via=w3xyz-10", proxied=True)
        self.get("/api/packets/?lat=40.12&lon=-75.34&miles=20&via=W3XYZ-10", proxied=True)       # asks again, wider
        self.get("/api/packets?lat=39.95&lon=-75.16&via=N3ABC-1")
        self.get("/api/packets?lat=40.5&lon=-75.5")                                              # no callsign given
        self.get("/api/packets?lat=95&lon=0&via=BAD-1")                                          # refused: not remembered
        code, raw = self.get("/api/clients")
        clients = {c["callsign"]: c for c in json.loads(raw)["clients"]}
        self.assertEqual((code, sorted(clients)), (200, ["", "N3ABC-1", "W3XYZ-10"]))
        c = clients["W3XYZ-10"]
        self.assertEqual((c["lat"], c["lon"], c["miles"], c["min"], c["count"], c["active"]), (40.12, -75.34, 20.0, "low", 2, True))
        self.assertEqual((c["address"], c["agent"]), ("203.0.113.9", "aprs-radio/1.0"))
        self.assertEqual(clients["N3ABC-1"]["address"], "127.0.0.1")
        self.assertEqual(clients["N3ABC-1"]["miles"], 25.0)

    def test_stations_survive_a_restart_and_are_forgotten_after_a_week(self):
        payload = {"centre": [40.12, -75.34], "miles": 10.0, "min": "medium", "count": 3}
        aprs_web.note_client({"via": "W3XYZ-10"}, payload, "203.0.113.9", "x", now=1000)
        aprs_web.CLIENTS.clear()
        aprs_web.load_clients()
        self.assertEqual([c["callsign"] for c in aprs_web.clients_snapshot(now=2000)["clients"]], ["W3XYZ-10"])
        self.assertFalse(aprs_web.clients_snapshot(now=2000)["clients"][0]["active"])           # silent over ten minutes
        self.assertEqual(aprs_web.clients_snapshot(now=1000 + 8 * 86400)["clients"], [])

    def test_through_a_proxy_only_the_packet_list_answers(self):
        self.assertEqual(self.get("/api/packets?lat=40.12&lon=-75.34", proxied=True)[0], 200)
        for path in ("/", "/api/objects", "/api/clients", "/api/aprs", "/eink.png", "/nothing"):
            self.assertEqual(self.get(path, proxied=True)[0], 403, path)
        for path, body in (("/api/settings", {"min_level": "critical"}), ("/api/delete", {"name": "FIRE1"}),
                           ("/api/create", {"name": "X1", "lat": 40, "lon": -75}), ("/api/packets", {})):
            self.assertEqual(self.get(path, proxied=True, method="POST", body=body)[0], 403, path)
        self.assertEqual(aprs_priority.load_min_level(), "low")                                  # nothing got through

    def test_straight_to_the_machine_everything_answers(self):
        for path in ("/", "/api/objects", "/api/clients"):
            self.assertEqual(self.get(path)[0], 200, path)
        self.assertEqual(self.get("/api/settings", method="POST", body={"min_level": "medium"})[0], 200)
        self.assertEqual(aprs_priority.load_min_level(), "medium")

    def test_proxied_all_opens_it_up(self):
        aprs_web.ARGS.proxied = "all"
        self.assertEqual(self.get("/api/objects", proxied=True)[0], 200)


class Positions(unittest.TestCase):
    def test_position_is_read_from_an_object_packet(self):
        lat, lon = aprs_send.packet_position(obj("X", "4007.20N", "07520.40W"))
        self.assertAlmostEqual(lat, 40.12, places=4)
        self.assertAlmostEqual(lon, -75.34, places=4)
        self.assertIsNone(aprs_send.packet_position(">status"))
        self.assertIsNone(aprs_send.packet_position(None))

    def test_distance(self):
        self.assertAlmostEqual(aprs_web.miles_between(40, -75, 41, -75), 69.1, delta=0.2)
        self.assertEqual(aprs_web.miles_between(40, -75, 40, -75), 0)


if __name__ == "__main__":
    unittest.main()
