"""Worldwide base map from Natural Earth (public domain): borders, highways, rivers, lakes, land, cities.

Built by tools/build_world.py into world.json.gz, which ships with the app, so it works anywhere
with no downloads.  Country and state/province borders are always drawn.  Roads, rivers, lakes,
the sea and town names come from here whenever the detailed OpenStreetMap tiles do not cover the
view: wide views (more than ~100 miles across), places whose tiles are still downloading, and
setups with OpenStreetMap turned off.

Detail follows the map scale: each feature carries Natural Earth's min_zoom (the web-map zoom
level at which it is worth showing), compared with the zoom level of the picture being drawn.
"""
import array
import gzip
import json
import math
import os
import sys
import time

CELL = 5.0                       # grid index cell, degrees
BAND = 0.5                       # latitude band for the land test, degrees
EXTRA_ZOOM = 1.0                 # show features a little earlier than a web map would (small pixels)


def _arr(flat):
    return array.array("i", flat)


class World:
    def __init__(self, path):
        t0 = time.time()
        with gzip.open(path, "rb") as f:
            raw = json.load(f)
        self.q = float(raw.get("q", 1000))
        self.index = {}                                  # (layer, i, j) -> [feature numbers]
        self.feats = {}                                  # layer -> [(minzoom, meta, [arrays], bbox)]
        for layer in ("borders", "states", "roads", "rivers", "lakes", "coast"):
            out = []
            for item in raw.get(layer) or []:
                if layer in ("borders", "states"):
                    mz, meta, parts = item[0], None, [item[1]]
                elif layer == "roads":
                    mz, meta, parts = item[1], (item[0], item[2]), [item[3]]
                elif layer == "rivers":
                    mz, meta, parts = item[0], item[1], [item[2]]
                elif layer == "lakes":
                    mz, meta, parts = item[0], item[1], item[2]
                else:                                    # coast
                    mz, meta, parts = 0, None, [item]
                arrs = [_arr(p) for p in parts]
                lats = [a[k] for a in arrs for k in range(0, len(a), 2)]
                lons = [a[k] for a in arrs for k in range(1, len(a), 2)]
                bbox = (min(lats), min(lons), max(lats), max(lons))
                n = len(out)
                out.append((float(mz or 0), meta, arrs, bbox))
                self._add(layer, n, bbox)
            self.feats[layer] = out
        self.places = [(float(p[0] or 0), p[1], p[2] / self.q, p[3] / self.q, p[4], p[5]) for p in raw.get("places") or []]
        # land test: polygon edges bucketed by latitude band (even-odd ray cast to the east)
        self.bands = {}
        for poly in raw.get("land") or []:
            for ring in poly:
                pts = [(ring[k] / self.q, ring[k + 1] / self.q) for k in range(0, len(ring), 2)]
                for (y0, x0), (y1, x1) in zip(pts, pts[1:] + pts[:1]):
                    if y0 == y1:
                        continue
                    lo, hi = min(y0, y1), max(y0, y1)
                    for b in range(math.floor(lo / BAND), math.floor(hi / BAND) + 1):
                        self.bands.setdefault(b, []).append((y0, x0, y1, x1))
        print(f"# world base map: {sum(len(v) for v in self.feats.values())} lines, {len(self.places)} places, "
              f"loaded in {time.time() - t0:.1f}s", file=sys.stderr, flush=True)

    def _add(self, layer, n, bbox):
        s, w, nn, e = (v / self.q for v in bbox)
        for i in range(math.floor(s / CELL), math.floor(nn / CELL) + 1):
            for j in range(math.floor(w / CELL), math.floor(e / CELL) + 1):
                self.index.setdefault((layer, i, j), []).append(n)

    def _candidates(self, layer, s, w, n, e):
        qs, qw, qn, qe = (round(v * self.q) for v in (s, w, n, e))
        seen, out = set(), []
        for i in range(math.floor(s / CELL), math.floor(n / CELL) + 1):
            for j in range(math.floor(w / CELL), math.floor(e / CELL) + 1):
                for k in self.index.get((layer, i, j), ()):
                    if k in seen:
                        continue
                    seen.add(k)
                    f = self.feats[layer][k]
                    b = f[3]
                    if b[0] <= qn and b[2] >= qs and b[1] <= qe and b[3] >= qw:
                        out.append(f)
        return out

    def _pts(self, a, step):
        """(lat, lon) points of one stored line, thinned to about one per pixel."""
        q, lim = self.q, step * self.q
        out, ly, lx = [], None, None
        last = len(a) - 2
        for k in range(0, len(a), 2):
            y, x = a[k], a[k + 1]
            if ly is None or k == last or abs(y - ly) >= lim or abs(x - lx) >= lim:
                out.append((y / q, x / q))
                ly, lx = y, x
        return out

    def is_land(self, lat, lon):
        inside = False
        for y0, x0, y1, x1 in self.bands.get(math.floor(lat / BAND), ()):
            if (y0 > lat) != (y1 > lat) and x0 + (lat - y0) * (x1 - x0) / (y1 - y0) > lon:
                inside = not inside
        return inside

    def layers(self, s, w, n, e, step):
        """What to draw in this view.  step = degrees per pixel (vertical)."""
        zoom = math.log2(360.0 / (256.0 * max(step, 1e-7))) + EXTRA_ZOOM
        pad = 2 * step * 10                              # a little beyond the edges, so lines run off cleanly
        S, W, N, E = s - pad, w - pad, n + pad, e + pad

        def pick(layer):
            return [f for f in self._candidates(layer, S, W, N, E) if f[0] <= zoom]
        out = {"borders": [], "roads": [], "rivers": [], "lakes": [], "coast": [], "places": []}
        for layer, k in (("states", 1), ("borders", 0)):
            for f in pick(layer):
                out["borders"].append({"k": k, "pts": self._pts(f[2][0], step)})
        for f in pick("roads"):
            out["roads"].append({"k": f[1][0], "ref": f[1][1], "pts": self._pts(f[2][0], step)})
        for f in pick("rivers"):
            out["rivers"].append({"n": f[1], "pts": self._pts(f[2][0], step)})
        for f in pick("lakes"):
            out["lakes"].append({"k": "w", "lines": [self._pts(a, step) for a in f[2]]})
        for f in self._candidates("coast", S, W, N, E):
            out["coast"].append(self._pts(f[2][0], step))
        out["places"] = [{"name": p[1], "k": p[5], "pop": p[4], "lat": p[2], "lon": p[3]}
                         for p in self.places if p[0] <= zoom and s <= p[2] <= n and w <= p[3] <= e]
        return out


def load(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "world.json.gz")
    if os.environ.get("WORLD_MAP", "1") == "0" or not os.path.exists(path):
        return None
    try:
        return World(path)
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"# world base map not loaded: {e}", file=sys.stderr, flush=True)
        return None
