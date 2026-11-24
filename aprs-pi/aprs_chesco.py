#!/usr/bin/env python3
"""
Chester County (PA) DES Live CAD web page  ->  APRS objects   (Google Geocoding API)

Source: https://webcad.chesco.org/WebCad/webcad.asp   (an HTML page, not a feed)

The page has three tables under bold headings "Fire Incidents", "EMS Incidents" and
"Traffic Incidents".  Columns:
  Incident No. | Incident Type | Incident Location | Municipality | Dispatch Time | Station (Fire/EMS) or Agency (Traffic)
e.g.  F26061774 | ACCIDENT - BLS | W STATE ST  & MEREDITH ST | Kennett Square Borough | 11-09-2026 15:17:08 | 25

There are no coordinates, so the "STREET & STREET" + municipality text is geocoded
with the Google Geocoding API (same approach as the Montgomery County script):
  1. "STREET & STREET, MUNICIPALITY, PA"   (Google resolves intersections directly)
  2. each street on its own                (marked approximate: comment ends in "?")
  3. the municipality itself               (marked approximate: comment ends in "?")
Results are cached in geocache_chesco.json for 30 days (Google's terms limit how long
lat/lng results may be stored).

API key:  set GOOGLE_MAPS_API_KEY in the environment (or pass --api-key).

Object names and text
  Names are 9 characters, the APRS maximum: a descriptive tag plus the tail of the CAD
  incident number, stable for the life of the incident - FIRE61774 (fire F26061774),
  EMS061789 (EMS), TRAF24346 (traffic P26424346).  The text gives call type, location,
  municipality, station/agency, dispatch time (local) and the full incident number,
  dropping the least important items first when space runs out.  It may be up to
  --comment-length characters (default 67).  APRS 1.0 specifies 43 for object comments;
  most modern clients show more, but if yours truncates the text or drops the objects,
  use --comment-length 43.
  Upgrading from an older version: objects sent under the old names are killed on the
  first run that uses the same state file, and re-created under the new names.

Being a good citizen of a web page (not an API)
  * At startup the script reads the site's robots.txt and REFUSES TO RUN if it
    disallows this script's user agent ("chesco-cad-aprs").  --ignore-robots overrides
    that; if you use it, you are deciding to do so.  Consider asking Chester County
    DES for permission or an official feed first.
  * The page itself refreshes every 60 s, so the poll interval defaults to 120 s and
    cannot go below 60 s.  robots.txt is re-checked every 6 hours.
  * The script identifies itself honestly ("chesco-cad-aprs/1.0").
  * If the page layout changes (no recognisable incident tables), the cycle is
    abandoned and NOTHING is killed.

Default behaviour is PRINT ONLY.  With  --send --callsign YOURCALL  (a licensed amateur
callsign) each poll's packets are handed to aprs_send.py, the one program that logs in to
APRS-IS; it sends what is new, changed or killed, and re-sends the rest every 26 minutes.

Object lifetime (APRS objects have no built-in time-to-live)
  * Every object sent is remembered in chesco_live_state.json.  On the next poll - or
    the next run, after a restart or a --once - anything no longer on the page gets
    a kill packet.
  * --max-age-hours (default 6) kills and ignores incidents whose dispatch time is
    older than that, even if the page still lists them.  0 disables.
  * State is tied to --callsign; a state file written by another callsign is ignored.

Only Fire and Traffic incidents are processed by default (see --categories).

Standard library only (Python 3.9+).

Examples
  export GOOGLE_MAPS_API_KEY=AIza...
  python3 aprs_chesco.py --once
  python3 aprs_chesco.py --callsign N0CALL-13 --send --categories Fire,Traffic,EMS
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import aprs_send                   # hands packets to the one program that sends to APRS-IS

try:
    import aprs_overrides            # edits/deletes made in the web UI (optional)
except ImportError:
    aprs_overrides = None

PAGE_URL = "https://webcad.chesco.org/WebCad/webcad.asp"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
API_KEY_ENV = "GOOGLE_MAPS_API_KEY"
USER_AGENT = "chesco-cad-aprs/1.0"
UA_TOKEN = "chesco-cad-aprs"            # product token matched against robots.txt
ROBOTS_RECHECK_SECONDS = 6 * 3600
EASTERN = ZoneInfo("America/New_York")
CACHE_FILE = "geocache_chesco.json"
CACHE_TTL = 30 * 24 * 3600  # seconds
STATE_FILE = "chesco_live_state.json"
MAX_AGE_HOURS = 6.0

# (south, west, north, east) - rough Chester County, PA bounds.
# Used both to bias Google and to reject results that land outside the county.
COUNTY_BOX = (39.70, -76.05, 40.33, -75.38)

API_KEY = ""  # set in main() from --api-key or $GOOGLE_MAPS_API_KEY

def env_symbol(key, default):
    """APRS symbol (table, code) from $SYM_<key> (two characters, e.g. /d), else the built-in default."""
    v = os.environ.get("SYM_" + key, "")
    if len(v) == 2 and (v[0] in "/\\" or v[0].isalnum()) and "!" <= v[1] <= "~":
        return v[0], v[1]
    if v:
        print(f"# SYM_{key}={v!r} is not a valid APRS symbol (table + code); using the default", file=sys.stderr)
    return default


# APRS symbols: (table, code).  Change them in aprs.env (SYM_FIRE, SYM_EMS, SYM_TRAFFIC, SYM_OTHER).
SYMBOLS = {
    "Fire": env_symbol("FIRE", ("/", "d")),          # fire department
    "EMS": env_symbol("EMS", ("/", "a")),            # ambulance
    "Traffic": env_symbol("TRAFFIC", ("\\", "'")),   # crash site
}
DEFAULT_SYMBOL = env_symbol("OTHER", ("\\", "!"))    # emergency

# Quality levels that are NOT flagged as approximate in the APRS comment.  Approximate
# positions get a trailing "?" ("~" and "|" are reserved in APRS comment text).
PRECISE = ("intersection", "address")
APPROX_MARK = "?"

_last_call = {}
RETRY_CODES = {429, 500, 502, 503, 504}
DOWN = {}  # services that failed (after retries) this cycle -> reason


class ServiceDown(Exception):
    pass


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------
def http_get(url, timeout=30, tries=3):
    """GET with retry + exponential backoff on 429/5xx and network errors.
    Returns (body_bytes, charset_or_None)."""
    delay, last = 2, None
    for attempt in range(tries):
        wait = delay
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.headers.get_content_charset()
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


def throttle(host, min_gap=0.05):
    wait = min_gap - (time.time() - _last_call.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.time()


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------
def robots_allowed(url):
    """True = allowed, False = disallowed, None = robots.txt could not be read."""
    p = urllib.parse.urlparse(url)
    robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
    try:
        raw, cs = http_get(robots_url, tries=2)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return True           # no robots.txt: nothing is disallowed
        if e.code in (401, 403):
            return False
        return None
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(raw.decode(cs or "utf-8", "replace").splitlines())
    return rp.can_fetch(UA_TOKEN, url)


def enforce_robots(args, first):
    ok = robots_allowed(args.url)
    if ok is True:
        return
    if args.ignore_robots:
        if first:
            print(f"# WARNING: robots.txt {'disallows' if ok is False else 'could not be read for'} "
                  f"this script; continuing only because --ignore-robots was given", file=sys.stderr)
        return
    if ok is False:
        sys.exit(f"# robots.txt for {urllib.parse.urlparse(args.url).netloc} disallows automated access "
                 f"by '{UA_TOKEN}'. Not running.\n"
                 f"# Ask Chester County DES for permission or an official feed, or pass --ignore-robots "
                 f"if you decide to proceed anyway.")
    if first:
        sys.exit("# Could not read robots.txt, so I can't tell whether automated access is allowed. "
                 "Try again, or pass --ignore-robots.")
    print("# robots.txt could not be re-checked; keeping the previous decision", file=sys.stderr)


# --------------------------------------------------------------------------
# Page parsing
# --------------------------------------------------------------------------
HEADINGS = {"fire": "Fire", "ems": "EMS", "traffic": "Traffic"}


class CadParser(HTMLParser):
    """Collects (category, [6 cell texts]) for every incident row."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.category = None
        self.in_row = False
        self.cells, self.cell = [], None
        self.bold, self.bold_text = False, ""
        self.rows, self.skipped, self.headings = [], 0, 0

    def handle_starttag(self, tag, attrs):
        if tag == "b":
            self.bold, self.bold_text = True, ""
        elif tag == "tr":
            self.in_row, self.cells = True, []
        elif tag in ("td", "th") and self.in_row:
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_endtag(self, tag):
        if tag == "b":
            self.bold = False
            m = re.fullmatch(r"(Fire|EMS|Traffic)\s+Incidents", self.bold_text.strip(), re.I)
            if m and not self.in_row:
                self.category = HEADINGS[m.group(1).lower()]
                self.headings += 1
        elif tag in ("td", "th") and self.cell is not None:
            self.cells.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
            self.cell = None
        elif tag == "tr" and self.in_row:
            self.in_row = False
            cells, self.cells = self.cells, []
            if not cells or cells[0].lower().startswith("incident no"):
                return  # header row
            if self.category and len(cells) >= 6 and cells[0]:
                self.rows.append((self.category, cells[:6]))
            else:
                self.skipped += 1

    def handle_data(self, data):
        if self.bold:
            self.bold_text += data
        if self.cell is not None:
            self.cell.append(data)


