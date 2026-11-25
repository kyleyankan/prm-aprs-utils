#!/usr/bin/env python3
"""The one program that sends to APRS-IS, for every feed script and the web editor.

  python3 aprs_send.py --callsign W3XYZ-9 --send      # normally started by aprs_ctl.sh (service "send")
  python3 aprs_send.py --list                          # what it knows: each object, last change, last send

The feed scripts and aprs_web.py no longer log in to APRS-IS themselves.  Each poll they
hand over every packet they want on the air by writing a small file into the outbox
folder (data/outbox/).  This program holds a single APRS-IS connection, picks those files
up within a second, and decides packet by packet whether anything needs to go out.

An object is sent when
  * it is new, or its position, symbol or text changed since it was last sent,
  * it is being killed, or
  * it was last sent 26 minutes or more ago (--resend-minutes) and a script still wants it.
Otherwise the packet is dropped: the object is already on the air as it should be.  The
time stamp inside an object packet does not count as a change.

It never invents objects.  An object is only re-sent while a script keeps handing it
over, so when a script stops (or its fetch keeps failing) its objects go quiet.  A kill is
sent once; a repeat of the same kill within the resend time is dropped.

Maximum age: an object that is on the air and that no script has handed over for two hours
(--max-quiet-minutes; 0 = never) is killed by this program, so a source that stays down does
not leave its objects on everyone's map.  When the source returns, its next hand-over puts
them back as new objects.  Nothing is taken off in the first ten minutes after a start.

Duplicates: the same crash often arrives from a county dispatch feed and from a 511 feed.
When two live traffic objects, one from each kind of source, are within --merge-miles
(default 0.3; 0 = off) of each other, only the more important one is sent; at the same level
the county's is kept.  The other is held back, or killed if it is already on the air.

Bulletins: while an object rated critical is on the air, a general bulletin with its text
(":BLN1     :...") is sent with it and repeated like an object.  At most --bulletins (default
3; 0 = off) are in use at once, BLN1 upwards; a slot is free again when its object ends.
/api/packets lists the bulletins whose object is in the area asked for, so radios carry them too.

While APRS-IS cannot be reached, what is due waits (newest version of each object) and
goes out after the next login.  When a connection is found dead, whatever was sent since
the server was last heard from (it sends a line every 20 seconds or so) is sent again, once.
If connections keep dying within a minute, each reconnect waits longer (up to 5 minutes).

Nothing else may log in to APRS-IS with this callsign and its passcode: servers close one
of two such connections.  The listener in aprs_web.py logs in receive-only for that reason.  A waiting object that no script has asked for in the last
30 minutes is dropped instead of being sent late; a waiting kill is always sent.

Minimum level: every object is rated critical, high, medium or low (aprs_priority.py).  With
a minimum level above low (chosen on the web map, or --min-level), live objects rated lower
are held back.  One that is already on the air when the minimum is raised gets a kill packet
on its script's next hand-over.  Kills, status lines and objects the rules do not know are
never held back.

Everything is remembered in sender_state.json, so a restart does not re-send everything.
Without --send nothing is transmitted and no connection is made: the packets that would
have been sent are printed.

For the scripts:   box = aprs_send.Outbox("montco", "W3XYZ-9")
                   box.send(packet) ... ; box.flush()     # one file per poll; box.close() discards
"""
import argparse
import json
import math
import os
import re
import select
import signal
import socket
import sys
import time

import aprs_priority

HOST, PORT = "rotate.aprs2.net", 14580
VERSION = "aprs-send 1.0"
OUTBOX = "outbox"
STATE_FILE = "sender_state.json"
RESEND_MINUTES = 26
STALE_SECONDS = 30 * 60          # a waiting object nobody has asked for this long is not sent late
FORGET_SECONDS = 24 * 3600       # a live object no script has mentioned for this long is forgotten
HELD_FORGET_SECONDS = 3600       # likewise for an object that was held back and never sent
BACKLOG_WARN = 300               # a script warns when the outbox holds a file older than this
SILENT_SECONDS = 90              # servers send a line every ~20 s; this long without one = dead link
KEEPALIVE_SECONDS = 600
MAX_QUIET_MINUTES = 120          # an object on the air that no script has handed over for this long is killed
GRACE_SECONDS = 600              # ... but not this soon after a start: the scripts must get a chance to ask
MERGE_MILES = 0.3                # a county crash and a 511 crash this close together are one incident
BULLETINS = 3                    # most bulletins (BLN1, BLN2, ...) in use at once
COUNTY_SOURCES = {"montco", "chesco", "bucks"}
ROAD_SOURCES = {"pa511", "nj511", "njta"}


