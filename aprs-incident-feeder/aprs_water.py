#!/usr/bin/env python3
"""River and tide gauges that are flooding or very low -> APRS objects.

  python3 aprs_water.py --once                         # print what would be sent, then exit
  python3 aprs_water.py --callsign W3XYZ-9 --send      # normally started by aprs_ctl.sh (service "water")

Source: the National Weather Service's National Water Prediction Service (water.noaa.gov), which
lists every gauge in an area with its latest reading and the flood category of that reading.
One request per poll:  https://api.water.noaa.gov/nwps/v1/gauges?bbox...

A gauge becomes an object ONLY while its latest reading is
  * at or above flood stage: minor, moderate or major flooding (--min-flood action adds the
    "action" stage just below flooding), or
  * at or below its low-water threshold, where the service defines one (--no-low-water leaves
    these out).
A gauge in its normal range sends nothing.  When a gauge returns to normal its object is killed.
Forecasts are not used: an object means the water is there now.  A reading older than
--max-age-hours (default 6) does not count, so a gauge that has stopped reporting is dropped.

Object name: RIV + the gauge's NWS id, for example RIVBDRN4 (Delaware River at Burlington).
Text: the condition, the level, the gauge's name and the time of the reading, for example
  MINOR FLOOD 9.7ft Delaware River at Burlington @21:15

If the service cannot be reached, or answers with no gauges at all, nothing is sent and nothing is
killed.  Corrections made on the web map (overrides.json) are applied like in the other feeds.
Without --send the packets are only printed.
"""
import argparse
import json
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

API_URL = "https://api.water.noaa.gov/nwps/v1/gauges"
USER_AGENT = "aprs-incident-feeder-water/1.0"
STATE_FILE = "water_live_state.json"
BOX = (39.2, -76.2, 40.7, -74.4)          # south, west, north, east: south-east PA and south Jersey
FLOOD_LEVELS = ["action", "minor", "moderate", "major"]
LABELS = {"action": "NEAR FLOOD", "minor": "MINOR FLOOD", "moderate": "MODERATE FLOOD", "major": "MAJOR FLOOD",
          "low": "LOW WATER"}
LOW = "low_threshold"                     # the service's category for a reading at or below the low-water mark
FLOOD_SYMBOL = ("\\", "w")                # flooding
LOW_SYMBOL = ("/", "w")                   # water station
MAX_AGE_HOURS = 6.0
EASTERN = ZoneInfo("America/New_York")
COMMENT_LIMIT = 67                        # set from --comment-length
LID_OK = re.compile(r"[A-Z0-9]{3,6}")