class Incident:
    def __init__(self, category, cells):
        self.category = category
        self.number, self.call_type, self.location, self.municipality, when, self.unit = cells
        self.location = re.sub(r"\s+", " ", self.location).strip()
        try:  # 'MM-DD-YYYY HH:MM:SS', local Eastern
            self.when_utc = (datetime.strptime(when, "%m-%d-%Y %H:%M:%S")
                             .replace(tzinfo=EASTERN).astimezone(timezone.utc))
        except ValueError:
            self.when_utc = datetime.now(timezone.utc)
        self.tag = TAGS.get(category) or re.sub(r"[^A-Z0-9]", "", category.upper())[:4] or "INC"
        self.name = None  # assigned by assign_names() once the set of wanted incidents is known

    @property
    def key(self):
        """Identity of the incident, stable between polls."""
        return f"{self.category}|{self.number}"

    def name_for(self, salt=0):
        """Candidate 9-char object name: descriptive tag + the tail of the CAD incident number
        (F26061774 -> FIRE61774), so it can be matched against the CAD page.  If that clashes,
        or the number has no digits, a salted hash is used instead."""
        room = 9 - len(self.tag)
        digits = re.sub(r"\D", "", self.number)
        if salt == 0 and digits:
            return self.tag + digits[-room:].rjust(room, "0")
        return self.tag + b36_hash(self.key + (f"#{salt}" if salt else ""), room)