def data_dir():
    return os.environ.get("APRS_DATA_DIR") or "."


def outbox_dir(folder=None):
    return folder or os.path.join(data_dir(), OUTBOX)


def passcode(call):
    call = call.split("-")[0].upper()
    h = 0x73E2
    for i in range(0, len(call), 2):
        h ^= ord(call[i]) << 8
        if i + 1 < len(call):
            h ^= ord(call[i + 1])
    return h & 0x7FFF


# --------------------------------------------------------------------------
# For the feed scripts and the web editor: hand packets over
# --------------------------------------------------------------------------
class Outbox:
    """Collects one poll's packets and hands them to aprs_send.py as a single file."""

    def __init__(self, source, callsign, status=None, folder=None):
        self.source, self.callsign = source, callsign.upper()
        self.folder = outbox_dir(folder)
        self.first = [">" + status] if status else []      # a status line goes with the first hand-over
        self.packets = []
        self.warned = 0.0

    def send(self, packet):
        self.packets.append(packet)

    def flush(self):
        """Write what was collected.  Raises OSError if the file cannot be written (nothing is handed over)."""
        packets, self.packets = self.first + self.packets, []
        if not packets:
            return
        os.makedirs(self.folder, exist_ok=True)
        now = time.time()
        name = f"{time.time_ns():020d}-{self.source}-{os.getpid()}.json"
        tmp = os.path.join(self.folder, "." + name + ".tmp")
        with open(tmp, "w") as f:
            json.dump({"source": self.source, "callsign": self.callsign, "time": now, "packets": packets}, f)
        os.replace(tmp, os.path.join(self.folder, name))
        self.first = []
        self.check_backlog(now)

    def check_backlog(self, now):
        try:
            waiting = sorted(n for n in os.listdir(self.folder) if n.endswith(".json") and not n.startswith("."))
            age = now - int(waiting[0][:20]) / 1e9 if waiting else 0
        except (OSError, ValueError):
            return
        if age > BACKLOG_WARN and now - self.warned > 900:
            self.warned = now
            print(f"# {len(waiting)} hand-over file(s) are waiting in {self.folder}, the oldest for "
                  f"{age / 60:.0f} min: nothing is being sent.  Is the sender running?  "
                  f"(aprs_ctl.sh status; aprs_ctl.sh start send)", file=sys.stderr, flush=True)

    def close(self):
        self.packets = []


def hand_over(source, callsign, packets, folder=None):
    """One-off hand-over of a list of packets (used by the web editor)."""
    box = Outbox(source, callsign, folder=folder)
    for p in packets:
        box.send(p)
    box.flush()


# --------------------------------------------------------------------------
# The decision: does this packet need to go out?
# --------------------------------------------------------------------------
def split_packet(packet):
    """-> (key, live, body).  For an object: its name, '*' or '_', and everything after the time stamp."""
    if len(packet) >= 18 and packet[0] == ";" and packet[10] in "*_":
        return packet[1:10], packet[10] == "*", packet[18:]
    return "\x00" + packet, True, packet                   # anything else (a status line): the text is the key


