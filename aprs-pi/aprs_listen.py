"""Listens to APRS-IS for everything heard around a point: objects, items and stations.

Used by aprs_web.py for  GET /api/aprs , the "other APRS" layer on the web map and
/eink-all.png.  It only receives (nothing is transmitted), keeps what it hears in
memory, and forgets anything not heard again within `max_age` seconds.  After a
restart the picture fills in again over the next half hour or so, as each object
or station is re-sent by its owner.

It also remembers where each one has been (Trails): every time a position changes
by more than about 30 m the new point is added to that object's path, and points
older than `trail_minutes` are dropped.

It also keeps the latest APRS text messages people send each other (not position
reports): acknowledgements, telemetry definitions, bulletins and weather-service
broadcasts are left out, and a message that is re-sent while waiting for its
acknowledgement is listed once.

state()/restore() let the web service save all of this to disk and load it again
after a restart.  backfill() is for a cold start: APRS-IS itself keeps no history,
but servers running javAPRSSrvr offer a "full feed with history" port (10151) that
first sends the last position of every station heard in about the last 30 minutes.
backfill() reads that port briefly, keeps what lies inside the listening radius and
disconnects.  It is best-effort: if no server offers the port, nothing is lost.
"""
import math
import re
import socket
import sys
import threading
import time

HOST, PORT = "rotate.aprs2.net", 14580
HISTORY_HOST, HISTORY_PORT = "rotate.aprs.net", 10151     # javAPRSSrvr "full feed with history"
MAX_ENTRIES = 5000
MAX_MESSAGES = 200

UNCOMP = re.compile(r"^(\d\d)([\d ]{2}\.[\d ]{2})([NS])(.)(\d{3})([\d ]{2}\.[\d ]{2})([EW])(.)(.*)$", re.S)
TABLE_OK = re.compile(r"[/\\A-Za-j0-9]")


class Trails:
    """Recent positions per key: {key: [[lat, lon, time], ...]} with age and length limits."""
    def __init__(self, minutes=180, max_points=300, min_move=0.0003):
        self.max_age, self.max_points, self.min_move = minutes * 60, max_points, min_move
        self.lock = threading.Lock()
        self.points = {}

    def add(self, key, lat, lon, now=None):
        now = now or time.time()
        with self.lock:
            pts = self.points.setdefault(key, [])
            if pts and abs(pts[-1][0] - lat) < self.min_move and abs(pts[-1][1] - lon) < self.min_move:
                return                                   # has not moved (or only GPS jitter)
            pts.append([round(lat, 5), round(lon, 5), now])
            cutoff = now - self.max_age
            while len(pts) > 1 and (pts[0][2] < cutoff or len(pts) > self.max_points):
                del pts[0]

    def get(self, key, now=None):
        """[[lat, lon], ...] oldest first; empty unless it has been in at least two places."""
        cutoff = (now or time.time()) - self.max_age
        with self.lock:
            pts = [p for p in self.points.get(key, []) if p[2] >= cutoff]
            if self.points.get(key):
                last = self.points[key][-1]
                if not pts or pts[-1] is not last:
                    pts.append(last)                     # always end at the current position
        return [[p[0], p[1]] for p in pts] if len(pts) >= 2 else []

    def drop(self, key):
        with self.lock:
            self.points.pop(key, None)

    def dump(self):
        with self.lock:
            return {k if isinstance(k, str) else "|".join(k): [list(p) for p in v] for k, v in self.points.items()}

    def restore(self, data, tuple_keys=False, now=None):
        cutoff = (now or time.time()) - self.max_age
        with self.lock:
            for k, pts in (data or {}).items():
                try:
                    pts = [[float(a), float(b), float(t)] for a, b, t in pts]
                except (TypeError, ValueError):
                    continue
                if pts and pts[-1][2] >= cutoff:
                    self.points[tuple(k.split("|", 1)) if tuple_keys else k] = pts[-self.max_points:]

    def keep_only(self, keys):
        keys = set(keys)
        with self.lock:
            for k in [k for k in self.points if k not in keys]:
                del self.points[k]


def passcode(call):
    call = call.split("-")[0].upper()
    h = 0x73E2
    for i in range(0, len(call), 2):
        h ^= ord(call[i]) << 8
        if i + 1 < len(call):
            h ^= ord(call[i + 1])
    return h & 0x7FFF


