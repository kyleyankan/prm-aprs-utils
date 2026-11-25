#!/usr/bin/env python3
"""Put the Incident Feeder's objects for your area on the air through your own TNC.

  python3 aprs_radio.py --callsign W3XYZ-10 --lat 40.12 --lon -75.34
  ... add --transmit to really send; without it the packets are only printed.
  ... or keep the settings in a file: --config FILE (see systemd/aprs-incident-rf-bridge.conf.example).
      INSTALL.md shows how to run it as a systemd service.

Every --poll seconds (default 120) it asks the feeder's /api/packets for the objects within
--miles of your position, rated at least --min, and sends the ones that need sending to a
KISS TNC: Direwolf or another software TNC over TCP (--kiss-tcp, default 127.0.0.1:8001), or a
hardware TNC on a serial port (--kiss-serial /dev/ttyUSB0 --baud 9600; Linux and macOS).

What goes on the air, and how often
  * An object is transmitted when it is new, when its position, symbol or text changed, and
    again every --refresh minutes (default 30) while the feeder still lists it.
  * When the feeder kills an object this station transmitted, the kill is transmitted too.
    An object that just drops out of the list (it left your radius, or was pushed out by
    --max-objects) is killed as well, so nothing is left behind on anyone's map.
  * At most --max-objects (default 10) are carried at once, the most important first, and at
    most --max-per-poll (default 6) packets go out per poll, --gap seconds apart (default 8).
    A shared 1200 baud channel cannot take much more; start small.
  * When an object rated critical is in your area, the feeder also lists a bulletin for it
    (":BLN1     :Tornado Warning ...").  It is transmitted when new or changed and again every
    --refresh minutes, ahead of the objects, and counts towards --max-per-poll.  A bulletin
    has no kill: when its object ends it is simply no longer sent.  --no-bulletins leaves
    them out.
  * If the feeder cannot be reached, nothing is transmitted and nothing is killed.

Auto mode (--auto): carry everything in your radius, a little at a time
  Without --auto the script carries a short list (medium and above, ten objects).  With it, it
  asks for every object in the radius, whatever its level, and works through them slowly: at
  most --burst packets (default 2, never more than 3) go out per poll.  What goes first:
    1. kills, so nothing this station put on the air is left behind;
    2. bulletins for critical objects;
    3. objects, the most important level first.  Within a level, a new or changed object goes
       before one that is only due for its --refresh repeat, and the one sent longest ago first.
  So a new building fire jumps ahead of the roadwork still waiting, and the low-importance
  objects go out only when nothing more important is waiting.  On a busy day some of them may
  wait a long time or never be sent; that is the intent.  --min and --max-objects still limit
  the list if you give them.

How the packets look on the air
  They are sent in APRS third-party form, from YOUR callsign:
      W3XYZ-10>APZIFD:}K3YLE-15>APRS,TCPIP,W3XYZ-10*:;FIRE7K2QX*241740z4007.80N/07519.80WdBUILDING FIRE ...
  That keeps the feeder as the owner of the object, and igates do not pass third-party
  packets back to APRS-IS, so the object does not arrive there twice.  --path adds
  digipeaters (for example --path WIDE2-1); the default is none.

You are the control operator of what your station transmits: you need a licence that covers
it, and you choose the radius, the minimum level and the limits.  N0CALL is refused.

What was transmitted is remembered in --state-file (default aprs_radio_state.json), so a
restart does not send everything again.  Standard library only (Python 3.8+).
"""
import argparse
import json
import os
import re
import signal
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.4"
SERVER = "https://aprs-incidents.phlm.sh"     # the feeder asked unless --server names another
TOCALL = "APZIFD"                 # APZ... = experimental software; change with --tocall
USER_AGENT = f"aprs-radio/{VERSION} (Incident Feeder RF gateway)"
CALL_OK = re.compile(r"[A-Z0-9]{1,6}(-([0-9]|1[0-5]))?")
LEVELS = ["low", "medium", "high", "critical"]
MAX_INFO = 256                    # AX.25 information field
FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD


# --------------------------------------------------------------------------
# AX.25 and KISS
# --------------------------------------------------------------------------
def ax25_address(call, last=False, command=False):
    """Seven bytes for one address field.  call: 'W3XYZ' or 'W3XYZ-10'."""
    call = call.upper()
    if not CALL_OK.fullmatch(call):
        raise ValueError(f"not a callsign an AX.25 address can carry: {call!r}")
    base, _, ssid = call.partition("-")
    out = bytes((ord(c) << 1) for c in base.ljust(6))
    return out + bytes([(0xE0 if command else 0x60) | (int(ssid or 0) << 1) | (1 if last else 0)])


def ax25_ui_frame(source, dest, path, info):
    """An AX.25 UI frame (no flags, no checksum: the TNC adds those)."""
    data = info.encode("ascii", "replace") if isinstance(info, str) else bytes(info)
    if len(data) > MAX_INFO:
        raise ValueError(f"packet text is {len(data)} bytes; AX.25 carries at most {MAX_INFO}")
    hops = [p for p in path if p]
    if len(hops) > 8:
        raise ValueError("at most 8 digipeaters")
    frame = ax25_address(dest, command=True) + ax25_address(source, last=not hops)
    for i, hop in enumerate(hops):
        frame += ax25_address(hop, last=i == len(hops) - 1)
    return frame + b"\x03\xf0" + data


def kiss_frame(frame, port=0):
    """Wrap a frame for a KISS TNC: FEND, data-frame command for `port`, escaped bytes, FEND."""
    out = bytearray([FEND, (port & 0x0F) << 4])
    for b in frame:
        if b == FEND:
            out += bytes([FESC, TFEND])
        elif b == FESC:
            out += bytes([FESC, TFESC])
        else:
            out.append(b)
    out.append(FEND)
    return bytes(out)


class KissTcp:
    """A software TNC's KISS port (Direwolf: KISSPORT 8001).  Connects for each batch of packets."""

    def __init__(self, host, port):
        self.host, self.port, self.sock = host, port, None

    def describe(self):
        return f"KISS TNC at {self.host}:{self.port}"

    def open(self):
        if not self.sock:
            self.sock = socket.create_connection((self.host, self.port), timeout=10)

    def send(self, data):
        self.open()
        self.sock.sendall(data)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


class KissSerial:
    """A hardware TNC already in KISS mode, on a serial port (Linux and macOS)."""

    def __init__(self, device, baud):
        self.device, self.baud, self.fd = device, baud, None

    def describe(self):
        return f"KISS TNC on {self.device} at {self.baud} baud"

    def open(self):
        if self.fd is not None:
            return
        import termios                              # not on Windows: use a software TNC over TCP there
        speed = getattr(termios, f"B{self.baud}", None)
        if speed is None:
            raise OSError(f"{self.baud} is not a serial speed this system knows")
        fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY)
        try:
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0                                             # input: raw
            attrs[1] = 0                                             # output: raw
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # 8 data bits, no parity, 1 stop bit
            attrs[3] = 0                                             # no echo, no line editing
            attrs[4] = attrs[5] = speed
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except Exception:
            os.close(fd)
            raise
        self.fd = fd

    def send(self, data):
        self.open()
        while data:
            data = data[os.write(self.fd, data):]

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


# --------------------------------------------------------------------------
# What needs transmitting
# --------------------------------------------------------------------------
def object_of(rf):
    """(start of the object inside a third-party packet, name, live) or None if it is not one."""
    i = rf.find(":;")
    if not rf.startswith("}") or i < 0 or len(rf) < i + 38 or rf[i + 11] not in "*_":
        return None
    return i + 1, rf[i + 2:i + 11].strip(), rf[i + 11] == "*"


def kill_of(rf):
    """The kill for an object this station transmitted: same name, time and position, '_' for '*', no text."""
    start, _name, _live = object_of(rf)
    return rf[:start + 10] + "_" + rf[start + 11:start + 37]


def body_of(rf):
    """Everything about the object except its time stamp, to tell a real change from a re-stamp."""
    start, _name, _live = object_of(rf)
    return rf[start + 18:]


BULLETIN = re.compile(r"\}[A-Z0-9-]+>[^:]+::(BLN[0-9A-Z]) {5}:.+")