class Book:
    """What has been sent and what is waiting to be sent.  No network in here; time is passed in."""

    def __init__(self, callsign, path=None, resend=RESEND_MINUTES * 60):
        self.callsign, self.path, self.resend = callsign.upper(), path, resend
        # key -> {live, body, sent (None = not yet), changed, seen, source, pending, reason, packet (as last sent)}
        self.records = {}
        self.bulletins = {}          # slot ("1", "2", ...) -> the 9-character name of the object it is for
        self.max_quiet = 0           # written to the state file so the web page can say when objects go
        self.dirty = False
        if path:
            self.load()

    def load(self):
        try:
            with open(self.path) as f:
                raw = json.load(f)
            records = raw["records"]
        except (OSError, ValueError, KeyError, TypeError):
            return
        if str(raw.get("callsign", "")).upper() != self.callsign:
            print(f"# {self.path} was written by {raw.get('callsign')}, not {self.callsign}; ignoring it",
                  file=sys.stderr, flush=True)
            return
        b = raw.get("bulletins")
        self.bulletins = {str(k): str(v) for k, v in b.items()} if isinstance(b, dict) else {}
        for key, r in records.items():
            if isinstance(r, dict) and isinstance(r.get("body"), str):
                self.records[key] = {"live": bool(r.get("live")), "body": r["body"],
                                     "sent": None if r.get("sent") is None else float(r["sent"]),
                                     "changed": float(r.get("changed") or 0), "packet": r.get("packet") or None,
                                     "seen": float(r.get("seen") or 0), "source": str(r.get("source") or ""),
                                     "pending": r.get("pending") or None, "reason": str(r.get("reason") or ""),
                                     "again": bool(r.get("again")), "held": bool(r.get("held")),
                                     "dup": str(r.get("dup") or "")}

    def save(self):
        if not self.path or not self.dirty:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"callsign": self.callsign, "saved": time.time(), "resend": self.resend,
                       "max_quiet": self.max_quiet, "bulletins": self.bulletins, "records": self.records}, f, indent=1)
        os.replace(tmp, self.path)
        self.dirty = False

    def offer(self, packet, source, now, hold=False, dup=""):
        """A script wants this packet on the air.  Returns why it will be sent, or '' if it need not be.
        hold: the object is below the minimum level for sending, or (dup = the other object's name) the
        same incident is already on the air from another source.  It is not sent; if it is on the air
        already it is killed ("taken off"), because an object is never just abandoned."""
        if not packet or len(packet) > 500 or any(ch in packet for ch in "\r\n"):
            return ""
        key, live, body = split_packet(packet)
        r = self.records.get(key)
        held = False
        if live and hold:
            if r and r["live"] and r["sent"] is not None:          # on the air: take it off
                packet = packet[:10] + "_" + packet[11:37]
                live, body, held = False, packet[18:], True
            else:                                                  # never on the air: only remember it, so that
                if r and not r["live"]:                            # the script's own kill later is not sent
                    r["seen"], r["held"], r["dup"] = now, True, dup
                else:
                    self.records[key] = {"live": False, "body": body, "sent": None, "changed": now, "seen": now,
                                         "source": source, "pending": None, "reason": "held", "packet": None,
                                         "again": False, "held": True, "dup": dup}
                self.dirty = True
                return ""
        if not live:
            if r and not r["live"] and r["sent"] is None and not r["pending"]:
                del self.records[key]                      # held back and never sent: nothing to kill
                self.dirty = True
                return ""
            if r and not r["live"] and (r["pending"] or now - r["sent"] < self.resend):
                if held:
                    r["seen"], r["held"], r["dup"] = now, True, dup
                return ""                                  # this kill is already sent or waiting
            reason = "taken off" if held else "kill"
        elif r is None or not r["live"]:
            reason = "new"
        elif r["body"] != body:
            reason = "changed"
        elif r["sent"] is None:
            reason = r["reason"] or "new"                  # still waiting for its first send: keep the reason
        elif now - r["sent"] >= self.resend:
            reason = "refresh"
        else:
            r["seen"], r["source"] = now, source
            if r["pending"]:
                r["pending"] = packet
            return ""
        same = r is not None and r["live"] == live and r["body"] == body      # only being sent again
        changed = r["changed"] if same else now
        self.records[key] = {"live": live, "body": body, "sent": r["sent"] if r else None, "changed": changed,
                             "seen": now, "source": source, "pending": packet, "reason": reason,
                             "packet": r["packet"] if r else None, "again": False, "held": held,
                             "dup": dup if held else ""}
        self.dirty = True
        return reason

    def expire(self, now, max_quiet):
        """Kill every object that is on the air and that no script has handed over for max_quiet seconds.
        Returns {source: how many}.  The kill waits like any other packet; a later hand-over is "new"."""
        gone = {}
        for key, r in self.records.items():
            if key.startswith("\x00") or not r["live"] or r["sent"] is None or r["pending"] or not r["packet"]:
                continue
            if now - r["seen"] <= max_quiet:
                continue
            kill = r["packet"][:10] + "_" + r["packet"][11:37]
            r.update(live=False, body=kill[18:], pending=kill, reason="stale", changed=now, held=False, again=False,
                     dup="")
            gone[r["source"]] = gone.get(r["source"], 0) + 1
        self.dirty = self.dirty or bool(gone)
        return gone

    def due(self, now):
        """[(key, packet, reason)] to send now, oldest request first; drops waiting objects nobody wants any more."""
        out = []
        for key, r in list(self.records.items()):
            if not r["pending"]:
                continue
            if r["live"] and now - r["seen"] > STALE_SECONDS:
                if r["sent"] is None:
                    del self.records[key]
                else:
                    r["pending"] = None
                self.dirty = True
                continue
            out.append((key, r["pending"], r["reason"]))
        return sorted(out, key=lambda x: self.records[x[0]]["seen"])

    def mark_sent(self, key, now):
        r = self.records.get(key)
        if r:
            r["sent"], r["packet"], r["pending"] = now, r["pending"] or r["packet"], None
            self.dirty = True

    def requeue_since(self, when):
        """The connection died: what was sent at or after `when` may not have arrived, so it waits again.
        Each packet gets this second chance once, so a connection that keeps dropping cannot cause a flood."""
        n = 0
        for r in self.records.values():
            if r["sent"] is not None and r["sent"] >= when and not r["pending"] and r["packet"] and not r["again"]:
                r["pending"], r["reason"], r["again"] = r["packet"], "again", True
                n += 1
        self.dirty = self.dirty or n > 0
        return n

    def prune(self, now):
        for key, r in list(self.records.items()):
            if r["pending"]:
                continue
            if r["sent"] is None:                          # held back and never sent, or waiting for its first send
                if r.get("held") and now - r["seen"] > HELD_FORGET_SECONDS:
                    del self.records[key]
                    self.dirty = True
                continue
            if (not r["live"] and now - r["sent"] >= self.resend) or now - r["seen"] > FORGET_SECONDS:
                del self.records[key]
                self.dirty = True