def decode_page(raw, charset):
    return raw.decode(charset or "iso-8859-1", "replace")  # the page declares iso-8859-1


def parse_page(html_text):
    p = CadParser()
    p.feed(html_text)
    p.close()
    if p.headings == 0:
        raise ValueError("page layout not recognised: no Fire/EMS/Traffic Incidents headings found")
    if p.skipped and not p.rows:
        raise ValueError(f"page layout not recognised: {p.skipped} rows did not match the expected columns")
    if p.skipped:
        print(f"# {p.skipped} table row(s) did not match the expected columns and were skipped",
              file=sys.stderr)
    by_number = {}
    for cat, cells in p.rows:
        inc = Incident(cat, cells)
        by_number[inc.number] = inc
    return list(by_number.values())


# --------------------------------------------------------------------------
# Geocoding (Google Geocoding API)
# --------------------------------------------------------------------------
def clean_street(s):
    """Normalise CAD street text; None for placeholders like 'DEAD END'."""
    s = re.sub(r"\s+", " ", s).strip().upper()
    if not s or s.startswith("DEAD END") or s in ("END", "CUL DE SAC"):
        return None
    if "RAMP" in s:  # 'RAMP RT30 EB TO CALN RD' -> 'CALN RD'
        s = s.split(" TO ")[-1] if " TO " in s else re.sub(r"^RAMP\s+", "", s)
    toks = [t for t in s.split() if t not in ("NB", "SB", "EB", "WB")]
    s = " ".join(toks)
    s = re.sub(r"\bRT\s*-?(\d+)\b", r"Route \1", s)  # RT30 -> Route 30
    return s or None


def in_county(lat, lon):
    s, w, n, e = COUNTY_BOX
    return s <= lat <= n and w <= lon <= e


