#!/usr/bin/env python3
"""
NWS weather alerts (api.weather.gov) + NHC tropical cyclones -> APRS objects.

What becomes an object (within --radius-miles of --home):

  * Tracked storms: Severe Thunderstorm / Tornado / Special Marine / Snow Squall
    Warnings and "strong thunderstorm" Special Weather Statements.  NWS gives the
    storm's position, direction and speed; the object sits ON THE STORM, carries
    an APRS course/speed, and is moved along its track every cycle until NWS
    issues a new position or the warning ends.
  * Polygon warnings without a storm fix (e.g. Flash Flood Warning): one object
    at the centre of the warned area.
  * Zone/county products (Tornado & Severe Thunderstorm Watches, Winter Storm,
    High Wind, Flood, Hurricane/Tropical Storm warnings ...): one object per
    event, placed in the affected zone nearest the middle of the in-range area.
  * Tropical cyclones from the National Hurricane Center (position, movement,
    intensity) within --tropical-radius-miles.

Objects are killed when the alert ends/is cancelled, or leaves the radius.

Shapes (--shapes, default "polygon"): a warning that NWS issues with a polygon
carries that outline as an APRS "multiline" shape, the format the APRS weather
servers use (aprs-is.net/WX/MultilineProtocol.aspx): red solid = tornado warning,
red dashed = tornado watch, yellow solid = severe thunderstorm warning, yellow
dashed = severe thunderstorm watch, green = everything else.  "--shapes all" also
outlines zone/county products with one simplified hull around the affected zones
(approximate: it can take in zones between them).  Tropical cyclones are sent in
the APRS storm format with wind radii when the NHC forecast advisory can be read.
Clients that do not know these formats simply show them as extra text.

  export NWS_CONTACT="you@example.com"        # NWS asks for a contact in the User-Agent
  python3 aprs_nws.py --home 40.12,-75.34 --once              # print only
  python3 aprs_nws.py --home 40.12,-75.34 --callsign W3XYZ-9 --send

Data: National Weather Service / National Hurricane Center (public domain).
This is a convenience display, NOT a warning system: delays of minutes are normal.
"""
import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aprs_send                   # hands packets to the one program that sends to APRS-IS

try:
    import aprs_overrides            # edits/deletes made in the web UI (optional)
except ImportError:
    aprs_overrides = None

API = "https://api.weather.gov"
NHC_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"
STATE_FILE = "nws_live_state.json"
ZONE_CACHE = "nws_zone_cache.json"
DEFAULT_HOME = "40.12,-75.34"          # Norristown, PA - change with --home / $APRS_HOME
DEFAULT_AREAS = "PA,NJ,DE,MD,DC,NY,CT,VA,WV,AN"   # states + AN (Atlantic coastal waters) to query
CONTACT_ENV, HOME_ENV = "NWS_CONTACT", "APRS_HOME"
COMMENT_LIMIT = 67                     # APRS 1.0 says 43; --comment-length 43 if clients truncate
LOCAL = ZoneInfo("America/New_York")
RETRY_CODES = {429, 500, 502, 503, 504}
SEVERITY_RANK = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3, "Unknown": 4}
USER_AGENT = "aprs-nws-objects/1.0"

# event regex -> (name tag, (symbol table, symbol code)).  First match wins.
CLASSES = [
    (r"tornado",                                 "TOR", ("\\", "t")),   # tornado
    (r"severe thunderstorm",                     "SVR", ("\\", "T")),   # thunderstorm
    (r"special marine",                          "SMW", ("\\", "T")),
    (r"snow squall",                             "SQW", ("\\", "*")),   # snow
    (r"hurricane|tropical|typhoon|storm surge",  "TRP", ("\\", "@")),   # hurricane
    (r"flash flood",                             "FFW", ("\\", "w")),   # flooding
    (r"flood",                                   "FLD", ("\\", "w")),
    (r"ice storm|freezing|sleet",                "ICE", ("\\", "F")),   # freezing rain
    (r"blizzard|winter|snow|lake effect",        "WIN", ("\\", "*")),
    (r"wind chill|cold|freeze|frost",            "CLD", ("\\", "F")),
    (r"wind|gale|storm warning|storm watch",     "WND", ("\\", "g")),   # gale flags
    (r"fog",                                     "FOG", ("\\", "{")),   # fog
    (r"heat",                                    "HOT", ("\\", "U")),   # sunny
    (r"red flag|fire",                           "FIR", ("/", ":")),    # fire
    (r"dust",                                    "DST", ("\\", "b")),   # blowing dust
    (r"special weather statement",               "SPS", ("\\", "<")),   # advisory flag
]
DEFAULT_CLASS = ("WXA", ("\\", "<"))


def env_symbol(key, default):
    """APRS symbol (table, code) from $SYM_<key> (two characters, e.g. /d), else the built-in default."""
    v = os.environ.get("SYM_" + key, "")
    if len(v) == 2 and (v[0] in "/\\" or v[0].isalnum()) and "!" <= v[1] <= "~":
        return v[0], v[1]
    if v:
        print(f"# SYM_{key}={v!r} is not a valid APRS symbol (table + code); using the default", file=sys.stderr)
    return default


