import os as _os, sys as _sys
APP = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "app")
import sys, os, re, json, time, collections, urllib.parse
sys.path.insert(0, APP)
import tiles, aprs_eink
tiles.PAUSE = tiles.LOOKUP_PAUSE = 0
C = collections.Counter()
def fake(self, url, data=None):
    if not data:
        raise tiles.urllib.error.URLError("no NWS here")
    q = urllib.parse.unquote_plus(data.decode())
    if "coastline" in q:
        C["water"] += 1
        return {"elements": [
            {"type": "way", "id": 1, "tags": {"natural": "water"}, "geometry": [{"lat": 39.41, "lon": -74.71}, {"lat": 39.41, "lon": -74.69}, {"lat": 39.43, "lon": -74.69}, {"lat": 39.41, "lon": -74.71}]},
            {"type": "way", "id": 2, "tags": {"natural": "water"}, "geometry": [{"lat": 39.41, "lon": -74.71}, {"lat": 39.4101, "lon": -74.7101}, {"lat": 39.41, "lon": -74.71}]},   # pond: too small
            {"type": "way", "id": 3, "tags": {"natural": "water"}, "geometry": [{"lat": 39.41, "lon": -74.71}, {"lat": 39.5, "lon": -74.6}]},  # open: part of a multipolygon
            {"type": "relation", "id": 9, "tags": {"natural": "water", "water": "river"}, "members": [
                {"type": "way", "role": "outer", "geometry": [{"lat": 39.32, "lon": -74.95}, {"lat": 39.32, "lon": -74.5}]},
                {"type": "way", "role": "outer", "geometry": [{"lat": 39.32, "lon": -74.5}, None, {"lat": 39.30, "lon": -74.5}, {"lat": 39.30, "lon": -74.95}, {"lat": 39.32, "lon": -74.95}]}]},
            {"type": "way", "id": 7, "tags": {"natural": "coastline"}, "geometry": [{"lat": 39.0, "lon": -74.55}, {"lat": 39.9, "lon": -74.55}]}]}
    C["roads"] += 1
    return {"elements": []}
tiles.TiledBasemap.http = fake
b = tiles.TiledBasemap("tdata", counties=False)
b.want(tiles.tiles_in(39.26, -74.9, 39.49, -74.51))
while b.status()["tiles_waiting"]: time.sleep(0.05)
time.sleep(0.2); b.save()
print("status", b.status(), dict(C), "| stored shapes:", sorted(b.water))
lay = b.layers(39.26, -74.9, 39.49, -74.51)
print("areas in view:", [(a["k"], len(a["lines"])) for a in lay["areas"]])
assert sorted(b.water) == ["r9", "w1", "w7"] and len(lay["areas"]) == 3
# an old cache (roads but no water) gets only the water layer
d = json.load(open("tdata/basemap_tiles.json"))
for e in d["tiles"].values(): e.pop("wa_ok"); e.pop("wa")
d.pop("water"); json.dump(d, open("tdata/basemap_tiles.json", "w"))
C.clear(); b2 = tiles.TiledBasemap("tdata", counties=False)
b2.layers(39.26, -74.9, 39.49, -74.51)
time.sleep(0.3)
while b2.status()["tiles_waiting"]: time.sleep(0.05)
print("old cache upgraded:", dict(C)); assert C["roads"] == 0 and C["water"] == 2
print("size on disk:", os.path.getsize("tdata/basemap_tiles.json"), "bytes")
data, _ = aprs_eink.image([], {"lat": "39.38", "lon": "-74.7", "miles": "12", "list": "0"}, b2, "colour")
open("tiles.png", "wb").write(data); print("TILES OK")