def send_times(path=None):
    """For the web page: object name -> {sent, again, waiting, changed}, read from the sender's state file.
    sent: when it last went to APRS-IS (None = not yet); again: the earliest time an unchanged object
    will be re-sent (it goes out on the first hand-over after that); waiting: due but not sent yet."""
    try:
        with open(path or os.path.join(data_dir(), STATE_FILE)) as f:
            raw = json.load(f)
        resend = float(raw.get("resend") or RESEND_MINUTES * 60)
        out = {}
        for key, r in raw["records"].items():
            if key.startswith("\x00"):
                continue
            if r.get("held"):                              # below the minimum level: not on APRS-IS
                out[key.strip()] = {"sent": None, "again": None, "waiting": False, "changed": None, "held": True,
                                    "dup": r.get("dup") or ""}
            if not r.get("live"):
                if r.get("reason") == "stale":             # taken off because its source went quiet
                    out[key.strip()] = {"sent": None, "again": None, "waiting": False, "changed": None,
                                        "held": False, "stale": True}
                continue
            sent = r.get("sent")
            out[key.strip()] = {"sent": sent, "again": sent + resend if sent is not None else None,
                                "waiting": bool(r.get("pending")), "changed": r.get("changed") or None, "held": False}
        return out
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def sender_info(path=None):
    """For the web page: {max_quiet: seconds an object may go unasked-for before it is killed (0 = never),
    bulletins: {slot: object name}} as the sender last saved them."""
    try:
        with open(path or os.path.join(data_dir(), STATE_FILE)) as f:
            raw = json.load(f)
        slots = raw.get("bulletins") if isinstance(raw.get("bulletins"), dict) else {}
        return {"max_quiet": float(raw.get("max_quiet") or 0),
                "bulletins": {str(k): str(v).strip() for k, v in sorted(slots.items())}}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"max_quiet": 0.0, "bulletins": {}}


def miles_between(a, b):
    """Great-circle distance in miles between two (lat, lon) points."""
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 3958.8 * 2 * math.asin(min(1.0, math.sqrt(h)))


def side_of(source):
    """Which kind of source reported a traffic incident: 'county' dispatch, a 'road' agency, or ''."""
    return "county" if source in COUNTY_SOURCES else "road" if source in ROAD_SOURCES else ""


def bulletin_packet(slot, packet):
    """The general bulletin for one object packet: its text without any outline, at most 67 characters."""
    text = re.split(r" \}", packet[37:])[0]
    text = re.sub(r"[|~{}]", "", text).strip()[:67].rstrip()
    return f":BLN{slot}     :{text}" if text else ""


POSITION = re.compile(r"^;.{9}[*_]\d{6}[zh/](\d\d)(\d\d\.\d\d)([NS]).(\d{3})(\d\d\.\d\d)([EW])")


def packet_position(packet):
    """(lat, lon) in degrees from an object packet with a plain (uncompressed) position, else None."""
    m = POSITION.match(packet or "")
    if not m:
        return None
    lat = int(m[1]) + float(m[2]) / 60
    lon = int(m[4]) + float(m[5]) / 60
    return (-lat if m[3] == "S" else lat, -lon if m[6] == "W" else lon)