# Every symbol above can be changed in aprs.env as SYM_WX_<tag>, e.g. SYM_WX_TOR, SYM_WX_FFW;
# SYM_WX_STORM is a tracked strong thunderstorm and SYM_WX_HURRICANE a tropical cyclone from NHC.
CLASSES = [(rx, tag, env_symbol("WX_" + tag, sym)) for rx, tag, sym in CLASSES]
DEFAULT_CLASS = (DEFAULT_CLASS[0], env_symbol("WX_" + DEFAULT_CLASS[0], DEFAULT_CLASS[1]))
SYM_STORM = env_symbol("WX_STORM", ("\\", "T"))
SYM_HURRICANE = env_symbol("WX_HURRICANE", ("\\", "@"))
WATCH_TAGS = {"TOR": "TOA", "SVR": "SVA", "FFW": "FFA"}

LABEL_ABBREV = [
    ("Severe Thunderstorm", "SVR TSTM"), ("Special Weather Statement", "WX STMT"),
    ("Special Marine", "MARINE"), ("Thunderstorm", "TSTM"), ("Flash Flood", "FLASH FLOOD"),
    ("Coastal Flood", "CSTL FLOOD"), ("Winter Weather", "WINTER WX"), ("Tropical Storm", "TROP STORM"),
    ("Excessive", "XTRM"), ("Extreme", "XTRM"), ("Warning", "WRN"), ("Advisory", "ADV"),
    ("Statement", "STMT"),
]
COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def parse_home(text):
    """(lat, lon) from text such as '40.12,-75.34', '40.12, -75.34', '40.12 -75.34' or
    '40.12N 75.34W' (quotes and degree signs are ignored).  Raises ValueError."""
    t = str(text or "")
    for odd, plain in (("\u2212", "-"), ("\u2013", "-"), ("\u2014", "-"), ("\u2010", "-"), ("\u2011", "-"),
                       ("\u201c", ""), ("\u201d", ""), ("\u2018", ""), ("\u2019", ""), ("\u00a0", " "),
                       ("\ufeff", ""), ("\u00b0", " ")):
        t = t.replace(odd, plain)                     # minus signs and quotes from copied text
    t = t.strip().strip("'\"").upper().strip()
    if not t or re.sub(r"[-+\d.\sNSEW,;/]", "", t):
        raise ValueError(f"cannot read a latitude and longitude from {text!r}"
                         + (" (it contains characters other than digits, '-', ',' and N/S/E/W)"
                            if re.search(r"[^\x20-\x7e]", str(text or "")) else ""))
    nums = list(re.finditer(r"[-+]?\d+(?:\.\d+)?", t))
    letters = list(re.finditer(r"[NSEW]", t))
    if len(nums) != 2 or len(letters) not in (0, 2):
        raise ValueError(f"cannot read a latitude and longitude from {text!r}")
    hemi = ["", ""]
    if letters:                                       # 40.12N 75.34W (suffix) or N40.12 W75.34 (prefix)
        suffix = t[-1] in "NSEW"
        for L in letters:
            owner = [i for i, n in enumerate(nums) if (n.end() <= L.start() if suffix else n.start() >= L.end())]
            if not owner:
                raise ValueError(f"cannot read a latitude and longitude from {text!r}")
            i = owner[-1] if suffix else owner[0]
            if hemi[i]:
                raise ValueError(f"cannot read a latitude and longitude from {text!r}")
            hemi[i] = L.group()
    vals = [-abs(float(n.group())) if h in ("S", "W") else float(n.group()) for n, h in zip(nums, hemi)]
    lat, lon = vals
    if hemi[0] in ("E", "W") or hemi[1] in ("N", "S") or (abs(lat) > 90 >= abs(lon) and not hemi[0]):
        lat, lon = lon, lat                           # written longitude first
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError(f"{text!r} is outside the range of a latitude and longitude")
    return lat, lon


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def http(url, timeout=30, tries=3, accept="application/geo+json"):
    """GET with retry + exponential backoff on 429/5xx and network errors."""
    delay, last = 2, None
    for attempt in range(tries):
        wait = delay
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in RETRY_CODES:
                raise
            ra = e.headers.get("Retry-After") if e.headers else None
            if ra and ra.isdigit():
                wait = min(int(ra), 30)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
        if attempt < tries - 1:
            time.sleep(wait)
            delay *= 2
    raise last


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------
def miles(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(min(1.0, math.sqrt(a)))


def project(lat, lon, bearing_deg, nm):
    """Point reached from (lat, lon) after nm nautical miles on bearing_deg."""
    d = nm / 3440.065
    b, p1, l1 = math.radians(bearing_deg), math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def all_points(geom):
    """Every (lat, lon) in a GeoJSON geometry (any type)."""
    pts = []

    def walk(c):
        if isinstance(c, (list, tuple)):
            if len(c) >= 2 and all(isinstance(x, (int, float)) for x in c[:2]):
                pts.append((float(c[1]), float(c[0])))
            else:
                for x in c:
                    walk(x)
    if isinstance(geom, dict):
        if geom.get("type") == "GeometryCollection":
            for g in geom.get("geometries") or []:
                pts.extend(all_points(g))
        else:
            walk(geom.get("coordinates"))
    return pts


def ring_centroid(ring):
    """Area-weighted centroid of a ring of (lat, lon); falls back to the vertex mean."""
    a = cx = cy = 0.0
    for (y1, x1), (y2, x2) in zip(ring, ring[1:] + ring[:1]):
        cross = x1 * y2 - x2 * y1
        a += cross; cx += (x1 + x2) * cross; cy += (y1 + y2) * cross
    if abs(a) < 1e-12:
        return sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)
    return cy / (3 * a), cx / (3 * a)