def quality_of(result):
    types = result.get("types", [])
    loc_type = result.get("geometry", {}).get("location_type", "")
    if "intersection" in types:
        return "intersection"
    if loc_type in ("ROOFTOP", "RANGE_INTERPOLATED") or "street_address" in types or "premise" in types:
        return "address"
    if "route" in types:
        return "street"
    return "approx"


def google(address):
    """One Geocoding API call -> (lat, lon, quality) | None (no usable result).
    Raises ServiceDown on transient failure; exits on a bad/denied API key."""
    if "google" in DOWN:
        raise ServiceDown("google")
    s, w, n, e = COUNTY_BOX
    params = {
        "address": address,
        "key": API_KEY,
        "region": "us",
        "components": "administrative_area:PA|country:US",
        "bounds": f"{s},{w}|{n},{e}",
    }
    url = GEOCODE_URL + "?" + urllib.parse.urlencode(params)

    for attempt in range(2):
        throttle("google")
        try:
            data = json.loads(http_get(url)[0])
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as ex:
            DOWN["google"] = str(ex)
            print(f"# google geocoding unavailable ({ex}); skipping until next cycle", file=sys.stderr)
            raise ServiceDown("google")

        status = data.get("status")
        if status == "OK":
            break
        if status == "ZERO_RESULTS":
            return None
        if status == "REQUEST_DENIED":
            msg = data.get("error_message", "request denied")
            sys.exit(f"Google Geocoding API: REQUEST_DENIED - {msg}\n"
                     f"Check ${API_KEY_ENV}, that the Geocoding API is enabled, and key restrictions/billing.")
        if status == "INVALID_REQUEST":
            print(f"# google: INVALID_REQUEST for '{address}'", file=sys.stderr)
            return None
        # OVER_QUERY_LIMIT / UNKNOWN_ERROR: one short retry, then give up for this cycle
        if attempt == 0:
            time.sleep(2)
            continue
        DOWN["google"] = status
        print(f"# google geocoding returned {status}; skipping until next cycle", file=sys.stderr)
        raise ServiceDown("google")

    for r in data.get("results", []):
        loc = r.get("geometry", {}).get("location", {})
        lat, lon = loc.get("lat"), loc.get("lng")
        if lat is not None and lon is not None and in_county(lat, lon):
            return lat, lon, quality_of(r)
    return None


def geocode_uncached(location, municipality, cache):
    """Returns (result_or_None, complete).  complete=False means the service failed
    part-way, so a miss / degraded result shouldn't be remembered."""
    complete = True
    m = municipality.strip()
    named = [s for s in (clean_street(p) for p in location.split("&")) if s]

    # 1. the intersection (or, with one street / an address, that alone) as a whole
    q = f"{named[0]} & {named[1]}, {m}, PA" if len(named) >= 2 else f"{' '.join(named) or location}, {m}, PA"
    best = None
    try:
        best = google(q)
        if best and best[2] in PRECISE:
            return best, True
    except ServiceDown:
        complete = False

    # 2. Google matched only one street, or nothing: try each street on its own
    if best is None and complete:
        for s in reversed(named):
            try:
                pt = google(f"{s}, {m}, PA")
            except ServiceDown:
                complete = False
                break
            if pt:
                best = (pt[0], pt[1], "street")
                break
    if best:
        return best, complete

    # 3. municipality centroid (cached on its own, shared by every incident there)
    mkey = f"M|{m}".upper()
    hit = cache_get(cache, mkey)
    if hit:
        return (hit[0], hit[1], "municipality"), complete
    try:
        pt = google(f"{m}, Chester County, PA")
    except ServiceDown:
        return None, False
    if pt:
        cache_put(cache, mkey, [pt[0], pt[1]])
        return (pt[0], pt[1], "municipality"), complete
    return None, complete


# ---- geocode cache (30-day TTL) -----------------------------------------
def load_cache():
    try:
        with open(CACHE_FILE) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    cutoff = time.time() - CACHE_TTL
    return {k: v for k, v in raw.items() if isinstance(v, dict) and v.get("ts", 0) >= cutoff}


def save_cache(cache):
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=1)
    os.replace(tmp, CACHE_FILE)


def cache_get(cache, key):
    e = cache.get(key)
    if e and e.get("ts", 0) >= time.time() - CACHE_TTL:
        return e.get("v")
    return None


