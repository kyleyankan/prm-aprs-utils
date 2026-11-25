#!/usr/bin/env python3
"""
PennDOT RCRS / 511PA live traffic events  ->  APRS objects

Data source: PennDOT's Road Condition Reporting System (RCRS) Event Data Service,
the feed behind 511PA:
  https://eventsdata.dot.pa.gov/liveEvents       active events
  https://eventsdata.dot.pa.gov/plannedEvents    scheduled events (--include-planned)
Access is free but needs credentials from PennDOT (HTTP Basic auth):
  Data Feed Request Form: https://paiedprod.powerappsportals.us/DataFeedRequestForm/

Credentials come from the environment (never from the command line or disk):
  export PENNDOT_RCRS_USER=...
  export PENNDOT_RCRS_PASSWORD=...

Response: {"Values": [ {event}, ... ]}.  Fields used (from PennDOT's documentation):
  EventID, EventType, Facility, Direction, LaneStatus, AffectedLanes, CountyName,
  IncidentLocLatLong / FromLocLatLong / ToLocLatLong ("lat, lon" text),
  LastUpdate, CreateTime, DateTimeNotified, DateTimeEventOccurs,
  ActualDateTimeOpened (set once the road reopens; kept in the feed ~30 min more),
  IsFatality, IsHazmat, IsCommercialVehicleRollover.
Coordinates are supplied, so no geocoding and no Google key are needed.

Object names and text
  Names are 9 characters, the APRS maximum: a descriptive tag plus the RCRS EventID in
  base 36, stable for the life of the event - TRAF.... for crashes and incidents,
  WORK.... for roadwork and restrictions (e.g. WORK0AB4C).  The text gives event type,
  roadway, direction, lane status, affected lanes, county, landmark and update time,
  dropping the least important items first when space runs out.  It may be up to
  --comment-length characters (default 67).  APRS 1.0 specifies 43 for object
  comments; most modern clients show more, but if yours truncates the text or drops the
  objects, use --comment-length 43.
  Upgrading from an older version: objects sent under the old names are killed on the
  first run that uses the same state file, and re-created under the new names.

Terms of use (PennDOT 511PA Developers Corner): credit PennDOT as the data source,
keep source data intact, and minimise load on PennDOT's feeds.  There is no stated
rate limit; this script polls every 120 s by default (minimum 60).  With --send it
also posts an APRS status line crediting PennDOT.

Default behaviour is PRINT ONLY.  With  --send --callsign YOURCALL  (a licensed amateur
callsign) each poll's packets are handed to aprs_send.py, the one program that logs in to
APRS-IS; it sends what is new, changed or killed, and re-sends the rest every 26 minutes.

Object lifetime (APRS objects have no built-in time-to-live)
  * Every object sent is remembered in pa511_live_state.json.  On the next poll - or
    the next run, after a restart or a --once - anything no longer in the feed gets
    a kill packet.  Events whose road has reopened are killed straight away.
  * --max-age-hours (default 0 = off) kills and ignores events whose LastUpdate is
    older than that.  Left off by default because long-running roadwork can go days
    without an update.
  * State is tied to --callsign; a state file written by another callsign is ignored.

Statewide there can be hundreds of events.  Use --county to limit what you transmit
(e.g. --county Montgomery,Bucks).

Standard library only (Python 3.9+).

Examples
  python3 aprs_pa511.py --once --county Montgomery
  python3 aprs_pa511.py --county Montgomery --callsign N0CALL-13 --send
"""
import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aprs_send                   # hands packets to the one program that sends to APRS-IS

try:
    import aprs_overrides            # edits/deletes made in the web UI (optional)
except ImportError:
    aprs_overrides = None

LIVE_URL = "https://eventsdata.dot.pa.gov/liveEvents"
PLANNED_URL = "https://eventsdata.dot.pa.gov/plannedEvents"
USER_ENV, PASS_ENV = "PENNDOT_RCRS_USER", "PENNDOT_RCRS_PASSWORD"
STATUS_TEXT = "Traffic events: PennDOT RCRS / 511PA (www.511PA.com)"
STATE_FILE = "pa511_live_state.json"
EASTERN = ZoneInfo("America/New_York")  # RCRS times are assumed to be local Eastern