def geom_centre(geom):
    """Centre of a GeoJSON geometry: polygon centroid if simple, else bounding-box centre."""
    if isinstance(geom, dict) and geom.get("type") == "Polygon" and geom.get("coordinates"):
        ring = [(float(p[1]), float(p[0])) for p in geom["coordinates"][0]]
        if len(ring) >= 3:
            return ring_centroid(ring)
    pts = all_points(geom)
    if not pts:
        return None
    lats, lons = [p[0] for p in pts], [p[1] for p in pts]
    return (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2


# --------------------------------------------------------------------------
# Shapes: the APRS "multiline" object format used by the APRS weather servers
#   <text> }<line type><shape><scale><lat/lon offset pairs>{<5-char sequence>
# --------------------------------------------------------------------------
LINE_TYPES = {"TOR": "a", "TOA": "b",                # red solid / red dashed
              "SVR": "d", "SMW": "d", "SQW": "d",    # yellow solid
              "SVA": "e", "SPS": "f"}                # yellow dashed / yellow double-dashed
LINE_OTHER, LINE_OTHER_WATCH = "j", "l"              # green solid / green double-dashed
MAX_SHAPE_POINTS = 20                                # the format allows about 23 pairs
SHAPE_TEXT_LIMIT = 40                                # keep the text short when a shape follows it


def convex_hull(points):
    """Convex hull of (lat, lon) points (monotone chain)."""
    pts = sorted(set((round(a, 5), round(b, 5)) for a, b in points))
    if len(pts) < 3:
        return pts

    def half(seq):
        h = []
        for q in seq:
            while len(h) >= 2 and ((h[-1][0] - h[-2][0]) * (q[1] - h[-2][1])
                                   - (h[-1][1] - h[-2][1]) * (q[0] - h[-2][0])) <= 0:
                h.pop()
            h.append(q)
        return h[:-1]
    return half(pts) + half(reversed(pts))


def simplify(ring, limit):
    """Drop the least significant corners of a closed ring until at most `limit` remain."""
    ring = list(ring)
    while len(ring) > max(limit, 3):
        def area(i):
            (y0, x0), (y1, x1), (y2, x2) = ring[i - 1], ring[i], ring[(i + 1) % len(ring)]
            return abs((x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0))
        del ring[min(range(len(ring)), key=area)]
    return ring


def multiline(lat, lon, ring, line_type, key, closed=True):
    """The multiline tail for an object at (lat, lon), or '' if the ring cannot be drawn."""
    ring = [p for i, p in enumerate(ring) if i == 0 or p != ring[i - 1]]
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring = ring[:-1]                                           # a closed polygon does not repeat its start
    ring = simplify(ring, MAX_SHAPE_POINTS)
    if len(ring) < (3 if closed else 2):
        return ""
    rlat, rlon = round(lat * 6000) / 6000.0, round(lon * 6000) / 6000.0    # what the packet actually carries
    need = max(max(abs(a - rlat), abs(b - rlon)) for a, b in ring)
    if need <= 0:
        return ""
    step = max(0, math.ceil(20 * math.log10(max(need / 44.0, 0.0001) / 0.0001) - 1e-9))   # 20 scale steps per decade
    if step > 80:
        return ""
    scale = 0.0001 * 10 ** (step / 20.0)
    pairs, last = "", None
    for a, b in ring:
        pair = (chr(78 + max(-45, min(44, round((a - rlat) / scale))))       # + is north
                + chr(78 + max(-45, min(44, round((rlon - b) / scale)))))    # + is west
        if pair != last:
            pairs += pair
        last = pair
    if len(pairs) < (6 if closed else 4):
        return ""
    return f" }}{line_type}{'0' if closed else '1'}{chr(33 + step)}{pairs}{{{b36_hash('shape|' + key, 5)}"


def alert_ring(a, zones, mode):
    """Outline to draw for an alert: NWS's own polygon, or (mode 'all') a hull around its zones."""
    if mode == "none":
        return []
    g = a.geometry
    if isinstance(g, dict) and g.get("type") == "Polygon" and g.get("coordinates"):
        try:
            return [(float(p[1]), float(p[0])) for p in g["coordinates"][0]]
        except (TypeError, ValueError, IndexError):
            return []
    pts = all_points(g)
    if not pts and mode == "all":
        for z in a.zones:
            pts.extend(zones.hull(z))
    return convex_hull(pts) if len(pts) >= 3 else []


def line_type(a):
    tag = a.klass[0]
    return LINE_TYPES.get(tag) or (LINE_OTHER_WATCH if "watch" in a.event.lower() else LINE_OTHER)


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------
def clean(text):
    text = text.replace("~", "-").replace("|", "/")
    text = text.encode("ascii", "replace").decode()
    return re.sub(r"\s+", " ", text).strip()


def pack(core, extras, limit):
    """core plus as many extras (in priority order) as fit in limit characters."""
    out = clean(core)[:limit]
    for e in extras:
        e = clean(e)
        if e and len(out) + 1 + len(e) <= limit:
            out += " " + e
    return out


def parse_time(s):
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def until_text(end, now):
    loc, today = end.astimezone(LOCAL), now.astimezone(LOCAL).date()
    return loc.strftime("til %H:%M") if loc.date() == today else loc.strftime("til %a %H:%M")


def compass(deg):
    return COMPASS[int((deg % 360) / 45 + 0.5) % 8]


def b36_hash(text, n):
    v = int.from_bytes(hashlib.sha1(text.encode()).digest()[:8], "big")
    digits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    out = ""
    for _ in range(n):
        v, r = divmod(v, 36)
        out = digits[r] + out
    return out


def first(params, key):
    v = (params or {}).get(key)
    if isinstance(v, list):
        v = v[0] if v else None
    return str(v).strip() if v not in (None, "") else ""


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------
MOTION_RE = re.compile(r"^(\S+?)\.\.\.([\w ]+?)\.\.\.(\d{1,3})DEG\.\.\.(\d+)KT\.\.\.(.+)$")
VTEC_RE = re.compile(r"/[OTEX]\.(\w{3})\.(\w{4})\.(\w\w)\.(\w)\.(\d{4})\.")


class Motion:
    """Storm fix from eventMotionDescription: DEG is the direction the storm comes FROM, speed in knots."""
    def __init__(self, text):
        m = MOTION_RE.match(text.strip())
        if not m:
            raise ValueError("no motion")
        self.time = parse_time(m.group(1))
        pts = [(float(a), float(b)) for a, b in re.findall(r"(-?\d+\.\d+),(-?\d+\.\d+)", m.group(5))]
        if not pts or self.time is None:
            raise ValueError("no motion")
        self.lat = sum(p[0] for p in pts) / len(pts)      # a line of storms -> its midpoint
        self.lon = sum(p[1] for p in pts) / len(pts)
        self.course = (int(m.group(3)) + 180) % 360        # direction it is heading TOWARD
        self.knots = int(m.group(4))

    def position_at(self, when, max_minutes):
        mins = max(0.0, min((when - self.time).total_seconds() / 60, max_minutes))
        if self.knots <= 0 or mins <= 0:
            return self.lat, self.lon
        return project(self.lat, self.lon, self.course, self.knots * mins / 60)


class Alert:
    def __init__(self, feature):
        p = feature.get("properties") or {}
        self.id = str(p.get("id") or feature.get("id") or "")
        self.event = str(p.get("event") or "Weather Alert")
        self.severity = p.get("severity") or "Unknown"
        self.status, self.msg_type = p.get("status"), p.get("messageType")
        self.params = p.get("parameters") or {}
        self.sent = parse_time(p.get("sent")) or datetime.now(timezone.utc)
        self.end = (parse_time(p.get("ends")) or parse_time(first(self.params, "eventEndingTime"))
                    or parse_time(p.get("expires")))
        self.area = str(p.get("areaDesc") or "")
        self.geometry = feature.get("geometry")
        self.zones = [str(z) for z in (p.get("affectedZones") or [])]
        refs = [str(r.get("identifier")) for r in (p.get("references") or []) if isinstance(r, dict)]
        self.root = sorted(refs)[0] if refs else self.id
        self.motion = None
        try:
            self.motion = Motion(first(self.params, "eventMotionDescription"))
        except ValueError:
            pass
        v = VTEC_RE.search(first(self.params, "VTEC"))
        self.vtec = v.groups() if v else None             # (action, office, phenomenon, significance, etn)
        if v:
            self.office = v.group(2)[-3:]
        else:
            m = (re.search(r"([A-Z]{3})\s*$", first(self.params, "AWIPSidentifier"))
                 or re.search(r"\b[KP]([A-Z]{3})\b", first(self.params, "WMOidentifier")))
            self.office = m.group(1) if m else "NWS"
        self.name = None
        self.lat = self.lon = None

    @property
    def klass(self):
        ev = self.event.lower()
        for rx, tag, sym in CLASSES:
            if re.search(rx, ev):
                if tag == "SPS" and self.motion:
                    return tag, SYM_STORM                  # a tracked strong thunderstorm
                if "watch" in ev:
                    tag = WATCH_TAGS.get(tag, tag)
                return tag, sym
        return DEFAULT_CLASS

    @property
    def key(self):
        """Identity of the event: stable across the updates/segments NWS issues for it."""
        if self.vtec:
            return ".".join(self.vtec[1:])                 # office.phenomenon.significance.event-number
        return self.root

    def name_for(self, salt=0):
        tag = self.klass[0]
        if self.vtec and not salt:
            return f"{tag}{self.office}{int(self.vtec[4]) % 1000:03d}"      # e.g. SVRPHI123
        return f"{tag}{self.office}{b36_hash(f'{self.key}|{salt}', 3)}"

    @property
    def label(self):
        s = self.event
        for a, b in LABEL_ABBREV:
            s = re.sub(a, b, s, flags=re.I)
        return s.upper()


def assign_names(items):
    """Unique 9-char names; deterministic so they stay the same from poll to poll."""
    used = {}
    for it in sorted(items, key=lambda a: a.key):
        salt = 0
        while True:
            name = it.name_for(salt)[:9]
            if used.get(name, it.key) == it.key:
                break
            salt += 1
        used[name] = it.key
        it.name = name


def fetch_alerts(areas):
    """Active alerts for the given areas (one request; falls back to one per area)."""
    def get(q):
        data = json.loads(http(f"{API}/alerts/active?area={q}"))
        feats = data.get("features")
        if not isinstance(feats, list):
            raise ValueError("unexpected response from api.weather.gov/alerts/active (no 'features' list)")
        return feats
    try:
        return get(",".join(areas))
    except urllib.error.HTTPError as e:
        if e.code != 400 or len(areas) == 1:
            raise
    feats, seen = [], set()
    for a in areas:
        for f in get(a):
            fid = (f.get("properties") or {}).get("id") or f.get("id")
            if fid not in seen:
                seen.add(fid); feats.append(f)
        time.sleep(0.3)
    return feats


# --------------------------------------------------------------------------
# Zone centres (cached forever: zones do not move)
# --------------------------------------------------------------------------
class Zones:
    def __init__(self, path, budget):
        self.path, self.budget, self.left, self.dirty = path, budget, budget, False
        try:
            with open(path) as f:
                self.cache = json.load(f)
        except (OSError, ValueError):
            self.cache = {}

    def new_cycle(self):
        self.left = self.budget

    def centre(self, url):
        """(lat, lon), None if the zone has no geometry, or 'later' if not fetched yet."""
        zid = url.rstrip("/").rsplit("/", 1)[-1]
        if zid in self.cache:
            c = self.cache[zid]
            return tuple(c) if c else None
        if self.left <= 0 or not url.startswith(API + "/zones/"):
            return "later"
        self.left -= 1
        try:
            data = json.loads(http(url, tries=2))
        except (urllib.error.URLError, OSError, ValueError) as e:
            if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                self.cache[zid] = None; self.dirty = True
                return None
            return "later"
        c = geom_centre(data.get("geometry"))
        self.cache[zid] = [round(c[0], 4), round(c[1], 4)] if c else None
        hull = simplify(convex_hull(all_points(data.get("geometry"))), 16)
        self.cache[zid + "#h"] = [[round(a, 3), round(b, 3)] for a, b in hull]
        self.dirty = True
        time.sleep(0.25)
        return c

    def hull(self, url):
        """Simplified outline of a zone as [(lat, lon), ...]; [] if unknown or not looked up yet."""
        zid = url.rstrip("/").rsplit("/", 1)[-1]
        if zid + "#h" not in self.cache and self.cache.get(zid, True):
            if self.left <= 0:
                return []
            self.cache.pop(zid, None)                # cached before outlines were kept: look it up again
            if self.centre(url) == "later":
                return []
        return [tuple(pt) for pt in self.cache.get(zid + "#h") or []]

    def save(self):
        if self.dirty:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.cache, f)
            os.replace(tmp, self.path)
            self.dirty = False