def on_air(path=None):
    """For the web service: every object packet last sent to APRS-IS, live ones and recent kills.
    -> [{name, live, packet, lat, lon, sent, changed, source}]; objects still waiting for their first
    send, and anything whose position cannot be read, are left out."""
    try:
        with open(path or os.path.join(data_dir(), STATE_FILE)) as f:
            records = json.load(f)["records"]
        out = []
        for key, r in records.items():
            pos = packet_position(r.get("packet"))
            if key.startswith("\x00") or r.get("sent") is None or not pos:
                continue
            out.append({"name": key.strip(), "live": r["packet"][10] == "*", "packet": r["packet"],
                        "lat": pos[0], "lon": pos[1], "sent": r["sent"], "changed": r.get("changed") or r["sent"],
                        "source": str(r.get("source") or "")})
        return out
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        return []


def bulletins_on_air(path=None):
    """For /api/packets: each bulletin that has gone to APRS-IS and whose object is still live, with
    that object's position so a radio can tell whether it is in its area.
    -> [{slot, name (the object's), packet, lat, lon, sent}]"""
    try:
        with open(path or os.path.join(data_dir(), STATE_FILE)) as f:
            raw = json.load(f)
        records, out = raw["records"], []
        for slot, name in sorted((raw.get("bulletins") or {}).items()):
            o = records.get(name) or {}
            pos = packet_position(o.get("packet")) if o.get("live") else None
            packet = bulletin_packet(slot, o["packet"]) if pos else ""
            b = records.get("\x00" + packet) or {}
            if packet and b.get("sent") is not None:
                out.append({"slot": str(slot), "name": name.strip(), "packet": packet, "lat": pos[0], "lon": pos[1],
                            "sent": b["sent"]})
        return out
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        return []


