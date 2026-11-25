"""Tests for GET /api/packets in aprs_web.py: our own packets within a radius of a point.

  python3 -m unittest test_web        (from this folder; reads temporary files only, no network)
"""
import argparse
import contextlib
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

    def test_an_entry_can_be_removed_and_a_station_that_asks_again_comes_back(self):
        for call, lat in (("W3XYZ-10", 40.12), ("N3ABC-1", 39.95), ("", 40.5)):
            self.get(f"/api/packets?lat={lat}&lon=-75.34&via={call}")
        listed = lambda: sorted(c["callsign"] for c in json.loads(self.get("/api/clients")[1])["clients"])
        self.assertEqual(self.get("/api/forget-client", method="POST", body={"callsign": "w3xyz-10", "lat": 40.12, "lon": -75.34})[0], 200)
        self.assertEqual(listed(), ["", "N3ABC-1"])
        aprs_web.CLIENTS.clear()
        aprs_web.load_clients()                                                   # the removal was saved
        self.assertEqual(listed(), ["", "N3ABC-1"])
        self.assertEqual(self.get("/api/forget-client", method="POST", body={"callsign": "W3XYZ-10", "lat": 40.12, "lon": -75.34})[0], 404)
        self.assertEqual(self.get("/api/forget-client", method="POST", body={"callsign": "N3ABC-1"})[0], 400)
        self.assertEqual(self.get("/api/forget-client", method="POST", body={"callsign": "", "lat": 40.5, "lon": -75.34})[0], 200)
        self.assertEqual(self.get("/api/forget-client", proxied=True, method="POST", body={"all": True})[0], 403)   # not from outside
        self.assertEqual(listed(), ["N3ABC-1"])
        self.get("/api/packets?lat=40.12&lon=-75.34&via=W3XYZ-10")                # it asks again
        self.assertEqual(listed(), ["N3ABC-1", "W3XYZ-10"])
        code, raw = self.get("/api/forget-client", method="POST", body={"all": True})
        self.assertEqual((code, json.loads(raw)["removed"], listed()), (200, 2, []))

    def test_through_a_proxy_only_the_packet_list_and_the_read_only_map_answer(self):
        self.assertEqual(self.get("/api/packets?lat=40.12&lon=-75.34", proxied=True)[0], 200)
        for path in ("/index.html", "/api/objects", "/api/clients", "/api/aprs", "/eink.png", "/nothing"):
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