def cache_put(cache, key, value):
    cache[key] = {"ts": time.time(), "v": value}
    save_cache(cache)


def geocode(inc, cache):
    key = f"{inc.location}|{inc.municipality}".upper()
    e = cache.get(key)
    if e and e.get("ts", 0) >= time.time() - CACHE_TTL:
        return tuple(e["v"]) if e["v"] else None

    res, complete = geocode_uncached(inc.location, inc.municipality, cache)
    # Remember good results, and misses only if the service answered.  A degraded
    # result (street/municipality guess made while the service was down) is used now
    # but retried next cycle.
    if res and (complete or res[2] in PRECISE):
        cache_put(cache, key, list(res))
    elif not res and complete:
        cache_put(cache, key, None)
    return res


# --------------------------------------------------------------------------
# Live-object state (so kills survive restarts)
# --------------------------------------------------------------------------
def load_state(path, callsign):
    """name -> (lat, lon, (table, code, comment)) for objects we transmitted earlier."""
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


# --------------------------------------------------------------------------
# APRS encoding
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


# ---- object names -------------------------------------------------------
# Descriptive prefix for each object name.  A name is 9 characters (the APRS maximum):
# the tag plus the tail of the CAD incident number, e.g. FIRE61774, EMS061789, TRAF24346.
TAGS = {"Fire": "FIRE", "EMS": "EMS", "Traffic": "TRAF"}
B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def b36_hash(key, n):
    """n uppercase base-36 characters derived from SHA-1 of key."""
    h = int(hashlib.sha1(key.encode()).hexdigest(), 16)
    out = []
    for _ in range(n):
        h, r = divmod(h, 36)
        out.append(B36[r])
    return "".join(out)


def assign_names(items):
    """Give every item a unique .name.  Items provide .key and .name_for(salt).  Two
    incidents that happen to clash are told apart deterministically (sorted by key)."""
    used = set()
    for it in sorted(items, key=lambda i: i.key):
        salt = 0
        while it.name_for(salt) in used:
            salt += 1
        it.name = it.name_for(salt)
        used.add(it.name)


# ---- object text --------------------------------------------------------
COMMENT_LIMIT = 67  # set from --comment-length.  APRS 1.0 specifies 43 characters for an
                    # object's comment; most modern clients show more, but if yours truncates
                    # the text or drops the objects, run with --comment-length 43.

PLACE_ABBREV = [
    (r"\s+(?:TOWNSHIP|TWP|BOROUGH|BORO|CITY)$", ""),
    (r"^EAST\b", "E"), (r"^WEST\b", "W"), (r"^NORTH\b", "N"), (r"^SOUTH\b", "S"),
    (r"^UPPER\b", "UP"), (r"^LOWER\b", "LWR"),
]


def short_place(name):
    """'West Caln Township' -> 'W CALN', 'Kennett Square Borough' -> 'KENNETT SQUARE'."""
    name = re.sub(r"\s+", " ", name).strip().upper()
    for pat, rep in PLACE_ABBREV:
        name = re.sub(pat, rep, name)
    return name


def pack(core, extras, limit, tail=""):
    """core text plus as many whole extras as fit (in priority order), then tail."""
    limit -= len(tail)
    text = core[:limit].rstrip()
    for e in extras:
        if e and len(text) + 1 + len(e) <= limit:
            text += " " + e
    return text + tail


def clean_comment(text, limit=None):
    limit = COMMENT_LIMIT if limit is None else limit
    text = re.sub(r"[^\x20-\x7e]", "", text).replace("|", "/").replace("~", "-")
    return text[:limit]


def object_packet(name, lat, lon, when_utc, table, code, comment, live=True):
    ts = when_utc.strftime("%d%H%M") + "z"
    return (f";{name:<9.9}{'*' if live else '_'}{ts}"
            f"{aprs_lat(lat)}{table}{aprs_lon(lon)}{code}{clean_comment(comment)}")


TYPE_ABBREV = [  # always applied, in order, case-insensitive
    (r"\s*\((?:PD|FD|EMS)\)", ""),            # "(PD)" agency tags (the unit is shown separately)
    (r"\s+(?:POLICE|FIRE|EMS)$", ""),         # trailing agency word on traffic calls
    (r"^TRAFFIC-GENERAL/HAZARDS?$", "TRAFFIC HAZARD"),
]
TYPE_TIGHT = [  # used only when the full call type would not fit
    (r"\bVEHICLE\b", "VEH"), (r"\bINVESTIGATION\b", "INVEST"), (r"\bRESPIRATORY\b", "RESP"),
    (r"\bDIFFICULTY\b", "DIFF"), (r"\bEMERGENCY\b", "EMERG"), (r"\bUNCONSCIOUS\b", "UNCONS"),
]


