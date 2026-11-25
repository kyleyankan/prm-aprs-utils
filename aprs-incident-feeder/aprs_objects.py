#!/usr/bin/env python3
"""List the APRS objects your callsign has put on the map, and kill them one by one.

Why this is harder than it sounds: APRS has no "list objects owned by X" query.
Once a script stops sending an object, nothing on the network announces it any
more, so an orphan (for example one under an old name) can only be found from
records you already have.  This tool combines three sources:

  1. Your own records: the supervisor/service logs and the *_live_state.json
     files (every object packet the scripts ever printed or sent).
  2. APRS-IS: it listens for a few minutes for packets from your callsign, which
     shows what is being transmitted right now.
  3. Names you give it (--names, --names-file, or  n NAME  at the prompt), for
     example ones you can see on the aprs.fi map.
  Then aprs.fi (needs a free API key) is asked about every name: is it still
  displayed, since when, and when was it last heard.  aprs.fi's API cannot
  search by owner or wildcard, so it can only answer for names we already have.

An object nobody ever logged or sent from these machines cannot be discovered;
for those use  k NAME  at the prompt (you type the exact object name).

  python3 aprs_objects.py --callsign W3XYZ-9                 # interactive
  python3 aprs_objects.py --callsign W3XYZ-9 --aprsfi-key KEY
  python3 aprs_objects.py --callsign W3XYZ-9 --list-only

Killing sends a normal APRS object-kill packet from your callsign (the only
callsign that is allowed to kill them).  Nothing is sent unless you confirm.
"""
import argparse, glob, json, os, re, socket, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
IS_HOST, IS_PORT = "rotate.aprs2.net", 14580
APRSFI_URL = "https://api.aprs.fi/api/get"
FALLBACK_POS = ("4000.00N", "07500.00W")      # used only for names we never saw a position for

PKT_RE = re.compile(r";(?P<name>[\x20-\x7e]{9})(?P<flag>[*_])(?P<ts>\d{6}[zh/])"
                    r"(?P<lat>\d{4}\.\d\d[NS])(?P<table>[/\\A-Za-z0-9])"
                    r"(?P<lon>\d{5}\.\d\d[EW])(?P<code>[\x21-\x7e])(?P<comment>[^\r\n]*)")


# --------------------------------------------------------------------------
def passcode(call):
    call = call.split("-")[0].upper()
    h = 0x73E2
    for i in range(0, len(call), 2):
        h ^= ord(call[i]) << 8
        if i + 1 < len(call):
            h ^= ord(call[i + 1])
    return h & 0x7FFF


def fmt_lat(v):
    h = "N" if v >= 0 else "S"; v = abs(v); d = int(v)
    return f"{d:02d}{(v - d) * 60:05.2f}{h}"


def fmt_lon(v):
    h = "E" if v >= 0 else "W"; v = abs(v); d = int(v)
    return f"{d:03d}{(v - d) * 60:05.2f}{h}"


def pretty_pos(lat, lon):
    def conv(s, deg):
        d = int(s[:deg]); m = float(s[deg:-1])
        v = d + m / 60
        return -v if s[-1] in "SW" else v
    try:
        return f"{conv(lat, 2):.4f},{conv(lon, 3):.4f}"
    except ValueError:
        return "?"


class Obj:
    def __init__(self, name):
        self.name = name.strip()
        self.lat, self.lon = FALLBACK_POS
        self.table, self.code = "/", "."
        self.comment = ""
        self.sources = set()      # log, state, is, manual
        self.managed = False      # a script still lists it as one of its current objects
        self.alive = None         # aprs.fi verdict: True / False / None (unknown)
        self.since = 0.0          # aprs.fi "time": when it arrived at its current position (~ creation)
        self.heard = 0.0          # aprs.fi "lasttime": last packet heard
        self.has_pos = False
        self.last = 0.0

    def kill_packet(self, call):
        ts = datetime.now(timezone.utc).strftime("%d%H%M") + "z"
        return f"{call}>APRS,TCPIP*:;{self.name:<9.9}_{ts}{self.lat}{self.table}{self.lon}{self.code}"


