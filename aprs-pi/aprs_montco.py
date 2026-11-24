#!/usr/bin/env python3
"""
Montgomery County (PA) live CAD RSS  ->  APRS objects   (Google Geocoding API)

The feed (https://webapp07.montcopa.org/eoc/cadinfo/livecadrss.asp) has NO
coordinates.  Each <item> looks like:

  <title>EMS: RESPIRATORY EMERGENCY</title>
  <description>DOCK DR & DETWILER RD; TOWAMENCIN; Station 345C; 2026-11-09 @ 11:01:33;</description>

so the "STREET & STREET; TOWNSHIP" part is geocoded with the Google Geocoding API
(https://maps.googleapis.com/maps/api/geocode/json):
  1. "STREET & STREET, TOWNSHIP, PA"   (Google resolves intersections directly)
  2. each street on its own            (marked approximate)
  3. the township itself               (marked approximate)

API key:  set the GOOGLE_MAPS_API_KEY environment variable (or pass --api-key).
          The key is never written to disk or printed.

Results are cached in geocache_google.json for 30 days (Google's terms limit how
long lat/lng results may be stored), so each location is only looked up once.

Default behaviour is PRINT ONLY.  To inject into APRS-IS you need a licensed
amateur callsign:  --send --callsign YOURCALL   (passcode is computed for you).

Standard library only (Python 3.9+).

Examples
  export GOOGLE_MAPS_API_KEY=AIza...
  python3 aprs_montco.py --once
  python3 aprs_montco.py --interval 120 --callsign N0CALL-13 --send
  python3 aprs_montco.py --once --categories Fire,Traffic,EMS   # include EMS too

Only Fire and Traffic incidents are processed by default (see --categories).

Object names and text
  Names are 9 characters, the APRS maximum: a descriptive tag plus a short code that is
  stable for the life of the incident - FIRE7K2QX (fire), TRAF0M1ZB (traffic), EMS4H9ABC
  (EMS).  The text gives call type, location, township, station and dispatch time
  (local), dropping the least important items first when space runs out.  It may be up
  to --comment-length characters (default 67).  APRS 1.0 specifies 43 for object
  comments; most modern clients show more, but if yours truncates the text or drops the
  objects, use --comment-length 43.
  Upgrading from an older version: objects sent under the old names are killed on the
  first run that uses the same state file, and re-created under the new names.

Object lifetime
  APRS objects have no built-in time-to-live, so this script retires them itself:
  * Every object sent is remembered in aprs_live_state.json.  On the next poll - or
    the next run, after a restart or a --once - anything no longer wanted gets a
    kill packet (';NAME_...').  Delete the state file only if you want to forget
    objects without killing them.
  * --max-age-hours (default 6) kills and ignores any incident whose own timestamp
    is older than that, even if the feed still lists it.  0 disables.
  * State is tied to --callsign; a state file written by another callsign is ignored.
"""
import argparse
import hashlib
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    import aprs_overrides            # edits/deletes made in the web UI (optional)
except ImportError:
    aprs_overrides = None

FEED_URL = "https://webapp07.montcopa.org/eoc/cadinfo/livecadrss.asp"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
API_KEY_ENV = "GOOGLE_MAPS_API_KEY"
APRS_IS_HOST, APRS_IS_PORT = "rotate.aprs2.net", 14580
EASTERN = ZoneInfo("America/New_York")
CACHE_FILE = "geocache_google.json"
CACHE_TTL = 30 * 24 * 3600  # seconds
STATE_FILE = "aprs_live_state.json"  # objects currently "live" on APRS; survives restarts
MAX_AGE_HOURS = 6.0                  # kill/ignore incidents older than this (0 = off)

# (south, west, north, east) - rough Montgomery County, PA bounds.
# Used both to bias Google and to reject results that land outside the county.
COUNTY_BOX = (39.97, -75.72, 40.45, -75.08)

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

# Services that failed (after retries) this cycle -> reason; skipped until next cycle.
DOWN = {}


class ServiceDown(Exception):
    pass


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------
def http(url, data=None, ua="montco-cad-aprs/1.0", timeout=30, tries=3):
    """GET/POST with retry + exponential backoff on 429/5xx and network errors."""
    delay, last = 2, None
    for attempt in range(tries):
        wait = delay
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": ua})
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