# Plausible Pennsylvania bounds (lat_min, lat_max, lon_min, lon_max) to reject garbage
PA_BOX = (39.6, 42.4, -80.7, -74.6)

# APRS symbols: (table, code).  A judgement call - edit to taste.
def env_symbol(key, default):
    """APRS symbol (table, code) from $SYM_<key> (two characters, e.g. /d), else the built-in default."""
    v = os.environ.get("SYM_" + key, "")
    if len(v) == 2 and (v[0] in "/\\" or v[0].isalnum()) and "!" <= v[1] <= "~":
        return v[0], v[1]
    if v:
        print(f"# SYM_{key}={v!r} is not a valid APRS symbol (table + code); using the default", file=sys.stderr)
    return default


# Change these in aprs.env (SYM_ROAD_WORK, SYM_ROAD_CRASH, SYM_ROAD_MAJOR).
SYM_WORKS = env_symbol("ROAD_WORK", ("/", "."))       # red X: lane/road restriction
SYM_CRASH = env_symbol("ROAD_CRASH", ("\\", "'"))     # crash site
SYM_MAJOR = env_symbol("ROAD_MAJOR", ("\\", "!"))     # emergency: major incidents
CRASH_WORDS = re.compile(r"accident|crash|collision|disabled|stalled|vehicle|fire|debris|spill|hazmat|rollover", re.I)

DIRS = {"northbound": "NB", "southbound": "SB", "eastbound": "EB", "westbound": "WB",
        "north": "N", "south": "S", "east": "E", "west": "W"}

RETRY_CODES = {429, 500, 502, 503, 504}


# --------------------------------------------------------------------------
# HTTP (Basic auth)
# --------------------------------------------------------------------------
class AuthError(Exception):
    pass


def http(url, auth, timeout=45, tries=3):
    """GET with Basic auth; retries 429/5xx and network errors with backoff."""
    headers = {"Authorization": "Basic " + base64.b64encode(auth.encode()).decode(),
               "Accept": "application/json", "User-Agent": "pa511-aprs/1.0"}
    delay, last = 2, None
    for attempt in range(tries):
        wait = delay
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthError(f"HTTP {e.code} from {url} - check ${USER_ENV} / ${PASS_ENV} "
                                f"and that PennDOT has approved your access")
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
# Feed parsing
# --------------------------------------------------------------------------
LATLON = re.compile(r"(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)")


def s(d, key):
    """Field value as text; field names are matched ignoring case and underscores."""
    v = d.get(key)
    if v is None:
        want = key.lower().replace("_", "")
        for k, val in d.items():
            if str(k).lower().replace("_", "") == want:
                v = val
                break
    return "" if v is None else str(v).strip()


def truthy(v):
    return str(v).strip().lower() in ("t", "true", "y", "yes", "1")


def parse_latlon(text):
    m = LATLON.search(text or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    a, b, c, d = PA_BOX
    return (lat, lon) if a <= lat <= b and c <= lon <= d else None


def parse_dt(text):
    """RCRS 'YYYY-MM-DD HH:MM[:SS]' (assumed Eastern) -> aware UTC datetime, or None."""
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text[:26] if "." in fmt else text[:19], fmt).replace(tzinfo=EASTERN).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


