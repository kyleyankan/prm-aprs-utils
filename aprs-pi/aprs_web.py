#!/usr/bin/env python3
"""Web map of every object the APRS feed scripts currently have live, with edit/delete.

  python3 aprs_web.py --port 8080              # then open http://<this machine>:8080/

It reads the scripts' *_live_state.json files from the data folder ($APRS_DATA_DIR,
else the current folder) and shows each object with the source it came from.

Edit (text, position) and Delete are stored in overrides.json, which every feed
script applies on its next poll, so a correction sticks even though the source
still reports the original.  With --send and --callsign the corrected object (or
the kill) is ALSO handed to aprs_send.py immediately, so the APRS map changes right away.
"Restore" removes the correction and the source's own data returns on the next poll.

"New object" creates your own object (name, symbol, text, position, optional expiry).
Those are kept in custom_live_state.json and, with --send, handed to aprs_send.py at once
and again every --custom-interval seconds until you delete them or they expire; aprs_send.py
sends them when they are new or changed and re-sends them every 26 minutes.
This program never logs in to APRS-IS to send: aprs_send.py holds the one sending connection.

GET /api/aprs returns everything ELSE heard on APRS-IS around your home position
(other people's objects and items; add ?stations=1 for stations too).  The web map
shows it as a layer you can switch off, along with the path each moving object or
station has taken (kept for --trail-minutes, default 180), and /eink-all.png adds it to the e-ink
picture.  The web map and /map.png draw each object with its APRS symbol (aprs.fi symbol set),
falling back to plain markers if the symbol pictures cannot be loaded.
/map.png (and /map-all.png) is the same picture in full colour, 800x480
by default.  The listener also keeps the latest text messages people send each other; they are
in /api/aprs ("messages"), in a section of the web page and on /map.png.
What has been heard, all paths and those messages are saved in aprs_heard.json every minute and
at shutdown, and loaded again at start.  Starting without a saved state from the
last 30 minutes, it also tries once to fetch recent history (see aprs_listen.py).
The listener only receives; set APRS_LISTEN=0 (or --no-listen) to disable it.

Every object is rated critical, high, medium or low (aprs_priority.py).  The page shows the level
of each object and a count of each level, and its "Send to APRS-IS" choice sets the minimum level
aprs_send.py sends (POST /api/settings {"min_level": "medium"}, kept in sender_settings.json).

GET /api/packets?lat=..&lon=..&miles=25 lists our own object packets within that distance of a
point, for a radio in that area to transmit (see README.txt, "Packets for radios").

Every station that asks /api/packets is remembered for 7 days (api_clients.json): the callsign it
gave, the point and radius it asked for, and when.  The page draws them as a "Radio gateways" layer,
a label at the point and a circle of the radius; GET /api/clients is the same as JSON.

Behind a reverse proxy only /api/packets answers: a request the proxy passed on (it carries
X-Forwarded-For) gets 403 for the page, the editor and every other address, because the page has no
login.  --proxied all (or WEB_PROXIED=all) lifts that; use it only if the proxy asks for a login.

GET /eink.png (or /eink.bmp) returns a black-and-white picture of the map and object
list for an e-ink display, 272x792 by default; see aprs_eink.py for the options.

There is NO login: run it on a trusted internal network only.
"""
import argparse
import glob
import json
import math
import os
import re
import signal
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import aprs_overrides as ov
import aprs_priority               # how important each object is, and the minimum level for sending
import aprs_send                   # the one program that sends to APRS-IS; this one only hands packets over

try:
    import aprs_eink                 # /eink.png for e-ink displays (optional file)
except ImportError:
    aprs_eink = None
try:
    import aprs_listen               # everything else heard on APRS-IS nearby (optional file)
except ImportError:
    aprs_listen = None

PRUNE_HOURS = 72           # forget corrections this long after their object is gone

# state file -> (key, label, link to the original data)
SOURCES = {
    "aprs_live_state.json":   ("montco", "Montgomery County CAD", "https://webapp07.montcopa.org/eoc/cadinfo/livecad.asp"),
    "montco_live_state.json": ("montco", "Montgomery County CAD", "https://webapp07.montcopa.org/eoc/cadinfo/livecad.asp"),
    "chesco_live_state.json": ("chesco", "Chester County WebCAD", "https://webcad.chesco.org/WebCad/webcad.asp"),
    "bucks_live_state.json":  ("bucks", "Bucks County 911 Incident Report", "https://www.buckscounty.gov/2150/911-Incident-Report"),
    "nj511_live_state.json":  ("nj511", "511NJ (NJ traffic)", "https://511nj.org/"),
    "njta_live_state.json":   ("njta", "NJ Turnpike Authority alerts", "https://www.njta.gov/"),
    "pa511_live_state.json":  ("pa511", "PennDOT 511PA (RCRS)", "https://www.511pa.com/"),
    "nws_live_state.json":    ("nws", "National Weather Service", "https://www.weather.gov/"),
    "custom_live_state.json": ("custom", "Created here (manual)", ""),
}
CUSTOM_FILE = "custom_live_state.json"
NAME_OK = re.compile(r"^[A-Z0-9][A-Z0-9_.\-]{2,8}$")
NHC = ("nhc", "National Hurricane Center", "https://www.nhc.noaa.gov/")
LOCK = threading.Lock()
BASEMAP = None
LISTENER = None
SYMBOLS = None             # APRS symbol pictures for /map.png
OWN_TRAILS = None          # where our own objects have been (tracked storms move)
HEARD_FILE = "aprs_heard.json"    # what the listener has heard + all paths, kept across restarts
ARGS = None


def data_dir():
    return os.environ.get("APRS_DATA_DIR") or "."


def read_states():
    """All live objects from the scripts' state files, plus per-source info."""
    objects, sources = {}, {}
    for f in sorted(glob.glob(os.path.join(data_dir(), "*_live_state.json"))):
        base = os.path.basename(f)
        key, label, url = SOURCES.get(base, (base.replace("_live_state.json", ""), base, ""))
        try:
            with open(f) as fh:
                raw = json.load(fh)
            mtime = os.path.getmtime(f)
            objs = raw.get("objects", {})
        except (OSError, ValueError, AttributeError):
            continue
        sources.setdefault(key, {"key": key, "label": label, "url": url, "updated": mtime, "count": 0})
        for name, o in objs.items():
            try:
                rec = {"name": name, "lat": float(o["lat"]), "lon": float(o["lon"]), "table": o.get("table", "/"),
                       "code": o.get("code", "."), "comment": str(o.get("comment", "")),
                       "source": key, "source_label": label, "source_url": url, "updated": mtime,
                       "custom": key == "custom", "expires": o.get("expires", 0) if key == "custom" else 0}
            except (KeyError, TypeError, ValueError):
                continue
            if key == "nws" and len(name) > 3 and name[2] == "-":          # tropical cyclones come from NHC
                rec.update(source=NHC[0], source_label=NHC[1], source_url=NHC[2])
                sources.setdefault(NHC[0], {"key": NHC[0], "label": NHC[1], "url": NHC[2], "updated": mtime, "count": 0})
            objects[name] = rec
    return objects, sources


def service_status():
    run = os.environ.get("APRS_RUN_DIR")
    out = []
    if not run:
        return out
    for f in sorted(glob.glob(os.path.join(run, "*.state"))):
        d = {"service": os.path.basename(f)[:-6]}
        try:
            with open(f) as fh:
                for line in fh:
                    k, _, v = line.strip().partition("=")
                    if k in ("state", "note", "restarts"):
                        d[k] = v
        except OSError:
            continue
        out.append(d)
    return out


def snapshot():
    objects, sources = read_states()
    overrides = ov.load()
    times = aprs_send.send_times() if ARGS.send else {}        # when each was last sent to APRS-IS
    live, suppressed = [], []
    now, held, levels = time.time(), 0, {lv: 0 for lv in aprs_priority.LEVELS}
    for name, rec in objects.items():
        rec["aprs_is"] = times.get(name.strip())
        e = overrides.get(name)
        if isinstance(e, dict) and e.get("deleted"):
            continue                                   # deleted here; the script kills it on its next poll
        rec["edited"] = False
        if isinstance(e, dict):
            orig = {"lat": rec["lat"], "lon": rec["lon"], "comment": rec["comment"]}
            if isinstance(e.get("orig"), dict):
                orig.update({k: e["orig"][k] for k in ("lat", "lon", "comment") if k in e["orig"]})
            lat, lon, (_t, _c, comment) = ov.merged(e, rec["lat"], rec["lon"], (rec["table"], rec["code"], rec["comment"]))
            rec.update(lat=lat, lon=lon, comment=comment, edited=True, orig=orig)
        rec["path"] = OWN_TRAILS.get(name) if OWN_TRAILS else []
        rec["shape"] = ov.decode_shape(rec["lat"], rec["lon"], rec["comment"])     # outline carried in the text
        rec["comment"] = ov.split_shape(rec["comment"])[0]
        t = rec["aprs_is"] or {}
        age = (now - t["changed"]) / 60 if t.get("changed") else None
        rec.update(aprs_priority.rate(name, rec["comment"], rec["source"], age))      # level, score, reasons, rated
        levels[rec["level"]] += 1
        held += bool(t.get("held"))
        live.append(rec)
        if rec["source"] in sources:
            sources[rec["source"]]["count"] += 1
    for name, e in overrides.items():
        if isinstance(e, dict) and e.get("deleted"):
            o = e.get("orig") if isinstance(e.get("orig"), dict) else {}
            suppressed.append({"name": name, "comment": o.get("comment", ""), "source_label": o.get("source_label", ""),
                               "ts": e.get("ts", 0)})
    live.sort(key=lambda r: r["name"])
    return {"objects": live, "suppressed": sorted(suppressed, key=lambda r: -r["ts"]),
            "sources": sorted(sources.values(), key=lambda s: s["label"]), "services": service_status(),
            "sending": bool(ARGS.send), "callsign": ARGS.callsign, "now": now,
            "levels": levels, "held": held, "min_level": aprs_priority.load_min_level()}


