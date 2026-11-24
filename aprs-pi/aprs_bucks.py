#!/usr/bin/env python3
"""
Bucks County (PA) 911 Incident Report -> APRS objects (Fire and Traffic by default).

Source: https://www.buckscounty.gov/2150/911-Incident-Report, which is a public
Microsoft Power BI report.  The report has no documented feed, so this script asks
the report's own data service the same kind of question the report's map asks, and
ONLY for the fields the public report displays (incident number, type, dispatch
time, public location, station, municipality and the map position).  The dataset
also contains internal location fields the report never shows; they are not
requested.  Because this is an unofficial interface it can change without notice:
if it does, the cycle fails, nothing is killed, and the script needs updating.

No geocoding is needed (the report supplies map coordinates).  Many incidents are
published with only a municipality; those are marked with a trailing "?" because
the position is approximate.

  python3 aprs_bucks.py --once                       # print only
  python3 aprs_bucks.py --callsign W3XYZ-9 --send
"""
import argparse
import base64
import gzip
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
import zlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    import aprs_overrides            # edits/deletes made in the web UI (optional)
except ImportError:
    aprs_overrides = None

REPORT_URL = ("https://app.powerbigov.us/view?r=eyJrIjoiZGU0YjU1ODQtY2RmMS00ZGE0LWEzZDAtMDkzZTg3MjZmYWE2Iiwid"
              "CI6Ijg0ZTJlODcxLWU3ZmMtNGIwMC1hMzg3LWNlODg0MGE0ZTU4ZCJ9")
API_HOST = "wabi-us-gov-virginia-api.analysis.usgovcloudapi.net"
ENTITY = "uVw_New_PFE_Data"
KNOWN_MODEL_ID = 1439607        # the report's data model when this script was written (fallback)
# Only what the public report shows.  Do NOT add the Internal_* columns.
COLUMNS = ["Incident_Number", "Incident_Type", "Dispatch_Time", "ExternalLocWMun",
           "Station", "Municipality_Name", "External_Lat", "External_Lon"]
PREFIXES = {"Fire": "FD", "Traffic": "PD", "EMS": "ED"}     # incident-number prefix per category
APRS_IS_HOST, APRS_IS_PORT = "rotate.aprs2.net", 14580
USER_AGENT = "bucks-cad-aprs/1.0"
EASTERN = ZoneInfo("America/New_York")
STATE_FILE = "bucks_live_state.json"
MAX_AGE_HOURS = 6.0
COUNTY_BOX = (39.95, -75.60, 40.75, -74.65)   # south, west, north, east: sanity check on positions
RETRY_CODES = {429, 500, 502, 503, 504}

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
APPROX_MARK = "?"
TAGS = {"Fire": "FIRE", "EMS": "EMS", "Traffic": "TRAF"}


# --------------------------------------------------------------------------
# Power BI report data service
# --------------------------------------------------------------------------
def http(url, body=None, headers=None, timeout=30, tries=3):
    """GET/POST with retry + exponential backoff on 429/5xx and network errors."""
    delay, last = 2, None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    hdrs.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    for attempt in range(tries):
        wait = delay
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return unpack(r.read(), r.headers.get("Content-Encoding", ""))
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


def unpack(raw, encoding=""):
    """Undo gzip/deflate if the service compressed the reply (it may do so even when not asked)."""
    try:
        if raw[:2] == b"\x1f\x8b" or "gzip" in encoding.lower():
            return gzip.decompress(raw)
        if "deflate" in encoding.lower():
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, EOFError, zlib.error):
        pass
    return raw


def load_json(raw):
    """Parse a reply as JSON whatever its text encoding (UTF-8 with or without BOM, or UTF-16)."""
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeError, ValueError):
            continue
    raise ValueError("reply is not JSON")


def preview(raw):
    return repr(raw[:160])


