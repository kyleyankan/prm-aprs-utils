"""Tests for GET /api/packets in aprs_web.py: our own packets within a radius of a point.

  python3 -m unittest test_web        (from this folder; reads temporary files only, no network)
"""
import argparse
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
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
        aprs_web.ARGS = argparse.Namespace(send=True, callsign=CALL)
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

    def test_bad_requests_are_refused(self):
        for q in ({}, {"lat": "40.12"}, {"lat": "x", "lon": "-75"}, {"lat": "95", "lon": "-75"},
                  {"lat": "40", "lon": "-75", "miles": "0"}, {"lat": "40", "lon": "-75", "miles": "5000"},
                  {"lat": "40", "lon": "-75", "via": "not a call"}):
            self.assertEqual(aprs_web.packets_near(q)[0], 400, q)

    def test_print_only_mode_builds_packets_from_the_scripts_state_files(self):
        aprs_web.ARGS = argparse.Namespace(send=False, callsign=CALL)
        with open(os.path.join(self.dir.name, "aprs_live_state.json"), "w") as f:
            json.dump({"callsign": CALL, "objects": {
                "FIRE1": {"lat": 40.12, "lon": -75.34, "table": "/", "code": "d", "comment": "Fire Main St"},
                "FIRE2": {"lat": 41.5, "lon": -75.34, "table": "/", "code": "d", "comment": "Far away"}}}, f)
        code, payload = aprs_web.packets_near({"lat": "40.12", "lon": "-75.34", "miles": "10"})
        self.assertEqual((code, [p["name"] for p in payload["packets"]]), (200, ["FIRE1"]))
        self.assertRegex(payload["packets"][0]["info"], r"^;FIRE1    \*\d{6}z4007\.20N/07520\.40WdFire Main St$")


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