class PublicMapAndFeedHealth(unittest.TestCase):
    """The read-only map anyone may open, and the mark on a source that has stopped answering."""
    setUp, get = Gateways.setUp, Gateways.get

    def write_state(self, source, objects, age=0):
        path = os.path.join(self.dir.name, f"{source}_live_state.json")
        with open(path, "w") as f:
            json.dump({"callsign": CALL, "objects": objects}, f)
        when = os.path.getmtime(path) - age
        os.utime(path, (when, when))

    def test_the_public_map_shows_what_is_on_aprs_is_and_nothing_that_edits(self):
        aprs_web.PUBLIC_CACHE[1] = None
        self.write_state("montco", {"FIRE1": {"lat": 40.12, "lon": -75.34, "table": "/", "code": "d", "comment": "BUILDING FIRE MAIN ST"},
                                    "FIRE2": {"lat": 40.13, "lon": -75.34, "table": "/", "code": "d", "comment": "FIRE ALARM OAK ST"}})
        for path in ("/", "/map"):
            code, page = self.get(path, proxied=True)
            self.assertEqual(code, 200, path)
            self.assertIn(b"Read-only view", page)
            for word in (b"New object", b"/api/delete", b"/api/settings", b"/api/clients", b"/api/objects"):
                self.assertNotIn(word, page)
        code, raw = self.get("/api/public", proxied=True)
        d = json.loads(raw)
        self.assertEqual((code, d["count"], d["levels"]["high"], d["levels"]["low"]), (200, 2, 1, 1))
        self.assertEqual(d["objects"][0]["name"], "FIRE1")                       # most important first
        self.assertEqual(sorted(d["objects"][0]), ["comment", "lat", "level", "lon", "name", "score", "shape", "source",
                                                   "source_label", "source_url", "updated"])
        self.assertEqual(self.get("/api/public", proxied=True, method="POST", body={})[0], 403)
        self.assertIn(b"New object", self.get("/")[1])                           # the editor is still at / on the inside

    def test_the_public_map_has_its_filters_and_a_link_to_the_documentation(self):
        page = self.get("/map", proxied=True)[1].decode()
        self.assertIn('href="/docs"', page)
        for part in ('id="srcs"', "aprs-public-levels", "aprs-public-sources", "Click a level to hide or show it"):
            self.assertIn(part, page)                                            # hide by source, hide by level

    def docs_folder(self, files):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for name, text in files.items():
            with open(os.path.join(folder.name, name), "w", encoding="utf-8") as f:
                f.write(text)
        old, aprs_web.DOCS_DIR = aprs_web.DOCS_DIR, folder.name
        self.addCleanup(setattr, aprs_web, "DOCS_DIR", old)

    def test_documents_are_public_pages_in_file_order_and_read_only(self):
        self.docs_folder({"20-second.md": "# The second one\n\nSee [the first](/docs/first).\n",
                          "10-first.md": "# The first one\n\n| A | B |\n|---|---|\n| 1 | <script>x</script> |\n\n- **bold** item\n",
                          "notes.txt": "not a document", "Bad Name.md": "# ignored"})
        self.assertEqual([d["slug"] for d in aprs_web.public_docs()], ["first", "second"])
        code, page = self.get("/docs", proxied=True)                             # the first document
        page = page.decode()
        self.assertEqual(code, 200)
        self.assertIn('<h1 id="the-first-one">The first one</h1>', page)
        self.assertIn('<a href="/docs/first" class="on">The first one</a><a href="/docs/second">The second one</a>', page)
        self.assertIn("<td>&lt;script&gt;x&lt;/script&gt;</td>", page)           # HTML in a file is text
        self.assertIn("<li><strong>bold</strong> item</li>", page)
        self.assertIn('href="/map"', page)
        code, page = self.get("/docs/second", proxied=True)
        self.assertEqual(code, 200)
        self.assertIn(b'<a href="/docs/first">the first</a>', page)
        self.assertEqual(self.get("/docs/nothing", proxied=True)[0], 404)
        self.assertEqual(self.get("/docs/second", proxied=True, method="POST", body={})[0], 403)

    def test_a_document_address_cannot_reach_other_files(self):
        self.docs_folder({"10-first.md": "# One\n"})
        with open(os.path.join(os.path.dirname(aprs_web.DOCS_DIR), "secret.md"), "w") as f:
            f.write("# secret")
        self.addCleanup(os.unlink, f.name)
        for path in ("/docs/..%2Fsecret", "/docs/../secret", "/docs/first.md", "/docs/first/extra", "/docs/FIRST", "/docsx"):
            code, page = self.get(path, proxied=True)
            self.assertIn(code, (403, 404), path)
            self.assertNotIn(b"secret", page.lower().replace(b"no such", b""), path)

    def test_links_in_a_document_cannot_run_script(self):
        html = aprs_web.markdown_html("[x](javascript:alert(1)) [ok](https://example.org/a?b=1&c=2) `<b>`")
        self.assertNotIn("<a href=\"javascript", html)
        self.assertIn('<a href="https://example.org/a?b=1&amp;c=2" target="_blank" rel="noopener">ok</a>', html)
        self.assertIn("<code>&lt;b&gt;</code>", html)

    def test_no_documents_yet_is_a_plain_page(self):
        self.docs_folder({})
        code, page = self.get("/docs", proxied=True)
        self.assertEqual(code, 200)
        self.assertIn(b"No documents have been added yet", page)

    def test_objects_held_back_or_taken_off_are_not_on_the_public_map(self):
        aprs_web.PUBLIC_CACHE[1] = None
        aprs_web.ARGS.send = True
        self.write_state("montco", {n: {"lat": 40.12, "lon": -75.34, "table": "/", "code": "d", "comment": "BUILDING FIRE"}
                                    for n in ("FIRE1", "FIRE2", "FIRE3")})
        book = aprs_send.Book(CALL, os.path.join(self.dir.name, aprs_send.STATE_FILE))
        for name in ("FIRE1", "FIRE2", "FIRE3"):
            book.offer(obj(name, "4007.20N", "07520.40W"), "montco", 1000, hold=name == "FIRE2", dup="TRAF9" if name == "FIRE2" else "")
        for key, _packet, _why in book.due(1000):
            book.mark_sent(key, 1000)
        book.records["FIRE3    "]["seen"] = 0
        book.expire(8000, 7200)
        book.save()
        names = [o["name"] for o in json.loads(self.get("/api/public")[1])["objects"]]
        self.assertEqual(names, ["FIRE1"])
        inside = {o["name"]: o["aprs_is"] for o in json.loads(self.get("/api/objects")[1])["objects"]}
        self.assertEqual((inside["FIRE2"]["dup"], inside["FIRE3"]["stale"]), ("TRAF9", True))

    def test_a_source_with_no_good_poll_for_ten_minutes_is_marked(self):
        aprs_web.PUBLIC_CACHE[1] = None
        aprs_web.ARGS.send = True
        book = aprs_send.Book(CALL, os.path.join(self.dir.name, aprs_send.STATE_FILE))
        book.max_quiet, book.dirty = 7200, True
        book.save()
        self.write_state("montco", {"FIRE1": {"lat": 40.12, "lon": -75.34, "comment": "BUILDING FIRE"}}, age=1800)
        self.write_state("bucks", {"FIRE2": {"lat": 40.2, "lon": -75.1, "comment": "BUILDING FIRE"}})
        self.write_state("custom", {}, age=86400)                                # only changes when edited: never late
        src = {s["key"]: s for s in json.loads(self.get("/api/objects")[1])["sources"]}
        self.assertEqual((src["montco"]["late"], src["bucks"]["late"], src["custom"]["late"]), (True, False, False))
        self.assertAlmostEqual(src["montco"]["off_at"], src["montco"]["updated"] + 7200, places=0)
        self.assertIsNone(src["bucks"]["off_at"])
        public = {s["label"]: s["late"] for s in json.loads(self.get("/api/public")[1])["sources"]}
        self.assertTrue(public["Montgomery County CAD"])