def resource_key(report_url):
    """The public report key ("k") carried in the report link's r= token."""
    try:
        token = urllib.parse.parse_qs(urllib.parse.urlparse(report_url).query)["r"][0]
        return json.loads(base64.b64decode(token + "=" * (-len(token) % 4)))["k"]
    except (KeyError, IndexError, ValueError, TypeError):
        raise ValueError("--report-url does not look like a Power BI 'view?r=...' link")


class Report:
    def __init__(self, report_url, api_host, model_id=None):
        self.key = resource_key(report_url)
        self.base = f"https://{api_host}/public/reports"
        self.model_id = model_id
        self.headers = {"X-PowerBI-ResourceKey": self.key}

    def find_model(self):
        """Look up the report's data model id; fall back to the last known one if that fails."""
        why = ""
        try:
            raw = http(f"{self.base}/{self.key}/modelsAndExploration?preferReadOnlySession=true", headers=self.headers)
            try:
                self.model_id = int(load_json(raw)["models"][0]["id"])
                return
            except (ValueError, KeyError, IndexError, TypeError):
                why = f"unexpected reply {preview(raw)}"
        except (urllib.error.URLError, OSError) as e:
            why = str(e)
        self.model_id = KNOWN_MODEL_ID
        print(f"# could not look up the report's data model ({why}); trying the last known one ({KNOWN_MODEL_ID})",
              file=sys.stderr)

    def query(self, categories):
        if self.model_id is None:
            self.find_model()
        col = lambda p: {"Column": {"Expression": {"SourceRef": {"Source": "u"}}, "Property": p}}
        query = {"Version": 2, "From": [{"Name": "u", "Entity": ENTITY, "Type": 0}],
                 "Select": [dict(col(c), Name=f"{ENTITY}.{c}") for c in COLUMNS]}
        cond = None
        for cat in categories:                      # incident number starts with FD / PD / ED
            sw = {"StartsWith": {"Left": col("Incident_Number"), "Right": {"Literal": {"Value": f"'{PREFIXES[cat]}'"}}}}
            cond = sw if cond is None else {"Or": {"Left": cond, "Right": sw}}
        if cond:
            query["Where"] = [{"Condition": cond}]
        body = {"version": "1.0.0", "queries": [{"Query": {"Commands": [{"SemanticQueryDataShapeCommand": {
            "Query": query,
            "Binding": {"Primary": {"Groupings": [{"Projections": list(range(len(COLUMNS)))}]},
                        "DataReduction": {"DataVolume": 3, "Primary": {"Window": {"Count": 500}}}, "Version": 1}}}]},
            "QueryId": ""}], "cancelQueries": [], "modelId": self.model_id}
        try:
            return http(f"{self.base}/querydata?synchronous=true", body=body, headers=self.headers)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = preview(unpack(e.read(), e.headers.get("Content-Encoding", "") if e.headers else ""))
            except Exception:
                pass
            if e.code in (400, 401, 403, 404):
                self.model_id = None                 # the model may have been republished: look it up again next cycle
            raise ValueError(f"the report's data service answered HTTP {e.code} {detail}")