def miles_between(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(min(1.0, math.sqrt(a)))


VIA_OK = re.compile(r"[A-Z0-9]{1,6}(-[A-Z0-9]{1,2})?")


def packets_near(q):
    """GET /api/packets: our own object packets within `miles` of lat,lon, for a radio there to transmit.
    min=high keeps only objects rated at least that important (aprs_priority.py); kills are always listed.
    -> (status, payload).  With --send these are the packets as last sent to APRS-IS, kills of the last
    26 minutes included; in print-only mode they are built from the scripts' state files (no kills)."""
    try:
        lat, lon = float(q["lat"]), float(q["lon"])
        miles = float(q.get("miles", 25))
        since = float(q.get("since", 0) or 0)
        limit = int(q.get("limit", 0) or 0)
    except KeyError:
        return 400, {"error": "lat and lon are required, e.g. /api/packets?lat=40.12&lon=-75.34&miles=25"}
    except ValueError:
        return 400, {"error": "lat, lon, miles, since and limit must be numbers"}
    if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 0 < miles <= 1000):
        return 400, {"error": "lat must be -90..90, lon -180..180 and miles more than 0 and at most 1000"}
    minimum = aprs_priority.parse_level(q.get("min"), None if q.get("min") else aprs_priority.LEVELS[0])
    if not minimum:
        return 400, {"error": "min must be one of: " + ", ".join(aprs_priority.LEVELS)}
    via = str(q.get("via", "")).upper()
    if via and not VIA_OK.fullmatch(via):
        return 400, {"error": "via must be a callsign, e.g. via=W3XYZ-10"}
    if ARGS.send:
        recs = aprs_send.on_air()
    else:
        recs = [{"name": o["name"], "live": True, "lat": o["lat"], "lon": o["lon"], "sent": None, "changed": o["updated"],
                 "source": o["source"],
                 "packet": ov.packet(o["name"], o["lat"], o["lon"], o["table"], o["code"], o["comment"])}
                for o in snapshot()["objects"]]
    out, now = [], time.time()
    for r in recs:
        d = miles_between(lat, lon, r["lat"], r["lon"])
        if d > miles or (not r["live"] and q.get("kills", "1") == "0") or (since and (r["changed"] or 0) < since):
            continue
        age = (now - r["changed"]) / 60 if r["changed"] else None
        rating = aprs_priority.rate(r["name"], r["packet"][37:], r["source"], age if r["live"] else None)
        if r["live"] and not aprs_priority.at_least(rating["level"], minimum):
            continue                              # a kill is always listed: the radio may have sent the object
        item = {"name": r["name"], "live": r["live"], "level": rating["level"], "score": rating["score"],
                "reasons": rating["reasons"], "lat": round(r["lat"], 5), "lon": round(r["lon"], 5),
                "miles": round(d, 1), "source": r["source"], "sent": r["sent"], "changed": r["changed"],
                "info": r["packet"], "tnc2": f"{ARGS.callsign}>APRS,TCPIP*:{r['packet']}"}
        if via:                                   # third-party form: what a gateway station puts on the air
            item["rf"] = f"}}{ARGS.callsign}>APRS,TCPIP,{via}*:{r['packet']}"
        out.append(item)
    out.sort(key=lambda i: (i["live"], -i["score"], i["miles"]))   # kills first, then most important, then nearest
    if limit > 0:
        out = out[:limit]
    counts = {lv: sum(1 for i in out if i["live"] and i["level"] == lv) for lv in aprs_priority.LEVELS}
    return 200, {"callsign": ARGS.callsign, "centre": [lat, lon], "miles": miles, "min": minimum, "now": now,
                 "sending": bool(ARGS.send), "count": len(out), "levels": counts, "packets": out}


# --------------------------------------------------------------------------
# Who asks /api/packets: the stations that carry our objects on the air
# --------------------------------------------------------------------------
CLIENTS_FILE = "api_clients.json"
CLIENT_DAYS = 7                    # a station that has not asked for this long is forgotten
CLIENT_LIMIT = 200
CLIENTS, CLIENTS_LOCK, CLIENTS_SAVED = {}, threading.Lock(), [0.0]


def clients_path():
    return os.path.join(data_dir(), CLIENTS_FILE)


def load_clients():
    try:
        with open(clients_path()) as f:
            raw = json.load(f)["clients"]
        with CLIENTS_LOCK:
            for c in raw:
                CLIENTS[(c["callsign"], round(c["lat"], 3), round(c["lon"], 3))] = c
    except (OSError, ValueError, KeyError, TypeError):
        pass


def save_clients():
    with CLIENTS_LOCK:
        data = {"saved": time.time(), "clients": list(CLIENTS.values())}
        CLIENTS_SAVED[0] = time.time()
    tmp = f"{clients_path()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, clients_path())


def note_client(q, payload, address="", agent="", now=None):
    """Remember one successful /api/packets request: who says they asked, for where, and how wide.
    The callsign is whatever the request gave as via=; nothing checks it."""
    now = time.time() if now is None else now
    call = str(q.get("via", "")).upper()[:9]
    lat, lon = payload["centre"]
    key = (call, round(lat, 3), round(lon, 3))
    with CLIENTS_LOCK:
        c = CLIENTS.get(key)
        fresh = c is None
        if fresh:
            c = CLIENTS[key] = {"callsign": call, "lat": lat, "lon": lon, "first": now, "count": 0}
        c.update(miles=payload["miles"], min=payload["min"], last=now, count=c["count"] + 1,
                 listed=payload["count"], address=str(address)[:60], agent=str(agent)[:80])
        for k in [k for k, v in CLIENTS.items() if now - v["last"] > CLIENT_DAYS * 86400]:
            del CLIENTS[k]
        while len(CLIENTS) > CLIENT_LIMIT:                         # the longest-silent go first
            del CLIENTS[min(CLIENTS, key=lambda k: CLIENTS[k]["last"])]
        due = fresh or now - CLIENTS_SAVED[0] > 60
    if due:
        try:
            save_clients()
        except OSError as e:
            print(f"# web: could not save {CLIENTS_FILE}: {e}", file=sys.stderr, flush=True)


def clients_snapshot(now=None):
    """GET /api/clients: the stations that asked /api/packets in the last CLIENT_DAYS days, newest first."""
    now = time.time() if now is None else now
    with CLIENTS_LOCK:
        rows = [dict(c) for c in CLIENTS.values() if now - c["last"] <= CLIENT_DAYS * 86400]
    for c in rows:
        c["active"] = now - c["last"] <= 600                     # asked in the last ten minutes
    return {"now": now, "days": CLIENT_DAYS, "clients": sorted(rows, key=lambda c: -c["last"])}


# What a request that came through a reverse proxy may reach.  The page has no login and can edit,
# delete and create objects that are then transmitted, so by default the outside world gets only
# the packet list.  A proxy marks the requests it passes on with X-Forwarded-For (Caddy, nginx and
# the like do); requests made straight to this machine carry no such header and get everything.
PUBLIC_PATHS = ("/api/packets",)


def allowed(handler, path):
    if ARGS.proxied == "all" or not handler.headers.get("X-Forwarded-For"):
        return True
    return handler.command == "GET" and path in PUBLIC_PATHS


def prune(overrides, objects):
    cutoff = time.time() - PRUNE_HOURS * 3600
    for name in [n for n, e in overrides.items()
                 if n not in objects and (not isinstance(e, dict) or e.get("ts", 0) < cutoff)]:
        del overrides[name]


