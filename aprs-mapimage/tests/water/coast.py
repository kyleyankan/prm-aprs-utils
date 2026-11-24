import os as _os, sys as _sys
APP = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "app")
import sys, time
sys.path.insert(0, APP)
import aprs_eink
def line(*pts): return [list(p) for p in pts]
areas = [
 {"k": "c", "lines": [line(*[(39.15 + i * 0.01, -74.55 + 0.01 * ((i % 7) - 3) / 3) for i in range(50)])]},        # mainland, sea to the east (walk north)
 {"k": "c", "lines": [line((39.25, -74.45), (39.25, -74.42), (39.55, -74.40), (39.55, -74.44), (39.25, -74.45))]},   # barrier island (anticlockwise)
 {"k": "s", "lines": [line((39.35, -74.53), (39.35, -74.48), (39.45, -74.48), (39.45, -74.53), (39.35, -74.53))]},   # bay in the lagoon
 {"k": "w", "lines": [line((39.45, -74.72), (39.43, -74.66), (39.47, -74.64), (39.49, -74.70), (39.45, -74.72))]},   # lake
 {"k": "w", "lines": [line((39.32, -74.95), (39.33, -74.75), (39.32, -74.58), (39.30, -74.58)),                     # wide river, two
                      line((39.30, -74.58), (39.31, -74.75), (39.30, -74.95), (39.32, -74.95))]},                    #  unjoined members
]
class B:
    version = 1
    def layers(self, *a, **k):
        return {"osm": {"roads": [{"k": "p", "ref": "US9", "pts": [[39.2, -74.6], [39.6, -74.58]]}], "water": [], "places": [
                 {"name": "Atlantic City", "k": "city", "pop": 38000, "lat": 39.36, "lon": -74.43}]}, "counties": [], "areas": areas}
objs = [{"name": "K3XYZ-9", "lat": 39.40, "lon": -74.50, "source": "station", "comment": "boat", "table": "/", "code": "s", "path": []}]
for fmt, name, extra in (("colour", "colour.png", {}), ("colour", "dark.png", {"dark": "1"}), ("png", "mono.png", {"w": "272", "h": "792"}), ("colour", "off.png", {"water": "0"})):
    t = time.time()
    data, _ = aprs_eink.image(objs, dict({"lat": "39.40", "lon": "-74.55", "miles": "14", "list": "0"}, **extra), B(), fmt)
    open(name, "wb").write(data); print(name, f"{time.time() - t:.2f}s")
    aprs_eink.BASE_CACHE.clear()
