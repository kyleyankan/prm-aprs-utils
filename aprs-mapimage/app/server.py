#!/usr/bin/env python3
"""APRS map images: a small web service that draws pictures of what is on the air.

It listens to APRS-IS (receive only) around one or more areas, keeps what it hears
(stations, objects, items, their paths and recent messages) and draws:

  GET /map.png           full-colour picture            (800x480 by default)
  GET /eink.png          black-and-white 1-bit picture  (272x792 by default)
  GET /eink.bmp          the same as a 1-bit BMP
  GET /p/NAME.png        a saved preset (or /p/NAME.bmp)
  GET /                  the builder page: pick the options, see a preview, save presets
  GET /api/status        listener status (also used by the Docker health check)
  GET /api/aprs          what has been heard, as JSON
  GET/POST /api/presets  list / save presets;  DELETE /api/presets/NAME

Picture options (all optional): w h lat lon miles rotate zoom list names symbols paths
messages stations objects detail roads rivers rivermin towns labels weight dark invert gray.
See aprs_eink.py for what each one does.

Settings come from environment variables (see .env.example).  There is no login: run it
on a network you trust, or put it behind something that adds one.
"""
import base64
import hmac
import json
import math
import os
import re
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import aprs_eink
import aprs_listen
import tiles

VERSION = "1.0"
DATA = os.environ.get("DATA_DIR") or "/data"
STATE_FILE = "aprs_heard.json"
PRESET_FILE = "presets.json"
PRESET_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
PARAMS = ("w", "h", "lat", "lon", "miles", "rotate", "zoom", "list", "names", "symbols", "paths", "messages",
          "stations", "objects", "wx", "detail", "roads", "rivers", "rivermin", "towns", "labels", "water", "weight", "dark",
          "invert", "gray")
FORMATS = {"colour": ("/map.png", "colour"), "mono": ("/eink.png", "png"), "bmp": ("/eink.bmp", "bmp")}
CACHE_SECONDS = 15
MAX_LISTEN_MILES = 400

# Fixed sign-in for the builder page and the API (HTTP Basic).  No ADMIN_PASSWORD = no sign-in.
ADMIN_USER = (os.environ.get("ADMIN_USER") or "admin").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or ""
# Optional key for the picture addresses (?key=...), for displays that cannot sign in.
IMAGE_KEY = (os.environ.get("IMAGE_KEY") or "").strip()

LISTENER = BASEMAP = SYMBOLS = None
PRESETS_LOCK = threading.Lock()
CACHE, CACHE_LOCK = {}, threading.Lock()


def env_float(name, default):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        print(f"# {name}={os.environ.get(name)!r} is not a number; using {default}", file=sys.stderr)
        return default


def path(name):
    return os.path.join(DATA, name)


# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------
def load_presets():
    try:
        with open(path(PRESET_FILE)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_presets(presets):
    tmp = path(PRESET_FILE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(presets, f, indent=1)
    os.replace(tmp, path(PRESET_FILE))


def clean_params(raw):
    out = {}
    for k in PARAMS:
        v = raw.get(k)
        if v is None or v == "":
            continue
        v = str(v).strip()[:24]
        if re.fullmatch(r"-?\d+(\.\d+)?", v):
            out[k] = v
    f = follow_call(raw.get("follow"))
    if f:
        out["follow"] = f
    return out


# --------------------------------------------------------------------------
# Following a station: the picture is centred wherever it was last heard
# --------------------------------------------------------------------------
CALL_RE = re.compile(r"^[A-Z0-9]{1,7}(-[A-Z0-9]{1,2})?$")          # a station: callsign-SSID
NAME_RE = re.compile(r"^[A-Z0-9][A-Z0-9._/-]{0,8}$")                 # a station, object or item name
ADHOC = {}                     # CALL -> when a picture last asked to follow it (not from a preset)
ADHOC_HOURS, ADHOC_MAX, ADHOC_MILES = 24, 20, 25
FOLLOW_LOCK = threading.Lock()


def follow_call(v):
    v = str(v or "").strip().upper()
    return v if NAME_RE.match(v) else ""


# --------------------------------------------------------------------------
# Last known positions: what the listener hears now, this server's own history (every station and
# object it has heard, HISTORY_DAYS on disk), and optionally aprs.fi for anything it never heard.
# --------------------------------------------------------------------------
HISTORY_FILE = "last_seen.json"
HISTORY, HISTORY_LOCK = {}, threading.Lock()         # NAME -> {name, kind, lat, lon, heard, table, code, comment}
APRSFI_CACHE, APRSFI_LOCK = {}, threading.Lock()     # NAME -> (when asked, record or None)
APRSFI_BACKOFF = {"until": 0.0, "wait": 0.0}
STALE_SECONDS = 1800                                  # older than this: drawn and labelled as an old position


def keep(rec):
    return {"name": str(rec["name"]), "kind": rec.get("kind", "station"), "lat": float(rec["lat"]),
            "lon": float(rec["lon"]), "heard": float(rec["heard"]), "table": rec.get("table") or "/",
            "code": rec.get("code") or "/", "comment": str(rec.get("comment") or "")[:80]}


def record_history(recs=None):
    """Copy what the listener holds into the long-term store, and drop entries past HISTORY_DAYS."""
    if recs is None:
        recs = LISTENER.snapshot(objects=True, stations=True) if LISTENER else []
    cutoff = time.time() - env_float("HISTORY_DAYS", 30) * 86400
    with HISTORY_LOCK:
        for r in recs:
            k = str(r["name"]).upper()
            old = HISTORY.get(k)
            if not old or r["heard"] >= old["heard"]:
                HISTORY[k] = keep(r)
        for k in [k for k, v in HISTORY.items() if v["heard"] < cutoff]:
            del HISTORY[k]


def utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def aprsfi_lookup(name):
    """Last position aprs.fi knows for `name`, or None.  Only asked when a picture or the builder needs
    it, answers cached APRSFI_CACHE_MINUTES (default 30), backing off after failures (their API rules)."""
    key = (os.environ.get("APRSFI_KEY") or "").strip()
    if not key:
        return None
    now = time.time()
    ttl = max(10.0, env_float("APRSFI_CACHE_MINUTES", 30)) * 60
    with APRSFI_LOCK:
        hit = APRSFI_CACHE.get(name)
        if hit and now - hit[0] < ttl:
            return hit[1]
        if now < APRSFI_BACKOFF["until"]:
            return hit[1] if hit else None
    home = (os.environ.get("PUBLIC_URL") or "").strip() or "self-hosted"
    url = "https://api.aprs.fi/api/get?" + urllib.parse.urlencode({"name": name, "what": "loc", "apikey": key,
                                                                     "format": "json"})
    req = urllib.request.Request(url, headers={"User-Agent": f"aprs-mapimage/{VERSION} (+{home})"})
    rec = None
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        if data.get("result") != "ok":
            raise ValueError(data.get("description") or data.get("code") or "error")
        for e in data.get("entries") or []:
            if str(e.get("name", "")).upper() == name:
                sym = str(e.get("symbol") or "//") + "  "
                rec = keep({"name": e["name"], "lat": e["lat"], "lon": e["lng"],
                            "heard": float(e.get("lasttime") or e.get("time")),
                            "kind": {"o": "object", "i": "item"}.get(e.get("type"), "station"),
                            "table": sym[0], "code": sym[1], "comment": e.get("comment", "")})
                break
        with APRSFI_LOCK:
            APRSFI_CACHE[name] = (now, rec)
            APRSFI_BACKOFF.update(until=0.0, wait=0.0)
            while len(APRSFI_CACHE) > 500:
                del APRSFI_CACHE[min(APRSFI_CACHE, key=lambda k: APRSFI_CACHE[k][0])]
    except Exception as e:
        with APRSFI_LOCK:
            wait = min(3600.0, max(60.0, APRSFI_BACKOFF["wait"] * 2))
            APRSFI_BACKOFF.update(until=now + wait, wait=wait)
        print(f"# {utc()} aprs.fi lookup of {name} failed ({e}); not asking again for {wait / 60:.0f} min",
              file=sys.stderr, flush=True)
        return None
    return rec


def locate(name, remote=False):
    """(record, source) for where `name` was last heard; source = live, history or aprs.fi.
    remote=True may ask aprs.fi: only from a request a person (or their display) made."""
    rec = None
    if LISTENER:
        with LISTENER.lock:
            for r in LISTENER.heard.values():
                if str(r.get("name", "")).upper() == name and (rec is None or r["heard"] > rec["heard"]):
                    rec = dict(r)
    if rec:
        record_history([rec])
        return keep(rec), "live"
    with HISTORY_LOCK:
        hist = HISTORY.get(name)
    if remote:
        far = aprsfi_lookup(name)
        if far and (not hist or far["heard"] > hist["heard"]):
            return far, "aprs.fi"
    return (dict(hist), "history") if hist else (None, None)


def follow_position(call):
    """(lat, lon, heard) where `call` was last heard (live or this server's history), or None."""
    rec, _src = locate(call)
    return (rec["lat"], rec["lon"], rec["heard"]) if rec else None


def age_text(seconds):
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}M AGO"
    if seconds < 48 * 3600:
        return f"{int(seconds // 3600)}H AGO"
    return f"{int(seconds // 86400)}D AGO"


def follow_title(call, pos):
    if not pos:
        return f"{call} NOT HEARD"
    age = time.time() - pos[2]
    return call if age < STALE_SECONDS else f"{call} {age_text(age)}"


def note_follow(call):
    """A picture asked to follow `call`: listen for it (kept for ADHOC_HOURS after the last ask)."""
    with FOLLOW_LOCK:
        new = call not in ADHOC
        ADHOC[call] = time.time()
        while len(ADHOC) > ADHOC_MAX:
            del ADHOC[min(ADHOC, key=ADHOC.get)]
    if new:
        update_listen_areas()
        threading.Thread(target=plan_basemap, daemon=True).start()


def follow_targets():
    """{CALL: miles} for every followed station: presets (with their own view size) and recent asks."""
    out = {}
    for pre in load_presets().values():
        prm = pre.get("params") or {}
        if prm.get("follow"):
            out[prm["follow"]] = max(out.get(prm["follow"], 0), view_area(dict(prm, follow=""))[2])
    cutoff = time.time() - ADHOC_HOURS * 3600
    with FOLLOW_LOCK:
        for c in [c for c, t in ADHOC.items() if t < cutoff]:
            del ADHOC[c]
        for c in ADHOC:
            out[c] = max(out.get(c, 0), ADHOC_MILES)
    return out


def view_area(params):
    """(lat, lon, miles) circle that a picture with these options can show."""
    lat, lon = aprs_eink.home_position()
    lat = aprs_eink.number(params.get("lat"), lat, -89, 89)
    lon = aprs_eink.number(params.get("lon"), lon, -180, 180)
    pos = follow_position(params["follow"]) if params.get("follow") else None
    if pos:
        lat, lon = pos[0], pos[1]
    miles = aprs_eink.number(params.get("miles"), 15, 0.5, 2000)
    w = aprs_eink.number(params.get("w"), 800, 64, 2000)
    h = aprs_eink.number(params.get("h"), 480, 64, 2000)
    return lat, lon, min(MAX_LISTEN_MILES, miles * math.hypot(1, max(w, h) / min(w, h)) + 2)


def covered(area, by):
    a, b, r = area
    for c, d, s in by:
        if math.hypot((a - c) * 69.0, (b - d) * 69.172 * math.cos(math.radians(c))) + r <= s + 0.5:
            return True
    return False


def update_listen_areas():
    """Listen to every preset's area too (those not already inside another area)."""
    if not LISTENER:
        return
    base = [(LISTENER.lat, LISTENER.lon, LISTENER.radius)]
    areas = sorted({view_area(p.get("params") or {}) for p in load_presets().values()
                    if not (p.get("params") or {}).get("follow")}, key=lambda x: -x[2])   # followed: f/ filter
    keep = []
    for area in areas:
        if not covered(area, base + keep):
            keep.append(area)
    LISTENER.set_extra_areas(keep)
    LISTENER.set_follow(list(follow_targets().items()))


# --------------------------------------------------------------------------
# Base map: download the tiles we know we will draw before anyone asks
# --------------------------------------------------------------------------
def plan_basemap():
    if not BASEMAP:
        return
    hlat, hlon = aprs_eink.home_position()
    BASEMAP.want(tiles.tiles_around(hlat, hlon, view_area({})[2]))           # the builder's first view
    for pre in load_presets().values():
        BASEMAP.want(tiles.tiles_around(*view_area(pre.get("params") or {})))
    for call, miles in follow_targets().items():
        pos = follow_position(call)
        if pos:
            BASEMAP.want(tiles.tiles_around(pos[0], pos[1], miles))
    wide = min(150.0, env_float("PREFETCH_MILES", min(60.0, LISTENER.radius if LISTENER else 30.0)))
    if wide > 0:
        BASEMAP.want(tiles.tiles_around(hlat, hlon, wide))


def planner():
    while True:
        time.sleep(120)
        try:
            update_listen_areas()                 # also drops follow requests nobody has made for a day
            plan_basemap()                        # followed stations move: queue the tiles around them
        except Exception as e:
            print(f"# planner: {e}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Listener state on disk
# --------------------------------------------------------------------------
def save_state():
    if not LISTENER:
        return
    tmp = path(STATE_FILE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"saved": time.time(), "listener": LISTENER.state()}, f, separators=(",", ":"))
    os.replace(tmp, path(STATE_FILE))


def save_history():
    record_history()
    with HISTORY_LOCK:
        data = json.dumps({"saved": time.time(), "last_seen": HISTORY}, separators=(",", ":"))
    tmp = path(HISTORY_FILE) + ".tmp"
    with open(tmp, "w") as f:
        f.write(data)
    os.replace(tmp, path(HISTORY_FILE))


def load_history():
    try:
        with open(path(HISTORY_FILE)) as f:
            data = json.load(f).get("last_seen") or {}
    except (OSError, ValueError, AttributeError):
        return
    recs = []
    for v in data.values():
        try:
            recs.append(keep(v))
        except (KeyError, TypeError, ValueError):
            pass
    record_history(recs)
    print(f"# history: last positions of {len(HISTORY)} station(s)/object(s)", file=sys.stderr, flush=True)


def load_state():
    try:
        with open(path(STATE_FILE)) as f:
            data = json.load(f)
        age = time.time() - float(data["saved"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    n = LISTENER.restore(data.get("listener"))
    old = []                                             # followed positions saved by an earlier version
    for c, v in (data.get("follow") or {}).items():
        try:
            old.append(keep(dict(v, name=c)))
        except (KeyError, TypeError, ValueError):
            pass
    record_history(old)
    print(f"# restored {n} station(s)/object(s) saved {age / 60:.0f} min ago", file=sys.stderr, flush=True)
    return age


def saver():
    n = 0
    while True:
        time.sleep(60)
        n += 1
        try:
            save_state()
            record_history()
            if n % 5 == 0:                               # the history file is bigger: every 5 minutes
                save_history()
        except Exception as e:
            print(f"# could not save state: {e}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Pictures
# --------------------------------------------------------------------------
def objects_for(params):
    want_obj = str(params.get("objects", "1")) != "0"
    want_sta = str(params.get("stations", "1")) != "0"
    if not LISTENER or not (want_obj or want_sta):
        return []
    want_wx = str(params.get("wx", "0")) == "1"
    out = []
    for r in LISTENER.snapshot(objects=want_obj, stations=want_sta):
        if r["kind"] == "station" and r.get("code") == "_" and not want_wx:
            continue                                     # weather station (APRS weather symbol): hidden unless wx=1
        out.append({"name": r["name"], "lat": r["lat"], "lon": r["lon"], "comment": r.get("comment", ""),
                    "table": r.get("table"), "code": r.get("code"), "path": r.get("path") or [],
                    "source": "aprs" if r["kind"] != "station" else "station"})
    return out


def picture(params, fmt):
    call, old = params.get("follow"), None
    if call:
        note_follow(call)
        rec, src = locate(call, remote=True)
        pos = (rec["lat"], rec["lon"], rec["heard"]) if rec else None
        params = dict(params, title=follow_title(call, pos))
        if pos:
            params["lat"], params["lon"] = f"{pos[0]:.5f}", f"{pos[1]:.5f}"
            age = time.time() - pos[2]
            if age >= STALE_SECONDS or src != "live":     # an old position: drawn, and labelled as old
                via = " VIA APRS.FI" if src == "aprs.fi" else ""
                old = dict(rec, path=[], stale=True, age_tag=age_text(age),
                           source="station" if rec["kind"] == "station" else "aprs",
                           comment=f"OLD POSITION{via}: {rec['comment']}".rstrip(": "))
                params["_old"] = f"{rec['name']}|{int(pos[2])}|{src}"
    key = (fmt, tuple(sorted(params.items())))
    now = time.time()
    with CACHE_LOCK:
        hit = CACHE.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1], hit[2]
    msgs = LISTENER.latest_messages(12) if LISTENER else []
    objs = objects_for(params)
    if old:
        objs = [o for o in objs if str(o["name"]).upper() != call] + [old]
    data, ctype = aprs_eink.image(objs, params, BASEMAP, fmt, SYMBOLS, msgs)
    with CACHE_LOCK:
        for k in [k for k, v in CACHE.items() if now - v[0] >= CACHE_SECONDS]:
            del CACHE[k]
        CACHE[key] = (now, data, ctype)
    return data, ctype


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = f"aprs-mapimage/{VERSION}"

    def log_message(self, fmt, *a):
        if self.command != "GET" or os.environ.get("LOG_REQUESTS") == "1":
            sys.stderr.write("# %s %s\n" % (self.address_string(), fmt % a))

    def reply(self, code, payload, ctype="application/json", cache=False):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", f"max-age={CACHE_SECONDS}" if cache else "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def signed_in(self):
        """True when the request carries the right ADMIN_USER / ADMIN_PASSWORD."""
        if not ADMIN_PASSWORD:
            return False
        h = self.headers.get("Authorization", "")
        if not h.lower().startswith("basic "):
            return False
        try:
            user, _, pw = base64.b64decode(h[6:].strip()).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        ok_user = hmac.compare_digest(user.encode(), ADMIN_USER.encode())
        ok_pass = hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode())
        return ok_user and ok_pass

    def allowed(self):
        return not ADMIN_PASSWORD or self.signed_in()

    def picture_allowed(self):
        if not IMAGE_KEY:
            return True
        return hmac.compare_digest(self.query().get("key", "").encode(), IMAGE_KEY.encode()) or self.signed_in()

    def challenge(self):
        data = b"Sign in to use the APRS map image builder.\n"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="APRS map images", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def query(self):
        return {k: v[-1] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        try:
            if p == "/healthz":                          # Docker health check: always open, says nothing
                return self.reply(200, {"ok": True})
            picture_route = p in [r for r, _ in FORMATS.values()] or p.startswith("/p/")
            if picture_route and not self.picture_allowed():
                return self.reply(403, {"error": "this picture address needs ?key=IMAGE_KEY"})
            if not picture_route and not self.allowed():
                return self.challenge()
            if p in ("/", "/index.html"):
                return self.reply(200, page().encode(), "text/html; charset=utf-8")
            for fmt, (route, kind) in FORMATS.items():
                if p == route:
                    data, ctype = picture(clean_params(self.query()), kind)
                    return self.reply(200, data, ctype, cache=True)
            m = re.fullmatch(r"/p/([a-z0-9][a-z0-9_-]{0,31})\.(png|bmp)", p)
            if m:
                pre = load_presets().get(m.group(1))
                if not pre:
                    return self.reply(404, {"error": f"no preset called {m.group(1)}"})
                fmt = pre.get("fmt", "colour")
                kind = "bmp" if m.group(2) == "bmp" else FORMATS.get(fmt, FORMATS["colour"])[1]
                if kind == "bmp" and fmt == "colour":
                    kind = "bmp"
                data, ctype = picture(clean_params(dict(pre.get("params") or {}, **self.query())), kind)
                return self.reply(200, data, ctype, cache=True)
            if p == "/api/status":
                st = LISTENER.status() if LISTENER else {"connected": False, "error": "listener disabled"}
                hlat, hlon = aprs_eink.home_position()
                return self.reply(200, dict(st, home=[hlat, hlon], version=VERSION,
                                            callsign=LISTENER.callsign if LISTENER else "",
                                            basemap=BASEMAP.status() if BASEMAP else {},
                                            follow=sorted(follow_targets())))
            if p == "/api/follow":
                call = follow_call(self.query().get("call"))
                if not call:
                    return self.reply(400, {"error": "a callsign-SSID or object name, up to 9 characters (e.g. K3XYZ-9)"})
                note_follow(call)
                rec, src = locate(call, remote=True)
                return self.reply(200, {"call": call, "found": bool(rec), "source": src,
                                        "name": rec["name"] if rec else None,
                                        "lat": rec["lat"] if rec else None, "lon": rec["lon"] if rec else None,
                                        "age": round(time.time() - rec["heard"]) if rec else None,
                                        "aprsfi": bool(os.environ.get("APRSFI_KEY"))})
            if p == "/api/aprs":
                q = self.query()
                recs = LISTENER.snapshot(objects=q.get("objects", "1") != "0",
                                         stations=q.get("stations", "1") != "0") if LISTENER else []
                return self.reply(200, {"objects": recs, "messages": LISTENER.latest_messages(50) if LISTENER else []})
            if p == "/api/presets":
                return self.reply(200, load_presets())
            self.reply(404, {"error": "not found"})
        except BrokenPipeError:
            pass
        except Exception as e:
            print(f"# error serving {self.path}: {e}", file=sys.stderr, flush=True)
            self.reply(500, {"error": str(e)})

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
        if not isinstance(data, dict):
            raise ValueError
        return data

    def do_POST(self):
        if not self.allowed():
            return self.challenge()
        if urllib.parse.urlparse(self.path).path != "/api/presets":
            return self.reply(404, {"error": "not found"})
        try:
            b = self.body()
        except ValueError:
            return self.reply(400, {"error": "bad JSON"})
        name = str(b.get("name", "")).strip().lower()
        if not PRESET_NAME.match(name):
            return self.reply(400, {"error": "name: 1-32 characters, letters, digits, - and _"})
        fmt = b.get("fmt") if b.get("fmt") in FORMATS else "colour"
        with PRESETS_LOCK:
            presets = load_presets()
            presets[name] = {"fmt": fmt, "params": clean_params(b.get("params") or {}), "saved": time.time()}
            save_presets(presets)
        update_listen_areas()
        threading.Thread(target=plan_basemap, daemon=True).start()
        ext = "bmp" if fmt == "bmp" else "png"
        self.reply(200, {"ok": True, "url": f"/p/{name}.{ext}"})

    def do_DELETE(self):
        if not self.allowed():
            return self.challenge()
        m = re.fullmatch(r"/api/presets/([a-z0-9][a-z0-9_-]{0,31})", urllib.parse.urlparse(self.path).path)
        if not m:
            return self.reply(404, {"error": "not found"})
        with PRESETS_LOCK:
            presets = load_presets()
            gone = presets.pop(m.group(1), None)
            save_presets(presets)
        update_listen_areas()
        self.reply(200 if gone else 404, {"ok": bool(gone)})


def page():
    hlat, hlon = aprs_eink.home_position()
    return (PAGE.replace("__HOME__", json.dumps([hlat, hlon]))
            .replace("__LISTEN__", json.dumps(LISTENER.radius if LISTENER else 0))
            .replace("__KEY__", json.dumps(IMAGE_KEY).replace("<", "\\u003c")))


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>APRS map images</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>(function(){var t;try{t=localStorage.getItem("mapimg-theme")}catch(e){}
 if(!t)t=matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";document.documentElement.dataset.theme=t;})();</script>
<style>
:root{--bg:#f4f5f7;--panel:#fff;--ink:#1c2330;--mute:#667085;--line:#dfe3ea;--accent:#1f5fd0;--warn:#b54708;--warnbg:#fff4e5;--field:#fff;color-scheme:light}
:root[data-theme=dark]{--bg:#0f1318;--panel:#171c23;--ink:#e6e9ee;--mute:#98a2b3;--line:#2b333e;--accent:#5b9bff;--warn:#fdb022;--warnbg:#3a2a0a;--field:#10151b;color-scheme:dark}
:root[data-theme=dark] .leaflet-tile-pane{filter:invert(1) hue-rotate(180deg) brightness(.92) contrast(.9)}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%;font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:var(--ink);background:var(--bg)}
header{display:flex;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel)}
header h1{font-size:17px;margin:0;flex:1}
#status{font-size:12px;color:var(--mute)}
main{display:grid;grid-template-columns:330px minmax(0,1fr) minmax(0,1fr);gap:14px;padding:14px 16px;align-items:start}
@media (max-width:1100px){main{grid-template-columns:330px minmax(0,1fr)}#previewCol{grid-column:1/-1}}
@media (max-width:720px){main{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mute);margin:14px 0 6px}
.card h2:first-child{margin-top:0}
label{display:block;font-size:12px;color:var(--mute);margin-top:6px}
input,select{width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:6px;font:inherit;background:var(--field);color:var(--ink)}
input[type=checkbox]{width:auto;margin:0 6px 0 0;vertical-align:-2px}
.row{display:flex;gap:8px}.row>*{flex:1;min-width:0}
.checks{display:grid;grid-template-columns:1fr 1fr;gap:2px 10px;margin-top:4px}
.checks label{margin:0;color:var(--ink);font-size:13px;padding:2px 0}
button{font:inherit;padding:6px 12px;border-radius:6px;border:1px solid var(--line);background:var(--field);color:var(--ink);cursor:pointer}
button.pri{background:var(--accent);border-color:var(--accent);color:#fff}
.seg{display:flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.seg button{flex:1;border:0;border-radius:0;border-right:1px solid var(--line);padding:6px 4px}
.seg button:last-child{border-right:0}
.seg button.on{background:var(--accent);color:#fff}
#map{height:420px;border-radius:10px;border:1px solid var(--line)}
.hint{font-size:12px;color:var(--mute);margin-top:6px}
.warn{display:none;margin-top:8px;padding:8px 10px;border-radius:6px;background:var(--warnbg);color:var(--warn);font-size:13px}
#previewBox{display:flex;justify-content:center;align-items:flex-start;overflow:auto;max-height:78vh;background:repeating-conic-gradient(var(--line) 0 25%,transparent 0 50%) 0 0/16px 16px;border-radius:8px;padding:10px}
#preview{max-width:100%;image-rendering:pixelated;box-shadow:0 1px 6px rgba(0,0,0,.25);background:#fff}
#url{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.presets .p{display:flex;align-items:center;gap:6px;padding:6px 0;border-top:1px solid var(--line)}
.presets .p b{flex:1;font-family:ui-monospace,Menlo,Consolas,monospace;font-weight:600;overflow:hidden;text-overflow:ellipsis}
.presets .p button{padding:3px 8px;font-size:12px}
a{color:var(--accent)}
#toast{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);background:#1c2330;color:#fff;padding:9px 14px;border-radius:8px;display:none;z-index:2000}
</style></head><body>
<header><h1>APRS map images</h1><span id="status">connecting…</span><button id="theme" onclick="flipTheme()">Dark</button></header>
<main>
 <div class="card" id="opts">
  <h2>Picture</h2>
  <div class="seg" id="fmt"><button data-v="colour" class="on">Colour</button><button data-v="mono">Black &amp; white</button><button data-v="bmp">B&amp;W BMP</button></div>
  <label>Size</label>
  <select id="size">
   <option value="800x480">800 × 480 (7.5" e-paper, small screens)</option>
   <option value="480x800">480 × 800 (portrait)</option>
   <option value="272x792">272 × 792 (5.79" e-paper, portrait)</option>
   <option value="792x272">792 × 272 (5.79" e-paper, landscape)</option>
   <option value="400x300">400 × 300 (4.2" e-paper)</option>
   <option value="296x128">296 × 128 (2.9" e-paper)</option>
   <option value="1024x600">1024 × 600</option>
   <option value="1280x720">1280 × 720</option>
   <option value="custom">Custom…</option>
  </select>
  <div class="row" id="custom" style="display:none"><div><label>Width</label><input id="w" type="number" min="64" max="2000" value="800"></div><div><label>Height</label><input id="h" type="number" min="64" max="2000" value="480"></div></div>
  <div class="row"><div><label>Rotate</label><select id="rotate"><option>0</option><option>90</option><option>180</option><option>270</option></select></div>
   <div><label>Draw larger</label><select id="zoom"><option value="1">1×</option><option value="2">2×</option><option value="3">3×</option><option value="4">4×</option></select></div></div>

  <h2>Area</h2>
  <div class="row"><div><label>Latitude</label><input id="lat" inputmode="decimal"></div><div><label>Longitude</label><input id="lon" inputmode="decimal"></div></div>
  <label>Radius: <span id="milesv"></span> miles (centre to the nearer edge)</label><input id="miles" type="range" min="1" max="150" step="1" value="15">
  <label>Follow a station or object (callsign-SSID or object name, optional)</label><input id="follow" placeholder="e.g. K3XYZ-9" maxlength="9" autocomplete="off" spellcheck="false" style="text-transform:uppercase">
  <div class="hint" id="followinfo"></div>
  <div class="hint" id="centrehint">Click the map, or drag the marker, to set the centre. <a href="#" onclick="home();return false">Back to home</a></div>
  <div class="warn" id="outside">This area is outside what the server listens to, so it may look empty. Save it as a preset and the server starts listening there too.</div>

  <h2>Show</h2>
  <div class="checks">
   <label><input type="checkbox" id="stations" checked>Stations</label>
   <label><input type="checkbox" id="objects" checked>Objects &amp; items</label>
   <label><input type="checkbox" id="wx">Weather stations</label>
   <label><input type="checkbox" id="paths" checked>Paths</label>
   <label><input type="checkbox" id="symbols" checked>APRS symbols</label>
   <label><input type="checkbox" id="names" checked>Callsign labels</label>
   <label><input type="checkbox" id="list" checked>List of objects</label>
  </div>
  <label>Latest messages in the list</label><select id="messages"><option value="0">None</option><option value="2">2</option><option value="4" selected>4</option><option value="6">6</option><option value="8">8</option><option value="12">12</option></select>

  <h2>Base map</h2>
  <div class="row"><div><label>Detail</label><select id="detail"><option value="2">Roads, rivers, towns</option><option value="1">Borders only</option><option value="0">None</option></select></div>
   <div><label>Roads</label><select id="roads"><option value="3">Highways + main roads</option><option value="2">Highways</option><option value="1">Interstates only</option><option value="0">None</option></select></div></div>
  <div class="row"><div><label>Rivers</label><select id="rivers"><option value="1">Major</option><option value="2">All</option><option value="0">None</option></select></div>
   <div><label>Lines</label><select id="weight"><option value="1">Thin</option><option value="2">Bold</option></select></div></div>
  <div class="checks"><label><input type="checkbox" id="towns" checked>Town names</label><label><input type="checkbox" id="labels" checked>Road numbers</label>
   <label><input type="checkbox" id="water" checked>Lakes, bays &amp; sea</label></div>

  <h2>Look</h2>
  <div class="checks">
   <label class="c-only"><input type="checkbox" id="dark">Dark background</label>
   <label class="m-only"><input type="checkbox" id="invert">White on black</label>
   <label class="m-only"><input type="checkbox" id="gray">8-bit grey PNG</label>
  </div>
 </div>

 <div>
  <div id="map"></div>
  <div class="card presets" style="margin-top:14px">
   <h2>Presets</h2>
   <div class="hint">A preset keeps these settings at a fixed address for a display to load, and makes the server listen to its area.</div>
   <div class="row" style="margin-top:8px"><input id="pname" placeholder="name, e.g. kitchen-eink" maxlength="32"><button class="pri" style="flex:0 0 auto" onclick="savePreset()">Save</button></div>
   <div id="plist"></div>
  </div>
 </div>

 <div class="card" id="previewCol">
  <h2>Preview <small id="pinfo" style="text-transform:none;letter-spacing:0"></small></h2>
  <div id="previewBox"><img id="preview" alt="Map preview"></div>
  <label>Picture address</label>
  <div class="row"><input id="url" readonly><button style="flex:0 0 auto" onclick="copyUrl()">Copy</button><a id="open" target="_blank" rel="noopener" style="flex:0 0 auto;align-self:center">Open</a></div>
 </div>
</main>
<div id="toast"></div>
<script>
const HOME=__HOME__, LISTEN=__LISTEN__, KEY=__KEY__;
const withKey=u=>KEY?u+(u.includes("?")?"&":"?")+"key="+encodeURIComponent(KEY):u;
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let fmt="colour", areas=[];
function toast(t){const e=$("toast");e.textContent=t;e.style.display="block";clearTimeout(toast.t);toast.t=setTimeout(()=>e.style.display="none",3500);}
function setThemeLabel(){$("theme").textContent=document.documentElement.dataset.theme==="dark"?"Light":"Dark";}
function flipTheme(){const t=document.documentElement.dataset.theme==="dark"?"light":"dark";document.documentElement.dataset.theme=t;try{localStorage.setItem("mapimg-theme",t)}catch(e){} setThemeLabel();}
setThemeLabel();

const map=L.map("map").setView(HOME,9);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:19,attribution:"&copy; OpenStreetMap contributors"}).addTo(map);
const centre=L.marker(HOME,{draggable:true}).addTo(map);
const followDot=L.circleMarker(HOME,{radius:8,color:"#d92d20",weight:3,fillOpacity:.5});
const CALL=/^[A-Z0-9][A-Z0-9._\/-]{0,8}$/; let followPos=null, followFor="";
const circle=L.circle(HOME,{radius:15*1609.344,color:"#1f5fd0",weight:2,fillOpacity:.06,interactive:false}).addTo(map);
let listenLayer=L.layerGroup().addTo(map);
map.on("click",e=>{setCentre(e.latlng.lat,e.latlng.lng);});
centre.on("dragend",()=>{const p=centre.getLatLng(); setCentre(p.lat,p.lng);});
function setCentre(lat,lon){$("lat").value=lat.toFixed(4); $("lon").value=lon.toFixed(4); update();}
function home(){setCentre(HOME[0],HOME[1]); map.setView(HOME,9);}

function size(){ if($("size").value==="custom") return [$("w").value,$("h").value]; return $("size").value.split("x"); }
function params(){
  const [w,h]=size(), p={w,h,lat:$("lat").value,lon:$("lon").value,miles:$("miles").value};
  const def={rotate:"0",zoom:"1",detail:"2",roads:fmt==="colour"?"3":"2",rivers:"1",weight:"1",messages:fmt==="colour"?"4":"0"};
  for(const k of ["rotate","zoom","detail","roads","rivers","weight","messages"]) if($(k).value!==def[k]) p[k]=$(k).value;
  const box={stations:true,objects:true,paths:true,symbols:fmt==="colour",names:fmt==="colour",list:true,towns:true,labels:true,water:true,wx:false};
  for(const k of Object.keys(box)) if($(k).checked!==box[k]) p[k]=$(k).checked?"1":"0";
  const f=followCall(); if(f) p.follow=f;
  if(fmt==="colour"&&$("dark").checked) p.dark="1";
  if(fmt!=="colour"&&$("invert").checked) p.invert="1";
  if(fmt==="mono"&&$("gray").checked) p.gray="1";
  return p;
}
function route(){return fmt==="colour"?"/map.png":fmt==="mono"?"/eink.png":"/eink.bmp";}
function address(){return withKey(location.origin+route()+"?"+new URLSearchParams(params()).toString());}
let timer=null;
function update(){
  $("custom").style.display=$("size").value==="custom"?"flex":"none";
  $("milesv").textContent=$("miles").value;
  for(const e of document.querySelectorAll(".c-only")) e.style.display=fmt==="colour"?"":"none";
  for(const e of document.querySelectorAll(".m-only")) e.style.display=fmt==="colour"?"none":"";
  for(const b of document.querySelectorAll("#fmt button")) b.classList.toggle("on",b.dataset.v===fmt);
  const lat=parseFloat($("lat").value), lon=parseFloat($("lon").value), mi=parseFloat($("miles").value);
  const f=followCall();
  if(isFinite(lat)&&isFinite(lon)){centre.setLatLng([lat,lon]); circle.setLatLng([lat,lon]);} circle.setRadius(mi*1609.344);
  if(f&&followPos&&followFor===f){ circle.setLatLng(followPos); followDot.setLatLng(followPos).addTo(map); } else map.removeLayer(followDot);
  $("centrehint").firstChild.textContent=f?"If the station has never been heard, the picture is centred here instead (click the map or drag the marker to move it). ":"Click the map, or drag the marker, to set the centre. ";
  if(f!==followFor) checkFollow();
  const [w,h]=size().map(Number), reach=Math.min(400, mi*Math.hypot(1,Math.max(w,h)/Math.min(w,h))+2);
  const covered=areas.some(([a,b,r])=>dist(a,b,lat,lon)+reach<=r+0.5);
  $("outside").style.display=areas.length&&!covered&&!f?"block":"none";
  const u=address(); $("url").value=u; $("open").href=u;
  clearTimeout(timer); timer=setTimeout(()=>{ $("pinfo").textContent="loading…"; $("preview").src=u.replace(location.origin,"")+"&_="+Date.now(); },400);
  try{localStorage.setItem("mapimg-last",JSON.stringify({fmt,size:$("size").value,p:params()}));}catch(e){}
}
$("preview").onload=()=>{const [w,h]=size(); const r=$("rotate").value; const dims=(r==="90"||r==="270")?`${h} × ${w}`:`${w} × ${h}`; $("pinfo").textContent=`· ${dims} px`;};
$("preview").onerror=()=>{$("pinfo").textContent="· could not draw this picture";};
function followCall(){const v=$("follow").value.trim().toUpperCase(); return CALL.test(v)?v:"";}
let followTimer=null;
function checkFollow(){
  const f=followCall(); followFor=f; followPos=null; clearTimeout(followTimer);
  const raw=$("follow").value.trim();
  if(!f){ $("followinfo").textContent=raw?"Use a callsign-SSID (e.g. K3XYZ-9) or an object name, up to 9 characters.":""; return; }
  $("followinfo").textContent=`Looking for ${f}…`;
  followTimer=setTimeout(async()=>{
    try{ const j=await (await fetch("/api/follow?call="+encodeURIComponent(f))).json(); if(followCall()!==f) return;
      if(j.found){ followPos=[j.lat,j.lon];
        const a=j.age<90?"just now":j.age<5400?`${Math.round(j.age/60)} min ago`:j.age<172800?`${Math.round(j.age/3600)} h ago`:`${Math.round(j.age/86400)} days ago`;
        const from=j.source==="aprs.fi"?` (position from <a href="https://aprs.fi/#!call=a%2F${encodeURIComponent(j.name)}" target="_blank" rel="noopener">aprs.fi</a>)`:j.source==="history"?" (from this server's history)":"";
        const old=j.age>=1800?" It is an old position, and the picture marks it as one.":" The picture moves with it.";
        $("followinfo").innerHTML=`Following ${esc(f)}: last heard ${a} at ${j.lat.toFixed(4)}, ${j.lon.toFixed(4)}${from}.${old}`;
        if(window._centredOn!==f){ map.setView(followPos,9); window._centredOn=f; } }
      else $("followinfo").textContent=`${f} has not been heard${j.aprsfi?", here or on aprs.fi":" by this server"}. The server is listening for it now; the picture centres on it as soon as it is heard.`;
      update(); followFor=f;
    }catch(e){ $("followinfo").textContent="Could not ask the server about that station."; }
  },500);
}
setInterval(()=>{ if(followCall()){ followFor=""; update(); } },60000);   // keep up with a moving station
function dist(a,b,c,d){const dy=(c-a)*69.0, dx=(d-b)*69.172*Math.cos(a*Math.PI/180); return Math.hypot(dx,dy);}
for(const el of document.querySelectorAll("#opts input,#opts select")) el.addEventListener(el.type==="range"?"input":"change",update);
let typing=null; $("follow").addEventListener("input",()=>{clearTimeout(typing); typing=setTimeout(update,700);});
// colour pictures and e-paper pictures start from different defaults; switching between them resets those few
for(const b of document.querySelectorAll("#fmt button")) b.onclick=()=>{
  const wasColour=fmt==="colour"; fmt=b.dataset.v;
  if(wasColour!==(fmt==="colour")){ const c=fmt==="colour";
    $("roads").value=c?"3":"2"; $("messages").value=c?"4":"0"; $("symbols").checked=c; $("names").checked=c; }
  update();};
function copyUrl(){ $("url").select(); (navigator.clipboard?navigator.clipboard.writeText($("url").value):Promise.reject()).then(()=>toast("Address copied"),()=>{document.execCommand("copy");toast("Address copied");}); }

function apply(f,sz,p){
  fmt=f||"colour";
  const s=sz||(p.w&&p.h?`${p.w}x${p.h}`:"800x480");
  const opt=[...$("size").options].some(o=>o.value===s); $("size").value=opt?s:"custom";
  if(p.w){$("w").value=p.w;} if(p.h){$("h").value=p.h;}
  $("lat").value=p.lat??HOME[0].toFixed(4); $("lon").value=p.lon??HOME[1].toFixed(4); $("miles").value=p.miles??15;
  const def={rotate:"0",zoom:"1",detail:"2",roads:fmt==="colour"?"3":"2",rivers:"1",weight:"1",messages:fmt==="colour"?"4":"0"};
  for(const k of Object.keys(def)) $(k).value=p[k]??def[k];
  const box={stations:true,objects:true,paths:true,symbols:fmt==="colour",names:fmt==="colour",list:true,towns:true,labels:true,water:true,wx:false};
  for(const k of Object.keys(box)) $(k).checked=p[k]===undefined?box[k]:p[k]==="1";
  $("dark").checked=p.dark==="1"; $("invert").checked=p.invert==="1"; $("gray").checked=p.gray==="1";
  $("follow").value=p.follow||"";
  update(); map.setView([parseFloat($("lat").value),parseFloat($("lon").value)],9);
}
async function loadPresets(){
  const r=await fetch("/api/presets"); const ps=await r.json(); const names=Object.keys(ps).sort();
  $("plist").innerHTML=names.map(n=>{const p=ps[n], ext=p.fmt==="bmp"?"bmp":"png", u=withKey(`/p/${n}.${ext}`);
    return `<div class="p"><b title="${esc(n)}">${esc(n)}</b><a href="${esc(u)}" target="_blank" rel="noopener">/p/${esc(n)}.${ext}</a>
    <button onclick='loadPreset(${JSON.stringify(n)})'>Load</button><button onclick='delPreset(${JSON.stringify(n)})'>Delete</button></div>`;}).join("")||'<div class="hint">No presets saved yet.</div>';
  window._presets=ps;
}
function loadPreset(n){const p=window._presets[n]; if(!p) return; $("pname").value=n; apply(p.fmt,null,p.params||{}); toast(`Loaded ${n}`);}
async function savePreset(){
  const name=$("pname").value.trim().toLowerCase();
  if(!/^[a-z0-9][a-z0-9_-]{0,31}$/.test(name)){toast("Name: letters, digits, - and _ (up to 32)"); return;}
  const r=await fetch("/api/presets",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,fmt,params:params()})});
  const j=await r.json(); if(!r.ok){toast(j.error||"Could not save"); return;}
  toast(`Saved: ${withKey(location.origin+j.url)}`); loadPresets(); setTimeout(status,1500);
}
async function delPreset(n){ if(!confirm(`Delete preset ${n}? Displays using its address will stop updating.`)) return;
  await fetch("/api/presets/"+encodeURIComponent(n),{method:"DELETE"}); loadPresets(); setTimeout(status,1500); }
async function status(){
  try{const s=await (await fetch("/api/status")).json(); areas=s.areas||[];
    const b=s.basemap||{}, bm=(b.osm===false&&b.county_lines===false)?" · base map off":b.tiles_waiting?` · base map: ${b.tiles_ready} tiles ready, ${b.tiles_waiting} downloading`:` · base map: ${b.tiles_ready??0} tiles ready`;
    $("status").textContent=`${s.connected?"listening":"not connected"} · ${s.stations_heard??0} stations, ${s.objects_heard??0} objects heard · ${areas.length} area(s)`+bm;
    listenLayer.clearLayers(); for(const [a,b,r] of areas) L.circle([a,b],{radius:r*1609.344,color:"#868e96",weight:1,dashArray:"4 6",fill:false,interactive:false}).addTo(listenLayer);
    update();
  }catch(e){$("status").textContent="server not reachable";}
}
let last=null; try{last=JSON.parse(localStorage.getItem("mapimg-last"));}catch(e){}
apply(last?last.fmt:"colour", last?last.size:null, last?last.p:{lat:HOME[0].toFixed(4),lon:HOME[1].toFixed(4)});
loadPresets(); status(); setInterval(status,30000);
</script></body></html>
"""


def main():
    global LISTENER, BASEMAP, SYMBOLS
    os.makedirs(DATA, exist_ok=True)
    os.environ.setdefault("APRS_DATA_DIR", DATA)
    hlat, hlon = aprs_eink.home_position()
    call = (os.environ.get("CALLSIGN") or "N0CALL").strip().upper()
    if os.environ.get("APRS_LISTEN", "1") != "0":
        LISTENER = aprs_listen.Listener(call, hlat, hlon, max(1.0, env_float("LISTEN_MILES", 50)),
                                        max_age=max(300, env_float("FORGET_MINUTES", 90) * 60), own="-",
                                        login_pass=-1,             # receive only: no passcode needed
                                        host=os.environ.get("APRS_IS_HOST") or aprs_listen.HOST,
                                        port=int(env_float("APRS_IS_PORT", aprs_listen.PORT)),
                                        trail_minutes=max(5, env_float("TRAIL_MINUTES", 180)))
        LISTENER.vers = f"aprs-mapimage {VERSION}"
        if "-" not in call:
            print(f"# NOTE: CALLSIGN={call} has no SSID.  APRS-IS servers hold back packets *from* the callsign a\n"
                  f"#   client logs in with, so if your own programs send objects as {call}, this server will not\n"
                  f"#   see them.  Give this server its own SSID, e.g. CALLSIGN={call}-13 (receive only, no passcode).",
                  file=sys.stderr, flush=True)
        update_listen_areas()
        age = load_state()
        load_history()
        LISTENER.start()
        if (age is None or age > 1800) and os.environ.get("APRS_BACKFILL", "1") != "0":
            threading.Thread(target=LISTENER.backfill, daemon=True, kwargs={
                "host": os.environ.get("APRS_HISTORY_HOST") or aprs_listen.HISTORY_HOST,
                "port": int(env_float("APRS_HISTORY_PORT", aprs_listen.HISTORY_PORT))}).start()
        threading.Thread(target=saver, daemon=True).start()
    BASEMAP = tiles.TiledBasemap(DATA, os.environ.get("NWS_CONTACT", ""))
    SYMBOLS = aprs_eink.Symbols(DATA)
    update_listen_areas()
    plan_basemap()
    threading.Thread(target=planner, daemon=True).start()

    def stop(_sig, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    port = int(env_float("PORT", 8080))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    print(f"# APRS map images {VERSION} on port {port}; home {hlat:.4f},{hlon:.4f}; callsign {call} (receive only)",
          file=sys.stderr, flush=True)
    print(f"# builder page: " + (f"sign-in required (user {ADMIN_USER})" if ADMIN_PASSWORD else
          "open to anyone who can reach it (set ADMIN_PASSWORD to require a sign-in)") +
          "; pictures: " + ("need ?key=IMAGE_KEY or the sign-in" if IMAGE_KEY else "open"), file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            save_state()
            save_history()
            print("# state saved; stopping", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"# could not save state: {e}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