def locate(alert, home, radius, zones, now, track_minutes):
    """Set alert.lat/lon.  Returns 'ok', 'far' (outside the radius) or 'later' (zones not looked up yet)."""
    hlat, hlon = home
    poly = all_points(alert.geometry)
    if alert.motion:
        cap = track_minutes
        if alert.end:
            cap = min(cap, max(0.0, (alert.end - alert.motion.time).total_seconds() / 60))
        lat, lon = alert.motion.position_at(now, cap)
        near = min([miles(hlat, hlon, lat, lon)] + [miles(hlat, hlon, a, b) for a, b in poly])
        if near > radius:
            return "far"
        alert.lat, alert.lon = lat, lon
        return "ok"
    if poly:
        c = geom_centre(alert.geometry)
        if min(miles(hlat, hlon, a, b) for a, b in poly + [c]) > radius:
            return "far"
        alert.lat, alert.lon = c
        return "ok"
    cents, pending = [], False
    for z in alert.zones:
        c = zones.centre(z)
        if c == "later":
            pending = True
        elif c and miles(hlat, hlon, c[0], c[1]) <= radius:
            cents.append(c)
    if not cents:
        return "later" if pending else "far"
    mlat = sum(c[0] for c in cents) / len(cents)
    mlon = sum(c[1] for c in cents) / len(cents)
    alert.lat, alert.lon = min(cents, key=lambda c: miles(mlat, mlon, c[0], c[1]))   # stay inside the alerted area
    return "ok"


