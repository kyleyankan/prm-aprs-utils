import os as _os, sys as _sys
APP = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "app")
"""Runs the real server in this process with fake map services and a fake APRS-IS."""
import sys, os, re, json, time, threading, socketserver, urllib.request, urllib.error, collections
sys.path.insert(0, APP)
D = os.path.abspath("data"); os.makedirs(D, exist_ok=True)
port_aprs = int(sys.argv[1]); phase = sys.argv[2]
os.environ.update(DATA_DIR=D, APRS_HOME="40.12,-75.34", LISTEN_MILES="20", PREFETCH_MILES="20", APRS_IS_HOST="127.0.0.1",
                  APRS_IS_PORT=str(port_aprs), APRS_BACKFILL="0", EINK_SYMBOLS="0", PORT="8781", TZ="America/New_York",
                  NWS_CONTACT="test@example.invalid")
import tiles, aprs_eink
CALLS = collections.Counter()
def fake_http(self, url, data=None):
    time.sleep(0.01)
    if data:                                          # Overpass
        CALLS["overpass"] += 1
        q = urllib.parse.unquote_plus(data.decode())
        s, w, n, e = map(float, re.search(r"\(([-\d.]+),([-\d.]+),([-\d.]+),([-\d.]+)\)", q).groups())
        els = [{"type": "way", "tags": {"highway": "primary", "ref": "PA 1"}, "geometry": [{"lat": s + .01, "lon": w + .01}, {"lat": n - .01, "lon": e - .01}]},
               {"type": "node", "tags": {"place": "town", "name": f"T{s:.2f}{w:.2f}"}, "lat": (s + n) / 2, "lon": (w + e) / 2}]
        if s <= 40.2 and n >= 40.0:                    # one long interstate crossing several tiles
            els.append({"type": "way", "tags": {"highway": "motorway", "ref": "I 76"},
                        "geometry": [{"lat": 40.10, "lon": lo / 100} for lo in range(-7600, -7400, 5)]})
        return {"elements": els}
    if "/points/" in url:
        CALLS["points"] += 1
        lat, lon = map(float, url.rsplit("/", 1)[1].split(","))
        return {"properties": {"county": aprs_eink.API + "/zones/county/" + ("PAC091" if lon < -75.25 else "PAC017")}}
    if "/zones/county/" in url:
        CALLS["zones"] += 1
        c = url.rsplit("/", 1)[1]; x = -75.6 if c == "PAC091" else -75.25
        return {"properties": {"name": "Montgomery" if c == "PAC091" else "Bucks"},
                "geometry": {"type": "Polygon", "coordinates": [[[x, 39.9], [x + .35, 39.9], [x + .35, 40.6], [x, 40.6], [x, 39.9]]]}}
    raise urllib.error.URLError("unexpected " + url)
tiles.TiledBasemap.http = fake_http; tiles.PAUSE = tiles.LOOKUP_PAUSE = 0.01
import server

B = "http://127.0.0.1:8781"
def get(u):
    with urllib.request.urlopen(B + u, timeout=30) as r: return r.read()
def js(u): return json.loads(get(u))
def wait_idle(t=60):
    end = time.time() + t
    while time.time() < end:
        b = js("/api/status")["basemap"]
        if b["tiles_waiting"] == 0 and b["tiles_ready"] > 0: return b
        time.sleep(0.5)
    raise SystemExit("base map never finished: %s" % b)
def test():
    try:
        time.sleep(1.5)
        if phase == "first":
            st = js("/api/status"); print("start:", st["basemap"])
            b = wait_idle(); print("prefetched:", b, "| calls:", dict(CALLS))
            assert CALLS["zones"] == 2, CALLS
            lay = server.BASEMAP.layers(39.95, -75.6, 40.3, -75.1)
            motorways = [r for r in lay["osm"]["roads"] if r["k"] == "m"]
            print("merged view:", len(lay["osm"]["roads"]), "roads,", len(motorways), "interstate piece (deduplicated),",
                  len(lay["counties"]), "counties,", len(lay["osm"]["places"]), "towns")
            assert len(motorways) == 1 and len(lay["counties"]) == 2
            open("first.png", "wb").write(get("/map.png?w=800&h=480&miles=12"))
            # follow a station that is outside the home area
            j = js("/api/follow?call=k3xyz-9"); print("follow before heard:", j)
            time.sleep(2.5)
            st = js("/api/status"); print("listening for:", st["follow"])
            j = js("/api/follow?call=K3XYZ-9"); print("follow after heard:", j); assert j["found"] and abs(j["lat"] - 40.5) < .01
            before = js("/api/status")["basemap"]["tiles_ready"]
            open("follow1.png", "wb").write(get("/map.png?w=800&h=480&miles=8&follow=K3XYZ-9"))
            b = wait_idle(); print("tiles around the followed station:", before, "->", b["tiles_ready"])
            assert "1,1" not in server.BASEMAP.tiles and "162,-299" in server.BASEMAP.tiles   # tile of 40.5,-74.5
            open("follow2.png", "wb").write(get("/map.png?w=800&h=480&miles=8&follow=K3XYZ-9&_=2"))
            time.sleep(4)                                                      # the fake feed moves the station
            j = js("/api/follow?call=K3XYZ-9"); print("after it moved:", j); assert abs(j["lat"] - 40.6) < .01
            open("follow3.png", "wb").write(get("/map.png?w=272&h=792&miles=8&follow=K3XYZ-9&_=3"))
            open("notheard.png", "wb").write(get("/eink.png?follow=W1AW"))
            r = urllib.request.Request(B + "/api/presets", data=json.dumps({"name": "car", "fmt": "mono",
                 "params": {"follow": "k3xyz-9", "miles": "8", "w": "272", "h": "792", "bad": "x"}}).encode(),
                 headers={"Content-Type": "application/json"}, method="POST")
            print("preset:", json.loads(urllib.request.urlopen(r).read()), js("/api/presets")["car"]["params"])
            try: get("/api/follow?call=not%20a%20call")
            except urllib.error.HTTPError as e: print("bad callsign ->", e.code)
        else:
            st = js("/api/status"); print("after restart:", st["basemap"], "| calls:", dict(CALLS), "| following:", st["follow"])
            assert CALLS["overpass"] == 0, "tiles should come from disk"
            j = js("/api/follow?call=K3XYZ-9"); print("remembered:", j); assert j["found"]
            print("login filter:", LOGINS[-1] if LOGINS else None)
        print("PHASE OK", phase)
    except BaseException as e:
        import traceback; traceback.print_exc()
    finally:
        server.save_state(); server.BASEMAP.save(); os._exit(0)
LOGINS = []
threading.Thread(target=test, daemon=True).start()
server.main()