# --------------------------------------------------------------------------
# Source 1: logs and state files
# --------------------------------------------------------------------------
def candidate_files(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            files += glob.glob(os.path.join(p, "*.log*")) + glob.glob(os.path.join(p, "*.json"))
            files += glob.glob(os.path.join(p, "*.txt")) + glob.glob(os.path.join(p, "*.out"))
        elif os.path.isfile(p):
            files.append(p)
    return sorted(set(files), key=lambda f: (os.path.getmtime(f), f))   # oldest first


def scan_files(paths, call, objs):
    """Replay every object packet in file order; a later kill cancels an earlier set."""
    state = {}                     # name -> (flag, packet fields)
    managed = set()
    for f in candidate_files(paths):
        mt = os.path.getmtime(f)
        if f.endswith(".json"):
            try:
                with open(f) as fh:
                    data = json.load(fh)
                if data.get("callsign", call).upper() != call.upper():
                    continue                       # state from some other callsign
                for name, o in data.get("objects", {}).items():
                    managed.add(name.strip())
                    m = {"lat": fmt_lat(o["lat"]), "lon": fmt_lon(o["lon"]), "table": o.get("table", "/"),
                         "code": o.get("code", "."), "comment": o.get("comment", "")}
                    state[name.strip()] = ("*", m, "state", mt)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass
            continue
        try:
            with open(f, errors="replace") as fh:
                for line in fh:
                    m = PKT_RE.search(line)
                    if not m:
                        continue
                    # if the line carries a source callsign, it must be ours
                    pre = line[:m.start()]
                    src = re.match(r"\s*(?:\S+\s+)?([A-Za-z0-9-]+)>", pre)
                    if src and src.group(1).upper() != call.upper():
                        continue
                    state[m["name"].strip()] = (m["flag"], m.groupdict(), "log", mt)
        except OSError:
            pass
    for name, (flag, m, src, mt) in state.items():
        if flag == "_":
            continue
        o = objs.setdefault(name, Obj(name))
        o.lat, o.lon, o.table, o.code = m["lat"], m["lon"], m["table"], m["code"]
        o.comment = m["comment"]; o.has_pos = True
        o.sources.add(src); o.last = max(o.last, mt)
    for name in managed:
        if name in objs:
            objs[name].managed = True
    return len(state)


# --------------------------------------------------------------------------
# Source 2: APRS-IS
# --------------------------------------------------------------------------
def is_connect(host, port, call, filt=None, timeout=15):
    s = socket.create_connection((host, port), timeout=timeout)
    f = s.makefile("rwb", buffering=0)
    f.readline()                                           # banner
    login = f"user {call} pass {passcode(call)} vers aprs_objects 1.0"
    if filt:
        login += f" filter {filt}"
    f.write((login + "\r\n").encode())
    resp = f.readline().decode("latin-1", "replace").strip()
    if "unverified" in resp.lower() or "logresp" not in resp.lower():
        raise RuntimeError(f"APRS-IS login problem: {resp}")
    return s, f


def listen(host, port, call, minutes, objs):
    seen = 0
    s, f = is_connect(host, port, call, filt=f"b/{call}")
    end = time.time() + minutes * 60
    try:
        while time.time() < end:
            s.settimeout(max(1.0, min(30.0, end - time.time())))
            try:
                line = f.readline().decode("latin-1", "replace").strip()
            except socket.timeout:
                continue
            if not line:
                break
            if line.startswith("#"):
                continue
            src, _, rest = line.partition(">")
            if src.upper() != call.upper():
                continue
            m = PKT_RE.search(rest)
            if not m:
                continue
            name = m["name"].strip()
            if m["flag"] == "_":
                objs.pop(name, None)
                continue
            o = objs.setdefault(name, Obj(name))
            o.lat, o.lon, o.table, o.code, o.comment = m["lat"], m["lon"], m["table"], m["code"], m["comment"]
            o.has_pos = True; o.sources.add("is"); o.last = time.time(); seen += 1
    finally:
        s.close()
    return seen


def send_kills(host, port, call, packets):
    s, f = is_connect(host, port, call)
    try:
        for p in packets:
            f.write((p + "\r\n").encode("latin-1", "replace"))
            time.sleep(0.3)
        time.sleep(1.0)
    finally:
        s.close()


# --------------------------------------------------------------------------
# Source 3: aprs.fi
# --------------------------------------------------------------------------
def aprsfi_fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "aprs_objects/1.0 (+personal object cleanup)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def check_aprsfi(key, call, objs, fetch=aprsfi_fetch):
    names = sorted(objs)
    for i in range(0, len(names), 20):
        batch = names[i:i + 20]
        q = urllib.parse.urlencode({"name": ",".join(batch), "what": "loc", "apikey": key, "format": "json"})
        data = fetch(f"{APRSFI_URL}?{q}")
        if data.get("result") != "ok":
            raise RuntimeError(f"aprs.fi: {data.get('description', data)}")
        found = {}
        for e in data.get("entries", []):
            if e.get("type", "o") not in ("o", "i"):
                continue                                  # a station, not an object/item
            src = (e.get("srccall") or "").upper()
            if src and src != call.upper():
                continue                                  # somebody else's object of the same name
            found[str(e.get("name", "")).strip()] = e
        for n in batch:
            objs[n].alive = n in found
            if n in found:
                e, o = found[n], objs[n]
                o.sources.add("aprs.fi")
                try:
                    o.since = float(e.get("time") or 0)
                    o.heard = float(e.get("lasttime") or e.get("time") or 0)
                except ValueError:
                    pass
                if not o.has_pos and e.get("lat") not in (None, "") and e.get("lng") not in (None, ""):
                    o.lat, o.lon, o.has_pos = fmt_lat(float(e["lat"])), fmt_lon(float(e["lng"])), True
                sym = str(e.get("symbol") or "")
                if len(sym) == 2 and not o.comment:
                    o.table, o.code = sym[0], sym[1]
        time.sleep(1.1)                                   # be polite to the API


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
ACTIVE_MIN = 15           # heard on aprs.fi within this many minutes => still being sent


def in_use(o):
    """'script' = a running script lists it as current, 'sending' = heard recently, '' = nobody is refreshing it."""
    if o.managed:
        return "script"
    if o.heard and time.time() - o.heard <= ACTIVE_MIN * 60:
        return "sending"
    if "is" in o.sources and time.time() - o.last <= ACTIVE_MIN * 60:
        return "sending"
    return ""


def ordered(objs, show_all=True, newest_first=False):
    items = [o for o in objs.values() if show_all or o.alive is not False]
    # oldest first by aprs.fi arrival time; names with no known time go last
    items.sort(key=lambda o: (o.since == 0, -o.since if newest_first else o.since, o.name))
    return items


def fmt_t(t):
    return time.strftime("%m-%d %H:%M", time.localtime(t)) if t else "-"


def show(items, key=True):
    print(f"\n{'#':>3}  {'NAME':<9}  {'CREATED':<11}  {'LAST HEARD':<11}  {'ON MAP':<6} {'IN USE':<8} {'FOUND IN':<16} COMMENT")
    for i, o in enumerate(items, 1):
        on = {True: "yes", False: "no", None: "?"}[o.alive]
        print(f"{i:>3}  {o.name:<9}  {fmt_t(o.since):<11}  {fmt_t(o.heard):<11}  {on:<6} {in_use(o) or 'no':<8} "
              f"{','.join(sorted(o.sources)):<16} {o.comment[:36]}")
    print("\nCREATED: when aprs.fi first saw it at its current position (closest thing to creation time; times are local)."
          "\nON MAP: yes = aprs.fi displays it now; no = already gone; ? = not checked (no API key)."
          f"\nIN USE: script = a running script lists it as current; sending = heard in the last {ACTIVE_MIN} min;"
          "\n        no = nothing is refreshing it, so it is an orphan that will sit there until it expires or is killed.")


def parse_selection(text, items):
    text = text.strip().lower()
    if text == "orphans":
        return [o for o in items if not in_use(o)]
    if text == "all":
        return list(items)
    out = []
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        if re.fullmatch(r"\d+-\d+", part):
            a, b = map(int, part.split("-")); out += [x for x in range(a, b + 1)]
        elif part.isdigit():
            out.append(int(part))
        else:
            raise ValueError(f"don't understand '{part}'")
    for n in out:
        if not 1 <= n <= len(items):
            raise ValueError(f"no item #{n}")
    return [items[n - 1] for n in out]


def add_names(objs, text):
    added = []
    for nm in re.split(r"[,\s]+", text.strip()):
        if nm and len(nm) <= 9 and nm not in objs:
            o = Obj(nm); o.sources.add("manual"); objs[nm] = o; added.append(nm)
    return added


def main():
    global ACTIVE_MIN
    ap = argparse.ArgumentParser(description="List and kill APRS objects owned by your callsign")
    ap.add_argument("--callsign", required=True, help="the callsign-SSID the objects were sent from")
    ap.add_argument("--aprsfi-key", default=os.environ.get("APRSFI_KEY", ""),
                    help="aprs.fi API key (default: $APRSFI_KEY); needed to know what is displayed on the map")
    ap.add_argument("--names", default="", help="extra object names to check, comma/space separated")
    ap.add_argument("--names-file", help="file with extra object names, one per line or separated by commas/spaces")
    ap.add_argument("--logs", action="append", default=[], help="extra file or folder to scan (repeatable)")
    ap.add_argument("--listen", type=float, default=5, help="minutes to listen on APRS-IS (0 = skip, default 5)")
    ap.add_argument("--active-minutes", type=int, default=ACTIVE_MIN,
                    help="heard within this many minutes counts as 'still being sent' (default 15)")
    ap.add_argument("--newest-first", action="store_true", help="sort newest first (default oldest first)")
    ap.add_argument("--show-all", action="store_true", help="also list names aprs.fi says are not displayed")
    ap.add_argument("--host", default=IS_HOST); ap.add_argument("--port", type=int, default=IS_PORT)
    ap.add_argument("--list-only", action="store_true", help="print the list and exit")
    args = ap.parse_args()
    call = args.callsign.upper()
    ACTIVE_MIN = args.active_minutes
    if call.startswith("N0CALL"):
        sys.exit("Use your real callsign.")

    objs = {}
    paths = [os.environ.get("APRS_LOG_DIR") or os.path.join(HERE, "logs"),
             os.environ.get("APRS_DATA_DIR") or os.path.join(HERE, "data"), HERE] + args.logs
    n = scan_files(paths, call, objs)
    print(f"Scanned logs/state files: {n} object names seen, {len(objs)} not killed afterwards.")
    extra = add_names(objs, args.names)
    if args.names_file:
        try:
            with open(args.names_file) as fh:
                extra += add_names(objs, fh.read())
        except OSError as e:
            print(f"Could not read {args.names_file}: {e}")
    if extra:
        print(f"Added {len(extra)} name(s) you supplied.")
    if args.listen > 0:
        print(f"Listening on APRS-IS for {args.listen:g} min (objects being sent right now)...")
        try:
            print(f"  heard {listen(args.host, args.port, call, args.listen, objs)} object packets from {call}")
        except (OSError, RuntimeError) as e:
            print(f"  APRS-IS listen failed: {e}")
    if args.aprsfi_key:
        if objs:
            print(f"Asking aprs.fi about {len(objs)} name(s) ...")
            try:
                check_aprsfi(args.aprsfi_key, call, objs)
            except (OSError, RuntimeError, ValueError) as e:
                print(f"  aprs.fi check failed: {e}")
    else:
        print("No aprs.fi key: cannot tell what is displayed on the map or when it was created."
              " (Free key: aprs.fi > My account; pass --aprsfi-key or set $APRSFI_KEY.)")
    show_all = args.show_all or not args.aprsfi_key
    print("aprs.fi cannot list objects by owner, so only names found above (or added with n NAME) can be checked.")

    if args.list_only:
        show(ordered(objs, show_all, args.newest_first)); return
    print("\nCommands:  d 3 | d 1,4,7 | d 2-5 | d orphans | d all   delete (asks first)"
          "\n           n NAME NAME   add names you can see on the map, then check them"
          "\n           k NAME        kill a name directly      c  re-check aprs.fi"
          "\n           s             flip sort order           a  show/hide names aprs.fi says are gone"
          "\n           l  list again                           q  quit")
    newest = args.newest_first
    while True:
        items = ordered(objs, show_all, newest)
        show(items) if items else print("\n(nothing to show)")
        try:
            cmd = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); return
        if not cmd:
            continue
        verb, _, arg = cmd.partition(" ")
        verb = verb.lower()
        if verb in ("q", "quit", "exit"):
            return
        if verb == "l":
            continue
        if verb == "s":
            newest = not newest; continue
        if verb == "a":
            show_all = not show_all; continue
        if verb in ("c", "n"):
            if verb == "n":
                print(f"Added: {', '.join(add_names(objs, arg)) or 'nothing new'}")
            if not args.aprsfi_key:
                print("Checking needs --aprsfi-key (or $APRSFI_KEY)."); continue
            try:
                check_aprsfi(args.aprsfi_key, call, objs)
            except (OSError, RuntimeError, ValueError) as e:
                print(f"aprs.fi check failed: {e}")
            continue
        targets = []
        if verb == "k":
            nm = arg.strip()
            if not nm or len(nm) > 9:
                print("Give the exact object name (1-9 characters)."); continue
            o = objs.get(nm) or Obj(nm)
            o.sources.add("manual"); targets = [o]
        elif verb == "d":
            try:
                targets = parse_selection(arg, items)
            except ValueError as e:
                print(e); continue
        else:
            print("Unknown command."); continue
        if not targets:
            print("Nothing selected."); continue
        print("\nAbout to send KILL packets from", call, "for:")
        for o in targets:
            u = in_use(o)
            print(f"   {o.name}" + (f"   <-- IN USE ({u}); it will be re-sent unless you stop the script" if u else ""))
        want = "yes" if len(targets) > 1 else "y"
        if input(f"Type '{want}' to send: ").strip().lower() != want:
            print("Cancelled."); continue
        try:
            send_kills(args.host, args.port, call, [o.kill_packet(call) for o in targets])
        except (OSError, RuntimeError) as e:
            print(f"Send failed: {e}"); continue
        for o in targets:
            objs.pop(o.name, None)
        print(f"Sent {len(targets)} kill packet(s).")


if __name__ == "__main__":
    main()