def alert_text(a, now, limit):
    """(course/speed extension or '', comment)."""
    ext, extras = "", []
    p = a.params
    core = a.label + (" " + until_text(a.end, now) if a.end else "")
    tor = first(p, "tornadoDetection")
    if tor:
        extras.append("TORNADO " + tor.upper().replace("RADAR INDICATED", "RDR IND") if "tornado" not in a.event.lower()
                      else tor.upper().replace("RADAR INDICATED", "RDR IND"))
    gust = re.sub(r"\s+", "", first(p, "maxWindGust")).lower().replace("upto", "")
    if gust and not gust.startswith("0"):
        extras.append(gust)
    hail = first(p, "maxHailSize").lower().replace("up to", "").replace("in", "").strip()
    try:
        if float(hail) > 0:
            extras.append(f"{float(hail):.2f}in hail")
    except ValueError:
        pass
    for k in ("tornadoDamageThreat", "thunderstormDamageThreat", "flashFloodDamageThreat"):
        v = first(p, k).upper()
        if v in ("CONSIDERABLE", "DESTRUCTIVE", "CATASTROPHIC"):
            extras.append({"CONSIDERABLE": "CONSID DMG", "DESTRUCTIVE": "DESTRUCTIVE", "CATASTROPHIC": "CATASTROPHIC"}[v])
    if a.motion:
        if a.motion.knots > 0:
            ext = f"{a.motion.course or 360:03d}/{min(a.motion.knots, 999):03d}"
            extras.insert(0, f"mv {compass(a.motion.course)} {round(a.motion.knots * 1.15078):d}mph")
        else:
            extras.insert(0, "stationary")
    else:
        places = [x.strip() for x in a.area.split(";") if x.strip()]
        if places:
            extras.append(places[0][:18] + (f" +{len(places) - 1}" if len(places) > 1 else ""))
    extras.append("NWS" if a.office == "NWS" else "NWS " + a.office)
    return ext, pack(core, extras, limit - len(ext))


# --------------------------------------------------------------------------
# Tropical cyclones (National Hurricane Center)
# --------------------------------------------------------------------------
TC_CLASS = {"HU": "HURRICANE", "TS": "TROP STORM", "TD": "TROP DEPRESSION", "STS": "SUBTROP STORM",
            "STD": "SUBTROP DEPRESSION", "PTC": "POST-TROP CYCLONE", "PC": "POTENTIAL TROP CYCLONE",
            "TY": "TYPHOON"}