def bulletin_of(rf):
    """'BLN1' for a general bulletin in third-party form, else None."""
    m = BULLETIN.fullmatch(rf)
    return m.group(1) if m else None


class Carried:
    """The objects this station has put on the air, kept across restarts."""

    def __init__(self, path=None):
        self.path, self.objects = path, {}        # name -> {rf, sent}
        self.bulletins = {}                       # BLN1, BLN2, ... -> {rf, sent}
        if path:
            try:
                with open(path) as f:
                    raw = json.load(f)
                self.objects = {n: {"rf": str(o["rf"]), "sent": float(o["sent"])}
                                for n, o in raw["objects"].items() if object_of(str(o["rf"]))}
                self.bulletins = {n: {"rf": str(b["rf"]), "sent": float(b["sent"])}
                                  for n, b in (raw.get("bulletins") or {}).items() if bulletin_of(str(b["rf"]))}
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass

    def save(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"objects": self.objects, "bulletins": self.bulletins}, f, indent=1)
        os.replace(tmp, self.path)

    def plan(self, packets, now, refresh, max_objects, bulletins=True):
        """[(reason, name, rf)] to transmit, in order: kills first, then bulletins, then the feeder's own
        order (most important first).  packets: the feeder's list, each with 'rf'; an entry marked
        'bulletin' is a bulletin for a critical object in our area."""
        wanted, kills, notices = [], {}, {}
        for p in packets:
            rf = p.get("rf") if isinstance(p, dict) else None
            if not (isinstance(rf, str) and len(rf) <= MAX_INFO and rf.isascii()):
                continue
            if p.get("bulletin"):
                if bulletins and bulletin_of(rf):
                    notices.setdefault(bulletin_of(rf), rf)
                continue
            obj = object_of(rf)
            if not obj:
                continue
            _start, name, live = obj
            if live and name not in dict(wanted):
                wanted.append((name, rf))
            elif not live:
                kills[name] = rf
        wanted = wanted[:max(0, max_objects)]
        names = dict(wanted)
        out = []
        for name, rec in self.objects.items():
            if name not in names:                   # killed by the feeder, or no longer on our list
                out.append(("kill" if name in kills else "dropped", name, kills.get(name) or kill_of(rec["rf"])))
        self.bulletins = {n: b for n, b in self.bulletins.items() if n in notices}    # ended: nothing to kill
        for slot, rf in sorted(notices.items()):
            rec = self.bulletins.get(slot)
            if rec is None or rec["rf"] != rf:
                out.append(("bulletin", slot, rf))
            elif now - rec["sent"] >= refresh:
                out.append(("bulletin again", slot, rf))
        for name, rf in wanted:
            rec = self.objects.get(name)
            if rec is None:
                out.append(("new", name, rf))
            elif body_of(rec["rf"]) != body_of(rf):
                out.append(("changed", name, rf))
            elif now - rec["sent"] >= refresh:
                out.append(("refresh", name, rf))
        return out

    def plan_auto(self, packets, now, refresh, max_objects=None, bulletins=True):
        """Auto mode: everything that needs transmitting, most urgent first (the caller sends only the
        first few).  Kills, then bulletins, then objects by level; within a level new or changed before
        a repeat, then the one sent longest ago, then the feeder's score."""
        wanted, kills, notices = {}, {}, {}
        for p in packets:
            rf = p.get("rf") if isinstance(p, dict) else None
            if not (isinstance(rf, str) and len(rf) <= MAX_INFO and rf.isascii()):
                continue
            if p.get("bulletin"):
                if bulletins and bulletin_of(rf):
                    notices.setdefault(bulletin_of(rf), rf)
                continue
            obj = object_of(rf)
            if not obj:
                continue
            _start, name, live = obj
            if not live:
                kills[name] = rf
            elif name not in wanted:
                level = LEVELS.index(p["level"]) if p.get("level") in LEVELS else 1
                score = p["score"] if isinstance(p.get("score"), (int, float)) else 0
                wanted[name] = (rf, level, score)
        if max_objects is not None:                 # keep the most important ones
            keep = sorted(wanted, key=lambda n: (-wanted[n][1], -wanted[n][2]))[:max(0, max_objects)]
            wanted = {n: wanted[n] for n in keep}
        queue = []
        for name, rec in self.objects.items():
            if name not in wanted:
                queue.append(((0, 0, 0, rec["sent"], 0), "kill" if name in kills else "dropped", name,
                              kills.get(name) or kill_of(rec["rf"])))
        self.bulletins = {n: b for n, b in self.bulletins.items() if n in notices}
        for slot, rf in notices.items():
            rec = self.bulletins.get(slot)
            if rec is None or rec["rf"] != rf:
                queue.append(((1, 0, 0, 0, 0), "bulletin", slot, rf))
            elif now - rec["sent"] >= refresh:
                queue.append(((1, 0, 1, rec["sent"], 0), "bulletin again", slot, rf))
        for name, (rf, level, score) in wanted.items():
            rec = self.objects.get(name)
            if rec is None:
                queue.append(((2, -level, 0, 0, -score), "new", name, rf))
            elif body_of(rec["rf"]) != body_of(rf):
                queue.append(((2, -level, 0, rec["sent"], -score), "changed", name, rf))
            elif now - rec["sent"] >= refresh:
                queue.append(((2, -level, 1, rec["sent"], -score), "refresh", name, rf))
        queue.sort(key=lambda q: (q[0], q[2]))
        return [(why, name, rf) for _key, why, name, rf in queue]

    def sent(self, reason, name, rf, now):
        if reason.startswith("bulletin"):
            self.bulletins[name] = {"rf": rf, "sent": now}
        elif reason in ("kill", "dropped"):
            self.objects.pop(name, None)
        else:
            self.objects[name] = {"rf": rf, "sent": now}


