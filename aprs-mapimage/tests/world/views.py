import os as _os, sys as _sys
APP = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "app")
import sys, time, os
sys.path.insert(0, APP)
t = time.time()
import tiles, aprs_eink
b = tiles.TiledBasemap("data", osm=False, counties=False)
print(f"load {time.time() - t:.1f}s")
import resource; print("memory MB", resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024)
views = [("philly", "colour", {"lat": "39.95", "lon": "-75.16", "miles": "15"}),
         ("midatl", "colour", {"lat": "39.9", "lon": "-75.2", "miles": "150"}),
         ("east", "colour", {"lat": "38.5", "lon": "-80", "miles": "600"}),
         ("channel", "colour", {"lat": "50.6", "lon": "0.5", "miles": "160"}),
         ("japan", "colour", {"lat": "35.6", "lon": "138.5", "miles": "250", "dark": "1"}),
         ("eink", "png", {"lat": "39.9", "lon": "-75.0", "miles": "60", "w": "792", "h": "272"})]
for name, fmt, p in views:
    t = time.time()
    data, _ = aprs_eink.image([], dict(p, list="0"), b, fmt)
    open(name + ".png", "wb").write(data)
    t1 = time.time() - t
    t = time.time(); aprs_eink.image([], dict(p, list="0"), b, fmt); t2 = time.time() - t
    print(f"{name:8s} first {t1:.2f}s, again {t2:.2f}s")
