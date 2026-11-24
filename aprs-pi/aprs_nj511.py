#!/usr/bin/env python3
"""
511NJ "All NJ Active Events" RSS feed -> APRS objects.

Source: https://511nj.org/client/rest/rss/RSSAllNJActiveEvents (511nj.org; data from
NJDOT, the NJ Turnpike Authority, the Port Authority NY/NJ, DRPA and TRANSCOM).  This
replaces aprs_njta.py: the Turnpike and Parkway events are in this feed too.

Each feed item has a title ("US 9 northbound : Crash"), a description that starts with
the reporting agency, a pubDate and a georss:point.  There is no event id and no county:

  * names are a hash of the title and position, so they stay the same from poll to poll;
  * counties come from the position.  On first use the script downloads the outlines of
    New Jersey's 21 counties from api.weather.gov and caches them in nj_counties.json.

What is sent (--types, default incident,closure,delay):
  incident  crashes, disabled vehicles, fires, spills, gas main breaks, police activity ...
  closure   any event with all lanes closed, roadwork included, and explicit road closures
  delay     congestion / travel-time reports
  roadwork  roadwork, construction, paving, bridge work that does not close the road
  event     games, concerts and other venue traffic
  other     anything else (fuel availability, truck restrictions ...)

  python3 aprs_nj511.py --once --county Camden,Burlington,Gloucester      # print only
  python3 aprs_nj511.py --callsign W3XYZ-9 --send --county Camden

Many items are scheduled for other days (overnight roadwork next week, a concert next
summer).  The dates written in the description decide: only events whose dates include
today are sent; those starting later, already over, or "Not currently Scheduled" are
left out (--lookahead-hours shows ones starting soon; --include-scheduled shows all).

If an aprs_njta.py state file (njta_live_state.json) is found, its objects are killed on
the first successful cycle and the file is renamed, so nothing is left on the map.
"""
import argparse
import email.utils
import hashlib
import json
import math
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    import aprs_overrides            # edits/deletes made in the web UI (optional)
except ImportError:
    aprs_overrides = None

FEED_URL = "https://511nj.org/client/rest/rss/RSSAllNJActiveEvents"
APRS_IS_HOST, APRS_IS_PORT = "rotate.aprs2.net", 14580
STATUS_TEXT = "NJ traffic events: 511NJ (511nj.org)"
STATE_FILE = "nj511_live_state.json"
OLD_NJTA_STATE = "njta_live_state.json"
COUNTY_CACHE = "nj_counties.json"
NWS_API = "https://api.weather.gov"
EASTERN = ZoneInfo("America/New_York")
RETRY_CODES = {429, 500, 502, 503, 504}
NJ_BOX = (38.92, -75.57, 41.36, -73.89)    # south, west, north, east: only when the outlines are unavailable
NEAR_COUNTY_MILES = 1.5          # a point just outside every outline (a river, a bridge) joins the nearest county

# New Jersey counties by FIPS code (the NWS county zone is NJC + code)
NJ_COUNTIES = {
    "001": "Atlantic", "003": "Bergen", "005": "Burlington", "007": "Camden", "009": "Cape May",
    "011": "Cumberland", "013": "Essex", "015": "Gloucester", "017": "Hudson", "019": "Hunterdon",
    "021": "Mercer", "023": "Middlesex", "025": "Monmouth", "027": "Morris", "029": "Ocean",
    "031": "Passaic", "033": "Salem", "035": "Somerset", "037": "Sussex", "039": "Union", "041": "Warren",
}
TYPES = ("incident", "closure", "delay", "roadwork", "event", "other")
DEFAULT_TYPES = "incident,closure,delay"


def env_symbol(key, default):
    """APRS symbol (table, code) from $SYM_<key> (two characters, e.g. /d), else the built-in default."""
    v = os.environ.get("SYM_" + key, "")
    if len(v) == 2 and (v[0] in "/\\" or v[0].isalnum()) and "!" <= v[1] <= "~":
        return v[0], v[1]
    if v:
        print(f"# SYM_{key}={v!r} is not a valid APRS symbol (table + code); using the default", file=sys.stderr)
    return default