# --------------------------------------------------------------------------
# The feeder's API
# --------------------------------------------------------------------------
def api_url(server, callsign, lat, lon, miles, minimum):
    q = urllib.parse.urlencode({"lat": f"{lat:.5f}", "lon": f"{lon:.5f}", "miles": f"{miles:g}",
                                "min": minimum, "via": callsign})
    return server.rstrip("/") + "/api/packets?" + q


def fetch(url, timeout=20):
    """The feeder's packet list.  Raises OSError or ValueError when it cannot be had."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    if not isinstance(data, dict) or not isinstance(data.get("packets"), list):
        raise ValueError("the answer is not a packet list")
    notices = data.get("bulletins") if isinstance(data.get("bulletins"), list) else []
    return data["packets"] + [dict(b, bulletin=True) for b in notices if isinstance(b, dict)]


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------
class Gateway:
    def __init__(self, args, tnc, carried, fetcher=fetch, sleep=time.sleep):
        self.args, self.tnc, self.carried, self.fetch, self.sleep = args, tnc, carried, fetcher, sleep
        self.path = [p.strip().upper() for p in args.path.split(",") if p.strip()]
        self.url = api_url(args.server, args.callsign, args.lat, args.lon, args.miles, args.min)

    def cycle(self, now=None):
        """One poll.  Returns how many packets were transmitted (or printed)."""
        a = self.args
        try:
            packets = self.fetch(self.url)
        except (OSError, ValueError) as e:
            print(f"# could not get the packet list ({e}); nothing transmitted, nothing killed", file=sys.stderr, flush=True)
            return 0
        now = time.time() if now is None else now
        if a.auto:
            plan = self.carried.plan_auto(packets, now, a.refresh * 60, a.max_objects, not a.no_bulletins)
        else:
            plan = self.carried.plan(packets, now, a.refresh * 60, a.max_objects, not a.no_bulletins)
        most = a.burst if a.auto else a.max_per_poll
        todo, waiting = plan[:most], len(plan) - min(len(plan), most)
        n = 0
        try:
            for i, (reason, name, rf) in enumerate(todo):
                line = f"{a.callsign}>{a.tocall}{''.join(',' + p for p in self.path)}:{rf}"
                if a.transmit:
                    self.tnc.send(kiss_frame(ax25_ui_frame(a.callsign, a.tocall, self.path, rf), a.kiss_port))
                print(f"{'' if a.transmit else '(not transmitted) '}{reason}: {line}", flush=True)
                self.carried.sent(reason, name, rf, now)
                n += 1
                if a.transmit and i < len(todo) - 1:
                    self.sleep(a.gap)
        except (OSError, ValueError) as e:
            print(f"# could not send to the {self.tnc.describe()}: {e}", file=sys.stderr, flush=True)
            self.tnc.close()
        finally:
            if n and a.transmit:                    # a print-only run leaves the record on disk alone
                self.carried.save()
            if a.transmit and isinstance(self.tnc, KissTcp):
                self.tnc.close()
        note = f"; {waiting} more wait for the next poll" if waiting else ""
        did = "sent" if a.transmit else "would be sent"
        print(f"# {len(packets)} listed, {len(self.carried.objects)} carried by this station, {n} {did}{note}",
              file=sys.stderr, flush=True)
        return n

    def run(self):
        a = self.args
        mode = f"transmitting through the {self.tnc.describe()}" if a.transmit else "print-only: nothing is transmitted"
        print(f"# aprs_radio {VERSION} for {a.callsign} ({mode})\n# asking {self.url}\n"
              f"# every {a.poll}s; "
              + (f"auto mode: everything at {a.min} and above, {a.burst} packet(s) per poll, most important first, "
                 if a.auto else f"at most {a.max_objects} objects, {a.max_per_poll} packets per poll, ") +
              f"{a.gap:g}s apart; refresh every {a.refresh:g} min", file=sys.stderr, flush=True)
        try:
            while True:
                self.cycle()
                if a.once:
                    break
                time.sleep(a.poll)
        except KeyboardInterrupt:
            pass
        finally:
            self.tnc.close()


YES, NO = ("yes", "true", "on", "1"), ("no", "false", "off", "0", "")


def config_arguments(path, ap):
    """Read a settings file and return it as command-line arguments.

    One setting per line, "name = value", named like the options without the dashes
    ("max-objects = 5"; "max_objects" works too).  Options that take no value (transmit, auto,
    no-bulletins, once) are switched with yes or no.  Blank lines and lines starting with # are
    skipped."""
    options = {s: act for act in ap._actions for s in act.option_strings}
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        ap.error(f"cannot read the settings file {path}: {e.strerror or e}")
    out = []
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        where = f"{path}, line {number}"
        name, equals, value = line.partition("=")
        if not equals:
            ap.error(f"{where}: expected \"name = value\", got {line!r}")
        option = "--" + name.strip().lower().replace("_", "-")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        act = options.get(option)
        if act is None or option in ("--config", "--help"):
            ap.error(f"{where}: there is no setting called {name.strip()!r}")
        if act.nargs == 0:                      # an on/off option
            if value.lower() in YES:
                out.append(option)
            elif value.lower() not in NO:
                ap.error(f"{where}: {name.strip()} must be yes or no, not {value!r}")
        elif value:
            out.append(f"{option}={value}")     # the = form keeps "lon = -75.34" from reading as an option
    return out


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Transmit the Incident Feeder's objects near you through a KISS TNC")
    ap.add_argument("--config", metavar="FILE",
                    help="read settings from this file (name = value lines); options given on the "
                         "command line as well win over the file")
    ap.add_argument("--server", default=SERVER, help=f"the feeder's address (default {SERVER})")
    ap.add_argument("--callsign", required=True, help="YOUR callsign-SSID, the station that transmits (e.g. W3XYZ-10)")
    ap.add_argument("--lat", type=float, required=True, help="latitude of the centre of your area, e.g. 40.12")
    ap.add_argument("--lon", type=float, required=True, help="longitude, negative for west, e.g. -75.34")
    ap.add_argument("--miles", type=float, default=10, help="radius around that point (default 10)")
    ap.add_argument("--auto", action="store_true",
                    help="carry every object in the radius, a few packets per poll, the most important first")
    ap.add_argument("--burst", type=int, default=2,
                    help="with --auto: packets to transmit per poll, 1 to 3 (default 2)")
    ap.add_argument("--min", default=None, choices=LEVELS,
                    help="least important level to carry: low, medium, high or critical "
                         "(default medium; low with --auto)")
    ap.add_argument("--max-objects", type=int, default=None,
                    help="most objects to keep on the air at once (default 10; no limit with --auto)")
    ap.add_argument("--transmit", action="store_true", help="really send to the TNC (without it, packets are only printed)")
    ap.add_argument("--kiss-tcp", default="127.0.0.1:8001", metavar="HOST:PORT",
                    help="KISS over TCP, for Direwolf and other software TNCs (default 127.0.0.1:8001)")
    ap.add_argument("--kiss-serial", metavar="DEVICE", help="KISS on a serial port instead, e.g. /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=9600, help="serial speed for --kiss-serial (default 9600)")
    ap.add_argument("--kiss-port", type=int, default=0, help="radio port number on a multi-port TNC (default 0)")
    ap.add_argument("--path", default="", help="digipeater path, e.g. WIDE2-1 or WIDE1-1,WIDE2-1 (default: none)")
    ap.add_argument("--tocall", default=TOCALL, help=f"AX.25 destination (default {TOCALL})")
    ap.add_argument("--poll", type=int, default=120, help="seconds between polls of the feeder (default 120, minimum 60)")
    ap.add_argument("--refresh", type=float, default=30, help="re-send an unchanged object after this many minutes (default 30, minimum 10)")
    ap.add_argument("--gap", type=float, default=8, help="seconds between two transmissions (default 8, minimum 2)")
    ap.add_argument("--max-per-poll", type=int, default=6, help="most packets to transmit per poll (default 6)")
    ap.add_argument("--no-bulletins", action="store_true",
                    help="do not transmit the bulletins the feeder sends for critical objects in your area")
    ap.add_argument("--state-file", default="aprs_radio_state.json", help="where what was transmitted is remembered")
    ap.add_argument("--once", action="store_true", help="poll once and exit")
    argv = sys.argv[1:] if argv is None else list(argv)
    first = argparse.ArgumentParser(add_help=False)
    first.add_argument("--config")
    chosen, _ = first.parse_known_args([x for x in argv if x not in ("-h", "--help")])
    if chosen.config:
        argv = config_arguments(chosen.config, ap) + argv      # the command line comes last, so it wins
    a = ap.parse_args(argv)
    a.callsign, a.tocall = a.callsign.upper(), a.tocall.upper()
    if not CALL_OK.fullmatch(a.callsign):
        ap.error("--callsign must look like W3XYZ or W3XYZ-10 (SSID 0 to 15)")
    if a.callsign.startswith("N0CALL"):
        ap.error("use your own callsign, not N0CALL")
    if not CALL_OK.fullmatch(a.tocall):
        ap.error("--tocall must be up to six letters and digits")
    for hop in a.path.split(","):
        if hop.strip() and not CALL_OK.fullmatch(hop.strip().upper()):
            ap.error(f"--path: {hop.strip()!r} is not a digipeater address")
    if not (-90 <= a.lat <= 90 and -180 <= a.lon <= 180 and 0 < a.miles <= 1000):
        ap.error("--lat must be -90..90, --lon -180..180 and --miles more than 0 and at most 1000")
    if not re.match(r"https?://", a.server):
        ap.error("--server must start with http:// or https://")
    a.poll, a.refresh, a.gap = max(60, a.poll), max(10.0, a.refresh), max(2.0, a.gap)
    a.min = a.min or ("low" if a.auto else "medium")
    if a.max_objects is None:
        a.max_objects = None if a.auto else 10
    else:
        a.max_objects = max(0, a.max_objects)
    a.max_per_poll, a.burst = max(1, a.max_per_poll), max(1, min(3, a.burst))
    return a


def main(argv=None):
    a = parse_args(argv)
    if a.kiss_serial:
        tnc = KissSerial(a.kiss_serial, a.baud)
    else:
        host, _, port = a.kiss_tcp.rpartition(":")
        if not host or not port.isdigit():
            sys.exit("--kiss-tcp must be HOST:PORT, e.g. 127.0.0.1:8001")
        tnc = KissTcp(host, int(port))

    def stop(_sig, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    Gateway(a, tnc, Carried(a.state_file)).run()


if __name__ == "__main__":
    main()