class Event:
    def __init__(self, d, planned):
        self.id = s(d, "EventID")
        self.planned = planned
        self.etype = s(d, "EventType")
        self.facility = s(d, "Facility")
        self.direction = s(d, "Direction")
        self.lane_status = s(d, "LaneStatus")
        self.lanes = s(d, "AffectedLanes")
        self.incident_loc = s(d, "IncidentLoc")
        self.county = s(d, "CountyName") or s(d, "CountyIncName")
        self.description = s(d, "Description")
        self.reopened = parse_dt(s(d, "ActualDateTimeOpened")) is not None
        self.major = any(truthy(d.get(k)) for k in ("IsFatality", "IsHazmat", "IsCommercialVehicleRollover"))
        self.updated = (parse_dt(s(d, "LastUpdate")) or parse_dt(s(d, "CreateTime"))
                        or parse_dt(s(d, "DateTimeNotified")) or datetime.now(timezone.utc))
        self.starts = parse_dt(s(d, "DateTimeEventOccurs"))
        self.pos = self._position(d)
        self.name = None  # assigned by assign_names() once the set of wanted events is known

    @property
    def tag(self):
        """Descriptive name prefix: TRAF for crashes/incidents, WORK for roadwork and restrictions."""
        return "TRAF" if CRASH_WORDS.search(self.etype) else "WORK"

    @property
    def key(self):
        return self.id

    def name_for(self, salt=0):
        """Candidate 9-char object name: tag + the EventID in base 36 (salted hash if it
        isn't a number that fits, or on a collision)."""
        room = 9 - len(self.tag)
        if salt == 0 and self.id.isdigit() and int(self.id) < 36 ** room:
            return self.tag + to_b36(int(self.id)).rjust(room, "0")
        return self.tag + b36_hash(self.id + (f"#{salt}" if salt else ""), room)

    @staticmethod
    def _position(d):
        inc = parse_latlon(s(d, "IncidentLocLatLong"))
        if inc:
            return inc
        frm, to = parse_latlon(s(d, "FromLocLatLong")), parse_latlon(s(d, "ToLocLatLong"))
        if frm and to:
            return ((frm[0] + to[0]) / 2, (frm[1] + to[1]) / 2)  # middle of the affected stretch
        return frm or to


STATS = {}          # what the last cycle saw, for the diagnostic summary line


def parse_feed(raw, planned):
    try:
        data = json.loads(raw)
    except ValueError:
        raise ValueError(f"feed did not return JSON (starts: {raw[:120]!r})")
    if isinstance(data, dict):
        values = data.get("Values")
        if values is None:
            lists = [v for v in data.values() if isinstance(v, list)]
            values = lists[0] if lists else None
    else:
        values = data
    if not isinstance(values, list):
        keys = list(data)[:8] if isinstance(data, dict) else type(data).__name__
        raise ValueError(f"unexpected feed structure: no event list found (top-level keys: {keys})")
    feed = "planned" if planned else "live"
    STATS[feed] = len(values)
    if values and DUMP:
        print(f"# first {feed} event as received:\n" + json.dumps(values[0], indent=1, default=str)[:3000],
              file=sys.stderr)
    out, no_id, no_pos = [], 0, 0
    for d in values:
        if not isinstance(d, dict):
            continue
        ev = Event(d, planned)
        if not ev.id:
            no_id += 1
            continue
        if ev.pos is None:
            no_pos += 1
            continue
        out.append(ev)
    STATS["no_id"] = STATS.get("no_id", 0) + no_id
    STATS["no_pos"] = STATS.get("no_pos", 0) + no_pos
    if no_id and not out:
        sample = next((d for d in values if isinstance(d, dict)), {})
        print(f"# none of the {feed} events has an EventID field - the feed's field names may differ "
              f"from the documentation.  Fields seen: {', '.join(list(sample)[:30])}", file=sys.stderr)
    return out