def decode_rows(raw):
    """Rows (lists of values, in COLUMNS order) from a Power BI query response.

    Power BI compresses the table: text values are indexes into per-column dictionaries,
    a row's "R" bitmask means "same as the previous row" and "Ø" means "empty"."""
    try:
        data = load_json(raw)
        result = data["results"][0]["result"]["data"]
        ds = result["dsr"]["DS"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        raise ValueError(f"unexpected response from the report's data service: {preview(raw)}")
    if "odata.error" in ds or "error" in result.get("dsr", {}):
        raise ValueError("the report's data service returned an error")
    dicts = ds.get("ValueDicts", {})
    members = (ds.get("PH") or [{}])[0].get("DM0") or []
    rows, schema, prev = [], None, None
    for m in members:
        if "S" in m:
            schema = m["S"]
        if schema is None or len(schema) != len(COLUMNS):
            raise ValueError("the report's data layout has changed")
        values, it = [], iter(m.get("C", []))
        repeat, null = m.get("R", 0), m.get("\u00d8", 0)
        try:
            for i, colinfo in enumerate(schema):
                if repeat & (1 << i):
                    if prev is None:
                        raise ValueError
                    values.append(prev[i])
                elif null & (1 << i):
                    values.append(None)
                else:
                    v = next(it)
                    dn = colinfo.get("DN")
                    if dn and isinstance(v, int) and not isinstance(v, bool):
                        v = dicts[dn][v]
                    values.append(v)
        except (StopIteration, ValueError, KeyError, IndexError, TypeError):
            raise ValueError("could not decode a row from the report's data service")
        rows.append(values)
        prev = values
    return rows


class Incident:
    def __init__(self, row):
        number, ctype, when, loc, station, muni, lat, lon = row
        self.number = str(number or "").strip()
        self.call_type = re.sub(r"\s+", " ", str(ctype or "")).strip()
        self.municipality = re.sub(r"\s+", " ", str(muni or "")).strip()
        loc = re.sub(r"\s+", " ", str(loc or "")).strip()
        self.location = re.sub(r"\s*\([^()]*\)\s*$", "", loc).strip()     # drop the trailing "(MUNICIPALITY)"
        station = re.sub(r"\s+", " ", str(station or "")).strip()
        self.station = "" if station.upper() in ("", "N/A", "NA", "NONE") else station
        self.lat, self.lon = float(lat), float(lon)
        self.category = next((c for c, p in PREFIXES.items() if self.number.upper().startswith(p)), "Other")
        try:  # 'YYYY-MM-DD HH:MM:SS', local Eastern
            self.when_utc = (datetime.strptime(str(when)[:19], "%Y-%m-%d %H:%M:%S")
                             .replace(tzinfo=EASTERN).astimezone(timezone.utc))
        except ValueError:
            self.when_utc = datetime.now(timezone.utc)
        self.tag = TAGS.get(self.category, "INC")
        self.name = None

    @property
    def key(self):
        return self.number

    def name_for(self, salt=0):
        """tag + B (Bucks, so it can't clash with the other counties' numbers) + the tail of
        the incident number: FD26000022680 -> FIREB2680.  A clash falls back to a salted hash."""
        room = 9 - len(self.tag) - 1
        digits = re.sub(r"\D", "", self.number)
        if salt == 0 and digits:
            return f"{self.tag}B{digits[-room:].rjust(room, '0')}"
        return f"{self.tag}B{b36_hash(self.key + (f'#{salt}' if salt else ''), room)}"


def parse_incidents(raw):
    out = []
    for row in decode_rows(raw):
        try:
            inc = Incident(row)
        except (TypeError, ValueError):
            continue                                  # no usable position
        s, w, n, e = COUNTY_BOX
        if inc.number and s <= inc.lat <= n and w <= inc.lon <= e:
            out.append(inc)
    return out


# --------------------------------------------------------------------------
# State, names, text, APRS-IS (same conventions as the other county scripts)
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


def shorten(text, n):
    """Cut to n chars at a word boundary when that keeps a reasonable amount of text."""
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0]
    return cut if len(cut) >= 8 else text[:n]



TYPE_TIGHT = [  # used only when the full call type would not fit
    (r"\bVEHICLE\b", "VEH"), (r"\bINVESTIGATION\b", "INVEST"), (r"\bACCIDENT\b", "ACC"),
    (r"\bINFORMATION\b", "INFO"), (r"\bEMERGENCY\b", "EMERG"), (r"\bCOMMERCIAL\b", "COMM"),
    (r"\bRESIDENTIAL\b", "RES"), (r"\bSTRUCTURE\b", "STRUCT"),
]