# Change these in aprs.env.  The first three are shared with aprs_pa511.py.
SYM_WORKS = env_symbol("ROAD_WORK", ("/", "."))       # red X: roadwork
SYM_CRASH = env_symbol("ROAD_CRASH", ("\\", "'"))     # crash site: incidents
SYM_MAJOR = env_symbol("ROAD_MAJOR", ("\\", "!"))     # emergency: road closed
SYM_DELAY = env_symbol("ROAD_DELAY", ("/", "."))      # delays
SYM_EVENT = env_symbol("ROAD_EVENT", ("/", "E"))      # venue events
SYM_OTHER = env_symbol("ROAD_OTHER", ("\\", "<"))     # anything else
SYMBOLS = {"incident": SYM_CRASH, "closure": SYM_MAJOR, "delay": SYM_DELAY, "roadwork": SYM_WORKS,
           "event": SYM_EVENT, "other": SYM_OTHER}
TAGS = {"incident": "TRAF", "closure": "TRAF", "delay": "TRAF", "roadwork": "WORK", "event": "EVNT",
        "other": "INFO"}

EVENT_WORDS = re.compile(r"\b(game|concert|show|special event|sports? event|skating|racing|race|festival|parade|"
                         r"market|fair|marathon|circus|exhibition|convention)\b", re.I)
DELAY_WORDS = re.compile(r"\b(delays?|congestion|heavy traffic|slow traffic)\b", re.I)
INCIDENT_WORDS = re.compile(r"\b(crash|accident|collision|disabled|stalled|overturned|jackknifed|fire|smoke|spill|"
                            r"debris|hazmat|gas main|water main|wires? down|downed|tree|pole|flood|police|"
                            r"emergency|pedestrian|struck|vehicle|rollover|sinkhole|investigation|ice|icing)\b", re.I)
CLOSURE_TYPE = re.compile(r"\b(closed|closure|closing)\b", re.I)
ROADWORK_WORDS = re.compile(r"\b(roadwork|road work|construction|paving|bridge|drainage|repairs?|maintenance|"
                            r"utility|milling|striping|resurfacing|replacement|improvements?|operational activity|"
                            r"inspection|sweeping|work)\b", re.I)
ALL_CLOSED = re.compile(r"\ball (?:travel )?lanes (?:are )?closed\b|\broad (?:is )?closed\b|\b(?:ramp|bridge|tunnel) closed\b",
                        re.I)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
USER_AGENT = "nj511-aprs/1.0"


def http(url, timeout=45, tries=3, accept="application/rss+xml, application/xml, text/xml"):
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
# Feed
# --------------------------------------------------------------------------
ITEM_RE = re.compile(r"<item\b[^>]*>(.*?)</item>", re.S | re.I)


def _field(block, name):
    m = re.search(rf"<(?:\w+:)?{name}\b[^>]*>(.*?)</(?:\w+:)?{name}>", block, re.S | re.I)
    if not m:
        return ""
    text = re.sub(r"^\s*<!\[CDATA\[(.*)\]\]>\s*$", r"\1", m.group(1), flags=re.S)
    return (text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'")
            .replace("&#39;", "'").replace("&amp;", "&").strip())