# --------------------------------------------------------------------------
# APRS encoding
# --------------------------------------------------------------------------
# ---- object names -------------------------------------------------------
B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def to_b36(n):
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = B36[r] + out
        if n == 0:
            return out


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
    events that happen to clash are told apart deterministically (sorted by key)."""
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


def clean_comment(text, limit=None):
    limit = COMMENT_LIMIT if limit is None else limit
    text = re.sub(r"[^\x20-\x7e]", "", text).replace("|", "/").replace("~", "-")
    return re.sub(r"\s+", " ", text)[:limit]


def pack(core, extras, limit):
    """core text plus as many whole extras as fit (in priority order)."""
    text = core[:limit].rstrip()
    for e in extras:
        if e and len(text) + 1 + len(e) <= limit:
            text += " " + e
    return text


def make_comment(ev, limit=None):
    """Type, roadway and direction first; then lane status, lanes, county, landmark, update time."""
    limit = COMMENT_LIMIT if limit is None else limit
    direction = DIRS.get(ev.direction.lower(), ev.direction)
    core = " ".join(p for p in (ev.etype, ev.facility, direction) if p) or ev.description or "Traffic event"
    extras = []
    if ev.lane_status:
        extras.append(ev.lane_status)
    if ev.lanes and ev.lanes.lower() != ev.lane_status.lower():
        extras.append(ev.lanes)
    if ev.county:
        extras.append(f"({re.sub(r' County$', '', ev.county, flags=re.I).upper()})")
    if ev.incident_loc:
        extras.append(f"@{ev.incident_loc}")
    extras.append(ev.updated.astimezone(EASTERN).strftime("upd %H:%M"))
    return pack(re.sub(r"\s+", " ", core), [re.sub(r"\s+", " ", e) for e in extras], limit)


def symbol_for(ev):
    if ev.major:
        return SYM_MAJOR
    if CRASH_WORDS.search(ev.etype):
        return SYM_CRASH
    return SYM_WORKS


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
            f"{aprs_lat(lat)}{table}{aprs_lon(lon)}{code}{clean_comment(comment)}")


def event_packet(ev):
    table, code = symbol_for(ev)
    comment = make_comment(ev)
    return object_packet(ev.name, ev.pos[0], ev.pos[1], ev.updated, table, code, comment), (table, code, comment)


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
# APRS-IS
# --------------------------------------------------------------------------
# Sending is done by aprs_send.py (one APRS-IS connection for every script): this script
# hands over each poll's packets with aprs_send.Outbox and never logs in itself.


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
DUMP = False


def fetch_events(args):
    auth = f"{args.user}:{args.password}"
    events = {}
    if args.include_planned:  # planned first so a live record of the same event wins
        for ev in parse_feed(http(PLANNED_URL, auth), planned=True):
            events[ev.id] = ev
    for ev in parse_feed(http(LIVE_URL, auth), planned=False):
        events[ev.id] = ev
    return list(events.values())


def run_cycle(args, live, aprs_is):
    STATS.clear()
    events = fetch_events(args)   # raises on a bad feed -> nothing is killed
    now = datetime.now(timezone.utc)
    dropped = {"reopened": 0, "other counties": 0, "planned for later": 0, "not updated recently": 0}
    counties = {}

    wanted_counties = {c.strip().lower().removesuffix(" county") for c in args.county.split(",") if c.strip()}
    keep = []
    for ev in events:
        cname = ev.county.lower().removesuffix(" county")
        counties[ev.county or "(blank)"] = counties.get(ev.county or "(blank)", 0) + 1
        if ev.reopened:
            dropped["reopened"] += 1
            continue  # road has reopened: leave it out so any live object is killed
        if wanted_counties and cname not in wanted_counties:
            dropped["other counties"] += 1
            continue
        if ev.planned and ev.starts and ev.starts > now + timedelta(hours=args.planned_lookahead_hours):
            dropped["planned for later"] += 1
            continue  # scheduled for later
        if args.max_age_hours > 0 and ev.updated < now - timedelta(hours=args.max_age_hours):
            dropped["not updated recently"] += 1
            continue
        keep.append(ev)

    got = ", ".join(f"{STATS[k]} {k}" for k in ("live", "planned") if k in STATS)
    why = [f"{n} {k}" for k, n in dropped.items() if n]
    if STATS.get("no_id"):
        why.append(f"{STATS['no_id']} without an EventID")
    if STATS.get("no_pos"):
        why.append(f"{STATS['no_pos']} without coordinates")
    print(f"# PennDOT returned {got or 'nothing'}; keeping {len(keep)}" + (f" (left out: {', '.join(why)})" if why else ""),
          file=sys.stderr)
    if wanted_counties and not keep and counties:
        top = sorted(counties.items(), key=lambda kv: -kv[1])[:12]
        print("# none matched --county; counties in the feed: " + ", ".join(f"{c} ({n})" for c, n in top), file=sys.stderr)

    assign_names(keep)
    placed, out = {}, []
    for ev in keep:
        pkt, sym = event_packet(ev)
        placed[ev.name] = (ev.pos[0], ev.pos[1], sym)
        out.append(pkt)

    if aprs_overrides:
        aprs_overrides.apply(placed, out)

    # Kill objects we sent earlier (this run or a previous one) that are no longer wanted
    gone = [n for n in live if n not in placed]
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
    return len(placed), len(gone)


def main():
    global COMMENT_LIMIT
    ap = argparse.ArgumentParser(description="PennDOT RCRS / 511PA events -> APRS objects")
    ap.add_argument("--user", default=os.environ.get(USER_ENV, ""), help=f"RCRS username (default: ${USER_ENV})")
    ap.add_argument("--county", default="", help="only these counties, comma-separated (default: all of PA)")
    ap.add_argument("--include-planned", action="store_true",
                    help="also read plannedEvents (only ones already under way are transmitted)")
    ap.add_argument("--planned-lookahead-hours", type=float, default=0,
                    help="with --include-planned: also show planned events starting within this many hours")
    ap.add_argument("--max-age-hours", type=float, default=0,
                    help="kill and ignore events whose LastUpdate is older than this (default 0 = off)")
    ap.add_argument("--state-file", default=STATE_FILE,
                    help=f"where live objects are remembered between runs (default {STATE_FILE})")
    ap.add_argument("--comment-length", type=int, default=COMMENT_LIMIT,
                    help=f"maximum characters of object text (default {COMMENT_LIMIT}; APRS 1.0 specifies 43 - "
                         f"use 43 if your clients truncate the text or drop the objects)")
    ap.add_argument("--callsign", default="N0CALL", help="source callsign-SSID for the packets")
    ap.add_argument("--passcode", type=int, help="ignored: aprs_send.py logs in to APRS-IS, not this script")
    ap.add_argument("--send", action="store_true", help="hand the packets to aprs_send.py for sending (requires a real callsign)")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--dump", action="store_true",
                    help="print the first event exactly as PennDOT sends it (to check field names)")
    ap.add_argument("--interval", type=int, default=120, help="seconds between polls (default 120, minimum 60)")
    args = ap.parse_args()
    print(f"# symbols in use: roadwork {''.join(SYM_WORKS)}  crash/incident {''.join(SYM_CRASH)}  "
          f"major {''.join(SYM_MAJOR)}  (set SYM_ROAD_WORK / SYM_ROAD_CRASH / SYM_ROAD_MAJOR in aprs.env)",
          file=sys.stderr, flush=True)
    global DUMP
    DUMP = args.dump

    if not 20 <= args.comment_length <= 200:
        ap.error("--comment-length must be between 20 and 200")
    COMMENT_LIMIT = args.comment_length
    args.password = os.environ.get(PASS_ENV, "")
    if not args.user or not args.password:
        ap.error(f"credentials missing: set ${USER_ENV} and ${PASS_ENV} (request access from PennDOT)")
    args.callsign = args.callsign.upper()
    args.interval = max(args.interval, 60)  # be kind to PennDOT's feed
    if args.send and args.callsign.startswith("N0CALL"):
        ap.error("--send requires your own callsign (--callsign)")

    live = load_state(args.state_file, args.callsign)
    if live:
        print(f"# restored {len(live)} live object(s) from {args.state_file}", file=sys.stderr)
    aprs_is = aprs_send.Outbox("pa511", args.callsign, status=STATUS_TEXT) if args.send else None

    try:
        while True:
            try:
                placed, killed = run_cycle(args, live, aprs_is)
                print(f"# {placed} event(s) placed, {killed} killed", file=sys.stderr)
            except AuthError as e:
                sys.exit(f"# {e}")
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