def parse_position(body):
    """(lat, lon, table, code, comment) from an uncompressed or compressed position, else None."""
    m = UNCOMP.match(body)
    if m:
        lat = int(m.group(1)) + float(m.group(2).replace(" ", "0")) / 60
        lon = int(m.group(5)) + float(m.group(6).replace(" ", "0")) / 60
        return (-lat if m.group(3) == "S" else lat, -lon if m.group(7) == "W" else lon,
                m.group(4), m.group(8), m.group(9))
    if len(body) >= 13 and TABLE_OK.match(body[0]) and not body[0].isdigit() \
            and all(33 <= ord(ch) <= 124 for ch in body[1:9]):
        def b91(s):
            v = 0
            for ch in s:
                v = v * 91 + ord(ch) - 33
            return v
        return (90 - b91(body[1:5]) / 380926.0, -180 + b91(body[5:9]) / 190463.0,
                body[0], body[9], body[13:])
    return None


def parse_mice(dest, info):
    """Mic-E position (most mobile trackers): latitude is in the destination call."""
    if len(dest) < 6 or len(info) < 9:
        return None
    digits = []
    for ch in dest[:6]:
        if "0" <= ch <= "9":
            digits.append(ord(ch) - 48)
        elif "A" <= ch <= "J":
            digits.append(ord(ch) - 65)
        elif "P" <= ch <= "Y":
            digits.append(ord(ch) - 80)
        elif ch in "KLZ":
            digits.append(0)
        else:
            return None
    lat = digits[0] * 10 + digits[1] + (digits[2] * 10 + digits[3] + (digits[4] * 10 + digits[5]) / 100.0) / 60
    if not "P" <= dest[3] <= "Z":
        lat = -lat
    d = ord(info[1]) - 28 + (100 if "P" <= dest[4] <= "Z" else 0)
    if 180 <= d <= 189:
        d -= 80
    elif 190 <= d <= 199:
        d -= 190
    mnt = ord(info[2]) - 28
    if mnt >= 60:
        mnt -= 60
    lon = d + (mnt + (ord(info[3]) - 28) / 100.0) / 60
    if "P" <= dest[5] <= "Z":
        lon = -lon
    return lat, lon, info[8], info[7], info[9:]


MESSAGE = re.compile(r"^:([A-Za-z0-9 \-]{9}):(.*)$", re.S)
NOT_CHAT = re.compile(r"^(BLN|NWS|SKY|CWA|QST|ALL|ANSRVR|WXBOT|WLNK|SMS|EMAIL|TELEM|TLM)", re.I)


def parse_message(line):
    """A person-to-person APRS message -> dict(src, to, text), else None."""
    head, sep, info = line.partition(":")
    src, gt, _rest = head.partition(">")
    if not sep or not gt or not src:
        return None
    if info.startswith("}"):                              # relayed by a gateway: the real packet is inside
        return parse_message(info[1:])
    m = MESSAGE.match(info)
    if not m:
        return None
    to, text = m.group(1).strip(), m.group(2)
    text = re.sub(r"\{[A-Za-z0-9}]{1,5}\s*$", "", text)     # message number
    text = re.sub(r"\s+", " ", text.encode("ascii", "replace").decode()).strip()
    low = text.lower()
    if (not to or not text or to.upper() == src.upper() or low.startswith(("ack", "rej"))
            or re.match(r"^(PARM|UNIT|EQNS|BITS)\.", text) or NOT_CHAT.match(to)):
        return None
    return {"src": src, "to": to, "text": text[:200]}


