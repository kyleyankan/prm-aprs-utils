#!/usr/bin/env python3
"""Build app/world.json.gz, the worldwide base map, from Natural Earth (public domain).

Natural Earth: https://www.naturalearthdata.com  (GeoJSON copies: github.com/nvkelso/natural-earth-vector)
Needs these files from its geojson/ folder:

  ne_10m_admin_0_boundary_lines_land     country borders
  ne_10m_admin_1_states_provinces_lines  state and province borders
  ne_10m_roads                           major and secondary highways
  ne_10m_rivers_lake_centerlines         rivers
  ne_10m_lakes                           lakes
  ne_50m_land, ne_50m_coastline          land and sea
  ne_10m_populated_places_simple         cities and towns

  git clone --depth 1 --filter=blob:none --sparse https://github.com/nvkelso/natural-earth-vector.git
  cd natural-earth-vector && git sparse-checkout set --no-cone '/geojson/ne_10m_roads.geojson' ...
  python3 tools/build_world.py natural-earth-vector/geojson app/world.json.gz

Lines are simplified (Douglas-Peucker) and coordinates kept to 0.001 degree (about 100 m).
Each feature keeps Natural Earth's min_zoom: the map scale at which it is worth showing.
"""
import gzip
import json
import os
import sys

Q = 1000                                   # coordinates stored as integers of 1/1000 degree


def lines_of(geom):
    if not geom:
        return []
    t, c = geom.get("type"), geom.get("coordinates") or []
    if t == "LineString":
        return [c]
    if t == "MultiLineString":
        return c
    if t == "Polygon":
        return c
    if t == "MultiPolygon":
        return [r for p in c for r in p]
    return []


def polygons_of(geom):
    if not geom:
        return []
    t, c = geom.get("type"), geom.get("coordinates") or []
    return [c] if t == "Polygon" else c if t == "MultiPolygon" else []


def simplify(pts, tol):
    """Douglas-Peucker on [lon, lat] points; keeps the ends."""
    if len(pts) < 3 or tol <= 0:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (x0, y0), (x1, y1) = pts[a][:2], pts[b][:2]
        dx, dy = x1 - x0, y1 - y0
        d2 = dx * dx + dy * dy
        far, at = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i][:2]
            if d2 == 0:
                d = (px - x0) ** 2 + (py - y0) ** 2
            else:
                t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / d2))
                d = (px - x0 - t * dx) ** 2 + (py - y0 - t * dy) ** 2
            if d > far:
                far, at = d, i
        if far > tol * tol:
            keep[at] = True
            stack += [(a, at), (at, b)]
    return [p for p, k in zip(pts, keep) if k]


def flat(pts, tol):
    """[lat, lon, lat, lon, ...] integers, simplified, repeated points dropped."""
    out, last = [], None
    for lon, lat in (p[:2] for p in simplify(pts, tol)):
        q = (round(lat * Q), round(lon * Q))
        if q != last:
            out += q
            last = q
    return out if len(out) >= 4 else None


def load(folder, name):
    with open(os.path.join(folder, name + ".geojson"), encoding="utf-8") as f:
        return json.load(f)["features"]


def prop(p, key, default=None):
    for k in (key, key.upper(), key.lower()):
        if k in p and p[k] is not None:
            return p[k]
    return default


def main(folder, out):
    world = {"q": Q, "source": "Natural Earth (public domain), naturalearthdata.com"}

    world["borders"] = [[prop(f["properties"], "min_zoom", 0), fl]
                        for f in load(folder, "ne_10m_admin_0_boundary_lines_land")
                        for ln in lines_of(f["geometry"]) if (fl := flat(ln, 0.002))]

    world["states"] = [[prop(f["properties"], "min_zoom", 5), fl]
                       for f in load(folder, "ne_10m_admin_1_states_provinces_lines")
                       for ln in lines_of(f["geometry"]) if (fl := flat(ln, 0.003))]

    kinds = {"Major Highway": "m", "Beltway": "m", "Bypass": "m", "Secondary Highway": "t"}
    world["roads"] = []
    for f in load(folder, "ne_10m_roads"):
        p = f["properties"]
        k = kinds.get(prop(p, "type"))
        if not k:
            continue                                   # local roads, tracks, ferries, unknown
        ref = str(prop(p, "name", "") or "")[:8]
        for ln in lines_of(f["geometry"]):
            if (fl := flat(ln, 0.002)):
                world["roads"].append([k, prop(p, "min_zoom", 6), ref, fl])

    world["rivers"] = []
    for f in load(folder, "ne_10m_rivers_lake_centerlines"):
        p = f["properties"]
        if "lake" in str(prop(p, "featurecla", "")).lower():
            continue                                   # centre lines through lakes: the lake is drawn instead
        for ln in lines_of(f["geometry"]):
            if (fl := flat(ln, 0.003)):
                world["rivers"].append([prop(p, "min_zoom", 5), str(prop(p, "name", "") or ""), fl])

    world["lakes"] = []
    for f in load(folder, "ne_10m_lakes"):
        p = f["properties"]
        for poly in polygons_of(f["geometry"]):
            rings = [fl for r in poly if (fl := flat(r, 0.003)) and len(fl) >= 8]
            if rings:
                world["lakes"].append([prop(p, "min_zoom", 5), str(prop(p, "name", "") or ""), rings])

    world["coast"] = [fl for f in load(folder, "ne_50m_coastline")
                      for ln in lines_of(f["geometry"]) if (fl := flat(ln, 0.0))]
    world["land"] = [rings for f in load(folder, "ne_50m_land") for poly in polygons_of(f["geometry"])
                     if (rings := [fl for r in poly if (fl := flat(r, 0.0)) and len(fl) >= 8])]

    world["places"] = []
    for f in load(folder, "ne_10m_populated_places_simple"):
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"][:2]
        pop = int(prop(p, "pop_max", 0) or 0)
        big = pop >= 500000 or "capital" in str(prop(p, "featurecla", "")).lower()
        world["places"].append([prop(p, "min_zoom", 6), str(prop(p, "name", "")), round(lat * Q), round(lon * Q),
                                pop, "city" if big else "town"])

    data = json.dumps(world, separators=(",", ":")).encode()
    with gzip.open(out, "wb", compresslevel=9) as f:
        f.write(data)
    counts = {k: len(v) for k, v in world.items() if isinstance(v, list)}
    print(f"{out}: {os.path.getsize(out) / 1e6:.1f} MB ({len(data) / 1e6:.1f} MB unpacked) {counts}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: build_world.py NATURAL_EARTH_GEOJSON_FOLDER OUTPUT.json.gz")
    main(sys.argv[1], sys.argv[2])