def throttle(host, min_gap=0.05):
    wait = min_gap - (time.time() - _last_call.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.time()


# --------------------------------------------------------------------------
# Feed parsing
# --------------------------------------------------------------------------
class Incident:
    def __init__(self, category, call_type, location, township, station, when_utc, raw):
        self.category = category
        self.call_type = call_type
        self.location = location
        self.township = township
        self.station = station
        self.when_utc = when_utc
        self.raw = raw
        self.tag = TAGS.get(category) or re.sub(r"[^A-Z0-9]", "", category.upper())[:4] or "INC"
        self.name = None  # assigned by assign_names() once the set of wanted incidents is known

    @property
    def key(self):
        """Identity of the incident, stable between polls."""
        return f"{self.category}|{self.call_type}|{self.location}|{self.township}|{self.when_utc:%Y%m%d%H%M%S}"

    def name_for(self, salt=0):
        """Candidate 9-char object name: descriptive tag + base-36 hash (salted on a collision)."""
        return self.tag + b36_hash(self.key + (f"#{salt}" if salt else ""), 9 - len(self.tag))


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    incidents = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        desc = (item.findtext("description") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()

        category, _, call_type = title.partition(":")
        category = category.strip()
        call_type = re.sub(r"\s*-\s*$", "", call_type.strip())

        parts = [p.strip() for p in desc.split(";") if p.strip()]
        if len(parts) < 2:
            continue
        location, township = parts[0], parts[1]

        m = re.search(r"Station[:\s]+(\w+)", desc, re.I)
        station = m.group(1) if m else ""

        when_utc = None
        m = re.search(r"(\d{4}-\d{2}-\d{2})\s*@\s*(\d{2}:\d{2}:\d{2})", desc)
        if m:  # incident time is local Eastern
            local = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=EASTERN)
            when_utc = local.astimezone(timezone.utc)
        elif pub:
            try:
                from email.utils import parsedate_to_datetime
                when_utc = parsedate_to_datetime(pub).astimezone(timezone.utc)
            except (TypeError, ValueError):
                pass
        if when_utc is None:
            when_utc = datetime.now(timezone.utc)

        incidents.append(Incident(category, call_type, location, township, station, when_utc, desc))
    return incidents


# --------------------------------------------------------------------------
# Geocoding (Google Geocoding API)
# --------------------------------------------------------------------------
def clean_street(s):
    """Normalise CAD street text; None for placeholders like 'DEAD END'."""
    s = s.strip().upper()
    if not s or s.startswith("DEAD END") or s in ("END", "CUL DE SAC"):
        return None
    if "RAMP" in s:  # 'RAMP RT309 SB TO NORRISTOWN RD' -> 'NORRISTOWN RD'
        s = s.split(" TO ")[-1] if " TO " in s else re.sub(r"^RAMP\s+", "", s)
    toks = [t for t in s.split() if t not in ("NB", "SB", "EB", "WB")]
    s = " ".join(toks)
    s = re.sub(r"\bRT\s*-?(\d+)\b", r"Route \1", s)  # RT309 -> Route 309
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
            data = json.loads(http(url))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            DOWN["google"] = str(e)
            print(f"# google geocoding unavailable ({e}); skipping until next cycle", file=sys.stderr)
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


def geocode_uncached(location, township, cache):
    """Returns (result_or_None, complete).  complete=False means the service failed
    part-way, so a miss / degraded result shouldn't be remembered."""
    complete = True
    t = township.title()
    named = [s for s in (clean_street(p) for p in location.split("&")) if s]

    # 1. the intersection (or, with no '&', the address) as a whole
    q = f"{named[0]} & {named[1]}, {t}, PA" if len(named) >= 2 else f"{location}, {t}, PA"
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
                pt = google(f"{s}, {t}, PA")
            except ServiceDown:
                complete = False
                break
            if pt:
                best = (pt[0], pt[1], "street")
                break
    if best:
        return best, complete

    # 3. township centroid (cached on its own, shared by every incident there)
    tkey = f"T|{township}".upper()
    hit = cache_get(cache, tkey)
    if hit:
        return (hit[0], hit[1], "township"), complete
    try:
        pt = google(f"{t}, Montgomery County, PA")
    except ServiceDown:
        return None, False
    if pt:
        cache_put(cache, tkey, [pt[0], pt[1]])
        return (pt[0], pt[1], "township"), complete
    return None, complete


# ---- cache (30-day TTL) --------------------------------------------------
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
    """Returns the cached value, or None. (A cached 'no result' is also None - use `key in cache`.)"""
    e = cache.get(key)
    if e and e.get("ts", 0) >= time.time() - CACHE_TTL:
        return e.get("v")
    return None


def cache_put(cache, key, value):
    cache[key] = {"ts": time.time(), "v": value}
    save_cache(cache)


# ---- live-object state (so kills survive restarts) -----------------------
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


def geocode(inc, cache):
    key = f"{inc.location}|{inc.township}".upper()
    e = cache.get(key)
    if e and e.get("ts", 0) >= time.time() - CACHE_TTL:
        return tuple(e["v"]) if e["v"] else None

    res, complete = geocode_uncached(inc.location, inc.township, cache)
    # Remember good results, and misses only if the service answered.  A degraded
    # result (street/township guess made while the service was down) is used now
    # but retried next cycle.
    if res and (complete or res[2] in PRECISE):
        cache_put(cache, key, list(res))
    elif not res and complete:
        cache_put(cache, key, None)
    return res


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
# the tag plus a base-36 code, e.g. FIRE7K2QX, TRAF0M1ZB, EMS4H9ABC.
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
    incidents that happen to hash alike are told apart deterministically (sorted by key)."""
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
    """'EAST NORRITON' -> 'E NORRITON', 'Lower Merion Township' -> 'LWR MERION'."""
    name = re.sub(r"\s+", " ", name).strip().upper()
    for pat, rep in PLACE_ABBREV:
        name = re.sub(pat, rep, name)
    return name


TYPE_TIGHT = [  # call-type abbreviations, used only when the full text would not fit
    (r"\bVEHICLE\b", "VEH"), (r"\bINVESTIGATION\b", "INVEST"), (r"\bRESPIRATORY\b", "RESP"),
    (r"\bDIFFICULTY\b", "DIFF"), (r"\bEMERGENCY\b", "EMERG"), (r"\bUNCONSCIOUS\b", "UNCONS"),
]


def shorten(text, n):
    """Cut to n chars at a word boundary when that keeps a reasonable amount of text."""
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0]
    return cut if len(cut) >= 8 else text[:n]


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


def incident_packet(inc, lat, lon, quality):
    table, code = SYMBOLS.get(inc.category, DEFAULT_SYMBOL)
    loc = re.sub(r"\s*&\s*", "/", inc.location).strip("/ ")
    tail = "" if quality in PRECISE else APPROX_MARK  # trailing "?" = approximate position
    # Core: call type + location (the type is shortened first if they don't fit together).
    room = COMMENT_LIMIT - len(tail) - len(loc) - 1
    ctype = re.sub(r"\s+", " ", inc.call_type).strip()
    if len(ctype) > room:  # abbreviate only when space is tight
        for pat, rep in TYPE_TIGHT:
            ctype = re.sub(pat, rep, ctype, flags=re.I)
    core = f"{shorten(ctype, max(room, 12))} {loc}"
    # Extras, most important first; whatever doesn't fit is dropped whole.
    extras = []
    twp = short_place(inc.township)
    if twp:
        extras.append(f"({twp})")
    if inc.station:
        extras.append(inc.station if inc.station.upper().startswith("STA") else f"STA{inc.station}")
    extras.append(inc.when_utc.astimezone(EASTERN).strftime("@%H:%M"))  # dispatch time, local
    comment = pack(core, extras, COMMENT_LIMIT, tail)
    return object_packet(inc.name, lat, lon, inc.when_utc, table, code, comment), (table, code, comment)


# --------------------------------------------------------------------------
# APRS-IS
# --------------------------------------------------------------------------
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
        self.sock.recv(512)  # server banner
        login = f"user {self.call} pass {self.passcode} vers montco-cad-aprs 1.0\r\n"
        self.sock.sendall(login.encode())
        resp = self.sock.recv(512).decode(errors="replace")
        if "verified" not in resp.lower() or "unverified" in resp.lower():
            raise RuntimeError(f"APRS-IS login not verified: {resp.strip()}")

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
def run_cycle(args, cache, live, aprs_is):
    DOWN.clear()  # give the service a fresh chance each cycle
    incidents = parse_feed(http(args.url))
    seen = set()
    out = []

    # Filter BEFORE geocoding so excluded categories cost no API calls
    wanted = {c.strip().lower() for c in args.categories.split(",") if c.strip()}
    if wanted:
        incidents = [i for i in incidents if i.category.lower() in wanted]

    # Safety net: ignore (and so kill) anything older than --max-age-hours, judged by
    # the incident's own timestamp, even if the feed keeps listing it.
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

    placed = {}
    for inc in incidents:
        res = geocode(inc, cache)
        if not res:
            print(f"# no coordinates for: {inc.call_type} @ {inc.location}; {inc.township}", file=sys.stderr)
            continue
        lat, lon, quality = res
        pkt, sym = incident_packet(inc, lat, lon, quality)
        placed[inc.name] = (lat, lon, sym)
        out.append(pkt)

    if aprs_overrides:
        current -= aprs_overrides.apply(placed, out)

    # Kill objects we sent earlier (this run or a previous one) that are no longer
    # wanted: gone from the feed, filtered out, or too old.  An incident that is
    # still wanted but failed to geocode is left alone rather than killed.
    gone = [n for n in live if n not in current]
    for name in gone:
        lat, lon, (table, code, comment) = live[name]
        out.append(object_packet(name, lat, lon, now, table, code, comment, live=False))

    for pkt in out:
        print(f"{args.callsign}>APRS,TCPIP*:{pkt}")
        if aprs_is:
            aprs_is.send(pkt)

    # Update bookkeeping only after everything was sent: if a send raised, the next
    # cycle repeats the same packets (harmless) instead of losing the kills.
    for name in gone:
        del live[name]
    live.update(placed)
    save_state(args.state_file, args.callsign, live)
    return len(incidents), len(placed)


def main():
    global API_KEY, COMMENT_LIMIT
    ap = argparse.ArgumentParser(description="Montgomery County PA CAD RSS -> APRS objects (Google geocoding)")
    ap.add_argument("--url", default=FEED_URL)
    ap.add_argument("--api-key", help=f"Google Geocoding API key (default: ${API_KEY_ENV})")
    ap.add_argument("--categories", default="Fire,Traffic",
                    help="comma-separated feed categories to keep: Fire, Traffic, EMS "
                         "(default: Fire,Traffic; pass '' for all)")
    ap.add_argument("--max-age-hours", type=float, default=MAX_AGE_HOURS,
                    help=f"kill and ignore incidents older than this many hours, by their own "
                         f"timestamp (default {MAX_AGE_HOURS:g}; 0 disables)")
    ap.add_argument("--state-file", default=STATE_FILE,
                    help=f"where live objects are remembered between runs (default {STATE_FILE})")
    ap.add_argument("--comment-length", type=int, default=COMMENT_LIMIT,
                    help=f"maximum characters of object text (default {COMMENT_LIMIT}; APRS 1.0 specifies 43 - "
                         f"use 43 if your clients truncate the text or drop the objects)")
    ap.add_argument("--callsign", default="N0CALL", help="source callsign-SSID for the packets")
    ap.add_argument("--passcode", type=int, help="APRS-IS passcode (default: computed from callsign)")
    ap.add_argument("--send", action="store_true", help="actually send to APRS-IS (requires a real callsign)")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--interval", type=int, default=120, help="seconds between polls (default 120)")
    args = ap.parse_args()

    if not 20 <= args.comment_length <= 200:
        ap.error("--comment-length must be between 20 and 200")
    COMMENT_LIMIT = args.comment_length
    API_KEY = args.api_key or os.environ.get(API_KEY_ENV, "")
    if not API_KEY:
        ap.error(f"no Google API key: set ${API_KEY_ENV} or pass --api-key")
    if args.send and args.callsign.upper().startswith("N0CALL"):
        ap.error("--send requires your own callsign (--callsign)")

    args.callsign = args.callsign.upper()
    cache = load_cache()
    # Objects sent by an earlier run; anything no longer in the feed gets killed on the first cycle
    live = load_state(args.state_file, args.callsign)
    if live:
        print(f"# restored {len(live)} live object(s) from {args.state_file}", file=sys.stderr)
    aprs_is = None
    if args.send:
        aprs_is = AprsIS(args.callsign.upper(), args.passcode or aprs_passcode(args.callsign))

    try:
        while True:
            try:
                total, placed = run_cycle(args, cache, live, aprs_is)
                print(f"# {placed}/{total} incidents placed", file=sys.stderr)
            except (urllib.error.URLError, OSError, ET.ParseError, RuntimeError) as e:
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