def parse_feed(raw):
    """[{title, description, pubdate, point}] from the RSS text.  Raises ValueError if it is not the feed."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    items = []
    try:
        root = ET.fromstring(text)
        if root.tag.lower().split("}")[-1] != "rss":
            raise ValueError
        for it in root.iter():
            if it.tag.lower().split("}")[-1] != "item":
                continue
            rec = {}
            for ch in it:
                rec[ch.tag.lower().split("}")[-1]] = (ch.text or "").strip()
            items.append({"title": rec.get("title", ""), "description": rec.get("description", ""),
                          "pubdate": rec.get("pubdate", ""), "point": rec.get("point", "")})
    except (ET.ParseError, ValueError):
        # a stray unescaped "&" makes the XML invalid; read the items with a simpler parser instead
        if "<rss" not in text[:2000].lower() and "<channel" not in text[:4000].lower():
            raise ValueError(f"the 511NJ feed did not return RSS (starts: {text[:120]!r})")
        items = [{"title": _field(b, "title"), "description": _field(b, "description"),
                  "pubdate": _field(b, "pubDate"), "point": _field(b, "point")} for b in ITEM_RE.findall(text)]
    if not items and "<item" not in text.lower() and "<channel" not in text.lower():
        raise ValueError(f"the 511NJ feed did not return RSS (starts: {text[:120]!r})")
    return items


def classify(etype, desc):
    """One of TYPES."""
    if EVENT_WORDS.search(etype):
        return "event"
    if DELAY_WORDS.search(etype):
        return "delay"
    if CLOSURE_TYPE.search(etype) or ALL_CLOSED.search(desc):
        return "closure"
    if INCIDENT_WORDS.search(etype):
        return "incident"
    if ROADWORK_WORDS.search(etype):
        return "roadwork"
    return "other"


class Event:
    def __init__(self, item):
        self.title = re.sub(r"\s+", " ", item.get("title") or "").strip()
        road, _, etype = self.title.partition(" : ")
        self.road, self.etype = road.strip(), etype.strip()
        desc = re.sub(r"\s+", " ", item.get("description") or "").strip()
        agency, sep, rest = desc.partition(": ")
        if sep and len(agency) <= 45:
            self.agency, self.text = agency.strip(), rest.strip()
        else:
            self.agency, self.text = "", desc
        self.kind = classify(self.etype, self.text)
        m = re.match(r"\s*(-?\d+(?:\.\d+)?)[\s,]+(-?\d+(?:\.\d+)?)\s*$", item.get("point") or "")
        self.pos = (float(m.group(1)), float(m.group(2))) if m else None
        try:
            self.updated = email.utils.parsedate_to_datetime(item.get("pubdate") or "").astimezone(timezone.utc)
        except (TypeError, ValueError):
            self.updated = datetime.now(timezone.utc)
        self.county = ""
        self.name = None
        self.salt_key = ""

    @property
    def key(self):
        return f"{self.title}|{self.pos[0]:.4f},{self.pos[1]:.4f}{self.salt_key}"

    @property
    def tag(self):
        return TAGS[self.kind]

    def name_for(self, salt=0):
        return f"{self.tag}{b36_hash(self.key + (f'#{salt}' if salt else ''), 9 - len(self.tag))}"


# --------------------------------------------------------------------------
# Counties: NWS county outlines, cached; point-in-polygon
# --------------------------------------------------------------------------
def outer_and_holes(geom):
    """[[ring, hole, hole...], ...] of (lat, lon) lists for a GeoJSON Polygon/MultiPolygon."""
    if not isinstance(geom, dict):
        return []
    polys = geom.get("coordinates") or []
    if geom.get("type") == "Polygon":
        polys = [polys]
    elif geom.get("type") != "MultiPolygon":
        return []
    return [[[(float(p[1]), float(p[0])) for p in ring] for ring in poly] for poly in polys if poly]


def thin(ring, step=0.001):
    out = []
    for lat, lon in ring:
        if not out or abs(lat - out[-1][0]) > step or abs(lon - out[-1][1]) > step:
            out.append([round(lat, 5), round(lon, 5)])
    return out


def inside_ring(lat, lon, ring):
    hit = False
    for (y0, x0), (y1, x1) in zip(ring, ring[1:] + ring[:1]):
        if (y0 > lat) != (y1 > lat) and lon < x0 + (lat - y0) * (x1 - x0) / (y1 - y0):
            hit = not hit
    return hit


def miles(lat1, lon1, lat2, lon2):
    return math.hypot((lat2 - lat1) * 69.0, (lon2 - lon1) * 69.172 * math.cos(math.radians((lat1 + lat2) / 2)))


class Counties:
    def __init__(self, path, contact=""):
        self.path, self.contact = path, (contact or "").strip()
        try:
            with open(path) as f:
                self.shapes = json.load(f)
        except (OSError, ValueError):
            self.shapes = {}

    def ready(self):
        return all(name in self.shapes for name in NJ_COUNTIES.values())

    def load(self):
        """Download any missing county outlines.  Raises if one cannot be had (nothing is killed then)."""
        missing = [(code, name) for code, name in NJ_COUNTIES.items() if name not in self.shapes]
        if not missing:
            return
        ua = f"(nj511-aprs/1.0, {self.contact})" if self.contact else "nj511-aprs/1.0 (personal APRS display)"
        for code, name in missing:
            req = urllib.request.Request(f"{NWS_API}/zones/county/NJC{code}",
                                         headers={"User-Agent": ua, "Accept": "application/geo+json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())
            polys = [[thin(ring) for ring in poly] for poly in outer_and_holes(data.get("geometry"))]
            polys = [p for p in polys if p and len(p[0]) >= 3]
            if not polys:
                raise ValueError(f"no outline for {name} County in the NWS reply")
            self.shapes[name] = polys
            time.sleep(0.3)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.shapes, f, separators=(",", ":"))
        os.replace(tmp, self.path)
        print(f"# county outlines cached for {len(self.shapes)} NJ counties", file=sys.stderr, flush=True)

    def county_of(self, lat, lon):
        """County name for a point, '' if it is not in (or right next to) New Jersey."""
        for name, polys in self.shapes.items():
            for poly in polys:
                if inside_ring(lat, lon, poly[0]) and not any(inside_ring(lat, lon, h) for h in poly[1:]):
                    return name
        best, dist = "", NEAR_COUNTY_MILES
        for name, polys in self.shapes.items():
            for poly in polys:
                for a, b in poly[0]:
                    if abs(a - lat) < 0.05 and abs(b - lon) < 0.05:
                        d = miles(lat, lon, a, b)
                        if d < dist:
                            best, dist = name, d
        return best


def county_names(text):
    """Normalised NJ county names from --county, or raises ValueError naming the unknown ones."""
    want, bad = set(), []
    key = lambda t: re.sub(r"[^a-z]", "", t.lower())              # "Cape May", "Cape_May", "capemay" all match
    lookup = {key(n): n for n in NJ_COUNTIES.values()}
    for part in re.split(r"[,;]", text or ""):
        p = key(re.sub(r"\s*county\s*$", "", part.strip(), flags=re.I))
        if not p:
            continue
        if p in lookup:
            want.add(lookup[p])
        else:
            bad.append(part.strip())
    if bad:
        raise ValueError(f"not a New Jersey county: {', '.join(bad)} (choose from {', '.join(sorted(lookup.values()))})")
    return want


# --------------------------------------------------------------------------
# Names and text
# --------------------------------------------------------------------------
B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def b36_hash(key, n):
    h = int(hashlib.sha1(key.encode()).hexdigest(), 16)
    out = []
    for _ in range(n):
        h, r = divmod(h, 36)
        out.append(B36[r])
    return "".join(out)


def assign_names(events):
    """Unique names that stay the same from poll to poll.  Several events can share a title and
    position (different lanes or time windows at one spot): those are told apart by their text."""
    groups = {}
    for ev in events:
        groups.setdefault(ev.key, []).append(ev)
    for group in groups.values():
        if len(group) > 1:
            for i, ev in enumerate(sorted(group, key=lambda e: e.text)):
                ev.salt_key = f"|{i}"
    used = set()
    for ev in sorted(events, key=lambda e: e.key):
        salt = 0
        while ev.name_for(salt) in used:
            salt += 1
        ev.name = ev.name_for(salt)
        used.add(ev.name)


COMMENT_LIMIT = 67  # --comment-length; APRS 1.0 specifies 43

ABBREV = [  # applied in order, case-insensitive
    (r"New Jersey Turnpike", "NJTP"), (r"Garden State Parkway", "GSP"), (r"Atlantic City Expressway", "ACE"),
    (r"\bHudson County Extension\b", "Hudson Ext"), (r"\bWestern Spur\b", "W Spur"), (r"\bEastern Spur\b", "E Spur"),
    (r"\bPearl Harbor Memorial Extension\b", "Pearl Hbr Ext"), (r"\bGeorge Washington Bridge\b", "GWB"),
    (r"\b(Inner|Outer) Roadway\b", r"\1"), (r"\bInterchange\b", "Int"), (r"\bService Area\b", "SA"),
    (r"\bnorthbound\b", "NB"), (r"\bsouthbound\b", "SB"), (r"\beastbound\b", "EB"), (r"\bwestbound\b", "WB"),
    (r"\bboth directions\b", "both dirs"), (r"\bTownship\b", "Twp"), (r"\bBorough\b", "Boro"),
    (r"\bAvenue\b", "Av"), (r"\bStreet\b", "St"), (r"\bRoad\b", "Rd"), (r"\bBoulevard\b", "Blvd"),
    (r"\bNorth of\b", "N of"), (r"\bSouth of\b", "S of"), (r"\bEast of\b", "E of"), (r"\bWest of\b", "W of"),
    (r"\bapproaching\b", "nr"), (r"\bbetween\b", "btwn"),
    (r"\bUpper Level\b", "Upper"), (r"\bLower Level\b", "Lower"), (r"\bExpress Lanes\b", "Express"),
    (r"\bLocal Lanes\b", "Local"), (r"\bNew Jersey Side\b", "NJ side"), (r"\bNew York Side\b", "NY side"),
    (r"\b(\d{1,2}):00\s*([AP])\.?M\.?", r"\1\2M"), (r"\b0(\d):(\d\d)\s*([AP])\.?M\.?", r"\1:\2\3M"),
    (r"\b(\d{1,2}:\d\d)\s*([AP])\.?M\.?", r"\1\2M"),
    (r"\b0(\d)([AP]M)\b", r"\1\2"), (r"\b0(\d:\d\d[AP]M)\b", r"\1"),
    (r"\s+-\s+", " "),
]
TIGHT = [  # only when nothing else fits
    (r"\bConstruction\b", "Constr"), (r"\bimprovements\b", "work"), (r"\binvestigation\b", "invest"),
    (r"\bScheduled roadwork\b", "Roadwork"), (r"\bDisabled vehicle\b", "Disabled veh"), (r"\bboth dirs\b", "both"),
    (r"\s+\(([^()]*?)\s+(?:Twp|Boro)\)", r" (\1)"),
]
LANE_ABBREV = [  # only in the lane/closure part, so "Prudential Center" stays whole
    (r"\bclosed\b", "clsd"), (r"\blanes\b", "lns"), (r"\blane\b", "ln"), (r"\bleft\b", "L"), (r"\bright\b", "R"),
    (r"\bcenter\b", "C"), (r"\bshoulder\b", "shldr"), (r"\bnorthbound\b", "NB"), (r"\bsouthbound\b", "SB"),
    (r"\beastbound\b", "EB"), (r"\bwestbound\b", "WB"), (r"\bdetoured\b", "detour"), (r"\bAll lns open\b\s*", ""),
    (r"\bdue to volume\b", ""), (r"\b(\d+(?: 1/2)?) mile delay\b", r"\1 mi delay"),
    (r"\bThe travel time (?:to the New York side )?is (\d+) minutes\b.*", r"\1 min to cross"),
    (r"\bmoving operation\b", "moving work"), (r"\buntil further notice\b", ""),
    (r"\bAll lns clsd (\w+)\s*[;,]\s*All lns clsd (\w+)", r"All lns clsd \1/\2"),   # both directions closed
    (r"\bAll lns clsd (\w+),\s*(\w+)$", r"All lns clsd \1/\2"),
]
WEEKDAY_DATES = re.compile(r",?\s*(?:(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,?\s+)?(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
                           r"[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s*\d{4},?|\b\d{1,2}/\d{1,2}/\d{4}\b", re.I)
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
DATE_NAMED = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})\b", re.I)
DATE_NUMERIC = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


def schedule(text, today, lookahead_days=0):
    """'active', 'future', 'past' or 'unscheduled' from the dates written in an event's text.
    Text without dates (crashes, delays, open-ended work) counts as active."""
    if re.search(r"not currently scheduled", text, re.I):
        return "unscheduled"
    dates = []
    for m in DATE_NAMED.finditer(text):
        try:
            dates.append(datetime(int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2))).date())
        except ValueError:
            pass
    for m in DATE_NUMERIC.finditer(text):
        try:
            dates.append(datetime(int(m.group(3)), int(m.group(1)), int(m.group(2))).date())
        except ValueError:
            pass
    if not dates:
        return "active"
    end = max(dates)
    start = None if (len(dates) == 1 and re.search(r"\buntil\b", text, re.I)) else min(dates)
    if end < today:
        return "past"
    if start and start > today + timedelta(days=lookahead_days):
        return "future"
    return "active"


def clean_comment(text, limit=None):
    limit = COMMENT_LIMIT if limit is None else limit
    text = re.sub(r"[^\x20-\x7e]", "", text).replace("|", "/").replace("~", "-")
    return re.sub(r"\s+", " ", text).strip()[:limit]


def abbreviate(text):
    for pat, rep in ABBREV:
        text = re.sub(pat, rep, text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def shorten(text, n):
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0].rstrip(",;")
    return cut if len(cut) >= 8 else text[:n]


def strip_dates(text):
    text = WEEKDAY_DATES.sub("", text)                       # long dates: the object already carries a time
    text = re.sub(r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)(?:\s*(?:/|thru)\s*"
                  r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))*,?", "", text)
    text = re.sub(r"\bContinuous\b|\bon\s*(?=,|$)|\buntil\s+(?:\d{1,2}:\d{2}\s*[AP]M\s+on\s*)?(?=,|\s*$)", "", text, flags=re.I)
    text = re.sub(r"\s*\bthru\b\s*(?=,|$)", "", text)
    text = re.sub(r"(^|[()]|,)\s*thru\b\s*", r"\1 ", text)              # a "thru" left behind by a removed date
    text = re.sub(r"(?:\s*,){2,}", ",", text)
    return re.sub(r"\s+,", ",", text).strip(" ,")


def lane_part(text):
    for pat, rep in LANE_ABBREV:
        text = re.sub(pat, rep, text, flags=re.I)
    text = abbreviate(text)
    return re.sub(r"\s+", " ", text).strip(" ,;.")


def make_comment(ev, limit=None):
    """'Crash US 9 NB S of CR 604/Green St (Woodbridge Twp) 1 L ln of 2 lns clsd upd 18:44'.
    The text is tried at decreasing detail until it fits: full location, then road and town only."""
    limit = COMMENT_LIMIT if limit is None else limit
    text = strip_dates(ev.text.replace("&", "/"))
    cut = text.rfind(")")                                    # location ends with "(Town)"; lanes and notes follow
    head, tail = (text[:cut + 1], text[cut + 1:]) if cut > 0 else (text, "")
    head = re.sub(r"^.*?\bon\s+", "", head, count=1) if " on " in head[:60] else head   # "Crash on US 9" -> "US 9"
    kind = ev.etype[:1].upper() + ev.etype[1:] if ev.etype else ""
    full = abbreviate(f"{kind} {head}")
    towns = re.findall(r"\(([^()]+)\)", head)
    short = abbreviate(f"{kind} {ev.road}") + (f" ({towns[0]})" if towns else "")
    lanes = lane_part(tail)
    upd = "" if ev.kind in ("event", "roadwork") else ev.updated.astimezone(EASTERN).strftime("upd %H:%M")
    full = re.sub(r"\s+at\s+\(", " (", full)
    short = re.sub(r"\s+at\s+\(", " (", short)
    tight = short
    for pat, rep in TIGHT:
        tight = re.sub(pat, rep, tight, flags=re.I)
    # the lane status, then the same without its time window, then just its first clause
    no_times = re.sub(r"\d{1,2}(?::\d\d)?[AP]M\s+thru\s+\d{1,2}(?::\d\d)?[AP]M,?\s*", "", lanes).strip(" ,")
    no_times = re.sub(r"\s+and detour\b", " +detour", no_times)
    first = re.split(r"[,.;]\s", no_times)[0]
    m = re.search(r"All lns clsd|\d+ (?:[LRC] )?lns? of \d+ lns clsd|\d+ (?:[LRC] )?lns? clsd|[LRC] ln clsd|"
                  r"[LRC]? ?shldr(?: clsd)?|\d+(?: 1/2)? mi delay|\d+ min to cross", lanes)
    status = m.group(0) if m else ""
    variants = list(dict.fromkeys(v for v in (lanes, no_times, first, status) if v)) or [""]
    no_town = re.sub(r"\s*\([^()]*\)$", "", tight)

    def join(*parts):
        return " ".join(x for x in parts if x)
    # most important first: what and where (town) and the lane status; then the cross street, then the time
    cands = []
    for lane in variants:
        cands += [join(full, lane, upd), join(full, lane), join(short, lane, upd), join(short, lane), join(tight, lane)]
    cands += ([join(no_town, status)] if status else []) + [join(full, upd), join(short, upd), full, short, tight, no_town]
    for cand in cands:
        if len(cand) <= limit:
            return clean_comment(cand, limit)
    return clean_comment(shorten(no_town, limit), limit)


# --------------------------------------------------------------------------
# APRS formatting, state, APRS-IS
# --------------------------------------------------------------------------
def aprs_lat(lat):
    hemi = "N" if lat >= 0 else "S"
    lat = abs(lat)
    d, m = int(lat), round((lat - int(lat)) * 60, 2)
    if m >= 60:
        d, m = d + 1, 0.0
    return f"{d:02d}{m:05.2f}{hemi}"


def aprs_lon(lon):
    hemi = "E" if lon >= 0 else "W"
    lon = abs(lon)
    d, m = int(lon), round((lon - int(lon)) * 60, 2)
    if m >= 60:
        d, m = d + 1, 0.0
    return f"{d:03d}{m:05.2f}{hemi}"


def object_packet(name, lat, lon, when_utc, table, code, comment, live=True):
    ts = when_utc.strftime("%d%H%M") + "z"
    return (f";{name:<9.9}{'*' if live else '_'}{ts}"
            f"{aprs_lat(lat)}{table}{aprs_lon(lon)}{code}{clean_comment(comment) if live else ''}")


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


def aprs_passcode(call):
    call = call.split("-")[0].upper()
    h = 0x73E2
    for i in range(0, len(call), 2):
        h ^= ord(call[i]) << 8
        if i + 1 < len(call):
            h ^= ord(call[i + 1])
    return h & 0x7FFF


class AprsIS:
    def __init__(self, call, passcode):
        self.call, self.passcode, self.sock = call, passcode, None

    def connect(self):
        self.sock = socket.create_connection((APRS_IS_HOST, APRS_IS_PORT), timeout=20)
        self.sock.recv(512)
        self.sock.sendall(f"user {self.call} pass {self.passcode} vers nj511-aprs 1.0\r\n".encode())
        resp = self.sock.recv(512).decode(errors="replace")
        if "verified" not in resp.lower() or "unverified" in resp.lower():
            raise RuntimeError(f"APRS-IS login not verified: {resp.strip()}")
        self.sock.sendall(f"{self.call}>APRS,TCPIP*:>{STATUS_TEXT}\r\n".encode())

    def send(self, packet):
        if self.sock is None:
            self.connect()
        self.sock.sendall(f"{self.call}>APRS,TCPIP*:{packet}\r\n".encode())

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def run_cycle(args, live, aprs_is, counties, retired):
    items = parse_feed(http(args.url))           # raises on a bad feed -> nothing is killed
    if not counties.ready():
        try:
            counties.load()
        except (urllib.error.URLError, OSError, ValueError) as e:
            if args.county:
                raise ValueError(f"county outlines not available from api.weather.gov ({e})")   # nothing is killed
            print(f"# county outlines not available ({e}); using a rough New Jersey box this cycle", file=sys.stderr)
    now = datetime.now(timezone.utc)
    seen, keep = {}, []
    dropped = {"other types": 0, "other days": 0, "ended": 0, "not scheduled": 0, "other counties": 0,
               "outside NJ": 0, "no position": 0, "not updated recently": 0}
    today = now.astimezone(EASTERN).date()
    ahead = math.ceil(args.lookahead_hours / 24) if args.lookahead_hours > 0 else 0
    for item in items:
        ev = Event(item)
        seen[ev.kind] = seen.get(ev.kind, 0) + 1
        if ev.pos is None:
            dropped["no position"] += 1
            continue
        if ev.kind not in args.types:
            dropped["other types"] += 1
            continue
        if not args.include_scheduled:
            when = schedule(ev.text, today, ahead)
            if when != "active":
                dropped[{"future": "other days", "past": "ended", "unscheduled": "not scheduled"}[when]] += 1
                continue
        if args.max_age_hours > 0 and ev.updated < now - timedelta(hours=args.max_age_hours):
            dropped["not updated recently"] += 1
            continue
        if counties.ready():
            ev.county = counties.county_of(*ev.pos)
            if not ev.county:
                dropped["outside NJ"] += 1
                continue
        elif not (NJ_BOX[0] <= ev.pos[0] <= NJ_BOX[2] and NJ_BOX[1] <= ev.pos[1] <= NJ_BOX[3]):
            dropped["outside NJ"] += 1
            continue
        if args.county and ev.county not in args.county:
            dropped["other counties"] += 1
            continue
        keep.append(ev)
    kinds = ", ".join(f"{n} {k}" for k, n in sorted(seen.items(), key=lambda kv: -kv[1]))
    why = ", ".join(f"{n} {k}" for k, n in dropped.items() if n)
    print(f"# 511NJ returned {len(items)} ({kinds or 'none'}); keeping {len(keep)}" + (f" (left out: {why})" if why else ""),
          file=sys.stderr)

    assign_names(keep)
    placed, out = {}, []
    for ev in keep:
        table, code = SYMBOLS[ev.kind]
        comment = make_comment(ev)
        placed[ev.name] = (ev.pos[0], ev.pos[1], (table, code, comment))
        out.append(object_packet(ev.name, ev.pos[0], ev.pos[1], ev.updated, table, code, comment))
    if aprs_overrides:
        aprs_overrides.apply(placed, out)

    gone = [n for n in live if n not in placed]
    old = [n for n in retired if n not in placed]          # aprs_njta.py's objects: killed once
    for name in gone + old:
        lat, lon, (table, code, _c) = live.get(name) or retired[name]
        out.append(object_packet(name, lat, lon, now, table, code, "", live=False))

    for pkt in out:
        print(f"{args.callsign}>APRS,TCPIP*:{pkt}")
        if aprs_is:
            aprs_is.send(pkt)
    sys.stdout.flush()

    # bookkeeping only after everything was sent, so a failed send repeats instead of losing kills
    for name in gone:
        del live[name]
    live.update(placed)
    save_state(args.state_file, args.callsign, live)
    if retired:
        retired.clear()
        try:
            os.replace(OLD_NJTA_STATE, OLD_NJTA_STATE + ".retired")
            print(f"# aprs_njta.py's {len(old)} object(s) killed; {OLD_NJTA_STATE} renamed to .retired",
                  file=sys.stderr)
        except OSError:
            pass
    return len(placed), len(gone) + len(old)


def main():
    global COMMENT_LIMIT, USER_AGENT
    ap = argparse.ArgumentParser(description="511NJ active events -> APRS objects")
    ap.add_argument("--county", nargs="+", default=[],
                    help="only these New Jersey counties, comma separated, e.g. Camden,Burlington,Cape May "
                         "(no quotes needed; default: all)")
    ap.add_argument("--types", default=DEFAULT_TYPES,
                    help=f"which kinds of event to send, from {', '.join(TYPES)} (default {DEFAULT_TYPES})")
    ap.add_argument("--lookahead-hours", type=float, default=0,
                    help="also send events whose written dates start within this many hours (default 0 = today only)")
    ap.add_argument("--include-scheduled", action="store_true",
                    help="send events whatever dates they mention (future, past or unscheduled)")
    ap.add_argument("--max-age-hours", type=float, default=0,
                    help="leave out events not updated for this many hours (default 0 = keep all)")
    ap.add_argument("--url", default=FEED_URL)
    ap.add_argument("--state-file", default=STATE_FILE)
    ap.add_argument("--county-cache", default=COUNTY_CACHE)
    ap.add_argument("--comment-length", type=int, default=COMMENT_LIMIT,
                    help=f"maximum characters of object text (default {COMMENT_LIMIT}; APRS 1.0 specifies 43)")
    ap.add_argument("--callsign", default="N0CALL", help="source callsign-SSID for the packets")
    ap.add_argument("--passcode", type=int, help="APRS-IS passcode (default: computed from callsign)")
    ap.add_argument("--send", action="store_true", help="actually send to APRS-IS (requires a real callsign)")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--interval", type=int, default=120, help="seconds between polls (default 120, minimum 60)")
    args = ap.parse_args()

    if not 20 <= args.comment_length <= 200:
        ap.error("--comment-length must be between 20 and 200")
    COMMENT_LIMIT = args.comment_length
    try:
        args.county = county_names(" ".join(args.county))
    except ValueError as e:
        ap.error(str(e))
    args.types = {t.strip().lower() for t in args.types.split(",") if t.strip()}
    if not args.types or args.types - set(TYPES):
        ap.error(f"--types must be chosen from {', '.join(TYPES)}")
    args.interval = max(60, args.interval)
    args.callsign = args.callsign.upper()
    if args.send and args.callsign.startswith("N0CALL"):
        ap.error("--send requires your own callsign (--callsign)")
    contact = os.environ.get("NWS_CONTACT", "")
    if contact:
        USER_AGENT = f"nj511-aprs/1.0 ({contact})"
    print(f"# sending: {', '.join(sorted(args.types))}; counties: "
          f"{', '.join(sorted(args.county)) if args.county else 'all of New Jersey'}", file=sys.stderr, flush=True)
    print(f"# symbols in use: incident {''.join(SYM_CRASH)}  closure {''.join(SYM_MAJOR)}  delay {''.join(SYM_DELAY)}  "
          f"roadwork {''.join(SYM_WORKS)}  event {''.join(SYM_EVENT)}  other {''.join(SYM_OTHER)}", file=sys.stderr, flush=True)

    live = load_state(args.state_file, args.callsign)
    if live:
        print(f"# restored {len(live)} live object(s) from {args.state_file}", file=sys.stderr)
    retired = load_state(OLD_NJTA_STATE, args.callsign) if os.path.exists(OLD_NJTA_STATE) else {}
    if retired:
        print(f"# found {len(retired)} object(s) left by aprs_njta.py; they will be killed", file=sys.stderr)
    counties = Counties(args.county_cache, contact)
    aprs_is = AprsIS(args.callsign, args.passcode or aprs_passcode(args.callsign)) if args.send else None

    try:
        while True:
            try:
                n, killed = run_cycle(args, live, aprs_is, counties, retired)
                print(f"# {n} event(s) placed, {killed} killed", file=sys.stderr)
            except (urllib.error.URLError, OSError, ValueError, RuntimeError) as e:
                print(f"# cycle failed: {e}", file=sys.stderr)
                if aprs_is:
                    aprs_is.close()
            sys.stderr.flush()
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