def incident_packet(inc):
    table, code = SYMBOLS.get(inc.category, DEFAULT_SYMBOL)
    loc = re.sub(r"\s*&\s*", "/", inc.location).strip("/ ")
    ctype = inc.call_type
    tail = "" if loc else APPROX_MARK             # municipality-only incidents: position is approximate
    room = COMMENT_LIMIT - len(tail) - (len(loc) + 1 if loc else 0)
    if len(ctype) > room:
        for pat, rep in TYPE_TIGHT:
            ctype = re.sub(pat, rep, ctype, flags=re.I)
    core = f"{shorten(ctype, max(room, 12))} {loc}".strip()
    extras = []
    muni = short_place(inc.municipality)
    if muni:
        extras.append(f"({muni})")
    extras.append(inc.when_utc.astimezone(EASTERN).strftime("@%H:%M"))     # dispatch time, local
    if inc.station:
        extras.append(shorten(inc.station, 22))
    extras.append(f"#{inc.number}")
    comment = pack(core, extras, COMMENT_LIMIT, tail)
    return object_packet(inc.name, inc.lat, inc.lon, inc.when_utc, table, code, comment), (table, code, comment)


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
        login = f"user {self.call} pass {self.passcode} vers bucks-cad-aprs 1.0\r\n"
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
# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def run_cycle(args, report, live, aprs_is):
    incidents = parse_incidents(report.query(args.cats))   # raises on a bad response -> nothing is killed
    incidents = [i for i in incidents if i.category in args.cats]
    now = datetime.now(timezone.utc)
    if args.max_age_hours > 0:
        cutoff = now - timedelta(hours=args.max_age_hours)
        fresh = [i for i in incidents if i.when_utc >= cutoff]
        if len(fresh) < len(incidents):
            print(f"# {len(incidents) - len(fresh)} incident(s) older than {args.max_age_hours:g}h ignored",
                  file=sys.stderr)
        incidents = fresh
    assign_names(incidents)

    placed, out = {}, []
    for inc in incidents:
        pkt, sym = incident_packet(inc)
        placed[inc.name] = (inc.lat, inc.lon, sym)
        out.append(pkt)
    if aprs_overrides:
        aprs_overrides.apply(placed, out)

    gone = [n for n in live if n not in placed]
    for name in gone:
        lat, lon, (table, code, comment) = live[name]
        out.append(object_packet(name, lat, lon, now, table, code, comment, live=False))

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
    return len(placed), len(gone)


def main():
    global COMMENT_LIMIT
    ap = argparse.ArgumentParser(description="Bucks County 911 Incident Report -> APRS objects")
    ap.add_argument("--report-url", default=REPORT_URL, help="the public Power BI report link")
    ap.add_argument("--api-host", default=API_HOST, help="the report's data service host")
    ap.add_argument("--model-id", type=int, help="data model id (default: looked up from the report)")
    ap.add_argument("--categories", default="Fire,Traffic",
                    help="which incident tables to include: Fire, Traffic, EMS (default Fire,Traffic)")
    ap.add_argument("--max-age-hours", type=float, default=MAX_AGE_HOURS,
                    help=f"ignore (and kill) incidents dispatched longer ago than this (default {MAX_AGE_HOURS:g}; 0 disables)")
    ap.add_argument("--state-file", default=STATE_FILE)
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
    wanted = [c.strip().lower() for c in args.categories.split(",") if c.strip()]
    args.cats = [c for c in PREFIXES if c.lower() in wanted]
    if not args.cats or len(args.cats) != len(set(wanted)):
        ap.error("--categories must be chosen from Fire, Traffic, EMS")
    args.interval = max(60, args.interval)
    args.callsign = args.callsign.upper()
    if args.send and args.callsign.startswith("N0CALL"):
        ap.error("--send requires your own callsign (--callsign)")
    try:
        report = Report(args.report_url, args.api_host, args.model_id)
    except ValueError as e:
        ap.error(str(e))

    live = load_state(args.state_file, args.callsign)
    if live:
        print(f"# restored {len(live)} live object(s) from {args.state_file}", file=sys.stderr)
    aprs_is = AprsIS(args.callsign, args.passcode or aprs_passcode(args.callsign)) if args.send else None

    try:
        while True:
            try:
                n, killed = run_cycle(args, report, live, aprs_is)
                print(f"# {n} incident(s) placed, {killed} killed", file=sys.stderr)
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