# --------------------------------------------------------------------------
# The single APRS-IS connection
# --------------------------------------------------------------------------
class Link:
    def __init__(self, callsign, code, host, port):
        self.callsign, self.code, self.host, self.port = callsign, code, host, port
        self.sock, self.buf = None, b""
        self.heard = self.spoke = 0.0
        self.retry_at, self.delay, self.told = 0.0, 15, False
        self.doubt = None            # set when the connection dies: packets sent since then may be lost
        self.since, self.short = 0.0, 0     # when this connection began; how many in a row died young

    def up(self):
        return self.sock is not None

    def connect(self, now):
        """Log in if it is time to try.  Returns True when connected."""
        if self.sock or now < self.retry_at:
            return self.up()
        sock = None
        try:
            sock = socket.create_connection((self.host, self.port), timeout=20)
            f = sock.makefile("rb")
            f.readline()                                               # server banner
            sock.sendall(f"user {self.callsign} pass {self.code} vers {VERSION}\r\n".encode())
            resp = f.readline().decode("latin-1", "replace").strip()
            if "logresp" not in resp.lower() or "unverified" in resp.lower():
                raise OSError(f"login not verified: {resp[:80]}")
            f.close()                                                  # the socket itself stays open
            sock.setblocking(False)
            self.sock, self.buf, self.heard, self.spoke, self.delay, self.told = sock, b"", now, now, 15, False
            self.since = now
            print(f"# connected to APRS-IS ({self.host}:{self.port}) as {self.callsign}", file=sys.stderr, flush=True)
            return True
        except OSError as e:
            if sock:
                sock.close()
            if not self.told:
                print(f"# APRS-IS: {e}; trying again every {self.delay}s and longer", file=sys.stderr, flush=True)
                self.told = True
            self.retry_at, self.delay = now + self.delay, min(self.delay * 2, 300)
            return False

    def drop(self, why, now):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.doubt = self.heard                        # the last moment the server was known to be there
            self.short = self.short + 1 if now - self.since < 60 else 0
            wait = min(300, 5 * 2 ** self.short)           # 5 s normally; longer each time a connection dies young
            print(f"# APRS-IS connection lost ({why}); reconnecting in {wait}s", file=sys.stderr, flush=True)
            if self.short == 3:
                print("# the connection keeps being closed straight after login: is something else logged in "
                      f"to APRS-IS as {self.callsign} with a passcode?", file=sys.stderr, flush=True)
            self.retry_at = now + wait
        self.sock = None

    def write(self, line, now):
        """Send one line.  Returns False (and drops the connection) if it could not all be sent."""
        data = (line + "\r\n").encode("ascii", "replace")
        end = time.time() + 20
        try:
            while data:
                _, w, _ = select.select([], [self.sock], [], max(0.1, end - time.time()))
                if not w:
                    raise OSError("send timed out")
                data = data[self.sock.send(data):]
        except OSError as e:
            self.drop(str(e), now)
            return False
        self.spoke = now
        return True

    def idle(self, seconds, now):
        """Wait up to `seconds`, reading (and ignoring) what the server sends; notice a dead connection."""
        if not self.sock:
            if seconds:
                time.sleep(seconds)
            return
        try:
            r, _, _ = select.select([self.sock], [], [], seconds)
            if r:
                data = self.sock.recv(4096)
                if not data:
                    return self.drop("closed by server", now)
                self.heard = now
        except OSError as e:
            return self.drop(str(e), now)
        if now - self.heard > SILENT_SECONDS:
            return self.drop(f"nothing heard for {SILENT_SECONDS}s", now)
        if now - self.spoke > KEEPALIVE_SECONDS:
            self.write("# aprs-send keepalive", now)


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------
class Sender:
    def __init__(self, callsign, send=False, folder=None, state=None, link=None, resend=RESEND_MINUTES * 60, pace=0.05,
                 min_level=aprs_priority.DEFAULT_MIN, settings_dir=None, max_quiet=MAX_QUIET_MINUTES * 60,
                 merge_miles=MERGE_MILES, bulletins=BULLETINS):
        self.callsign, self.transmit, self.link, self.pace = callsign.upper(), send, link, pace
        self.folder = outbox_dir(folder)
        self.book = Book(self.callsign, state, resend)
        self.max_quiet, self.merge_miles, self.slots = max_quiet, merge_miles, max(0, min(9, int(bulletins)))
        self.book.max_quiet = max_quiet
        self.started = None                                    # when the first step ran
        self.min_default, self.min_level = min_level, None     # the web map's choice wins over the default
        self.settings_dir = settings_dir or (os.path.dirname(os.path.abspath(state)) if state else None)

    def read_min_level(self):
        level = aprs_priority.load_min_level(self.min_default, self.settings_dir)
        if level != self.min_level:
            what = "everything is sent" if level == aprs_priority.LEVELS[0] else f"objects below {level} are held back"
            print(f"# minimum level for sending: {level} ({what})", file=sys.stderr, flush=True)
            self.min_level = level
        return level

    def below_minimum(self, packet, source):
        """True for a live object the rules rate below the minimum level.  Kills, status lines and
        objects the rules do not know are never held back."""
        if self.min_level == aprs_priority.LEVELS[0] or packet[10:11] != "*":
            return False
        r = aprs_priority.rate_packet(packet, source)
        return bool(r and r["rated"] and not aprs_priority.at_least(r["level"], self.min_level))

    def rank(self, packet, source):
        """Which of two reports of one incident is kept: the more important; at the same level the county's."""
        r = aprs_priority.rate_packet(packet, source)
        return (aprs_priority.LEVELS.index(r["level"]) if r else 0, side_of(source) == "county")

    def duplicate_of(self, packet, source):
        """The name of a live traffic object from the other kind of source that is the same incident and
        is the one to keep, or ''.  Only county dispatch against road agencies, never within one kind."""
        mine = side_of(source)
        if not self.merge_miles or not mine or packet[10:11] != "*":
            return ""
        pos = packet_position(packet)
        if not pos or aprs_priority.kind_of(packet[1:10], source)[0] != "traffic":
            return ""
        rank = self.rank(packet, source)
        for key, r in self.book.records.items():
            other = r["pending"] or r["packet"]
            if not r["live"] or not other or key == packet[1:10]:
                continue
            theirs = side_of(r["source"])
            if not theirs or theirs == mine or aprs_priority.kind_of(key, r["source"])[0] != "traffic":
                continue
            there = packet_position(other)
            if there and miles_between(pos, there) <= self.merge_miles and self.rank(other, r["source"]) > rank:
                return key.strip()
        return ""

    def bulletin(self, packet, source, hold, when):
        """Keep a bulletin going for a critical object that is on the air; give its slot back otherwise."""
        if not self.slots or len(packet) < 38 or packet[0] != ";" or packet[10] not in "*_":
            return
        name, slots = packet[1:10], self.book.bulletins
        mine = next((s for s, n in slots.items() if n == name), None)
        r = aprs_priority.rate_packet(packet, source) if packet[10] == "*" and not hold else None
        if not (r and r["rated"] and r["level"] == "critical"):
            if mine:
                del slots[mine]
                self.book.dirty = True
            return
        if not mine:
            mine = next((str(n) for n in range(1, self.slots + 1) if str(n) not in slots), None)
            if not mine:
                return                                         # every slot is in use: no bulletin for this one
            slots[mine] = name
            self.book.dirty = True
        text = bulletin_packet(mine, packet)
        if text and self.book.offer(text, "bulletin", when):
            print(f"# bulletin BLN{mine} for {name.strip()}", file=sys.stderr, flush=True)

    def free_bulletins(self):
        """A slot whose object is no longer live (killed, taken off, forgotten) is free again."""
        for slot, name in list(self.book.bulletins.items()):
            r = self.book.records.get(name)
            if not r or not r["live"]:
                del self.book.bulletins[slot]
                self.book.dirty = True

    def take_outbox(self, now):
        """Read every waiting hand-over file, oldest first.  Returns the names read (delete them after saving)."""
        self.read_min_level()
        try:
            names = sorted(n for n in os.listdir(self.folder) if n.endswith(".json") and not n.startswith("."))
        except OSError:
            return []
        for name in names:
            try:
                with open(os.path.join(self.folder, name)) as f:
                    d = json.load(f)
                source, call, packets = str(d["source"]), str(d["callsign"]).upper(), list(d["packets"])
                when = float(d.get("time") or now)
            except (OSError, ValueError, KeyError, TypeError) as e:
                print(f"# ignoring unreadable hand-over file {name}: {e}", file=sys.stderr, flush=True)
                continue
            if call != self.callsign:
                print(f"# ignoring {len(packets)} packet(s) from {source}: they are for {call}, "
                      f"this sender is {self.callsign}", file=sys.stderr, flush=True)
                continue
            when = min(when, now)                              # an old file counts as asked-for when it was written
            reasons, held, dups = {}, 0, 0
            for p in packets:
                if not isinstance(p, str):
                    continue
                dup = self.duplicate_of(p, source)
                hold = bool(dup) or self.below_minimum(p, source)
                why = self.book.offer(p, source, when, hold, dup)
                self.bulletin(p, source, hold, when)
                held += hold and not why and not dup
                dups += bool(dup) and not why
                reasons[why] = reasons.get(why, 0) + (not (hold and not why))
            wanted = ", ".join(f"{n} {why}" for why, n in sorted(reasons.items()) if why)
            if wanted:
                back = f"; {held} held back (below {self.min_level})" if held else ""
                back += f"; {dups} held back (same incident as another source's object)" if dups else ""
                print(f"# {source}: {wanted}; {reasons.get('', 0)} need no sending{back}", file=sys.stderr, flush=True)
        return names

    def clear(self, names):
        for name in names:
            try:
                os.remove(os.path.join(self.folder, name))
            except OSError:
                pass

    def send_due(self, now):
        """Send what is waiting.  Returns how many packets went out (or were printed, without --send)."""
        if self.transmit:
            self.link.idle(0, now)                             # notice a connection the server has closed
            if self.link.doubt is not None:
                n = self.book.requeue_since(self.link.doubt)
                self.link.doubt = None
                if n:
                    print(f"# {n} packet(s) sent just before the connection was lost will be sent again",
                          file=sys.stderr, flush=True)
        due = self.book.due(now)
        if not due or (self.transmit and not self.link.connect(now)):
            return 0
        n = 0
        for key, packet, reason in due:
            line = f"{self.callsign}>APRS,TCPIP*:{packet}"
            if self.transmit and not self.link.write(line, now):
                break
            print(line, flush=True)
            self.book.mark_sent(key, now)
            n += 1
            if self.transmit and self.pace:
                time.sleep(self.pace)
        return n

    def step(self, now=None):
        now = time.time() if now is None else now
        names = self.take_outbox(now)
        if self.started is None:
            self.started = now
        if self.max_quiet and now - self.started >= GRACE_SECONDS:
            for source, n in sorted(self.book.expire(now, self.max_quiet).items()):
                print(f"# {source or 'unknown'}: {n} object(s) taken off: not handed over for "
                      f"{self.max_quiet / 60:g} min (is the source down?)", file=sys.stderr, flush=True)
        self.free_bulletins()
        sent = self.send_due(now)
        self.book.prune(now)
        self.book.save()                                       # before the files go: a crash here repeats, never loses
        self.clear(names)
        return sent

    def run(self):
        mode = "sending" if self.transmit else "print-only: nothing is transmitted"
        print(f"# sender for {self.callsign} ({mode}); outbox {os.path.abspath(self.folder)}; "
              f"re-send after {self.book.resend / 60:g} min", file=sys.stderr, flush=True)
        try:
            while True:
                now = time.time()
                if self.transmit:
                    self.link.connect(now)
                self.step(now)
                if self.transmit:
                    self.link.idle(1.0, time.time())
                else:
                    time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            self.book.dirty = True
            self.book.save()