def parse_line(line):
    """One APRS-IS line -> dict(kind, name, src, live, lat, lon, table, code, comment), or None."""
    head, sep, info = line.partition(":")
    src, gt, rest = head.partition(">")
    if not sep or not gt or not info or not src:
        return None
    dest = rest.split(",")[0]
    t, pos, kind, name, live = info[0], None, "station", src, True
    if t == ";" and len(info) >= 31 and info[10] in "*_":
        kind, name, live = "object", info[1:10].strip(), info[10] == "*"
        pos = parse_position(info[18:])
    elif t == ")":
        m = re.match(r"\)([^!_]{3,9})([!_])(.*)$", info, re.S)
        if m:
            kind, name, live = "item", m.group(1).strip(), m.group(2) == "!"
            pos = parse_position(m.group(3))
    elif t in "!=":
        pos = parse_position(info[1:])
    elif t in "/@" and len(info) > 8:
        pos = parse_position(info[8:])
    elif t in "`'":
        pos = parse_mice(dest, info)
    if not pos or not name:
        return None
    lat, lon, table, code, comment = pos
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (abs(lat) < 0.001 and abs(lon) < 0.001):
        return None
    comment = re.sub(r"\s+", " ", comment.encode("ascii", "replace").decode()).strip()[:120]
    return {"kind": kind, "name": name, "src": src, "live": live, "lat": round(lat, 5), "lon": round(lon, 5),
            "table": table, "code": code, "comment": comment}