def shorten(text, n):
    """Cut to n chars at a word boundary when that keeps a reasonable amount of text."""
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0]
    return cut if len(cut) >= 8 else text[:n]


def incident_packet(inc, lat, lon, quality):
    table, code = SYMBOLS.get(inc.category, DEFAULT_SYMBOL)
    loc = re.sub(r"\s*&\s*", "/", inc.location).strip("/ ")
    ctype = re.sub(r"\s+", " ", inc.call_type).strip()
    for pat, rep in TYPE_ABBREV:
        ctype = re.sub(pat, rep, ctype, flags=re.I).strip()
    tail = "" if quality in PRECISE else APPROX_MARK     # trailing "?" = approximate position
    # Core: call type + location (the type is shortened first if they don't fit together).
    room = COMMENT_LIMIT - len(tail) - len(loc) - 1
    if len(ctype) > room:  # abbreviate further only when space is tight
        for pat, rep in TYPE_TIGHT:
            ctype = re.sub(pat, rep, ctype, flags=re.I)
    core = f"{shorten(ctype, max(room, 12))} {loc}"
    # Extras, most important first; whatever doesn't fit is dropped whole.
    extras = []
    muni = short_place(inc.municipality)
    if muni:
        extras.append(f"({muni})")
    unit = inc.unit.strip()
    if unit:   # Fire/EMS pages give a station number; Traffic gives an agency such as 41PD
        extras.append(f"STA{unit}" if inc.category in ("Fire", "EMS") else unit)
    extras.append(inc.when_utc.astimezone(EASTERN).strftime("@%H:%M"))  # dispatch time, local
    extras.append(f"#{inc.number}")                                      # full CAD incident number
    comment = pack(core, extras, COMMENT_LIMIT, tail)
    return object_packet(inc.name, lat, lon, inc.when_utc, table, code, comment), (table, code, comment)


# --------------------------------------------------------------------------
# APRS-IS
# --------------------------------------------------------------------------
# Sending is done by aprs_send.py (one APRS-IS connection for every script): this script
# hands over each poll's packets with aprs_send.Outbox and never logs in itself.


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def run_cycle(args, cache, live, aprs_is):
    DOWN.clear()  # give the geocoder a fresh chance each cycle
    raw, charset = http_get(args.url)
    incidents = parse_page(decode_page(raw, charset))   # raises on an unrecognised page -> nothing is killed

    # Filter BEFORE geocoding so excluded categories cost no API calls
    wanted = {c.strip().lower() for c in args.categories.split(",") if c.strip()}
    if wanted:
        incidents = [i for i in incidents if i.category.lower() in wanted]

    # Safety net: ignore (and so kill) anything older than --max-age-hours, judged by
    # the incident's own dispatch time, even if the page keeps listing it.
    now = datetime.now(timezone.utc)
    if args.max_age_hours > 0:
        cutoff = now - timedelta(hours=args.max_age_hours)
        fresh = [i for i in incidents if i.when_utc >= cutoff]
        if len(fresh) < len(incidents):
            print(f"# {len(incidents) - len(fresh)} incident(s) older than "
                  f"{args.max_age_hours:g}h ignored", file=sys.stderr)
        incidents = fresh
    assign_names(incidents)
    current = {i.name for i in incidents}  # everything still wanted this cycle

    placed, out = {}, []
    for inc in incidents:
        res = geocode(inc, cache)
        if not res:
            print(f"# no coordinates for: {inc.call_type} @ {inc.location}; {inc.municipality}", file=sys.stderr)
            continue
        lat, lon, quality = res
        pkt, sym = incident_packet(inc, lat, lon, quality)
        placed[inc.name] = (lat, lon, sym)
        out.append(pkt)

    if aprs_overrides:
        current -= aprs_overrides.apply(placed, out)

    # Kill objects we sent earlier (this run or a previous one) that are no longer
    # wanted: gone from the page, filtered out, or too old.  An incident that is still
    # wanted but failed to geocode is left alone rather than killed.
    gone = [n for n in live if n not in current]
    for name in gone:
        lat, lon, (table, code, comment) = live[name]
        out.append(object_packet(name, lat, lon, now, table, code, comment, live=False))

    for pkt in out:
        print(f"{args.callsign}>APRS,TCPIP*:{pkt}")
        if aprs_is:
            aprs_is.send(pkt)
    if aprs_is:
        aprs_is.flush()                # hand this poll's packets to aprs_send.py in one go

    # Bookkeeping only after everything was sent: if a send raised, the next cycle
    # repeats the same packets (harmless) instead of losing the kills.
    for name in gone:
        del live[name]
    live.update(placed)
    save_state(args.state_file, args.callsign, live)
    return len(incidents), len(placed)