def fetch_gauges(url, box, timeout=40):
    """Every gauge in the box with its latest reading.  Raises OSError or ValueError when it cannot be had."""
    south, west, north, east = box
    q = urllib.parse.urlencode({"bbox.xmin": west, "bbox.ymin": south, "bbox.xmax": east, "bbox.ymax": north,
                                "srid": "EPSG_4326"})
    req = urllib.request.Request(f"{url}?{q}", headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    gauges = data.get("gauges") if isinstance(data, dict) else None
    if not isinstance(gauges, list) or not gauges:
        raise ValueError("the gauge service answered without any gauges")
    return gauges


def condition(gauge, now, min_flood="minor", low_water=True, max_age_hours=MAX_AGE_HOURS):
    """What the gauge's latest reading shows: 'minor' | 'moderate' | 'major' | 'action' | 'low', or None
    for a normal, missing or stale reading.  -> (condition, level in feet, time of the reading)."""
    try:
        obs = gauge["status"]["observed"]
        category, level = str(obs["floodCategory"]), float(obs["primary"])
        when = datetime.fromisoformat(str(obs["validTime"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    if level <= -900 or when.tzinfo is None or now - when > timedelta(hours=max_age_hours) or when - now > timedelta(hours=1):
        return None
    if category in FLOOD_LEVELS and FLOOD_LEVELS.index(category) >= FLOOD_LEVELS.index(min_flood):
        return category, level, when
    if category == LOW and low_water:
        return "low", level, when
    return None


def place_name(name):
    """The gauge's name without the datum note: 'Maurice River at Bivalve (IN MLLW)' -> 'Maurice River at Bivalve'."""
    return re.sub(r"\s+", " ", re.sub(r"\([^)]*\)?", "", str(name or ""))).strip(" ,-")


def comment_for(what, level, when, name, limit=None):
    limit = limit or COMMENT_LIMIT
    head = f"{LABELS[what]} {level:.1f}ft "
    tail = f" @{when.astimezone(EASTERN):%H:%M}"
    place = place_name(name)
    room = limit - len(head) - len(tail)
    if len(place) > room:
        place = place[:max(0, room)].rstrip(" ,-")
    text = (head + place).rstrip() + tail
    return text.encode("ascii", "replace").decode()[:limit]


def gauge_objects(gauges, now, args):
    """name -> (lat, lon, (table, code, comment)) for every gauge that is flooding or low."""
    placed = {}
    for g in gauges:
        if not isinstance(g, dict):
            continue
        lid = str(g.get("lid", "")).upper()
        state = (g.get("state") or {}).get("abbreviation") if isinstance(g.get("state"), dict) else None
        if not LID_OK.fullmatch(lid) or (args.states and state not in args.states):
            continue
        found = condition(g, now, args.min_flood, not args.no_low_water, args.max_age_hours)
        if not found:
            continue
        what, level, when = found
        try:
            lat, lon = float(g["latitude"]), float(g["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            continue
        table, code = LOW_SYMBOL if what == "low" else FLOOD_SYMBOL
        placed["RIV" + lid] = (lat, lon, (table, code, comment_for(what, level, when, g.get("name"))))
    return placed


def load_state(path, callsign):
    """name -> (lat, lon, (table, code, comment)) for objects handed over earlier."""
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


def packet(name, lat, lon, sym, live=True):
    table, code, comment = sym
    return aprs_overrides.packet(name, lat, lon, table, code, comment if live else "", live=live)


def run_cycle(args, live, aprs_is, now=None):
    gauges = fetch_gauges(args.url, args.box)          # raises on a bad answer -> nothing is killed
    now = now or datetime.now(timezone.utc)
    placed = gauge_objects(gauges, now, args)
    out = [packet(name, lat, lon, sym) for name, (lat, lon, sym) in placed.items()]
    aprs_overrides.apply(placed, out)

    gone = [n for n in live if n not in placed]
    for name in gone:
        lat, lon, sym = live[name]
        out.append(packet(name, lat, lon, sym, live=False))

    for pkt in out:
        print(f"{args.callsign}>APRS,TCPIP*:{pkt}")
        if aprs_is:
            aprs_is.send(pkt)
    if aprs_is:
        aprs_is.flush()                # hand this poll's packets to aprs_send.py in one go
    sys.stdout.flush()

    for name in gone:                  # bookkeeping only after the hand-over, so a failure repeats the kills
        del live[name]
    live.update(placed)
    save_state(args.state_file, args.callsign, live)
    return len(gauges), len(placed), len(gone)


def parse_args(argv=None):
    global COMMENT_LIMIT
    ap = argparse.ArgumentParser(description="NWS river and tide gauges that are flooding or very low -> APRS objects")
    ap.add_argument("--url", default=API_URL, help="the gauge list address (default: the NWS water prediction service)")
    ap.add_argument("--box", default=",".join(f"{v:g}" for v in BOX),
                    help="area to watch as south,west,north,east in degrees (default: south-east PA and south Jersey)")
    ap.add_argument("--states", default="", help="only gauges in these states, e.g. NJ,PA (default: every gauge in the box)")
    ap.add_argument("--min-flood", default="minor", choices=FLOOD_LEVELS,
                    help="lowest flood category that makes an object (default minor = at flood stage; "
                         "action = the stage just below it)")
    ap.add_argument("--no-low-water", action="store_true", help="no objects for gauges at or below their low-water mark")
    ap.add_argument("--max-age-hours", type=float, default=MAX_AGE_HOURS,
                    help=f"ignore a reading older than this (default {MAX_AGE_HOURS:g})")
    ap.add_argument("--state-file", default=STATE_FILE)
    ap.add_argument("--comment-length", type=int, default=COMMENT_LIMIT,
                    help=f"maximum characters of object text (default {COMMENT_LIMIT}; APRS 1.0 specifies 43)")
    ap.add_argument("--callsign", default="N0CALL", help="source callsign-SSID for the packets")
    ap.add_argument("--passcode", type=int, help="ignored: aprs_send.py logs in to APRS-IS, not this script")
    ap.add_argument("--send", action="store_true", help="hand the packets to aprs_send.py for sending (requires a real callsign)")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--interval", type=int, default=600, help="seconds between polls (default 600, minimum 300)")
    args = ap.parse_args(argv)
    if not 20 <= args.comment_length <= 200:
        ap.error("--comment-length must be between 20 and 200")
    COMMENT_LIMIT = args.comment_length
    try:
        args.box = tuple(float(v) for v in args.box.split(","))
        south, west, north, east = args.box
        if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
            raise ValueError
    except ValueError:
        ap.error("--box must be south,west,north,east in degrees, e.g. 39.2,-76.2,40.7,-74.4")
    if not re.match(r"https?://", args.url):
        ap.error("--url must start with http:// or https://")
    args.states = {s.strip().upper() for s in args.states.split(",") if s.strip()}
    args.interval = max(300, args.interval)
    args.max_age_hours = max(0.5, args.max_age_hours)
    args.callsign = args.callsign.upper()
    if args.send and args.callsign.startswith("N0CALL"):
        ap.error("--send requires your own callsign (--callsign)")
    return args


def main():
    args = parse_args()
    if aprs_overrides is None:
        sys.exit("aprs_water.py needs aprs_overrides.py beside it (it builds the object packets)")
    live = load_state(args.state_file, args.callsign)
    if live:
        print(f"# restored {len(live)} live object(s) from {args.state_file}", file=sys.stderr)
    aprs_is = aprs_send.Outbox("water", args.callsign) if args.send else None
    try:
        while True:
            try:
                seen, n, killed = run_cycle(args, live, aprs_is)
                print(f"# {seen} gauge(s) read, {n} flooding or low, {killed} killed", file=sys.stderr)
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