class BulletinsForRadios(unittest.TestCase):
    """/api/packets lists the bulletin of each critical object in the area asked for."""

    def setUp(self):
        PacketsNear.setUp(self)
        sender = aprs_send.Sender(CALL, False, os.path.join(self.dir.name, "outbox"),
                                  os.path.join(self.dir.name, aprs_send.STATE_FILE), None)
        aprs_send.hand_over("nws", CALL, [obj("TORPHI01", "4007.20N", "07520.40W", "Tornado Warning til 9:15PM"),
                                          obj("TORCTP02", "4100.00N", "07520.40W", "Tornado Warning til 9:30PM")],
                            folder=sender.folder)
        with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet):
            sender.step(1000)

    def near(self, **q):
        return aprs_web.packets_near(dict({"lat": HOME[0], "lon": HOME[1], "miles": 25}, **q))[1]

    def test_only_the_bulletin_for_an_object_in_the_area_is_listed(self):
        b = self.near(via="w3xyz-10", min="critical")["bulletins"]
        self.assertEqual([(x["slot"], x["object"]) for x in b], [("1", "TORPHI01")])
        self.assertEqual(b[0]["info"], ":BLN1     :Tornado Warning til 9:15PM")
        self.assertEqual(b[0]["tnc2"], f"{CALL}>APRS,TCPIP*::BLN1     :Tornado Warning til 9:15PM")
        self.assertEqual(b[0]["rf"], f"}}{CALL}>APRS,TCPIP,W3XYZ-10*::BLN1     :Tornado Warning til 9:15PM")
        self.assertEqual(len(self.near(miles=100)["bulletins"]), 2)
        self.assertEqual(self.near(bulletins="0")["bulletins"], [])
        self.assertNotIn("rf", self.near()["bulletins"][0])

    def test_no_bulletin_is_listed_in_print_only_mode_or_once_the_object_has_ended(self):
        aprs_web.ARGS.send = False
        self.assertEqual(self.near()["bulletins"], [])
        aprs_web.ARGS.send = True
        book = aprs_send.Book(CALL, os.path.join(self.dir.name, aprs_send.STATE_FILE))
        book.offer(obj("TORPHI01", "4007.20N", "07520.40W", live=False), "nws", 2000)
        book.save()
        self.assertEqual(self.near()["bulletins"], [])


if __name__ == "__main__":
    unittest.main()