def transmit(pkt, quiet=False):
    """Hand one packet (or a list of packets) to aprs_send.py now.  Returns a short status string for the UI."""
    pkts = pkt if isinstance(pkt, list) else [pkt]
    lines = [f"{ARGS.callsign}>APRS,TCPIP*:{x}" for x in pkts]
    for line in lines:
        print(line, flush=True)
    if not ARGS.send:
        return "saved (print-only mode: nothing was transmitted)"
    try:
        aprs_send.hand_over("web", ARGS.callsign, pkts)        # aprs_send.py picks it up within a second
        return "saved and handed to the sender for APRS-IS"
    except OSError as e:
        if not quiet:
            print(f"# web: could not hand packets to the sender: {e}", file=sys.stderr, flush=True)
        return f"saved, but it could not be handed to the sender ({e})"


# ---- objects created in the web UI ----------------------------------------
def custom_path():
    return os.path.join(data_dir(), CUSTOM_FILE)


def load_custom():
    try:
        with open(custom_path()) as f:
            objs = json.load(f).get("objects", {})
        return objs if isinstance(objs, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def save_custom(objs):
    tmp = custom_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"callsign": ARGS.callsign, "objects": objs}, f, indent=1)
    os.replace(tmp, custom_path())


def custom_packet(name, o, live=True):
    return ov.packet(name, float(o["lat"]), float(o["lon"]), o.get("table", "/"), o.get("code", "."),
                     o.get("comment", ""), live=live)


