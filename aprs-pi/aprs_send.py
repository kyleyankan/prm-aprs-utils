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

It never invents traffic.  An object is only re-sent while a script keeps handing it
over, so when a script stops (or its fetch keeps failing) its objects go quiet exactly as
before; this program does not kill or refresh anything on its own.  A kill is sent once;
a repeat of the same kill within the resend time is dropped.

While APRS-IS cannot be reached, what is due waits (newest version of each object) and
goes out after the next login.  When a connection is found dead, whatever was sent since
the server was last heard from (it sends a line every 20 seconds or so) is sent again.  A waiting object that no script has asked for in the last
30 minutes is dropped instead of being sent late; a waiting kill is always sent.

Everything is remembered in sender_state.json, so a restart does not re-send everything.
Without --send nothing is transmitted and no connection is made: the packets that would
have been sent are printed.

For the scripts:   box = aprs_send.Outbox("montco", "W3XYZ-9")
                   box.send(packet) ... ; box.flush()     # one file per poll; box.close() discards
"""
import argparse
import json
import os
import select
import signal
import socket
import sys
import time

HOST, PORT = "rotate.aprs2.net", 14580
VERSION = "aprs-send 1.0"
OUTBOX = "outbox"
STATE_FILE = "sender_state.json"
RESEND_MINUTES = 26
STALE_SECONDS = 30 * 60          # a waiting object nobody has asked for this long is not sent late
FORGET_SECONDS = 24 * 3600       # a live object no script has mentioned for this long is forgotten
BACKLOG_WARN = 300               # a script warns when the outbox holds a file older than this
SILENT_SECONDS = 90              # servers send a line every ~20 s; this long without one = dead link
KEEPALIVE_SECONDS = 600


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
        for key, r in records.items():
            if isinstance(r, dict) and isinstance(r.get("body"), str):
                self.records[key] = {"live": bool(r.get("live")), "body": r["body"],
                                     "sent": None if r.get("sent") is None else float(r["sent"]),
                                     "changed": float(r.get("changed") or 0), "packet": r.get("packet") or None,
                                     "seen": float(r.get("seen") or 0), "source": str(r.get("source") or ""),
                                     "pending": r.get("pending") or None, "reason": str(r.get("reason") or "")}

    def save(self):
        if not self.path or not self.dirty:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"callsign": self.callsign, "saved": time.time(), "records": self.records}, f, indent=1)
        os.replace(tmp, self.path)
        self.dirty = False

    def offer(self, packet, source, now):
        """A script wants this packet on the air.  Returns why it will be sent, or '' if it need not be."""
        if not packet or len(packet) > 500 or any(ch in packet for ch in "\r\n"):
            return ""
        key, live, body = split_packet(packet)
        r = self.records.get(key)
        if not live:
            if r and not r["live"] and (r["pending"] or now - r["sent"] < self.resend):   # not live: sent or waiting
                return ""                                  # this kill is already sent or waiting
            reason = "kill"
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
                             "packet": r["packet"] if r else None}
        self.dirty = True
        return reason

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
        """The connection died: what was sent at or after `when` may not have arrived, so it waits again."""
        n = 0
        for r in self.records.values():
            if r["sent"] is not None and r["sent"] >= when and not r["pending"] and r["packet"]:
                r["pending"], r["reason"] = r["packet"], "again"
                n += 1
        self.dirty = self.dirty or n > 0
        return n

    def prune(self, now):
        for key, r in list(self.records.items()):
            if r["pending"] or r["sent"] is None:
                continue
            if (not r["live"] and now - r["sent"] >= self.resend) or now - r["seen"] > FORGET_SECONDS:
                del self.records[key]
                self.dirty = True


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
            print(f"# APRS-IS connection lost ({why}); reconnecting", file=sys.stderr, flush=True)
            self.doubt = self.heard                        # the last moment the server was known to be there
        self.sock, self.retry_at = None, now + 5

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
    def __init__(self, callsign, send=False, folder=None, state=None, link=None, resend=RESEND_MINUTES * 60, pace=0.05):
        self.callsign, self.transmit, self.link, self.pace = callsign.upper(), send, link, pace
        self.folder = outbox_dir(folder)
        self.book = Book(self.callsign, state, resend)

    def take_outbox(self, now):
        """Read every waiting hand-over file, oldest first.  Returns the names read (delete them after saving)."""
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
            reasons = {}
            for p in packets:
                why = self.book.offer(p, source, when) if isinstance(p, str) else ""
                reasons[why] = reasons.get(why, 0) + 1
            wanted = ", ".join(f"{n} {why}" for why, n in sorted(reasons.items()) if why)
            if wanted:
                print(f"# {source}: {wanted}; {reasons.get('', 0)} need no sending", file=sys.stderr, flush=True)
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
    Sender(args.callsign, args.send, args.outbox, state, link, resend).run()


if __name__ == "__main__":
    main()
