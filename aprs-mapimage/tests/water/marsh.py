import os as _os, sys as _sys
APP = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "app")
import sys, time, random
sys.path.insert(0, APP)
import aprs_eink
exec(open("coast.py").read().split("class B:")[0])
random.seed(4)
for k in range(400):                                   # marsh islands in the lagoon (anticlockwise squares)
    a, b, r = random.uniform(39.2, 39.6), random.uniform(-74.54, -74.46), random.uniform(0.002, 0.006)
    areas.append({"k": "c", "lines": [[[a, b], [a, b + r], [a + r, b + r], [a + r, b], [a, b]]]})
class B:
    version = 2
    def layers(self, *a, **k): return {"osm": {"roads": [], "water": [], "places": []}, "counties": [], "areas": areas}
for fmt, name, extra in (("png", "mono2.png", {"w": "400", "h": "300"}), ("colour", "marsh.png", {"miles": "8"})):
    t = time.time()
    data, _ = aprs_eink.image([], dict({"lat": "39.40", "lon": "-74.52", "miles": "10", "list": "0"}, **extra), B(), fmt)
    open(name, "wb").write(data); print(name, f"{time.time() - t:.2f}s")