def parse_custom_fields(body, base=None):
    """Validated lat/lon/comment/symbol/expiry from a request body (raises ValueError with a message)."""
    o = dict(base or {})
    try:
        lat, lon = float(body["lat"]), float(body["lon"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("latitude/longitude are not valid numbers")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or lat != lat or lon != lon:
        raise ValueError("latitude/longitude are out of range")
    o["lat"], o["lon"] = round(lat, 5), round(lon, 5)
    o["comment"] = ov.clean_text(body.get("comment", ""), ARGS.comment_length)
    sym = str(body.get("symbol") or (o.get("table", "/") + o.get("code", ".")))
    if len(sym) != 2 or not (sym[0] in "/\\" or sym[0].isalnum()) or not "!" <= sym[1] <= "~":
        raise ValueError("symbol must be two characters: table (/ or \\) and code")
    o["table"], o["code"] = sym[0], sym[1]
    if "expires_hours" in body:
        try:
            hrs = float(body.get("expires_hours") or 0)
        except (TypeError, ValueError):
            raise ValueError("expiry must be a number of hours")
        if hrs < 0 or hrs > 24 * 365:
            raise ValueError("expiry is out of range")
        o["expires"] = time.time() + hrs * 3600 if hrs > 0 else 0
    return o


def heard_path():
    return os.path.join(data_dir(), HEARD_FILE)


def save_heard():
    """Write the listener's picture and all paths to disk (every minute and at shutdown)."""
    if not (LISTENER or OWN_TRAILS):
        return
    data = {"saved": time.time(), "listener": LISTENER.state() if LISTENER else {},
            "own_trails": OWN_TRAILS.dump() if OWN_TRAILS else {}}
    tmp = f"{heard_path()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, heard_path())


def load_heard():
    """Restore the saved picture.  Returns its age in seconds, or None if there was nothing usable."""
    try:
        with open(heard_path()) as f:
            data = json.load(f)
        age = time.time() - float(data["saved"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if OWN_TRAILS:
        OWN_TRAILS.restore(data.get("own_trails"))
    n = LISTENER.restore(data.get("listener")) if LISTENER else 0
    print(f"# restored {n} APRS object(s)/station(s) and their paths, saved {age / 60:.0f} min ago",
          file=sys.stderr, flush=True)
    return age


def track_own():
    """Note where each of our own objects is now, so its path can be drawn."""
    if not OWN_TRAILS:
        return
    objects, _ = read_states()
    for name, rec in objects.items():
        OWN_TRAILS.add(name, rec["lat"], rec["lon"])
    OWN_TRAILS.keep_only(objects)


def custom_loop():
    """Hand custom objects to the sender periodically (it re-sends them when due) and expire old ones."""
    last, ticks = 0.0, 0
    while True:
        try:
            track_own()
            ticks += 1
            if ticks % 3 == 0:
                save_heard()
        except Exception as e:
            print(f"# web: path tracking: {e}", file=sys.stderr, flush=True)
        try:
            with LOCK:
                objs, now = load_custom(), time.time()
                dead = {n: o for n, o in objs.items() if o.get("expires") and o["expires"] <= now}
                pkts = [custom_packet(n, o, live=False) for n, o in dead.items()]
                if dead:
                    for n in dead:
                        del objs[n]
                    save_custom(objs)
                    print(f"# web: {len(dead)} custom object(s) expired", file=sys.stderr, flush=True)
                if objs and now - last >= ARGS.custom_interval:
                    pkts += [custom_packet(n, o) for n, o in objs.items()]
                    last = now
            if pkts:
                transmit(pkts)
        except Exception as e:
            print(f"# web: custom object loop: {e}", file=sys.stderr, flush=True)
        time.sleep(20)


def do_action(action, body):
    name = str(body.get("name", "")).strip()
    if not name or len(name) > 9:
        return 400, {"error": "bad object name"}
    with LOCK:
        objects, _ = read_states()
        overrides = ov.load()
        prune(overrides, objects)
        rec, cur = objects.get(name), overrides.get(name) if isinstance(overrides.get(name), dict) else {}
        orig = cur.get("orig") if isinstance(cur.get("orig"), dict) else None
        if orig is None and rec:
            orig = {k: rec[k] for k in ("lat", "lon", "comment", "table", "code")}
            orig["source_label"] = rec["source_label"]
        msg = "saved"
        custom = load_custom()
        if action == "create":
            name = name.upper()
            if not NAME_OK.match(name):
                return 400, {"error": "name must be 3-9 characters: letters, digits, - _ ."}
            if name in objects or name in overrides:
                return 409, {"error": f"{name} already exists"}
            try:
                o = parse_custom_fields(body)
            except ValueError as e:
                return 400, {"error": str(e)}
            o["created"] = time.time()
            custom[name] = o
            save_custom(custom)
            return 200, {"ok": True, "message": transmit(custom_packet(name, o)), "name": name}
        if name in custom and action in ("edit", "delete"):
            if action == "delete":
                o = custom.pop(name)
                save_custom(custom)
                return 200, {"ok": True, "message": transmit(custom_packet(name, o, live=False))}
            try:
                o = parse_custom_fields(body, custom[name])
            except ValueError as e:
                return 400, {"error": str(e)}
            custom[name] = o
            save_custom(custom)
            return 200, {"ok": True, "message": transmit(custom_packet(name, o))}
        if action == "restore":
            overrides.pop(name, None)
            msg = "restored; the source's own data returns on the feed's next poll"
        elif action == "delete":
            if not rec and not orig:
                return 404, {"error": "no such object"}
            overrides[name] = {"deleted": True, "ts": time.time(), "orig": orig}
            o = rec or orig
            msg = transmit(ov.packet(name, float(o["lat"]), float(o["lon"]), o.get("table", "/"), o.get("code", "."), "", live=False))
        elif action == "edit":
            if not rec:
                return 404, {"error": "no such object (it may have just ended)"}
            try:
                lat, lon = float(body["lat"]), float(body["lon"])
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                return 400, {"error": "latitude/longitude are not valid numbers"}
            comment = ov.clean_text(body.get("comment", ""), ARGS.comment_length)
            if abs(lat - rec["lat"]) < 2e-5 and abs(lon - rec["lon"]) < 2e-5:
                comment += ov.split_shape(rec["comment"])[1]       # not moved: keep the outline as it is
            overrides[name] = {"lat": round(lat, 5), "lon": round(lon, 5), "comment": comment,
                               "ts": time.time(), "orig": orig}
            msg = transmit(ov.packet(name, lat, lon, rec["table"], rec["code"], comment))
        else:
            return 404, {"error": "unknown action"}
        ov.save(overrides)
    return 200, {"ok": True, "message": msg}


class Handler(BaseHTTPRequestHandler):
    server_version = "aprs-web/1.0"

    def log_message(self, fmt, *a):
        if self.command != "GET":
            sys.stderr.write("# web %s %s\n" % (self.address_string(), fmt % a))

    def reply(self, code, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def query(self):
        return {k: v[-1] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}

    def refuse(self):
        self.reply(403, {"error": "only /api/packets is available at this address"})

    def do_GET(self):
        p = self.path.split("?")[0]
        p = p.rstrip("/") or "/"
        if not allowed(self, p):
            return self.refuse()
        if p in ("/", "/index.html"):
            self.reply(200, PAGE.encode(), "text/html; charset=utf-8")
        elif p == "/api/objects":
            self.reply(200, snapshot())
        elif p == "/api/packets":
            q = self.query()
            code, payload = packets_near(q)
            if code == 200:
                note_client(q, payload, (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip(),
                            self.headers.get("User-Agent", ""))
            if code == 200 and q.get("format") == "text":          # one packet per line, ready to transmit
                lines = [i.get("rf") or i["tnc2"] for i in payload["packets"]]
                return self.reply(200, ("\n".join(lines) + "\n" if lines else "").encode("ascii", "replace"),
                                  "text/plain; charset=us-ascii")
            self.reply(code, payload)
        elif p == "/api/clients":
            self.reply(200, clients_snapshot())
        elif p == "/api/aprs":
            if not LISTENER:
                return self.reply(200, {"enabled": False, "objects": []})
            q = self.query()
            own = [o["name"] for o in snapshot()["objects"]]
            recs = LISTENER.snapshot(objects=q.get("objects", "1") != "0", stations=q.get("stations", "0") == "1",
                                     skip_names=own)
            self.reply(200, dict(LISTENER.status(), enabled=True, objects=recs, now=time.time(),
                                 messages=LISTENER.latest_messages(int(q.get("messages", "20") or 20))))
        elif p in ("/eink.png", "/eink.bmp", "/eink", "/eink-all.png", "/eink-all.bmp",
                   "/map.png", "/map-all.png") and aprs_eink:
            q = self.query()
            objs = snapshot()["objects"]
            if LISTENER and ("-all" in p or q.get("all") == "1"):
                for r in LISTENER.snapshot(stations=q.get("stations", "0") == "1", skip_names=[o["name"] for o in objs]):
                    objs.append({"name": r["name"], "lat": r["lat"], "lon": r["lon"], "source": "aprs",
                                 "table": r.get("table"), "code": r.get("code"),
                                 "path": r.get("path") or [],
                                 "comment": r["comment"]})
            try:
                fmt = "colour" if p.startswith("/map") else "bmp" if p.endswith(".bmp") else "png"
                data, ctype = aprs_eink.image(objs, q, BASEMAP, fmt, SYMBOLS,
                                              LISTENER.latest_messages(12) if LISTENER else [])
            except Exception as e:
                return self.reply(500, {"error": str(e)})
            self.reply(200, data, ctype)
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0].rstrip("/")
        if not allowed(self, p):
            return self.refuse()
        if not p.startswith("/api/"):
            return self.reply(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self.reply(400, {"error": "bad JSON"})
        if p == "/api/settings":                    # the minimum level for sending to APRS-IS
            try:
                level = aprs_priority.save_min_level(body.get("min_level"))
            except (ValueError, OSError) as e:
                return self.reply(400, {"error": str(e)})
            what = ("everything is sent" if level == aprs_priority.LEVELS[0]
                    else f"objects below {level} are held back, and taken off APRS-IS if they are on it")
            return self.reply(200, {"ok": True, "min_level": level,
                                    "message": f"Minimum level: {level}. From each script's next poll, {what}."})
        try:
            code, payload = do_action(p[5:], body)
        except Exception as e:                      # keep the server alive whatever happens
            code, payload = 500, {"error": str(e)}
        self.reply(code, payload)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>APRS live objects</title>
<script>(function(){var t;try{t=localStorage.getItem("aprs-theme")}catch(e){}
 if(!t)t=matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";document.documentElement.dataset.theme=t;})();</script>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
:root{--bg:#f4f5f7;--panel:#fff;--ink:#1c2330;--mute:#667085;--line:#dfe3ea;--accent:#1f5fd0;--danger:#c0362c;
 --hover:#f0f4fb;--soft:#f8fafc;--field:#fff;--tagbg:#fff3cd;--tagink:#7a5b00;--dline:#e7b8b3;color-scheme:light}
:root[data-theme=dark]{--bg:#0f1318;--panel:#171c23;--ink:#e6e9ee;--mute:#98a2b3;--line:#2b333e;--accent:#5b9bff;--danger:#ff7b6e;
 --hover:#1f2731;--soft:#1b212a;--field:#10151b;--tagbg:#4a3b00;--tagink:#ffd866;--dline:#6b3530;color-scheme:dark}
:root[data-theme=dark] .leaflet-tile-pane{filter:invert(1) hue-rotate(180deg) brightness(.92) contrast(.9)}
:root[data-theme=dark] .leaflet-popup-content-wrapper,:root[data-theme=dark] .leaflet-popup-tip,:root[data-theme=dark] .leaflet-tooltip,
:root[data-theme=dark] .leaflet-bar a,:root[data-theme=dark] .leaflet-control-attribution{background:var(--panel);color:var(--ink);border-color:var(--line)}
:root[data-theme=dark] .leaflet-container{background:#0b0e12}
a{color:var(--accent)}
*{box-sizing:border-box}
html,body{height:100%;margin:0;font:14px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:var(--ink);background:var(--bg)}
#app{display:flex;height:100%}
#side{width:370px;max-width:100%;background:var(--panel);border-right:1px solid var(--line);display:flex;flex-direction:column}
#map{flex:1;min-width:0}
header{padding:12px 14px;border-bottom:1px solid var(--line)}
header h1{font-size:16px;margin:0 0 2px}
#meta{color:var(--mute);font-size:12px}
#othr{padding:6px 14px 8px;border-bottom:1px solid var(--line);display:flex;flex-direction:column;gap:4px}
.sym{width:24px;height:24px;background-repeat:no-repeat;background-size:384px 144px;position:relative;filter:drop-shadow(0 1px 2px rgba(0,0,0,.5))}
.sym .ovl{position:absolute;inset:0;text-align:center;font:700 11px/24px system-ui,sans-serif;color:#fff;text-shadow:0 0 2px #000,0 0 2px #000}
.symw{display:flex;flex-direction:column;align-items:center;width:24px}
.symw i{display:block;width:20px;height:3px;border-radius:2px;margin-top:1px}
.symw.ed .sym{outline:2px dashed #ffb300;outline-offset:1px}
.osym{display:flex;align-items:center;gap:2px;white-space:nowrap}
.osym span{font:600 10px/12px ui-monospace,Menlo,Consolas,monospace;background:rgba(255,255,255,.8);color:#212529;padding:0 2px;border-radius:2px}
#msgbox{border-bottom:1px solid var(--line);font-size:12px}
#msgbox summary{padding:6px 14px;cursor:pointer;color:var(--mute);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
#msgs{max-height:150px;overflow:auto;padding:0 14px 6px}
.msg{padding:3px 0;border-top:1px solid var(--line);overflow-wrap:anywhere}
.msg b{font-family:ui-monospace,Menlo,Consolas,monospace;font-weight:600}
.msg .k{color:var(--mute)}
.opin{border-radius:8px;color:#fff;background:#868e96;font:600 10px/14px ui-monospace,Menlo,Consolas,monospace;padding:0 4px;border:1px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,.4);white-space:nowrap;opacity:.9}
.opin.sta{background:#adb5bd;border-radius:2px;color:#212529}
#srcs{padding:8px 14px;border-bottom:1px solid var(--line);display:flex;flex-direction:column;gap:4px}
.src{display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer}
.src input{margin:0}
.dot{width:11px;height:11px;border-radius:50%;flex:none}
.src .n{margin-left:auto;font-variant-numeric:tabular-nums;color:var(--mute)}
.src small{color:var(--mute)}
#svc{padding:6px 14px;border-bottom:1px solid var(--line);font-size:12px;color:var(--mute);display:none}
#svc b.bad{color:var(--danger)}
#find{margin:8px 14px;padding:7px 9px;border:1px solid var(--line);border-radius:6px;font:inherit;background:var(--field);color:var(--ink)}
#list{flex:1;overflow:auto;border-top:1px solid var(--line)}
.row{padding:8px 14px;border-bottom:1px solid var(--line);cursor:pointer;border-left:4px solid transparent}
.row:hover,.row.sel{background:var(--hover)}
.row .nm{font-weight:600;font-family:ui-monospace,Menlo,Consolas,monospace}
.row .cm{color:var(--ink);opacity:.9;overflow-wrap:anywhere}
.row .sr{color:var(--mute);font-size:12px}
.row .dist{float:right;color:var(--mute);font-size:12px;font-variant-numeric:tabular-nums}
.lv{display:inline-block;font-size:11px;font-weight:600;padding:0 6px;border-radius:8px;color:#fff;margin-left:6px;text-transform:capitalize}
.lv.critical{background:#c0362c}.lv.high{background:#d9630b}.lv.medium{background:#a47b00}.lv.low{background:#7a8594}
#imp{font-size:13px;margin-top:8px;padding-top:8px;border-top:1px solid var(--line)}
#imp .cnt{display:flex;flex-wrap:wrap;gap:6px 12px;margin:4px 0 6px}#imp .cnt span.lv{margin-left:0}
#imp select{font:inherit;padding:2px 4px;background:var(--field);color:var(--ink);border:1px solid var(--line);border-radius:6px}
#imp small{color:var(--mute)}
.gw{font:600 11px/16px ui-monospace,Menlo,Consolas,monospace;color:#fff;background:#7048e8;border:2px solid #fff;border-radius:4px;padding:0 5px;white-space:nowrap;box-shadow:0 1px 4px rgba(0,0,0,.45)}
.gw.idle{background:#868e96}
.tag{display:inline-block;font-size:11px;padding:0 5px;border-radius:8px;background:var(--tagbg);color:var(--tagink);margin-left:6px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mute);margin:0;padding:10px 14px 4px}
#edit{display:none;padding:12px 14px;border-bottom:1px solid var(--line);background:var(--soft)}
#edit label{display:block;font-size:12px;color:var(--mute);margin-top:8px}
#edit input,#edit textarea,#edit select{width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:6px;font:inherit;background:var(--field);color:var(--ink)}
#edit textarea{height:64px;resize:vertical}
.two{display:flex;gap:8px}.two>div{flex:1}
.btns{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
button{font:inherit;padding:6px 12px;border-radius:6px;border:1px solid var(--line);background:var(--field);color:var(--ink);cursor:pointer}
.top{display:flex;align-items:center;gap:8px}.top h1{flex:1}.top button{padding:4px 10px;font-size:13px}
button.pri{background:var(--accent);border-color:var(--accent);color:#fff}
button.del{color:var(--danger);border-color:var(--dline)}
#toast{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);background:#1c2330;color:#fff;padding:9px 14px;border-radius:8px;display:none;z-index:2000;max-width:90%}
.pin{border-radius:10px;color:#fff;font:600 11px/18px ui-monospace,Menlo,Consolas,monospace;padding:0 6px;border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.45);white-space:nowrap;text-align:center}
.pin.ed{outline:2px dashed #ffb300}
.pop b{font-family:ui-monospace,Menlo,Consolas,monospace}
.pop .k{color:var(--mute);font-size:12px}
.hint{font-size:12px;color:var(--mute);margin-top:6px}
@media (max-width:760px){#app{flex-direction:column-reverse}#side{width:100%;height:48%;border-right:0;border-top:1px solid var(--line)}}
</style></head><body>
<div id="app">
 <div id="side">
  <header><div class="top"><h1>APRS live objects</h1><button class="pri" onclick="startCreate()">New object</button><button id="theme" onclick="flipTheme()" title="Switch light/dark">Dark</button></div>
   <div id="meta">loading…</div><div id="fi" class="hint"></div></header>
  <div id="edit"></div>
  <div id="srcs"></div>
  <div id="imp"></div>
  <div id="othr">
   <label class="src"><input type="checkbox" id="o_obj" onchange="setOther()"><span class="dot" style="background:#868e96"></span><span>Other APRS objects <small id="o_st"></small></span><span class="n" id="o_n1"></span></label>
   <label class="src"><input type="checkbox" id="o_path" onchange="setOther();render()"><span class="dot" style="background:none;border-top:2px dashed #868e96;border-radius:0;height:0"></span><span>Paths (where things have been)</span></label>
   <label class="src"><input type="checkbox" id="o_sym" onchange="setOther();resetIcons()"><span class="dot" style="background:none;border:2px solid #868e96"></span><span>APRS symbols as markers</span></label>
   <label class="src"><input type="checkbox" id="o_gw" onchange="setOther();loadClients()"><span class="dot" style="background:none;border:2px solid #7048e8"></span><span>Radio gateways <small>· stations asking /api/packets</small></span><span class="n" id="o_n3"></span></label>
   <label class="src"><input type="checkbox" id="o_sta" onchange="setOther()"><span class="dot" style="background:#adb5bd;border-radius:2px"></span><span>APRS stations</span><span class="n" id="o_n2"></span></label>
  </div>
  <div id="svc"></div>
  <details id="msgbox"><summary>Latest APRS messages <small id="msgn"></small></summary><div id="msgs"></div></details>
  <input id="find" placeholder="Filter by name or text…" autocomplete="off">
  <div id="list"></div>
 </div>
 <div id="map"></div>
</div>
<div id="toast"></div>
<script>
const COLORS={montco:"#d9480f",chesco:"#7b2cbf",bucks:"#b8860b",nj511:"#0b7285",njta:"#0b7285",pa511:"#1f5fd0",nws:"#c0362c",nhc:"#a61e8a",custom:"#2b8a3e"};
const SYMBOLS=[["/.","Red X"],["//","Red dot"],["\\!","Emergency (!)"],["\\'","Crash site"],["/f","Fire truck"],["/a","Ambulance"],["/!","Police"],
 ["/c","Incident command post"],["/o","EOC"],["/h","Hospital"],["/+","Red cross"],["/E","Eyeball / event"],["/r","Antenna"],["/-","House"],
 ["\\<","Advisory flag"],["\\w","Flooding"],["\\T","Thunderstorm"],["\\n","Red triangle"]];
const fiUrl=n=>"https://aprs.fi/#!call="+encodeURIComponent("a/"+n);
const fiLink=n=>`<a href="${esc(fiUrl(n))}" target="_blank" rel="noopener" onclick="event.stopPropagation()">aprs.fi</a>`;
function setThemeLabel(){$("theme").textContent=document.documentElement.dataset.theme==="dark"?"Light":"Dark";}
function flipTheme(){const t=document.documentElement.dataset.theme==="dark"?"light":"dark";document.documentElement.dataset.theme=t;try{localStorage.setItem("aprs-theme",t)}catch(e){} setThemeLabel();}
function symOptions(cur){let h=SYMBOLS.map(([v,l])=>`<option value="${esc(v)}"${v===cur?" selected":""}>${esc(l)} (${esc(v)})</option>`).join("");
  if(cur&&!SYMBOLS.some(([v])=>v===cur)) h=`<option value="${esc(cur)}" selected>Current (${esc(cur)})</option>`+h; return h;}
const colorOf=k=>COLORS[k]||"#495057";
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const js=v=>esc(JSON.stringify(v));
const $=id=>document.getElementById(id);
const hidden=new Set(); let data={objects:[],suppressed:[],sources:[],services:[]}; let markers={}; let editing=null; let first=true; let sel=null; let creating=null; let shapes=null;
const SHAPE_COL={red:"#e03131",yellow:"#e8a100",blue:"#1c7ed6",green:"#2f9e44"}, SHAPE_DASH={solid:null,dashed:"8 6","double-dashed":"2 6"};
const map=L.map("map").setView([40.1,-75.3],9);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:19,attribution:"&copy; OpenStreetMap contributors"}).addTo(map);

map.on("click",e=>{ if(creating===null) return; placeNew(e.latlng.lat,e.latlng.lng); });
setThemeLabel();
function toast(t){const e=$("toast");e.textContent=t;e.style.display="block";clearTimeout(toast.t);toast.t=setTimeout(()=>e.style.display="none",4500);}
function hhmm(ts){return ts?new Date(ts*1000).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"}):"-";}
function srcLink(o){return o.source_url?`<a href="${esc(o.source_url)}" target="_blank" rel="noopener">${esc(o.source_label)}</a>`:esc(o.source_label);}
function tagOf(n){const m=n.match(/^[A-Z]+/);return (m?m[0]:n).slice(0,4);}
// APRS symbol pictures (aprs.fi symbol set); if they cannot be loaded the map keeps the plain markers
const SPR="https://cdn.jsdelivr.net/gh/hessu/aprs-symbols@master/png/aprs-symbols-24-";
let symOK=false;
function symHtml(o){ const box=document.getElementById("o_sym");
  if(!symOK||!box||!box.checked||!o.table||!o.code) return null;
  const i=o.code.charCodeAt(0)-33; if(i<0||i>93) return null;
  const ov=/^[0-9A-Za-z]$/.test(o.table)?`<span class="ovl">${esc(o.table)}</span>`:"";
  return `<div class="sym" style="background-image:url(${SPR}${o.table==="/"?0:1}.png);background-position:-${(i%16)*24}px -${Math.floor(i/16)*24}px">${ov}</div>`; }
function icon(o){ const sh=symHtml(o);
  if(sh) return L.divIcon({className:"",html:`<div class="symw${o.edited?" ed":""}">${sh}<i style="background:${colorOf(o.source)}"></i></div>`,iconSize:[24,28],iconAnchor:[12,12]});
  return L.divIcon({className:"",html:`<div class="pin${o.edited?" ed":""}" style="background:${colorOf(o.source)}">${esc(tagOf(o.name))}</div>`,iconSize:null,iconAnchor:[18,11]});}
// When the object last went to APRS-IS and when an unchanged one is sent again (from aprs_send.py's records).
function lvTag(o){return `<span class="lv ${o.level}" title="${esc((o.reasons||[]).join("; "))} (score ${o.score})">${o.level}</span>`;}
function setMin(v){api("settings",{min_level:v});}
function sentInfo(o){ if(!data.sending) return null; const t=o.aprs_is, now=Date.now()/1000;
  if(t&&t.held) return {sent:`held back: rated ${o.level}, below the minimum (${data.min_level})`, again:""};
  if(!t||!t.sent) return {sent:t&&t.waiting?"waiting to be sent":"not sent yet", again:""};
  const sent=`${hhmm(t.sent)} (${ago(t.sent)})`;
  if(t.waiting) return {sent, again:"waiting to be sent now"};
  return {sent, again:t.again<=now?"on the next poll":`after ${hhmm(t.again)}, or sooner if it changes`}; }
function sentShort(o){ const s=sentInfo(o); if(!s) return ""; const t=o.aprs_is;
  if(t&&t.held) return " · held back (below minimum)";
  if(!t||!t.sent||t.waiting) return ` · ${s.again||s.sent}`;
  return ` · sent ${hhmm(t.sent)}, again ${t.again<=Date.now()/1000?"next poll":"after "+hhmm(t.again)}`; }
function popup(o){const s=sentInfo(o); return `<div class="pop"><b>${esc(o.name)}</b>${lvTag(o)}${o.edited?'<span class="tag">edited</span>':""}<br>${esc(o.comment)||"<i>(no text)</i>"}<br>
<span class="k">Importance:</span> ${o.level} (score ${o.score}) · ${esc((o.reasons||[]).join("; "))}<br>
<span class="k">Source:</span> ${srcLink(o)}<br><span class="k">Position:</span> ${o.lat.toFixed(5)}, ${o.lon.toFixed(5)}<br>
${s?`<span class="k">Last sent to APRS-IS:</span> ${s.sent}<br>${s.again?`<span class="k">Sends again:</span> ${s.again}<br>`:""}`:""}<span class="k">Last poll:</span> ${hhmm(o.updated)} · ${fiLink(o.name)}${o.expires?`<br><span class="k">Expires:</span> ${new Date(o.expires*1000).toLocaleString()}`:""}
<div class="btns"><button onclick="startEdit(${js(o.name)})">Edit</button><button class="del" onclick="del(${js(o.name)})">Delete</button>
${o.edited?`<button onclick="restore(${js(o.name)})">Restore original</button>`:""}</div></div>`;}

async function api(action,body){
  try{const r=await fetch("/api/"+action,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const j=await r.json(); toast(r.ok?(j.message||"done"):("Error: "+(j.error||r.status))); if(r.ok){await load();} return r.ok;}
  catch(e){toast("Error: "+e); return false;}
}
function visible(o){const q=$("find").value.trim().toLowerCase();
  return !hidden.has(o.source)&&(!q||o.name.toLowerCase().includes(q)||o.comment.toLowerCase().includes(q)||o.source_label.toLowerCase().includes(q));}

function render(){
  $("fi").innerHTML=`<a href="https://aprs.fi/info/a/${encodeURIComponent(data.callsign)}" target="_blank" rel="noopener">${esc(data.callsign)} on aprs.fi</a>`;
  $("meta").textContent=`${data.objects.length} live · callsign ${data.callsign} · ${data.sending?"sending to APRS-IS":"print-only (not transmitting)"} · ${hhmm(data.now)}`;
  $("srcs").innerHTML=data.sources.map(s=>`<label class="src"><input type="checkbox" ${hidden.has(s.key)?"":"checked"} onchange="toggle(${js(s.key)},this.checked)">
    <span class="dot" style="background:${colorOf(s.key)}"></span><span>${esc(s.label)} <small>· polled ${hhmm(s.updated)}</small></span><span class="n">${s.count}</span></label>`).join("")||'<span class="hint">No state files found yet.</span>';
  const LV=["critical","high","medium","low"], lv=data.levels||{};
  $("imp").innerHTML=`<b>Importance</b><div class="cnt">${LV.map(l=>`<span><span class="lv ${l}">${l}</span> ${lv[l]||0}</span>`).join("")}</div>
    <label>Send to APRS-IS: <select onchange="setMin(this.value)">${[["low","everything"],["medium","medium and above"],["high","high and above"],["critical","critical only"]]
      .map(([v,t])=>`<option value="${v}"${data.min_level===v?" selected":""}>${t}</option>`).join("")}</select></label>
    ${data.sending?(data.held?`<br><small>${data.held} held back, not on APRS-IS</small>`:""):"<br><small>print-only mode: nothing is sent</small>"}`;
  const sv=data.services.filter(s=>s.state&&s.state!=="STOPPED");
  $("svc").style.display=sv.length?"block":"none";
  $("svc").innerHTML="Services: "+sv.map(s=>`<b class="${s.state==="RUNNING"?"":"bad"}" title="${esc(s.note||"")}">${esc(s.service)} ${esc(s.state.toLowerCase())}</b>`).join(" · ");
  const shown=data.objects.filter(visible), keep=new Set();
  if(!shapes) shapes=L.layerGroup().addTo(map);
  shapes.clearLayers();
  for(const o of shown){ if(!o.shape||editing===o.name) continue;
    const st={color:SHAPE_COL[o.shape.colour]||"#888",weight:2,dashArray:SHAPE_DASH[o.shape.style],fillOpacity:.08,interactive:false};
    (o.shape.closed?L.polygon(o.shape.points,st):L.polyline(o.shape.points,st)).addTo(shapes); }
  if($("o_path").checked) for(const o of shown){ if(o.path&&o.path.length>1&&editing!==o.name)
    L.polyline(o.path,{color:colorOf(o.source),weight:3,opacity:.75,dashArray:"1 6",lineCap:"round",interactive:false}).addTo(shapes); }
  for(const o of shown){ keep.add(o.name);
    if(editing===o.name) continue;
    let m=markers[o.name];
    if(!m){m=markers[o.name]=L.marker([o.lat,o.lon],{icon:icon(o)}).addTo(map); m.on("click",()=>select(o.name,false));}
    else{m.setLatLng([o.lat,o.lon]); m.setIcon(icon(o));}
    m.bindPopup(popup(o),{maxWidth:320}); m.bindTooltip(`${o.name} · ${o.source_label}`);
  }
  for(const n of Object.keys(markers)) if(!keep.has(n)&&editing!==n){map.removeLayer(markers[n]); delete markers[n];}
  // the list: nearest to the centre of the map first (re-sorted whenever the map is moved)
  const c=map.getCenter(), byDist=shown.map(o=>[o,away(c.lat,c.lng,o.lat,o.lon)]).sort((a,b)=>a[1].mi-b[1].mi);
  let h=byDist.map(([o,d])=>`<div class="row${sel===o.name?" sel":""}" style="border-left-color:${colorOf(o.source)}" onclick="select(${js(o.name)},true)">
    <span class="nm">${esc(o.name)}</span>${lvTag(o)}${o.edited?'<span class="tag">edited</span>':""}<span class="dist">${d.text}</span><div class="cm">${esc(o.comment)}</div><div class="sr">${esc(o.source_label)} · ${fiLink(o.name)}${sentShort(o)}</div></div>`).join("");
  if(!shown.length) h='<div class="row"><span class="sr">No live objects match.</span></div>';
  if(data.suppressed.length){h+=`<h2>Deleted here (${data.suppressed.length})</h2>`+data.suppressed.map(s=>`<div class="row"><span class="nm">${esc(s.name)}</span>
    <div class="cm">${esc(s.comment)}</div><div class="sr">${esc(s.source_label)} · deleted ${hhmm(s.ts)}</div>
    <div class="btns"><button onclick="restore(${js(s.name)})">Restore</button></div></div>`).join("");}
  $("list").innerHTML=h;
  if(first&&shown.length){first=false; map.fitBounds(L.latLngBounds(shown.map(o=>[o.lat,o.lon])).pad(0.15),{maxZoom:12});}
}
function toggle(k,on){on?hidden.delete(k):hidden.add(k); render();}
function away(lat0,lon0,lat,lon){   // distance (miles) and compass direction from the map centre
  const dy=(lat-lat0)*69.0, dx=(lon-lon0)*69.172*Math.cos(lat0*Math.PI/180), mi=Math.hypot(dx,dy);
  const dir=["N","NE","E","SE","S","SW","W","NW"][Math.round(((Math.atan2(dx,dy)*180/Math.PI)+360)%360/45)%8];
  return {mi, text: mi<0.1?"here":`${mi<10?mi.toFixed(1):Math.round(mi)} mi ${dir}`};
}
let moveTimer=null;
map.on("moveend",()=>{ clearTimeout(moveTimer); moveTimer=setTimeout(()=>{ if(!editing&&creating===null) render(); },150); });
function select(n,pan){sel=n; const m=markers[n]; if(m&&pan){map.setView(m.getLatLng(),Math.max(map.getZoom(),12)); m.openPopup();} render();}
function find(n){return data.objects.find(o=>o.name===n);}

function startEdit(n){
  const o=find(n); if(!o) return; cancelEdit(); editing=n; map.closePopup();
  const m=markers[n]; if(m){m.dragging.enable(); m.on("drag",onDrag);}
  $("edit").style.display="block";
  $("edit").innerHTML=`<b>Edit ${esc(n)}</b> <span class="k">· ${srcLink(o)}</span>
   <label>Text shown on the object</label><textarea id="e_c" maxlength="200">${esc(o.comment)}</textarea>
   <div class="two"><div><label>Latitude</label><input id="e_lat" value="${o.lat.toFixed(5)}"></div><div><label>Longitude</label><input id="e_lon" value="${o.lon.toFixed(5)}"></div></div>
   ${o.custom?`<label>Symbol</label><select id="e_sym">${symOptions(o.table+o.code)}</select>`:""}
   <div class="hint">Tip: drag the marker on the map to move it.${o.shape?" Moving this object removes its outline.":""}</div>
   <div class="btns"><button class="pri" onclick="saveEdit()">Save</button><button onclick="cancelEdit(true)">Cancel</button>
   ${o.edited?`<button onclick="restore(${js(n)})">Restore original</button>`:""}<button class="del" onclick="del(${js(n)})">Delete</button></div>`;
}
function onDrag(e){const p=e.target.getLatLng(); $("e_lat").value=p.lat.toFixed(5); $("e_lon").value=p.lng.toFixed(5);}
function startCreate(){
  cancelEdit(); map.closePopup(); creating=false; const c=map.getCenter();
  $("edit").style.display="block";
  $("edit").innerHTML=`<b>New object</b> <span class="k">· sent from ${esc(data.callsign||"")}</span>
   <label>Name (3-9 characters: letters, digits, - _ .)</label><input id="n_name" maxlength="9" autocomplete="off" style="text-transform:uppercase">
   <label>Text shown on the object</label><textarea id="e_c" maxlength="200"></textarea>
   <label>Symbol</label><select id="e_sym">${symOptions("/.")}</select>
   <div class="two"><div><label>Latitude</label><input id="e_lat" value="${c.lat.toFixed(5)}"></div><div><label>Longitude</label><input id="e_lon" value="${c.lng.toFixed(5)}"></div></div>
   <label>Remove automatically after (hours, 0 = keep until deleted)</label><input id="n_exp" value="0" inputmode="decimal">
   <div class="hint">Click the map to set the position, or drag the green marker.</div>
   <div class="btns"><button class="pri" onclick="saveCreate()">Create and send</button><button onclick="cancelEdit(true)">Cancel</button></div>`;
  placeNew(c.lat,c.lng);
  for(const id of ["e_lat","e_lon"]) $(id).addEventListener("change",()=>{const a=parseFloat($("e_lat").value),b=parseFloat($("e_lon").value); if(isFinite(a)&&isFinite(b)) placeNew(a,b);});
}
function placeNew(lat,lng){
  if(creating&&creating.setLatLng){creating.setLatLng([lat,lng]);}
  else{creating=L.marker([lat,lng],{draggable:true,icon:L.divIcon({className:"",html:'<div class="pin" style="background:#2b8a3e">NEW</div>',iconSize:null,iconAnchor:[18,11]})}).addTo(map); creating.on("drag",onDrag);}
  if($("e_lat")){$("e_lat").value=lat.toFixed(5); $("e_lon").value=lng.toFixed(5);}
}
async function saveCreate(){
  const body={name:$("n_name").value.trim().toUpperCase(),comment:$("e_c").value,symbol:$("e_sym").value,lat:parseFloat($("e_lat").value),lon:parseFloat($("e_lon").value),expires_hours:parseFloat($("n_exp").value)||0};
  if(!/^[A-Z0-9][A-Z0-9_.\-]{2,8}$/.test(body.name)){toast("Name must be 3-9 characters: letters, digits, - _ ."); return;}
  if(!isFinite(body.lat)||!isFinite(body.lon)){toast("Latitude and longitude must be numbers"); return;}
  if(await api("create",body)){cancelEdit(true); sel=body.name;}
}
function cancelEdit(rerender){ if(creating!==null){ if(creating&&creating.setLatLng) map.removeLayer(creating); creating=null; }
  if(editing&&markers[editing]){markers[editing].dragging.disable(); markers[editing].off("drag",onDrag);}
  editing=null; $("edit").style.display="none"; $("edit").innerHTML=""; if(rerender) render();}
async function saveEdit(){const n=editing; const body={name:n,comment:$("e_c").value,lat:parseFloat($("e_lat").value),lon:parseFloat($("e_lon").value)};
  if($("e_sym")) body.symbol=$("e_sym").value;
  if(!isFinite(body.lat)||!isFinite(body.lon)){toast("Latitude and longitude must be numbers"); return;}
  cancelEdit(); await api("edit",body);}
async function del(n){ const o=find(n); if(!confirm(o&&o.custom?`Delete ${n}? It is removed from the APRS map.`:`Delete ${n}? It is removed from the APRS map and kept off while the source still reports it.`)) return; cancelEdit(); map.closePopup(); await api("delete",{name:n});}
async function restore(n){cancelEdit(); map.closePopup(); await api("restore",{name:n});}
async function load(){ try{const r=await fetch("/api/objects"); data=await r.json(); render();}catch(e){$("meta").textContent="Cannot reach the server: "+e;} }
$("find").addEventListener("input",render);
// ---- everything else heard on APRS-IS (not ours): a layer that can be switched off ----
let others={objects:[]}, otherMarkers={}, otherTrails=null, gwLayer=null;
// Stations that ask /api/packets: a label at the point they ask for, and a circle of the radius they ask for.
async function loadClients(){
  if(!gwLayer) gwLayer=L.layerGroup().addTo(map);
  gwLayer.clearLayers();
  let d={clients:[]}; try{const r=await fetch("/api/clients"); d=await r.json();}catch(e){}
  const act=d.clients.filter(c=>c.active).length; $("o_n3").textContent=d.clients.length?`${act}/${d.clients.length}`:"";
  $("o_n3").title=`${act} asked in the last 10 minutes, ${d.clients.length} in the last ${d.days||7} days`;
  if(!$("o_gw").checked) return;
  for(const c of d.clients){ const col=c.active?"#7048e8":"#868e96", name=c.callsign||"(no callsign)";
    L.circle([c.lat,c.lon],{radius:c.miles*1609.344,color:col,weight:2,opacity:.9,fillColor:col,fillOpacity:.06,dashArray:c.active?null:"6 6",interactive:false}).addTo(gwLayer);
    L.marker([c.lat,c.lon],{zIndexOffset:-500,icon:L.divIcon({className:"",html:`<div class="gw${c.active?"":" idle"}">${esc(name)}</div>`,iconSize:null,iconAnchor:[20,9]})})
     .bindPopup(`<div class="pop"><b>${esc(name)}</b> <span class="k">radio gateway</span><br>
<span class="k">Asks for:</span> ${c.miles} miles around ${c.lat.toFixed(4)}, ${c.lon.toFixed(4)}<br>
<span class="k">Least important level:</span> ${esc(c.min)} · ${c.listed} packet(s) in its last list<br>
<span class="k">Last asked:</span> ${hhmm(c.last)} (${ago(c.last)}) · ${c.count} request(s) since ${new Date(c.first*1000).toLocaleString()}<br>
<span class="k">From:</span> ${esc(c.address||"?")} · ${esc(c.agent||"")}<br>
<span class="k">The callsign is what the request gave; it is not checked.</span></div>`,{maxWidth:340})
     .bindTooltip(`${name} · ${c.miles} mi · ${c.active?"asking now":"last asked "+ago(c.last)}`).addTo(gwLayer); }
}
function pref(k,d){try{const v=localStorage.getItem(k);return v===null?d:v;}catch(e){return d;}}
$("msgbox").open=pref("aprs-msgs","1")==="1";
$("msgbox").addEventListener("toggle",()=>{try{localStorage.setItem("aprs-msgs",$("msgbox").open?"1":"0");}catch(e){}});
$("o_sym").checked=pref("aprs-symbols","1")==="1";
function resetIcons(){ for(const id of Object.keys(otherMarkers)){map.removeLayer(otherMarkers[id]); delete otherMarkers[id];} render(); loadOther(); }
(function(){const im=new Image(); im.onload=()=>{symOK=true; resetIcons();}; im.src=SPR+"0.png";})();
$("o_gw").checked=pref("aprs-gateways","1")==="1";
$("o_obj").checked=pref("aprs-other","1")==="1"; $("o_sta").checked=pref("aprs-stations","0")==="1"; $("o_path").checked=pref("aprs-paths","1")==="1";
function setOther(){try{localStorage.setItem("aprs-gateways",$("o_gw").checked?"1":"0");localStorage.setItem("aprs-other",$("o_obj").checked?"1":"0");localStorage.setItem("aprs-stations",$("o_sta").checked?"1":"0");localStorage.setItem("aprs-paths",$("o_path").checked?"1":"0");localStorage.setItem("aprs-symbols",$("o_sym").checked?"1":"0");}catch(e){} loadOther();}
function ago(ts){const m=Math.max(0,Math.round((Date.now()/1000-ts)/60));return m<1?"just now":m+" min ago";}
function oIcon(o){ const sh=symHtml(o);
  if(sh) return L.divIcon({className:"",html:`<div class="osym">${sh}<span>${esc(o.name.slice(0,9))}</span></div>`,iconSize:null,iconAnchor:[12,12]});
  return L.divIcon({className:"",html:`<div class="opin${o.kind==="station"?" sta":""}">${esc(o.name.slice(0,9))}</div>`,iconSize:null,iconAnchor:[14,8]});}
function oPopup(o){return `<div class="pop"><b>${esc(o.name)}</b> <span class="k">${esc(o.kind)}</span><br>${esc(o.comment)||"<i>(no text)</i>"}<br>
<span class="k">${o.kind==="station"?"Station":"Sent by "+esc(o.src)}</span> · heard ${ago(o.heard)}<br><span class="k">Position:</span> ${o.lat.toFixed(5)}, ${o.lon.toFixed(5)}${o.path&&o.path.length>1?` · path of ${o.path.length} points`:""}<br>
<span class="k">Not one of ours (read-only) ·</span> <a href="${esc(o.kind==="station"?"https://aprs.fi/#!call="+encodeURIComponent("a/"+o.name):fiUrl(o.name))}" target="_blank" rel="noopener">aprs.fi</a></div>`;}
async function loadOther(){
  const wantO=$("o_obj").checked, wantS=$("o_sta").checked;
  try{const r=await fetch(`/api/aprs?objects=${wantO?1:0}&stations=${wantS?1:0}`); others=await r.json();}catch(e){others={objects:[],error:String(e)};}
  const keep=new Set();
  if(!otherTrails) otherTrails=L.layerGroup().addTo(map);
  otherTrails.clearLayers();
  for(const o of (wantO||wantS)?others.objects:[]){ const id=o.kind+":"+o.name; keep.add(id); let m=otherMarkers[id];
    if($("o_path").checked&&o.path&&o.path.length>1) L.polyline(o.path,{color:"#868e96",weight:2,opacity:.8,interactive:false}).addTo(otherTrails);
    if(!m){m=otherMarkers[id]=L.marker([o.lat,o.lon],{icon:oIcon(o),zIndexOffset:-1000}).addTo(map);} else m.setLatLng([o.lat,o.lon]);
    m.bindPopup(oPopup(o),{maxWidth:320}); m.bindTooltip(o.name+" · "+o.kind); }
  for(const id of Object.keys(otherMarkers)) if(!keep.has(id)){map.removeLayer(otherMarkers[id]); delete otherMarkers[id];}
  $("o_n1").textContent=others.enabled?(others.objects_heard??""):""; $("o_n2").textContent=others.enabled?(others.stations_heard??""):"";
  const ms=others.messages||[];
  $("msgn").textContent=others.enabled===false?"(listening is off)":`(${ms.length})`;
  $("msgs").innerHTML=ms.map(m=>`<div class="msg"><span class="k">${hhmm(m.time)}</span> <b>${esc(m.src)}</b> <span class="k">to</span> <b>${esc(m.to)}</b>${m.count>1?` <span class="k">×${m.count}</span>`:""}<br>${esc(m.text)}</div>`).join("")||'<div class="msg k">None heard yet.</div>';
  $("o_st").textContent=others.enabled===false?"· listening is off":others.connected?`· within ${others.radius_miles} mi`:"· not connected";
}
load(); setInterval(()=>{ if(!document.hidden) load(); },10000);
loadOther(); setInterval(()=>{ if(!document.hidden) loadOther(); },30000);
loadClients(); setInterval(()=>{ if(!document.hidden) loadClients(); },30000);
</script></body></html>
"""


def main():
    global ARGS
    ap = argparse.ArgumentParser(description="Web map + editor for the live APRS objects")
    ap.add_argument("--port", type=int, default=int(os.environ.get("WEB_PORT") or 8080))
    ap.add_argument("--bind", default=os.environ.get("WEB_BIND") or "0.0.0.0")
    ap.add_argument("--callsign", default="N0CALL")
    ap.add_argument("--passcode", type=int, help="passcode for the listener (default: -1, a receive-only login; leave it unset)")
    ap.add_argument("--send", action="store_true", help="hand edits/deletes to aprs_send.py for sending at once")
    ap.add_argument("--comment-length", type=int, default=67)
    ap.add_argument("--interval", type=int, default=0, help="ignored (accepted so aprs_ctl.sh can launch this)")
    ap.add_argument("--no-listen", action="store_true", help="do not listen to APRS-IS for other people's objects")
    ap.add_argument("--listen-miles", type=float, default=float(os.environ.get("APRS_LISTEN_MILES") or 50),
                    help="radius around $APRS_HOME to listen to (default 50)")
    ap.add_argument("--listen-minutes", type=float, default=90,
                    help="forget an object or station not heard for this long (default 90)")
    ap.add_argument("--no-backfill", action="store_true",
                    help="do not fetch recent APRS history when starting without a recent saved state")
    ap.add_argument("--trail-minutes", type=float, default=float(os.environ.get("APRS_TRAIL_MINUTES") or 180),
                    help="how much of each object's path to keep and draw (default 180)")
    ap.add_argument("--proxied", default=os.environ.get("WEB_PROXIED") or "packets", choices=["packets", "all"],
                    help="what requests arriving through a reverse proxy may reach: only /api/packets (default), "
                         "or all of it, editor included (only if the proxy itself asks for a login)")
    ap.add_argument("--custom-interval", type=int, default=120,
                    help="seconds between hand-overs of objects created in the UI to aprs_send.py, which decides "
                         "when a re-send is due (default 120, minimum 120)")
    ARGS = ap.parse_args()
    ARGS.callsign = ARGS.callsign.upper()
    if ARGS.send and ARGS.callsign.startswith("N0CALL"):
        ap.error("--send requires your own callsign (--callsign)")
    ARGS.custom_interval = max(120, ARGS.custom_interval)
    global OWN_TRAILS
    if aprs_listen:
        OWN_TRAILS = aprs_listen.Trails(max(5, ARGS.trail_minutes))
    threading.Thread(target=custom_loop, daemon=True).start()
    if aprs_eink:
        global BASEMAP
        BASEMAP = aprs_eink.Basemap(data_dir(), os.environ.get("NWS_CONTACT", ""))
        global SYMBOLS
        SYMBOLS = aprs_eink.Symbols(data_dir())
    if aprs_listen and not ARGS.no_listen and os.environ.get("APRS_LISTEN", "1") != "0":
        global LISTENER
        hlat, hlon = aprs_eink.home_position() if aprs_eink else (40.12, -75.34)
        LISTENER = aprs_listen.Listener(ARGS.callsign, hlat, hlon, max(1, ARGS.listen_miles),
                                        max_age=max(300, ARGS.listen_minutes * 60),
                                        # receive-only login: a second verified login with the callsign
                                        # aprs_send.py uses makes APRS-IS servers drop one of the two
                                        login_pass=-1 if ARGS.passcode is None else ARGS.passcode,
                                        trail_minutes=max(5, ARGS.trail_minutes),
                                        host=os.environ.get("APRS_IS_HOST") or aprs_listen.HOST,
                                        port=int(os.environ.get("APRS_IS_PORT") or aprs_listen.PORT))
    age = load_heard()
    load_clients()
    if LISTENER:
        LISTENER.start()
        if (age is None or age > 1800) and not ARGS.no_backfill and os.environ.get("APRS_BACKFILL", "1") != "0":
            threading.Thread(target=LISTENER.backfill, daemon=True, kwargs={
                "host": os.environ.get("APRS_HISTORY_HOST") or aprs_listen.HISTORY_HOST,
                "port": int(os.environ.get("APRS_HISTORY_PORT") or aprs_listen.HISTORY_PORT)}).start()

    def stop(_sig, _frame):                          # aprs_ctl.sh stops services with SIGTERM
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    srv = ThreadingHTTPServer((ARGS.bind, ARGS.port), Handler)
    print(f"# web UI on http://{ARGS.bind}:{ARGS.port}/  (data: {os.path.abspath(data_dir())}, "
          f"{'sending' if ARGS.send else 'print-only'})", file=sys.stderr, flush=True)
    print("# through a reverse proxy: " + ("everything is reachable, the editor included" if ARGS.proxied == "all"
                                          else "only /api/packets is reachable"), file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            save_heard()
        except OSError as e:
            print(f"# could not save {HEARD_FILE}: {e}", file=sys.stderr, flush=True)
        try:
            save_clients()
        except OSError as e:
            print(f"# could not save {CLIENTS_FILE}: {e}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