def hemi(v):
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)\s*([NSEW]?)", str(v or ""), re.I)
    if not m:
        raise ValueError("bad coordinate")
    x = float(m.group(1))
    return -x if m.group(2).upper() in ("S", "W") else x


STORM_TYPES = {"HU": "HC", "TY": "HC", "TS": "TS", "STS": "TS", "TD": "TD", "STD": "TD"}
ADVISORIES = {}        # advisory url -> (issue marker, parsed details)


def parse_advisory(text):
    """Gust and wind radii (knots / nautical miles) at the CURRENT position from an NHC forecast advisory."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.split(r"FORECAST VALID|REPEAT\.\.\.CENTER", text, maxsplit=1)[0]     # ignore the forecast positions
    out = {}
    m = re.search(r"MAX SUSTAINED WINDS\s+(\d+)\s*KT WITH GUSTS TO\s+(\d+)\s*KT", text)
    if m:
        out["wind"], out["gust"] = int(m.group(1)), int(m.group(2))
    for kt in (64, 50, 34):
        m = re.search(rf"\b{kt} KT\.+\s*(\d+)NE\s+(\d+)SE\s+(\d+)SW\s+(\d+)NW", text)
        out[f"r{kt}"] = max(int(x) for x in m.groups()) if m else 0       # widest quadrant
    return out


def advisory_for(storm):
    """Details from the storm's forecast advisory, fetched once per advisory; {} if unavailable."""
    adv = storm.get("forecastAdvisory")
    url = adv.get("url") if isinstance(adv, dict) else None
    if not isinstance(url, str) or not url.startswith("https://www.nhc.noaa.gov/"):
        return {}
    marker = f"{adv.get('advNum')}|{adv.get('issuance')}"
    if ADVISORIES.get(url, ("",))[0] != marker:
        try:
            details = parse_advisory(http(url, tries=2, accept="text/html, text/plain").decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"# NHC forecast advisory not read ({e}); wind radii omitted", file=sys.stderr)
            return {}
        ADVISORIES[url] = (marker, details)
    return ADVISORIES[url][1]


def storm_data(storm, cls, kt, pressure):
    """APRS storm data (after course/speed): /ST/www^GGG/pppp>RRR&rrr%ggg, or '' if details are missing."""
    st = STORM_TYPES.get(cls)
    d = advisory_for(storm) if st else {}
    if not st or kt is None or pressure is None or "gust" not in d:
        return ""
    return (f"/{st}/{min(kt, 999):03d}^{min(d['gust'], 999):03d}/{min(pressure, 9999):04d}"
            f">{min(d['r64'], 999):03d}&{min(d['r34'], 999):03d}%{min(d['r50'], 999):03d}")


def tropical_objects(url, home, radius, limit, now, storm_format=False):
    """name -> (lat, lon, (table, code, text)) for active storms within radius."""
    data = json.loads(http(url, accept="application/json"))
    storms = data.get("activeStorms")
    if not isinstance(storms, list):
        raise ValueError("unexpected NHC response (no 'activeStorms' list)")
    out = {}
    for s in storms:
        try:
            lat = s.get("latitudeNumeric", s.get("latitude_numeric"))
            lon = s.get("longitudeNumeric", s.get("longitude_numeric"))
            lat = float(lat) if lat is not None else hemi(s.get("latitude"))
            lon = float(lon) if lon is not None else hemi(s.get("longitude"))
        except (TypeError, ValueError):
            continue
        if miles(home[0], home[1], lat, lon) > radius:
            continue
        cls = str(s.get("classification") or "TC").upper()
        nm = re.sub(r"[^A-Z0-9]", "", str(s.get("name") or s.get("id") or "STORM").upper())
        name = f"{cls[:2]}-{nm}"[:9]
        ext, extras, kt, mb = "", [], None, None
        try:
            kt = int(float(s.get("intensity")))
            cat = next((c for c, lo in ((5, 137), (4, 113), (3, 96), (2, 83), (1, 64)) if kt >= lo), 0)
            extras.append(f"{round(kt * 1.15078)}mph" + (f" CAT{cat}" if cat and cls == "HU" else ""))
        except (TypeError, ValueError):
            pass
        try:
            mph, mdir = float(s.get("movementSpeed")), int(float(s.get("movementDir"))) % 360
            if mph > 0:
                ext = f"{mdir or 360:03d}/{min(round(mph / 1.15078), 999):03d}"
                extras.append(f"mv {compass(mdir)} {round(mph)}mph")
            else:
                extras.append("stationary")
        except (TypeError, ValueError):
            pass
        try:
            mb = int(float(s.get("pressure")))
            extras.append(f"{mb}mb")
        except (TypeError, ValueError):
            pass
        lim = limit
        storm = storm_data(s, cls, kt, mb) if storm_format else ""
        if storm:                       # storm data needs a course/speed in front of it
            ext = (ext or "000/000") + storm
            lim = max(limit, len(ext) + SHAPE_TEXT_LIMIT)
        upd = parse_time(s.get("lastUpdate"))
        if upd:
            extras.append(upd.astimezone(LOCAL).strftime("at %a %H:%M"))
        extras.append("NHC")
        core = f"{TC_CLASS.get(cls, 'TROP CYCLONE')} {str(s.get('name') or '').upper()}".strip()
        out[name] = (lat, lon, (SYM_HURRICANE[0], SYM_HURRICANE[1], ext + (" " if storm else "") + pack(core, extras, lim - len(ext))))
    return out