def main():
    global API_KEY, COMMENT_LIMIT
    ap = argparse.ArgumentParser(description="Chester County PA live CAD web page -> APRS objects (Google geocoding)")
    ap.add_argument("--url", default=PAGE_URL)
    ap.add_argument("--api-key", help=f"Google Geocoding API key (default: ${API_KEY_ENV})")
    ap.add_argument("--categories", default="Fire,Traffic",
                    help="comma-separated page sections to keep: Fire, Traffic, EMS "
                         "(default: Fire,Traffic; pass '' for all)")
    ap.add_argument("--max-age-hours", type=float, default=MAX_AGE_HOURS,
                    help=f"kill and ignore incidents older than this many hours, by dispatch time "
                         f"(default {MAX_AGE_HOURS:g}; 0 disables)")
    ap.add_argument("--state-file", default=STATE_FILE,
                    help=f"where live objects are remembered between runs (default {STATE_FILE})")
    ap.add_argument("--comment-length", type=int, default=COMMENT_LIMIT,
                    help=f"maximum characters of object text (default {COMMENT_LIMIT}; APRS 1.0 specifies 43 - "
                         f"use 43 if your clients truncate the text or drop the objects)")
    ap.add_argument("--ignore-robots", action="store_true",
                    help="run even if the site's robots.txt disallows this script (your decision)")
    ap.add_argument("--callsign", default="N0CALL", help="source callsign-SSID for the packets")
    ap.add_argument("--passcode", type=int, help="ignored: aprs_send.py logs in to APRS-IS, not this script")
    ap.add_argument("--send", action="store_true", help="hand the packets to aprs_send.py for sending (requires a real callsign)")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--interval", type=int, default=120,
                    help="seconds between polls (default 120, minimum 60 - the page itself refreshes every 60 s)")
    args = ap.parse_args()

    if not 20 <= args.comment_length <= 200:
        ap.error("--comment-length must be between 20 and 200")
    COMMENT_LIMIT = args.comment_length
    API_KEY = args.api_key or os.environ.get(API_KEY_ENV, "")
    if not API_KEY:
        ap.error(f"no Google API key: set ${API_KEY_ENV} or pass --api-key")
    args.callsign = args.callsign.upper()
    args.interval = max(args.interval, 60)
    if args.send and args.callsign.startswith("N0CALL"):
        ap.error("--send requires your own callsign (--callsign)")

    enforce_robots(args, first=True)
    last_robots = time.time()

    cache = load_cache()
    live = load_state(args.state_file, args.callsign)
    if live:
        print(f"# restored {len(live)} live object(s) from {args.state_file}", file=sys.stderr)
    aprs_is = aprs_send.Outbox("chesco", args.callsign) if args.send else None

    try:
        while True:
            try:
                total, placed = run_cycle(args, cache, live, aprs_is)
                print(f"# {placed}/{total} incidents placed", file=sys.stderr)
            except (urllib.error.URLError, OSError, ValueError, RuntimeError) as e:
                print(f"# cycle failed: {e}", file=sys.stderr)
                if aprs_is:
                    aprs_is.close()
            if args.once:
                break
            time.sleep(args.interval)
            if time.time() - last_robots > ROBOTS_RECHECK_SECONDS:
                enforce_robots(args, first=False)
                last_robots = time.time()
    except KeyboardInterrupt:
        pass
    finally:
        if aprs_is:
            aprs_is.close()


if __name__ == "__main__":
    main()