def show(book, now):
    def ago(t):
        return "never" if not t else f"{(now - t) / 60:.0f} min ago"
    print(f"{'NAME':<9} {'STATE':<8} {'FROM':<8} {'CHANGED':<12} {'SENT':<12} NOTE")
    for key, r in sorted(book.records.items()):
        name = key if not key.startswith("\x00") else "(status)"
        state = "live" if r["live"] else "killed"
        note = f"waiting to be sent ({r['reason']})" if r["pending"] else ""
        if r["live"] and not r["pending"] and r["sent"] is not None:
            left = (book.resend - (now - r["sent"])) / 60
            note = "re-send due when next handed over" if left <= 0 else f"re-send due in {left:.0f} min"
        print(f"{name:<9} {state:<8} {r['source']:<8} {ago(r['changed']):<12} {ago(r['sent']):<12} {note}")
    print(f"{len(book.records)} entr{'y' if len(book.records) == 1 else 'ies'}")


def main():
    ap = argparse.ArgumentParser(description="The single APRS-IS sender for the feed scripts and the web editor")
    ap.add_argument("--callsign", default="N0CALL", help="callsign-SSID to log in and send as")
    ap.add_argument("--passcode", type=int, help="APRS-IS passcode (default: computed from the callsign)")
    ap.add_argument("--send", action="store_true", help="really send to APRS-IS (without it, packets are only printed)")
    ap.add_argument("--resend-minutes", type=float, default=RESEND_MINUTES,
                    help=f"re-send an unchanged object after this many minutes (default {RESEND_MINUTES}, minimum 5)")
    ap.add_argument("--min-level", default=aprs_priority.DEFAULT_MIN, choices=aprs_priority.LEVELS,
                    help="send only objects rated at least this important; the choice made on the web map "
                         "(sender_settings.json) wins over this (default low = everything; see aprs_priority.py)")
    ap.add_argument("--max-quiet-minutes", type=float, default=MAX_QUIET_MINUTES,
                    help="kill an object on the air that no script has handed over for this many minutes "
                         f"(default {MAX_QUIET_MINUTES}, minimum 30; 0 = never)")
    ap.add_argument("--merge-miles", type=float, default=MERGE_MILES,
                    help="a county dispatch crash and a 511 crash within this distance are one incident, and only "
                         f"the more important is sent (default {MERGE_MILES}; 0 = off)")
    ap.add_argument("--bulletins", type=int, default=BULLETINS,
                    help=f"most bulletins for critical objects in use at once, BLN1 upwards (default {BULLETINS}, "
                         "at most 9; 0 = off)")
    ap.add_argument("--outbox", help=f"hand-over folder (default: {OUTBOX}/ in $APRS_DATA_DIR, else in the current folder)")
    ap.add_argument("--state-file", help=f"default: {STATE_FILE} beside the outbox folder")
    ap.add_argument("--list", action="store_true", help="show what is on the air and when each object was sent, then exit")
    ap.add_argument("--interval", type=int, default=0, help="ignored (accepted so aprs_ctl.sh can launch this)")
    ap.add_argument("--comment-length", type=int, default=0, help="ignored (accepted so aprs_ctl.sh can launch this)")
    args = ap.parse_args()
    args.callsign = args.callsign.upper()
    state = args.state_file or os.path.join(data_dir(), STATE_FILE)
    resend = max(5.0, args.resend_minutes) * 60
    if args.list:
        return show(Book(args.callsign, state, resend), time.time())
    if args.send and args.callsign.startswith("N0CALL"):
        ap.error("--send requires your own callsign (--callsign)")
    link = None
    if args.send:
        link = Link(args.callsign, args.passcode or passcode(args.callsign),
                    os.environ.get("APRS_IS_HOST") or HOST, int(os.environ.get("APRS_IS_PORT") or PORT))

    def stop(_sig, _frame):                                    # aprs_ctl.sh stops services with SIGTERM
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    os.makedirs(outbox_dir(args.outbox), exist_ok=True)
    quiet = max(30.0, args.max_quiet_minutes) * 60 if args.max_quiet_minutes > 0 else 0
    Sender(args.callsign, args.send, args.outbox, state, link, resend, min_level=args.min_level, max_quiet=quiet,
           merge_miles=max(0.0, args.merge_miles), bulletins=args.bulletins).run()


if __name__ == "__main__":
    main()
