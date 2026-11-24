"""Manual corrections made in the web UI, shared by the feed scripts.

overrides.json lives in the data folder ($APRS_DATA_DIR, else the current folder):

  { "TRAF1A2B3": {"deleted": true, "ts": 1731350000, ...},
    "FIRE61774": {"lat": 40.1, "lon": -75.3, "comment": "corrected text", "ts": ...} }

Each feed script calls apply() once per cycle, after it has built its objects and
before it works out what to kill:
  * a deleted name is dropped (so the script kills it and keeps it off the map for
    as long as the source still lists it);
  * an edited name gets the corrected position and/or text.
The web UI writes the file; the scripts only read it.
"""
import json
import os
import re
import sys
import time

FILE = "overrides.json"


def path():
    return os.path.join(os.environ.get("APRS_DATA_DIR") or ".", FILE)


def load():
    try:
        with open(path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data):
    p = path()
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, p)


def clean_text(text, limit=200):
    text = str(text).replace("~", "-").replace("|", "/")
    text = text.encode("ascii", "replace").decode()
    return re.sub(r"\s+", " ", text).strip()[:limit]


def fmt_lat(lat):
    h = "N" if lat >= 0 else "S"
    lat = abs(lat)
    d, m = int(lat), round((lat - int(lat)) * 60, 2)
    if m >= 60:
        d, m = d + 1, 0.0
    return f"{d:02d}{m:05.2f}{h}"


def fmt_lon(lon):
    h = "E" if lon >= 0 else "W"
    lon = abs(lon)
    d, m = int(lon), round((lon - int(lon)) * 60, 2)
    if m >= 60:
        d, m = d + 1, 0.0
    return f"{d:03d}{m:05.2f}{h}"


def packet(name, lat, lon, table, code, comment, live=True, ts=None):
    ts = ts or time.strftime("%d%H%M", time.gmtime()) + "z"
    return f";{name:<9.9}{'*' if live else '_'}{ts}{fmt_lat(lat)}{table}{fmt_lon(lon)}{code}{comment if live else ''}"


# APRS "multiline" shape appended to an object's text:  " }" line-type shape-type scale
# then lat/lon offset pairs, "{" and a 5-character sequence (see aprs_nws.py).
SHAPE_RE = re.compile(r" \}([a-l])([01])([!-z])((?:[!-z]{2}){2,})\{[0-9A-Za-z]{5}$")
SHAPE_COLOURS = ["red", "yellow", "blue", "green"]
SHAPE_STYLES = ["solid", "dashed", "double-dashed"]


def split_shape(comment):
    """(text without the shape, the shape tail or '')."""
    m = SHAPE_RE.search(comment or "")
    return (comment[:m.start()], comment[m.start():]) if m else (comment or "", "")


def decode_shape(lat, lon, comment):
    """The shape carried in an object's text as {points, colour, style, closed}, or None."""
    m = SHAPE_RE.search(comment or "")
    if not m:
        return None
    kind = ord(m.group(1)) - ord("a")
    scale = 0.0001 * 10 ** ((ord(m.group(3)) - 33) / 20.0)
    rlat, rlon = round(lat * 6000) / 6000.0, round(lon * 6000) / 6000.0    # the packet carries 0.01 minute
    pairs = m.group(4)
    pts = [[round(rlat + (ord(pairs[i]) - 78) * scale, 5), round(rlon - (ord(pairs[i + 1]) - 78) * scale, 5)]
           for i in range(0, len(pairs), 2)]                                  # offsets are + north, + west
    return {"points": pts, "colour": SHAPE_COLOURS[kind // 3], "style": SHAPE_STYLES[kind % 3],
            "closed": m.group(2) == "0"}


def merged(entry, lat, lon, sym):
    """(lat, lon, (table, code, comment)) with an edit override applied."""
    table, code, comment = sym
    lat0, lon0 = lat, lon
    try:
        if entry.get("lat") is not None and entry.get("lon") is not None:
            la, lo = float(entry["lat"]), float(entry["lon"])
            if -90 <= la <= 90 and -180 <= lo <= 180:
                lat, lon = la, lo
    except (TypeError, ValueError):
        pass
    if isinstance(entry.get("comment"), str):
        comment = clean_text(entry["comment"])
    elif (lat, lon) != (lat0, lon0):
        comment = split_shape(comment)[0]       # a shape is drawn relative to the position: moving it would skew it
    return lat, lon, (table, code, comment)


def apply(placed, out=None):
    """Apply web-UI corrections to this cycle's objects.

    placed: name -> (lat, lon, (table, code, comment)), modified in place.
    out:    optional list of object packets already built for those names; kept in step.
    Returns the set of names that were suppressed (deleted in the UI).
    """
    suppressed = set()
    try:
        ov = load()
        if not ov:
            return suppressed
        index = {}
        if out is not None:
            for i, pkt in enumerate(out):
                if len(pkt) > 18 and pkt[0] == ";" and pkt[10] == "*":
                    index[pkt[1:10].strip()] = i
        drop = []
        for name in list(placed):
            entry = ov.get(name)
            if not isinstance(entry, dict):
                continue
            if entry.get("deleted"):
                suppressed.add(name)
                del placed[name]
                if name in index:
                    drop.append(index[name])
                continue
            lat, lon, sym = placed[name]
            new = merged(entry, lat, lon, sym)
            if new != (lat, lon, sym):
                placed[name] = new
                if name in index:
                    i = index[name]
                    out[i] = packet(name, new[0], new[1], new[2][0], new[2][1], new[2][2], ts=out[i][11:18])
        for i in sorted(drop, reverse=True):
            del out[i]
        if suppressed:
            print(f"# {len(suppressed)} object(s) suppressed in the web UI", file=sys.stderr)
    except Exception as e:                      # a bad overrides file must never stop a feed
        print(f"# overrides ignored: {e}", file=sys.stderr)
    return suppressed