def is_tropical_name(name):
    return len(name) > 3 and name[2] == "-"


# --------------------------------------------------------------------------
# APRS formatting, state, APRS-IS
# --------------------------------------------------------------------------
def aprs_lat(lat):
    h = "N" if lat >= 0 else "S"
    lat = abs(lat)
    d, m = int(lat), round((lat - int(lat)) * 60, 2)
    if m >= 60:
        d, m = d + 1, 0.0
    return f"{d:02d}{m:05.2f}{h}"


def aprs_lon(lon):
    h = "E" if lon >= 0 else "W"
    lon = abs(lon)
    d, m = int(lon), round((lon - int(lon)) * 60, 2)
    if m >= 60:
        d, m = d + 1, 0.0
    return f"{d:03d}{m:05.2f}{h}"


def object_packet(name, lat, lon, when_utc, table, code, text, live=True):
    ts = when_utc.strftime("%d%H%M") + "z"
    return f";{name:<9.9}{'*' if live else '_'}{ts}{aprs_lat(lat)}{table}{aprs_lon(lon)}{code}{text}"


def load_state(path, callsign):
    try:
        with open(path) as f:
            raw = json.load(f)
        objs = raw["objects"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    if str(raw.get("callsign", "")).upper() != callsign:
        print(f"# {path} was written by {raw.get('callsign')}, not {callsign}; ignoring it "
              f"(its objects will not be killed)", file=sys.stderr)
        return {}
    live = {}
    for name, o in objs.items():
        try:
            live[name] = (float(o["lat"]), float(o["lon"]), (o["table"], o["code"], o["comment"]))
        except (KeyError, TypeError, ValueError):
            continue
    return live


def save_state(path, callsign, live):
    data = {"callsign": callsign,
            "objects": {n: {"lat": lat, "lon": lon, "table": t, "code": c, "comment": cm}
                        for n, (lat, lon, (t, c, cm)) in live.items()}}
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


# Sending is done by aprs_send.py (one APRS-IS connection for every script): this script
# hands over each poll's packets with aprs_send.Outbox and never logs in itself.


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def select_alerts(features, args, now):
    """Parse, filter and merge the feed into one Alert per weather event."""
    limit_rank = SEVERITY_RANK[args.min_severity]
    exclude = re.compile(args.exclude, re.I) if args.exclude else None
    best = {}
    for f in features:
        try:
            a = Alert(f)
        except (AttributeError, TypeError, ValueError):
            continue
        if a.status not in (None, "Actual") or a.msg_type == "Cancel":
            continue
        if a.vtec and a.vtec[0] in ("CAN", "EXP", "UPG"):      # this segment was cancelled/expired/replaced
            continue
        if a.end and a.end <= now:
            continue
        if exclude and exclude.search(a.event):
            continue
        if SEVERITY_RANK.get(a.severity, 4) > limit_rank and not a.motion:
            continue
        prev = best.get(a.key)
        if prev is None:
            best[a.key] = a
            continue
        # same event issued in several segments/updates: keep the newest storm fix, pool the zones
        keep, other = (a, prev) if (bool(a.motion), a.sent) > (bool(prev.motion), prev.sent) else (prev, a)
        keep.zones = list(dict.fromkeys(keep.zones + other.zones))
        if other.area and other.area not in keep.area:
            keep.area = f"{keep.area}; {other.area}" if keep.area else other.area
        if keep.end and other.end:
            keep.end = max(keep.end, other.end)
        best[a.key] = keep
    return list(best.values())


def run_cycle(args, live, aprs_is, zones):
    now = datetime.now(timezone.utc)
    alerts = select_alerts(fetch_alerts(args.areas), args, now)   # raises on a bad feed -> nothing is killed
    assign_names(alerts)
    zones.new_cycle()
    placed, waiting = {}, 0
    ranked = []
    for a in alerts:
        res = locate(a, args.home, args.radius_miles, zones, now, args.track_minutes)
        if res == "ok":
            ranked.append(a)
        elif res == "later":
            waiting += 1
            if a.name in live:                      # keep what is already on the map until zones resolve
                placed[a.name] = live[a.name]
    zones.save()
    ranked.sort(key=lambda a: (not a.motion, SEVERITY_RANK.get(a.severity, 4),
                               miles(args.home[0], args.home[1], a.lat, a.lon)))
    dropped = max(0, len(ranked) - args.max_objects)
    zones.new_cycle()                                   # outlines get their own lookup budget
    for a in ranked[:args.max_objects]:
        tag, (table, code) = a.klass
        ring = alert_ring(a, zones, args.shapes)
        shape = multiline(a.lat, a.lon, ring, line_type(a), a.key) if ring else ""
        limit = min(args.comment_length, SHAPE_TEXT_LIMIT + (7 if a.motion else 0)) if shape else args.comment_length
        ext, comment = alert_text(a, now, limit)
        placed[a.name] = (a.lat, a.lon, (table, code, ext + comment + shape))
    zones.save()

    if not args.no_tropical:
        try:
            placed.update(tropical_objects(args.tropical_url, args.home, args.tropical_radius_miles,
                                           args.comment_length, now, storm_format=args.shapes != "none"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"# tropical feed failed ({e}); keeping existing tropical objects", file=sys.stderr)
            placed.update({n: v for n, v in live.items() if is_tropical_name(n)})

    if aprs_overrides:
        aprs_overrides.apply(placed)

    out = [object_packet(n, lat, lon, now, t, c, text) for n, (lat, lon, (t, c, text)) in placed.items()]
    gone = [n for n in live if n not in placed]
    for name in gone:
        lat, lon, (table, code, _text) = live[name]
        out.append(object_packet(name, lat, lon, now, table, code, "", live=False))

    for pkt in out:
        print(f"{args.callsign}>APRS,TCPIP*:{pkt}")
        if aprs_is:
            aprs_is.send(pkt)
    if aprs_is:
        aprs_is.flush()                # hand this poll's packets to aprs_send.py in one go
    sys.stdout.flush()

    # bookkeeping only after everything was sent, so a failed send repeats instead of losing kills
    for name in gone:
        del live[name]
    live.update(placed)
    save_state(args.state_file, args.callsign, live)
    return len(placed), len(gone), waiting, dropped


def main():
    global USER_AGENT
    ap = argparse.ArgumentParser(description="NWS alerts + NHC tropical cyclones -> APRS objects")
    ap.add_argument("--home", default=os.environ.get(HOME_ENV, DEFAULT_HOME),
                    help=f"centre of your area as lat,lon (default: ${HOME_ENV} or {DEFAULT_HOME})")
    ap.add_argument("--radius-miles", type=float, default=200, help="show alerts within this distance (default 200)")
    ap.add_argument("--areas", default=DEFAULT_AREAS,
                    help=f"state/marine area codes to query (default {DEFAULT_AREAS})")
    ap.add_argument("--contact", default=os.environ.get(CONTACT_ENV, ""),
                    help=f"your e-mail or website for the NWS User-Agent (default: ${CONTACT_ENV})")
    ap.add_argument("--min-severity", default="Severe", choices=["Extreme", "Severe", "Moderate", "Minor"],
                    help="least severe alert to show (default Severe); tracked storms are always shown")
    ap.add_argument("--exclude", default="", help="regex of event names to skip, e.g. 'Rip Current|Air Quality'")
    ap.add_argument("--max-objects", type=int, default=40, help="most alert objects at once (default 40)")
    ap.add_argument("--track-minutes", type=float, default=45,
                    help="move a storm along its track for at most this long after NWS's last fix (default 45)")
    ap.add_argument("--zone-lookups", type=int, default=30, help="new zone lookups per cycle (default 30)")
    ap.add_argument("--shapes", default="polygon", choices=["none", "polygon", "all"],
                    help="draw outlines: polygon = warnings NWS issues with a polygon (default), "
                         "all = also a hull around zone/county products, none = points only")
    ap.add_argument("--no-tropical", action="store_true", help="skip National Hurricane Center storms")
    ap.add_argument("--tropical-radius-miles", type=float, default=750,
                    help="show tropical cyclones within this distance (default 750)")
    ap.add_argument("--tropical-url", default=NHC_URL)
    ap.add_argument("--state-file", default=STATE_FILE)
    ap.add_argument("--zone-cache", default=ZONE_CACHE)
    ap.add_argument("--comment-length", type=int, default=COMMENT_LIMIT,
                    help=f"maximum characters of object text (default {COMMENT_LIMIT}; APRS 1.0 specifies 43)")
    ap.add_argument("--callsign", default="N0CALL", help="source callsign-SSID for the packets")
    ap.add_argument("--passcode", type=int, help="ignored: aprs_send.py logs in to APRS-IS, not this script")
    ap.add_argument("--send", action="store_true", help="hand the packets to aprs_send.py for sending (requires a real callsign)")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--interval", type=int, default=120, help="seconds between polls (default 120, minimum 60)")
    args = ap.parse_args()

    if not 20 <= args.comment_length <= 200:
        ap.error("--comment-length must be between 20 and 200")
    try:
        args.home = parse_home(args.home)
    except ValueError as e:
        where = f"${HOME_ENV} in aprs.env" if args.home == os.environ.get(HOME_ENV) else "--home"
        ap.error(f"{where}: {e}.  Write it as latitude,longitude, e.g. 40.12,-75.34")
    if not args.contact.strip():
        ap.error(f"NWS asks every client to identify itself: pass --contact (or set ${CONTACT_ENV}) "
                 f"to an e-mail address or website where they can reach you")
    USER_AGENT = f"(aprs-nws-objects/1.0, {args.contact.strip()})"
    args.areas = [a.strip().upper() for a in args.areas.split(",") if a.strip()]
    args.interval = max(60, args.interval)
    args.callsign = args.callsign.upper()
    if args.send and args.callsign.startswith("N0CALL"):
        ap.error("--send requires your own callsign (--callsign)")

    live = load_state(args.state_file, args.callsign)
    if live:
        print(f"# restored {len(live)} live object(s) from {args.state_file}", file=sys.stderr)
    zones = Zones(args.zone_cache, args.zone_lookups)
    aprs_is = aprs_send.Outbox("nws", args.callsign) if args.send else None

    try:
        while True:
            try:
                n, killed, waiting, dropped = run_cycle(args, live, aprs_is, zones)
                msg = f"# {n} object(s) live, {killed} killed"
                if waiting:
                    msg += f", {waiting} alert(s) waiting for zone lookups"
                if dropped:
                    msg += f", {dropped} over --max-objects not shown"
                print(msg, file=sys.stderr)
            except (urllib.error.URLError, OSError, ValueError, RuntimeError) as e:
                print(f"# cycle failed: {e}", file=sys.stderr)
                if aprs_is:
                    aprs_is.close()
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if aprs_is:
            aprs_is.close()


if __name__ == "__main__":
    main()