class Listener:
    def __init__(self, callsign, lat, lon, radius_miles=50, max_age=5400, own=None, host=HOST, port=PORT,
                 login_pass=None, trail_minutes=180):
        self.callsign = (callsign or "N0CALL").upper()
        self.lat, self.lon, self.radius = lat, lon, radius_miles
        self.max_age, self.host, self.port = max_age, host, port
        self.own = (own or self.callsign).upper()        # packets from this call are our own objects: skipped
        self.login_pass = login_pass if login_pass is not None else (
            -1 if self.callsign.startswith("N0CALL") else passcode(self.callsign))
        self.lock = threading.Lock()
        self.heard = {}                                  # (kind, name) -> record
        self.trails = Trails(trail_minutes)
        self.connected, self.since, self.packets, self.error = False, 0.0, 0, ""
        self.messages = []                               # newest last: {time, src, to, text, count}

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()
        return self

    def state(self):
        """Everything worth keeping across a restart, as plain JSON data."""
        with self.lock:
            heard = [dict(r) for r in self.heard.values()]
            messages = [dict(m) for m in self.messages]
        return {"heard": heard, "trails": self.trails.dump(), "messages": messages}

    def restore(self, data):
        """Load state() back in, skipping whatever has expired meanwhile.  Returns how many were kept."""
        cutoff, n = time.time() - self.max_age, 0
        with self.lock:
            for r in (data or {}).get("heard") or []:
                try:
                    if float(r["heard"]) >= cutoff and r["kind"] in ("object", "item", "station"):
                        float(r["lat"]); float(r["lon"])
                        self.heard[(r["kind"], str(r["name"]))] = dict(r)
                        n += 1
                except (KeyError, TypeError, ValueError):
                    continue
            for m in (data or {}).get("messages") or []:
                try:
                    self.messages.append({"time": float(m["time"]), "src": str(m["src"]), "to": str(m["to"]),
                                          "text": str(m["text"]), "count": int(m.get("count", 1))})
                except (KeyError, TypeError, ValueError):
                    continue
            self.messages = self.messages[-MAX_MESSAGES:]
        self.trails.restore((data or {}).get("trails"), tuple_keys=True)
        return n

    def add_message(self, msg, now=None):
        now = now or time.time()
        with self.lock:
            for m in reversed(self.messages[-40:]):       # a re-send of something recent: count it, do not repeat it
                if (m["src"], m["to"], m["text"]) == (msg["src"], msg["to"], msg["text"]) and now - m["time"] < 900:
                    m["count"] += 1
                    return
            self.messages.append({"time": now, "src": msg["src"], "to": msg["to"], "text": msg["text"], "count": 1})
            del self.messages[:-MAX_MESSAGES]

    def latest_messages(self, limit=20):
        with self.lock:
            return [dict(m) for m in self.messages[-limit:]][::-1]      # newest first

    def backfill(self, host=HISTORY_HOST, port=HISTORY_PORT, seconds=40, max_lines=400000):
        """Read a 'full feed with history' port for a short while and keep what is inside the radius."""
        seen = kept = 0
        sock = None
        try:
            sock = socket.create_connection((host, port), timeout=15)
            f = sock.makefile("rb")
            f.readline()
            sock.sendall(f"user {self.callsign} pass -1 vers aprs-web-listen 1.0\r\n".encode())
            sock.settimeout(10)
            end = time.time() + seconds
            klat = 69.0
            klon = 69.172 * math.cos(math.radians(self.lat))
            while time.time() < end and seen < max_lines:
                raw = f.readline()
                if not raw:
                    break
                line = raw.decode("latin-1").rstrip("\r\n")
                if not line or line.startswith("#"):
                    continue
                seen += 1
                try:
                    rec = parse_line(line)
                    if rec and math.hypot((rec["lat"] - self.lat) * klat, (rec["lon"] - self.lon) * klon) <= self.radius:
                        key = (rec["kind"], rec["name"])
                        with self.lock:
                            fresh = key not in self.heard
                        if fresh or not rec["live"]:      # never overwrite something heard live with older history
                            self.handle(line, rec=rec)
                            kept += 1
                except Exception:
                    pass
            print(f"# APRS history: read {seen} recent packets from {host}:{port}, {kept} inside {self.radius:g} miles",
                  file=sys.stderr, flush=True)
        except Exception as e:
            print(f"# APRS history not available from {host}:{port} ({e}); the map will fill in as stations re-send",
                  file=sys.stderr, flush=True)
        finally:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
        return kept

    def handle(self, line, now=None, rec=None):
        rec = rec or parse_line(line)
        if not rec:
            msg = parse_message(line)
            if msg:
                self.add_message(msg, now)
            return
        self.packets += 1
        if rec["src"].upper() == self.own:
            return
        key = (rec["kind"], rec["name"])
        with self.lock:
            if not rec["live"]:
                self.heard.pop(key, None)                # killed by its owner
                self.trails.drop(key)
                return
            rec["heard"] = now or time.time()
            del rec["live"]
            self.heard[key] = rec
            if len(self.heard) > MAX_ENTRIES:
                for k in sorted(self.heard, key=lambda k: self.heard[k]["heard"])[:MAX_ENTRIES // 10]:
                    del self.heard[k]
                    self.trails.drop(k)
        self.trails.add(key, rec["lat"], rec["lon"], rec["heard"])

    def snapshot(self, objects=True, stations=False, skip_names=()):
        cutoff, skip = time.time() - self.max_age, set(skip_names)
        with self.lock:
            for k in [k for k, r in self.heard.items() if r["heard"] < cutoff]:
                del self.heard[k]
                self.trails.drop(k)
            recs = [dict(r, path=self.trails.get(k)) for k, r in self.heard.items()]
        out = [r for r in recs if ((objects and r["kind"] != "station") or (stations and r["kind"] == "station"))
               and not (r["kind"] != "station" and r["name"] in skip)]
        return sorted(out, key=lambda r: r["name"])

    def status(self):
        with self.lock:
            n_obj = sum(1 for r in self.heard.values() if r["kind"] != "station")
            n_all = len(self.heard)
        return {"connected": self.connected, "since": self.since, "radius_miles": self.radius,
                "centre": [self.lat, self.lon], "objects_heard": n_obj, "stations_heard": n_all - n_obj,
                "max_age_minutes": round(self.max_age / 60), "error": self.error}

    def run(self):
        delay = 15
        while True:
            sock = None
            try:
                sock = socket.create_connection((self.host, self.port), timeout=30)
                f = sock.makefile("rb")
                f.readline()
                flt = f"r/{self.lat:.4f}/{self.lon:.4f}/{max(1, round(self.radius * 1.609344))}"
                sock.sendall(f"user {self.callsign} pass {self.login_pass} vers aprs-web-listen 1.0 filter {flt}\r\n".encode())
                sock.settimeout(120)                     # the server sends a keep-alive line every ~20 s
                self.connected, self.since, self.error, delay = True, time.time(), "", 15
                print(f"# listening on APRS-IS within {self.radius:g} miles of {self.lat:.3f},{self.lon:.3f}",
                      file=sys.stderr, flush=True)
                while True:
                    raw = f.readline()
                    if not raw:
                        raise OSError("connection closed by server")
                    line = raw.decode("latin-1").rstrip("\r\n")
                    if line and not line.startswith("#"):
                        try:
                            self.handle(line)
                        except Exception:
                            pass                         # one odd packet must not stop the listener
            except Exception as e:
                self.error = str(e)
                if self.connected or delay == 15:
                    print(f"# APRS-IS listener: {e}; reconnecting in {delay}s", file=sys.stderr, flush=True)
            finally:
                self.connected = False
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
            time.sleep(delay)
            delay = min(delay * 2, 300)
