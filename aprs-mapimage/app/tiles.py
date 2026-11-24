"""Base map in fixed tiles, downloaded ahead of time.

The picture renderer asks for the roads, rivers, towns and county lines inside its view.
aprs_eink.Basemap caches one download per exact view, which suits a fixed picture but not a
map that follows a moving station.  This keeps the same data in 0.25-degree tiles
(about 17 x 13 miles here), so any view is put together from tiles that are already on
disk, and a queue downloads tiles in the background: the areas the server knows it will
draw (home, presets, followed stations) first, then whatever a picture asks for.

Roads, rivers and towns come from OpenStreetMap (Overpass API); county lines from the
National Weather Service.  Each county outline is downloaded once and shared by the tiles
it touches.  Everything is kept in DATA_DIR/basemap_tiles.json.
"""
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.parse

import aprs_eink
import world as world_map

TILE = 0.25                    # degrees
STEP = 0.0005                  # keep road/river points ~50 m apart (fine enough for close-up pictures)
FILE = "basemap_tiles.json"
RETRY_SECONDS = 600
MAX_VIEW_TILES = 144           # views larger than this (~100 miles) use only tiles already on disk
SAMPLES = 3                    # county lookups per tile side (3 x 3 points)
WATER_FORMAT = 1               # bump to make existing caches refetch the water layer
MIN_POND = 0.003               # degrees (~300 m): smaller lakes and ponds are left out
PAUSE, LOOKUP_PAUSE = 1.0, 0.25  # seconds between downloads: be gentle with the free map services


def tile_of(lat, lon):
    return math.floor(lat / TILE), math.floor(lon / TILE)


def key(t):
    return f"{t[0]},{t[1]}"


def tiles_in(south, west, north, east):
    i0, j0 = tile_of(south, west)
    i1, j1 = tile_of(north, east)
    return [(i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]


def tiles_around(lat, lon, miles):
    dlat = miles / 69.0
    dlon = miles / (69.172 * max(0.05, math.cos(math.radians(lat))))
    ts = tiles_in(lat - dlat, lon - dlon, lat + dlat, lon + dlon)

    def dist(t):                                  # nearest tiles first
        clat, clon = (t[0] + 0.5) * TILE, (t[1] + 0.5) * TILE
        return math.hypot((clat - lat) * 69.0, (clon - lon) * 69.172 * math.cos(math.radians(lat)))
    return sorted(ts, key=dist)


class TiledBasemap(aprs_eink.Basemap):
    def __init__(self, data_dir, contact="", osm=None, counties=None):
        self.path = os.path.join(data_dir, FILE)
        self.contact = (contact or "").strip()
        self.osm = (os.environ.get("EINK_OSM", "1") != "0") if osm is None else osm
        self.counties = (os.environ.get("EINK_COUNTIES", "1") != "0") if counties is None else counties
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.version = 0
        self.queue, self.failed, self.current = [], {}, None
        self.merged_cache = {}
        self.dirty, self.saved_at = False, 0.0
        try:
            with open(self.path) as f:
                data = json.load(f)
            self.tiles, self.zones = dict(data.get("tiles") or {}), dict(data.get("zones") or {})
            self.water = dict(data.get("water") or {})
        except (OSError, ValueError, AttributeError):
            self.tiles, self.zones, self.water = {}, {}, {}
        self.world = world_map.load()            # Natural Earth: borders everywhere, the rest where tiles don't reach
        self.world_cache = {}
        threading.Thread(target=self.worker, daemon=True).start()

    # ---- what the renderer calls ----
    def layers(self, south, west, north, east, step=None):
        ts = tiles_in(south, west, north, east)
        missing = []
        if len(ts) <= MAX_VIEW_TILES:
            missing = [t for t in ts if not self.ready(t)]
            if missing:
                ci, cj = tile_of((south + north) / 2, (west + east) / 2)
                self.want(sorted(missing, key=lambda t: (t[0] - ci) ** 2 + (t[1] - cj) ** 2), front=True)
        merged = self.merged(tuple(key(t) for t in ts))
        if not self.world:
            return merged
        # Detailed OpenStreetMap data when every tile of the view is on disk; otherwise (wide views,
        # tiles still downloading, OpenStreetMap turned off) Natural Earth's roads, rivers, lakes, sea, towns.
        complete = self.osm and len(ts) <= MAX_VIEW_TILES and not missing
        px = 2 * step if step else (north - south) / 480       # degrees per pixel
        ck = (round(south, 4), round(west, 4), round(north, 4), round(east, 4), round(px, 7), complete, self.version)
        hit = self.world_cache.get(ck)
        if hit is not None:
            return hit
        w = self.world.layers(south, west, north, east, px)
        out = dict(merged, borders=w["borders"])
        if not complete:
            out["osm"] = {"roads": w["roads"], "water": w["rivers"], "places": w["places"]}
            out["areas"] = w["lakes"]
            out["sea"] = {"coast": w["coast"], "is_land": self.world.is_land}
        if len(self.world_cache) > 24:
            self.world_cache.clear()
        self.world_cache[ck] = out
        return out

    # ---- what the server calls ----
    def want(self, tiles, front=False):
        """Queue tiles for download (front=True: before everything already waiting)."""
        now = time.time()
        with self.lock:
            new = [t for t in tiles if not self.ready(t, locked=True) and now - self.failed.get(t, 0) > RETRY_SECONDS
                   and t != self.current]
            if not new:
                return
            if front:
                self.queue = new + [t for t in self.queue if t not in set(new)]
            else:
                have = set(self.queue)
                self.queue += [t for t in new if t not in have]
        self.wake.set()

    def status(self):
        with self.lock:
            done = sum(1 for k in self.tiles if self.ready(tuple(int(x) for x in k.split(",")), locked=True))
            return {"tiles_ready": done, "tiles_waiting": len(self.queue) + (1 if self.current else 0),
                    "counties": len(self.zones), "osm": self.osm, "county_lines": self.counties}

    def ready(self, t, locked=False):           # (dict reads are safe without the lock)
        e = self.tiles.get(key(t)) or {}
        return ((not self.osm or (e.get("osm_ok") == aprs_eink.OSM_FORMAT and e.get("wa_ok") == WATER_FORMAT))
                and (not self.counties or bool(e.get("zones_ok"))))

    # ---- putting tiles together ----
    def merged(self, keys):
        with self.lock:
            ck = (keys, self.version)
            hit = self.merged_cache.get(ck)
            if hit is not None:
                return hit
            roads, water, places, zone_urls, area_ids = {}, {}, {}, [], []
            for k in keys:
                e = self.tiles.get(k) or {}
                o = e.get("osm") or {}
                for r in o.get("roads") or []:
                    roads.setdefault((r["k"], r.get("ref", ""), tuple(r["pts"][0]), tuple(r["pts"][-1]), len(r["pts"])), r)
                for r in o.get("water") or []:
                    water.setdefault((r.get("n", ""), tuple(r["pts"][0]), tuple(r["pts"][-1]), len(r["pts"])), r)
                for p in o.get("places") or []:
                    places.setdefault((p["name"], p["lat"], p["lon"]), p)
                for u in e.get("zones") or []:
                    if u not in zone_urls:
                        zone_urls.append(u)
                for i in e.get("wa") or []:
                    if i not in area_ids:
                        area_ids.append(i)
            out = {"osm": {"roads": list(roads.values()), "water": list(water.values()), "places": list(places.values())},
                   "counties": [self.zones[u] for u in zone_urls if u in self.zones],
                   "areas": [self.water[i] for i in area_ids if i in self.water]}
            if len(self.merged_cache) > 16:
                self.merged_cache.clear()
            self.merged_cache[ck] = out
            return out

    # ---- downloading ----
    def worker(self):
        while True:
            with self.lock:
                t = self.queue.pop(0) if self.queue else None
                self.current = t
            if t is None:
                self.save()
                self.wake.wait(30)
                self.wake.clear()
                continue
            try:
                self.fetch_tile(t)
            except Exception as e:                       # never let one tile stop the queue
                print(f"# base map tile {key(t)}: {e}", file=sys.stderr, flush=True)
                self.failed[t] = time.time()
            finally:
                with self.lock:
                    self.current = None
            if time.time() - self.saved_at > 30:
                self.save()
            time.sleep(PAUSE)

    def fetch_tile(self, t):
        south, west = t[0] * TILE, t[1] * TILE
        north, east = south + TILE, west + TILE
        with self.lock:
            e = dict(self.tiles.get(key(t)) or {})
        failed = False
        if self.osm and e.get("osm_ok") != aprs_eink.OSM_FORMAT:
            try:
                e["osm"] = self.fetch_osm(south, west, north, east, STEP)
                e["osm_ok"] = aprs_eink.OSM_FORMAT
            except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as ex:
                print(f"# base map tile {key(t)}: OpenStreetMap not reached ({ex}); will retry",
                      file=sys.stderr, flush=True)
                failed = True
        if self.osm and e.get("wa_ok") != WATER_FORMAT:
            try:
                e["wa"] = self.fetch_water(south, west, north, east)
                e["wa_ok"] = WATER_FORMAT
            except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as ex:
                print(f"# base map tile {key(t)}: water outlines not fetched ({ex}); will retry",
                      file=sys.stderr, flush=True)
                failed = True
        if self.counties and not e.get("zones_ok"):
            urls, ok = self.zone_urls(south, west, north, east)
            for u in sorted(urls):
                if u in self.zones:
                    continue
                try:
                    z = self.http(u)
                    rings = [aprs_eink.thin(r) for r in aprs_eink.rings_of(z.get("geometry"))]
                    zone = {"name": str((z.get("properties") or {}).get("name") or ""),
                            "rings": [r for r in rings if len(r) >= 3]}
                    with self.lock:
                        self.zones[u] = zone
                except (urllib.error.URLError, OSError, ValueError, TypeError):
                    ok = False
                time.sleep(LOOKUP_PAUSE)
            e["zones"], e["zones_ok"] = sorted(urls), ok and all(u in self.zones for u in urls)
            failed = failed or not e["zones_ok"]
        with self.lock:
            self.tiles[key(t)] = e
            self.version += 1
            self.dirty = True
        if failed:
            self.failed[t] = time.time()

    def fetch_water(self, south, west, north, east):
        """Lakes, reservoirs, wide rivers, bays and coastline in the tile.  Each outline is kept once
        (in self.water, by OpenStreetMap id) and the tile lists the ids it touches."""
        box = f"({south:.4f},{west:.4f},{north:.4f},{east:.4f})"
        query = ("[out:json][timeout:120];("
                 f'way["natural"="water"]{box};relation["natural"="water"]{box};'
                 f'way["landuse"="reservoir"]{box};'
                 f'way["waterway"="riverbank"]{box};relation["waterway"="riverbank"]{box};'
                 f'way["natural"="bay"]{box};relation["natural"="bay"]{box};'
                 f'way["natural"="coastline"]{box};'
                 ");out geom;")
        last = None
        for url in aprs_eink.OVERPASS:
            try:
                data = self.http(url, data=urllib.parse.urlencode({"data": query}).encode())
                break
            except (urllib.error.URLError, OSError, ValueError) as e:
                last = e
                time.sleep(2)
        else:
            raise last
        ids, found = [], {}
        for el in data["elements"]:
            tags = el.get("tags") or {}
            kind = "c" if tags.get("natural") == "coastline" else "s" if tags.get("natural") == "bay" else "w"
            if el.get("type") == "way":
                lines = [[(g["lat"], g["lon"]) for g in el.get("geometry") or [] if g]]
            elif el.get("type") == "relation":
                lines = [[(g["lat"], g["lon"]) for g in m.get("geometry") or [] if g]
                         for m in el.get("members") or [] if m.get("type") == "way" and m.get("role") in ("outer", "inner", "")]
            else:
                continue
            lines = [ln for ln in lines if len(ln) >= 2]
            if not lines:
                continue
            if kind != "c" and el["type"] == "way":
                ln = lines[0]
                if ln[0] != ln[-1]:
                    continue                             # not a closed outline (a piece of a multipolygon)
                lats, lons = [p[0] for p in ln], [p[1] for p in ln]
                if max(lats) - min(lats) < MIN_POND and max(lons) - min(lons) < MIN_POND:
                    continue
            ident = f"{el['type'][0]}{el['id']}"
            found[ident] = {"k": kind, "lines": [aprs_eink.thin(ln, STEP, keep_last=True) for ln in lines]}
            ids.append(ident)
        with self.lock:
            self.water.update(found)
        return ids

    def zone_urls(self, south, west, north, east):
        urls, ok = set(), True
        for a in range(SAMPLES):
            for b in range(SAMPLES):
                lat = south + (north - south) * (a + 0.5) / SAMPLES
                lon = west + (east - west) * (b + 0.5) / SAMPLES
                try:
                    u = self.http(f"{aprs_eink.API}/points/{lat:.4f},{lon:.4f}")["properties"].get("county")
                    if isinstance(u, str) and u.startswith(aprs_eink.API + "/zones/county/"):
                        urls.add(u)
                except urllib.error.HTTPError as e:
                    ok = ok and e.code == 404               # 404 = open water or outside the US
                except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
                    ok = False
                time.sleep(LOOKUP_PAUSE)
        return urls, ok

    def save(self):
        with self.lock:
            if not self.dirty:
                return
            data = json.dumps({"tiles": self.tiles, "zones": self.zones, "water": self.water}, separators=(",", ":"))
            self.dirty = False
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                f.write(data)
            os.replace(tmp, self.path)
            self.saved_at = time.time()
        except OSError as e:
            print(f"# base map not saved: {e}", file=sys.stderr, flush=True)
