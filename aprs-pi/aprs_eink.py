"""Black-and-white map image of the live APRS objects, for e-ink displays.

Used by aprs_web.py for  GET /eink.png  (and /eink.bmp), and for /eink-all.png, which
also shows the APRS objects other people have on the air (add stations=1 for stations).  Pure standard library:
it draws into a 1-bit canvas and writes the PNG/BMP itself, so the Pi needs no
imaging packages.

The picture has a title bar, a map centred on your home position, and a numbered
list of the objects on the map.  The base map is fetched once per map view, in the
background, and cached in eink_basemap.json; until that has finished the map is
drawn without it:
  * highways, rivers and town names from OpenStreetMap (via the public Overpass
    API; data (c) OpenStreetMap contributors, ODbL).  Set EINK_OSM=0 to skip.
  * county outlines from api.weather.gov (needs $NWS_CONTACT, like the nws service).

/map.png (and /map-all.png) is the same picture in full colour, 800x480 by default:
coloured roads and rivers, markers coloured by source as on the web map, warning
outlines in their APRS colours.  It takes the same options, plus dark=1.  A picture
wider than it is tall puts the object list down the right-hand side.

APRS symbols: the colour picture draws each object with its real APRS symbol, using
the aprs.fi symbol set (https://github.com/hessu/aprs-symbols), which is downloaded
once into the data folder.  Until that has arrived, or with symbols=0, it uses the
plain shapes.  The black-and-white picture keeps the shapes unless you ask for
symbols=1 (small colour pictures do not survive well in one bit).  EINK_SYMBOLS=0
stops the download altogether.

Query options (all optional):
  w, h        size in pixels                         (default 272 x 792)
  lat, lon    centre of the map                      (default $APRS_HOME)
  miles       distance from the centre to the nearer edge of the map (default 15)
  list        0 = map only, no object list           (default 1)
  detail      0 = no base map, 1 = county outlines only, 2 = roads, rivers, towns too (default 2)
  roads       0 = none, 1 = motorways/interstates only, 2 = plus US-route expressways (default),
              3 = plus other primary roads
  rivers      0 = none, 1 = major rivers only (default), 2 = every river
  rivermin    what counts as major: miles of river inside the map (default: twice `miles`)
  weight      1 = thin lines (default), 2 = bold motorways and rivers
  labels      0 = no road numbers                    (default 1)
  towns       0 = no town names                      (default 1)
  rotate      0, 90, 180 or 270 degrees clockwise    (default 0)
  invert      1 = white on black                     (default 0)
  gray        1 = 8-bit grayscale PNG instead of 1-bit, for decoders that need it
  paths       0 = do not draw the paths of moving objects (default 1)
  names       1/0 = label each marker with its callsign / object name instead of a number
              (default 1 colour, 0 black-and-white).  Our own traffic, fire and roadwork
              objects are labelled just TRAFFIC, FIRE or WORK.
  messages    how many of the latest APRS messages to show at the foot of the list, 0-12
              (default 4 colour, 0 black-and-white)
  symbols     1/0 = draw APRS symbols instead of plain shapes (default 1 colour, 0 black-and-white)
  zoom        2-4 = draw everything that many times larger (bigger text on big screens)
  dark        1 = dark background                    (colour picture only)
"""
import json
import math
import os
import re
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

API = "https://api.weather.gov"
DEFAULT_HOME = (40.12, -75.34)
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
CACHE_FILE = "eink_basemap.json"
SYMBOL_URLS = ["https://raw.githubusercontent.com/hessu/aprs-symbols/master/png/aprs-symbols-24-{t}.png",
               "https://cdn.jsdelivr.net/gh/hessu/aprs-symbols@master/png/aprs-symbols-24-{t}.png"]
OSM_FORMAT = 2            # bump to make existing caches refetch the OpenStreetMap layer
RETRY_SECONDS = 900

# Classic 5x7 font, one byte per column (bit 0 = top row), characters 0x20-0x5A.
FONT = bytes.fromhex(
    "0000000000" "00005F0000" "0007000700" "147F147F14" "242A7F2A12" "2313086462" "3649552250" "0005030000"
    "001C224100" "0041221C00" "14083E0814" "08083E0808" "0050300000" "0808080808" "0060600000" "2010080402"
    "3E5149453E" "00427F4000" "4261514946" "2141454B31" "1814127F10" "2745454539" "3C4A494930" "0171090503"
    "3649494936" "064949291E" "0036360000" "0056360000" "0814224100" "1414141414" "0041221408" "0201510906"
    "324979413E" "7E1111117E" "7F49494936" "3E41414122" "7F4141221C" "7F49494941" "7F09090901" "3E4149497A"
    "7F0808087F" "00417F4100" "2040413F01" "7F08142241" "7F40404040" "7F020C027F" "7F0408107F" "3E4141413E"
    "7F09090906" "3E4151215E" "7F09192946" "4649494931" "01017F0101" "3F4040403F" "1F2040201F" "3F4038403F"
    "6314081463" "0708700807" "6151494543")
CW, CH = 6, 9          # character cell (5x7 glyph plus spacing)

# Colour picture: palette index -> RGB.  0 is the background and 1 the text colour, exactly as in
# the black-and-white picture, so the same drawing code serves both.
ROLES = ["bg", "ink", "county", "river", "m", "t", "p", "bar", "home", "red", "yellow", "blue", "green",
         "montco", "chesco", "bucks", "njta", "nj511", "pa511", "nws", "nhc", "custom", "aprs", "other", "bartext", "town"]
LIGHT = {"bg": "f7f7f4", "ink": "1c2330", "county": "b9bec6", "river": "4c9be8", "m": "e8590c", "t": "b08968",
         "p": "c9ced6", "bar": "1c2330", "home": "e03131", "red": "e03131", "yellow": "e8a100", "blue": "1c7ed6",
         "green": "2f9e44", "montco": "d9480f", "chesco": "7b2cbf", "bucks": "b8860b", "njta": "0b7285", "nj511": "0b7285",
         "pa511": "1f5fd0", "nws": "c0362c", "nhc": "a61e8a", "custom": "2b8a3e", "aprs": "868e96",
         "other": "495057", "bartext": "ffffff", "town": "3b4452"}
DARK = dict(LIGHT, bg="12161c", ink="e6e9ee", county="3a4350", river="3d8bdb", p="56606e", bar="0a0d11",
            pa511="5b9bff", njta="22b8cf", nj511="22b8cf", chesco="b197fc", other="adb5bd", town="c3cad3", nws="ff6b6b",
            montco="ff922b", bucks="e0b03a", nhc="e599f7", custom="51cf66")
COLOUR_INK = {r: i for i, r in enumerate(ROLES)}


class Canvas:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.px = bytearray(w * h)             # 1 = black
        self.clip = (0, 0, w, h)
        self.over = {}                         # (x, y) -> (r, g, b, a): true-colour pixels (APRS symbols)

    def paste(self, icon, cx, cy):
        """Put a symbol picture (size, RGBA bytes) centred on cx, cy as true-colour pixels."""
        size, data = icon
        x0, y0 = cx - size // 2, cy - size // 2
        cx0, cy0, cx1, cy1 = self.clip
        for j in range(size):
            for i in range(size):
                a = data[(j * size + i) * 4 + 3]
                x, y = x0 + i, y0 + j
                if a > 8 and cx0 <= x < cx1 and cy0 <= y < cy1:
                    k = (j * size + i) * 4
                    self.over[(x, y)] = (data[k], data[k + 1], data[k + 2], a)

    def paste_mono(self, icon, cx, cy):
        """The same in one bit: dark parts black, light parts background."""
        size, data = icon
        x0, y0 = cx - size // 2, cy - size // 2
        for j in range(size):
            for i in range(size):
                k = (j * size + i) * 4
                if data[k + 3] > 110:
                    lum = (data[k] * 299 + data[k + 1] * 587 + data[k + 2] * 114) // 1000
                    self.set(x0 + i, y0 + j, 1 if lum < 150 else 0)

    def set(self, x, y, v=1):
        x0, y0, x1, y1 = self.clip
        if x0 <= x < x1 and y0 <= y < y1:
            self.px[y * self.w + x] = v

    def rect(self, x, y, w, h, v=1):
        for yy in range(max(y, 0), min(y + h, self.h)):
            for xx in range(max(x, 0), min(x + w, self.w)):
                self.set(xx, yy, v)

    def frame(self, x, y, w, h, v=1):
        self.line(x, y, x + w - 1, y, v); self.line(x, y + h - 1, x + w - 1, y + h - 1, v)
        self.line(x, y, x, y + h - 1, v); self.line(x + w - 1, y, x + w - 1, y + h - 1, v)

    def line(self, x0, y0, x1, y1, v=1, dash=None, width=1):
        """Bresenham line, clipped to the canvas; dash=(on, off) pixels."""
        seg = clip_segment(x0, y0, x1, y1, -2, -2, self.w + 2, self.h + 2)
        if not seg:
            return
        x0, y0, x1, y1 = (int(round(c)) for c in seg)
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err, n = dx + dy, 0
        while True:
            if not dash or n % (dash[0] + dash[1]) < dash[0]:
                if width == 1:
                    self.set(x0, y0, v)
                else:
                    self.rect(x0 - width // 2, y0 - width // 2, width, width, v)
            n += 1
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy; x0 += sx
            if e2 <= dx:
                err += dx; y0 += sy

    def text(self, x, y, s, v=1, scale=1):
        for ch in s.upper():
            i = ord(ch) - 0x20
            if 0 <= i < len(FONT) // 5:
                for col in range(5):
                    bits = FONT[i * 5 + col]
                    for row in range(7):
                        if bits >> row & 1:
                            if scale == 1:
                                self.set(x + col, y + row, v)
                            else:
                                self.rect(x + col * scale, y + row * scale, scale, scale, v)
            x += CW * scale

    def label(self, x, y, s):
        """Text on a white box with a black border (stays readable over lines)."""
        w = len(s) * CW + 3
        self.rect(x, y, w, CH + 2, 0)
        self.frame(x, y, w, CH + 2, 1)
        self.text(x + 2, y + 2, s)
        return w, CH + 2

    # ---- output ----
    def rotated(self, deg):
        deg %= 360
        if deg == 0:
            return self
        out = Canvas(self.h, self.w) if deg in (90, 270) else Canvas(self.w, self.h)
        for (x, y), v in self.over.items():
            out.over[(self.h - 1 - y, x) if deg == 90 else (self.w - 1 - x, self.h - 1 - y) if deg == 180
                     else (y, self.w - 1 - x)] = v
        for y in range(self.h):
            for x in range(self.w):
                v = self.px[y * self.w + x]
                if v:
                    if deg == 90:
                        out.px[x * out.w + (self.h - 1 - y)] = v
                    elif deg == 180:
                        out.px[(self.h - 1 - y) * out.w + (self.w - 1 - x)] = v
                    else:
                        out.px[(self.w - 1 - x) * out.w + y] = v
        return out

    def rows_1bit(self, black_is_one):
        rows = []
        for y in range(self.h):
            row = self.px[y * self.w:(y + 1) * self.w]
            out = bytearray((self.w + 7) // 8)
            for x, p in enumerate(row):
                if bool(p) == black_is_one:
                    out[x >> 3] |= 0x80 >> (x & 7)
            rows.append(bytes(out))
        return rows

    def png(self, invert=False, gray=False):
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        if gray:
            on, off = (b"\xff", b"\x00") if invert else (b"\x00", b"\xff")
            raw = b"".join(b"\x00" + b"".join(on if p else off for p in self.px[y * self.w:(y + 1) * self.w])
                           for y in range(self.h))
            depth = 8
        else:                                   # grayscale 1-bit: 0 = black, 1 = white
            raw = b"".join(b"\x00" + r for r in self.rows_1bit(black_is_one=invert))
            depth = 1
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, depth, 0, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))

    def png_palette(self, colours):
        """8-bit paletted PNG; colours: list of 'rrggbb' per palette index."""
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        raw = b"".join(b"\x00" + bytes(self.px[y * self.w:(y + 1) * self.w]) for y in range(self.h))
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 3, 0, 0, 0))
                + chunk(b"PLTE", b"".join(bytes.fromhex(c) for c in colours))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))

    def png_rgb(self, colours):
        """24-bit PNG: the palette picture with the true-colour pixels blended on top."""
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        pal = [bytes.fromhex(c) for c in colours] + [b"\x00\x00\x00"] * (256 - len(colours))
        tr = [bytes(p[k] for p in pal) for k in range(3)]
        rows = []
        for y in range(self.h):
            idx = bytes(self.px[y * self.w:(y + 1) * self.w])
            row = bytearray(self.w * 3)
            for k in range(3):
                row[k::3] = idx.translate(tr[k])
            rows.append(row)
        for (x, y), (r, g, b, a) in self.over.items():
            if 0 <= x < self.w and 0 <= y < self.h:
                row, o = rows[y], x * 3
                row[o] = (r * a + row[o] * (255 - a)) // 255
                row[o + 1] = (g * a + row[o + 1] * (255 - a)) // 255
                row[o + 2] = (b * a + row[o + 2] * (255 - a)) // 255
        raw = b"".join(b"\x00" + bytes(r) for r in rows)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))

    def scaled(self, z):
        if z <= 1:
            return self
        out = Canvas(self.w * z, self.h * z)
        for (x, y), v in self.over.items():
            for j in range(z):
                for i in range(z):
                    out.over[(x * z + i, y * z + j)] = v
        for y in range(self.h):
            row = bytes(b for b in self.px[y * self.w:(y + 1) * self.w] for _ in range(z))
            for k in range(z):
                out.px[(y * z + k) * out.w:(y * z + k + 1) * out.w] = row
        return out

    def bmp(self, invert=False):
        rows = self.rows_1bit(black_is_one=not invert)        # palette: 0 = white, 1 = black
        pad = (-len(rows[0])) % 4
        data = b"".join(r + b"\x00" * pad for r in reversed(rows))
        off = 14 + 40 + 8
        return (b"BM" + struct.pack("<IHHI", off + len(data), 0, 0, off)
                + struct.pack("<IiiHHIIiiII", 40, self.w, self.h, 1, 1, 0, len(data), 2835, 2835, 2, 2)
                + b"\xff\xff\xff\x00" + b"\x00\x00\x00\x00" + data)


def clip_segment(x0, y0, x1, y1, xmin, ymin, xmax, ymax):
    """Liang-Barsky: the part of the segment inside the box, or None."""
    dx, dy, t0, t1 = x1 - x0, y1 - y0, 0.0, 1.0
    for p, q in ((-dx, x0 - xmin), (dx, xmax - x0), (-dy, y0 - ymin), (dy, ymax - y0)):
        if p == 0:
            if q < 0:
                return None
        else:
            t = q / p
            if p < 0:
                if t > t1:
                    return None
                t0 = max(t0, t)
            else:
                if t < t0:
                    return None
                t1 = min(t1, t)
    return x0 + t0 * dx, y0 + t0 * dy, x0 + t1 * dx, y0 + t1 * dy


# --------------------------------------------------------------------------
# APRS symbol pictures (aprs.fi symbol set, fetched once and kept in the data folder)
# --------------------------------------------------------------------------
def decode_png(data):
    """(width, height, RGBA bytes) for an 8-bit non-interlaced PNG (RGBA, RGB, grey or palette)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    pos, idat, plte, trns, head = 8, [], b"", b"", None
    while pos + 8 <= len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if kind == b"IHDR":
            head = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"PLTE":
            plte = body
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IEND":
            break
    if not head or head[2] != 8 or head[6] != 0 or head[3] not in (0, 2, 3, 4, 6):
        raise ValueError("unsupported PNG variety")
    w, h, ctype = head[0], head[1], head[3]
    bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    raw, stride = zlib.decompress(b"".join(idat)), w * bpp
    out, prev, p = bytearray(), bytearray(stride), 0
    for _ in range(h):
        ft = raw[p]; line = bytearray(raw[p + 1:p + 1 + stride]); p += 1 + stride
        if ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                line[i] = (line[i] + (((line[i - bpp] if i >= bpp else 0) + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        out += line
        prev = line
    if ctype == 6:
        return w, h, bytes(out)
    rgba = bytearray(w * h * 4)
    if ctype == 2:
        rgba[0::4], rgba[1::4], rgba[2::4] = out[0::3], out[1::3], out[2::3]
        rgba[3::4] = b"\xff" * (w * h)
    elif ctype == 0:
        rgba[0::4] = rgba[1::4] = rgba[2::4] = out
        rgba[3::4] = b"\xff" * (w * h)
    elif ctype == 4:
        rgba[0::4] = rgba[1::4] = rgba[2::4] = out[0::2]
        rgba[3::4] = out[1::2]
    else:
        alpha = trns + b"\xff" * 256
        for i, v in enumerate(out):
            rgba[i * 4:i * 4 + 3] = plte[v * 3:v * 3 + 3] or b"\x00\x00\x00"
            rgba[i * 4 + 3] = alpha[v]
    return w, h, bytes(rgba)


class Symbols:
    """The two APRS symbol sheets (primary and alternate table), 16 symbols per row starting at '!'."""
    def __init__(self, data_dir, fetch=None):
        self.dir = data_dir
        self.enabled = (os.environ.get("EINK_SYMBOLS", "1") != "0") if fetch is None else fetch
        self.sheets, self.icons = {}, {}
        self.lock = threading.Lock()
        self.busy, self.tried = False, 0.0

    def ready(self):
        if len(self.sheets) == 2:
            return True
        with self.lock:
            if self.enabled and not self.busy and time.time() - self.tried > RETRY_SECONDS:
                self.busy, self.tried = True, time.time()
                threading.Thread(target=self.load, daemon=True).start()
        return False

    def load(self):
        try:
            for t in (0, 1):
                path = os.path.join(self.dir, f"aprs-symbols-24-{t}.png")
                data = None
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        data = f.read()
                else:
                    last = None
                    for url in SYMBOL_URLS:
                        try:
                            req = urllib.request.Request(url.format(t=t), headers={"User-Agent": "aprs-eink-map/1.0"})
                            with urllib.request.urlopen(req, timeout=30) as r:
                                data = r.read()
                            break
                        except (urllib.error.URLError, OSError) as e:
                            last = e
                    if data is None:
                        raise last
                sheet = decode_png(data)
                if sheet[0] % 16 or sheet[1] < (sheet[0] // 16) * 6:
                    raise ValueError("symbol sheet has an unexpected size")
                if not os.path.exists(path):
                    with open(path + ".tmp", "wb") as f:
                        f.write(data)
                    os.replace(path + ".tmp", path)
                self.sheets[t] = sheet
            print("# APRS symbol pictures loaded", file=sys.stderr, flush=True)
        except Exception as e:
            self.sheets.clear()
            print(f"# APRS symbol pictures not available ({e}); maps use plain shapes", file=sys.stderr, flush=True)
        finally:
            self.busy = False

    def icon(self, table, code):
        """(size, RGBA bytes) for a symbol, or None."""
        if not table or not code or len(self.sheets) != 2:
            return None
        t, idx = (0 if table == "/" else 1), ord(code[0]) - 33
        if not 0 <= idx < 94:
            return None
        if (t, idx) not in self.icons:
            w, _h, data = self.sheets[t]
            size = w // 16
            x0, y0 = (idx % 16) * size, (idx // 16) * size
            self.icons[(t, idx)] = (size, b"".join(data[((y0 + j) * w + x0) * 4:((y0 + j) * w + x0 + size) * 4]
                                                   for j in range(size)))
        return self.icons[(t, idx)]


# --------------------------------------------------------------------------
# Base map: county outlines from api.weather.gov, fetched once and cached
# --------------------------------------------------------------------------
class Basemap:
    def __init__(self, data_dir, contact, osm=None):
        self.path = os.path.join(data_dir, CACHE_FILE)
        self.contact = (contact or "").strip()
        self.osm = (os.environ.get("EINK_OSM", "1") != "0") if osm is None else osm
        self.lock = threading.Lock()
        self.busy = set()
        self.tried = {}
        self.version = 0                         # bumped when new data arrives (invalidates drawn base maps)
        try:
            with open(self.path) as f:
                self.cache = json.load(f)
        except (OSError, ValueError):
            self.cache = {}

    def http(self, url, data=None):
        req = urllib.request.Request(url, data=data, headers={
            "User-Agent": f"(aprs-eink-map/1.0, {self.contact or 'personal e-ink display'})",
            "Accept": "application/geo+json, application/json"})
        with urllib.request.urlopen(req, timeout=120 if data else 30) as r:
            return json.loads(r.read())

    def layers(self, south, west, north, east, step=0.001):
        """Cached base-map data for the view: {counties: [...], osm: {...}}; starts a background fetch if needed."""
        key = f"{south:.2f},{west:.2f},{north:.2f},{east:.2f}"
        with self.lock:
            entry = self.cache.get(key) or {}
            need_c = bool(self.contact) and not entry.get("complete")
            need_o = self.osm and entry.get("osm_ok") != OSM_FORMAT
            due = time.time() - self.tried.get(key, 0) > RETRY_SECONDS
            if (need_c or need_o) and key not in self.busy and due:
                self.busy.add(key); self.tried[key] = time.time()
                threading.Thread(target=self.fetch, args=(key, south, west, north, east, step, need_c, need_o),
                                 daemon=True).start()
        return entry

    def fetch(self, key, south, west, north, east, step, need_c, need_o):
        new = {}
        try:
            if need_o:
                try:
                    new["osm"] = self.fetch_osm(south, west, north, east, step)
                    new["osm_ok"] = OSM_FORMAT
                    o = new["osm"]
                    print(f"# e-ink base map: {len(o['roads'])} road and {len(o['water'])} river pieces, "
                          f"{len(o['places'])} place names cached", file=sys.stderr, flush=True)
                except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
                    print(f"# e-ink base map: OpenStreetMap data not fetched ({e}); will retry", file=sys.stderr, flush=True)
            if need_c:
                new["counties"], new["complete"] = self.fetch_counties(south, west, north, east)
                print(f"# e-ink base map: {len(new['counties'])} county outline(s) cached"
                      + ("" if new["complete"] else " (incomplete; will retry)"), file=sys.stderr, flush=True)
        except Exception as e:
            print(f"# e-ink base map fetch failed: {e}", file=sys.stderr, flush=True)
        finally:
            with self.lock:
                if new:
                    entry = dict(self.cache.get(key) or {})
                    entry.update(new)
                    self.cache[key] = entry
                    self.version += 1
                    try:
                        tmp = self.path + ".tmp"
                        with open(tmp, "w") as f:
                            json.dump(self.cache, f, separators=(",", ":"))
                        os.replace(tmp, self.path)
                    except OSError as e:
                        print(f"# e-ink base map not saved: {e}", file=sys.stderr, flush=True)
                self.busy.discard(key)

    def fetch_osm(self, south, west, north, east, step):
        box = f"({south:.4f},{west:.4f},{north:.4f},{east:.4f})"
        query = ("[out:json][timeout:90];("
                 f'way["highway"~"^(motorway|trunk|primary)$"]{box};'
                 f'way["waterway"="river"]{box};'
                 f'node["place"~"^(city|town)$"]{box};'
                 ");out geom;")
        last = None
        for url in OVERPASS:
            try:
                data = self.http(url, data=urllib.parse.urlencode({"data": query}).encode())
                break
            except (urllib.error.URLError, OSError, ValueError) as e:
                last = e
                time.sleep(2)
        else:
            raise last
        roads, water, places = [], [], []
        for el in data["elements"]:
            tags = el.get("tags") or {}
            if el.get("type") == "way":
                pts = thin([(g["lat"], g["lon"]) for g in el.get("geometry") or [] if g], step, keep_last=True)
                if len(pts) < 2:
                    continue
                if tags.get("highway"):
                    ref = re.sub(r"\s+", "", str(tags.get("ref") or "").split(";")[0])[:7]
                    roads.append({"k": tags["highway"][0], "ref": ref, "pts": pts})      # m / t / p
                elif tags.get("waterway"):
                    water.append({"n": str(tags.get("name") or ""), "pts": pts})
            elif el.get("type") == "node" and tags.get("name"):
                try:
                    pop = int(re.sub(r"\D", "", str(tags.get("population") or "0")) or 0)
                except ValueError:
                    pop = 0
                places.append({"name": str(tags["name"]), "k": tags.get("place", "town"), "pop": pop,
                               "lat": round(float(el["lat"]), 4), "lon": round(float(el["lon"]), 4)})
        return {"roads": roads, "water": water, "places": places}

    def fetch_counties(self, south, west, north, east):
        urls, counties, ok = set(), [], True
        for i in range(9):                              # sample the view to learn which counties are in it
            for j in range(5):
                lat = south + (north - south) * (i + 0.5) / 9
                lon = west + (east - west) * (j + 0.5) / 5
                try:
                    u = self.http(f"{API}/points/{lat:.4f},{lon:.4f}")["properties"].get("county")
                    if isinstance(u, str) and u.startswith(API + "/zones/county/"):
                        urls.add(u)
                except urllib.error.HTTPError as e:
                    ok = ok and e.code == 404           # 404 = open water / outside the US
                except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
                    ok = False
                time.sleep(0.25)
        for u in sorted(urls):
            try:
                z = self.http(u)
                rings = [thin(r) for r in rings_of(z.get("geometry"))]
                counties.append({"name": str((z.get("properties") or {}).get("name") or ""),
                                 "rings": [r for r in rings if len(r) >= 3]})
            except (urllib.error.URLError, OSError, ValueError, TypeError):
                ok = False
            time.sleep(0.25)
        return counties, ok


def rings_of(geom):
    """Outer rings of a GeoJSON Polygon/MultiPolygon as lists of [lat, lon]."""
    if not isinstance(geom, dict):
        return []
    if geom.get("type") == "GeometryCollection":
        return [r for g in geom.get("geometries") or [] for r in rings_of(g)]
    polys = geom.get("coordinates") or []
    if geom.get("type") == "Polygon":
        polys = [polys]
    elif geom.get("type") != "MultiPolygon":
        return []
    return [[[float(p[1]), float(p[0])] for p in poly[0]] for poly in polys if poly]


def thin(ring, step=0.002, keep_last=False):
    """Drop points closer than `step` degrees to the previous kept one (keeps the cache small)."""
    out = []
    for lat, lon in ring:
        if not out or abs(lat - out[-1][0]) > step or abs(lon - out[-1][1]) > step:
            out.append([round(lat, 4), round(lon, 4)])
    if keep_last and ring:
        end = [round(ring[-1][0], 4), round(ring[-1][1], 4)]
        if out[-1] != end:
            out.append(end)                              # keep road/river pieces joined end to end
    return out


def overlaps(box, taken):
    return any(box[0] < t[2] and t[0] < box[2] and box[1] < t[3] and t[1] < box[3] for t in taken)


def path_miles(pts):
    return sum(math.hypot((a1 - a0) * 69.0, (b1 - b0) * 69.172 * math.cos(math.radians(a0)))
               for (a0, b0), (a1, b1) in zip(pts, pts[1:]))


def draw_base(c, layers, xy, inside, ppm, detail, home=None, opt=None):
    """County outlines, rivers, highways, road numbers and town names."""
    osm = (layers.get("osm") or {}) if detail >= 2 else {}
    opt = opt or {}
    road_level, river_level, bold = opt.get("roads", 2), opt.get("rivers", 1), opt.get("weight", 1) >= 2
    ink = opt.get("ink") or {}
    colour = bool(ink)

    def I(role):
        return ink.get(role, 1)
    county_names = []
    for county in layers.get("counties") or []:
        best = None
        for ring in county.get("rings", []):
            pts = [xy(a, b) for a, b in ring]
            for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
                c.line(x0, y0, x1, y1, I("county"), dash=None if colour else (1, 3))
            if not best or len(ring) > len(best):
                best = ring
        if best:
            cx, cy = xy(sum(p[0] for p in best) / len(best), sum(p[1] for p in best) / len(best))
            county_names.append((cx, cy, county.get("name", "")))

    rivers = [w for w in osm.get("water") or [] if isinstance(w, dict)] if river_level else []
    if river_level == 1:                                 # major rivers: enough named length inside the map
        length = {}
        for w in rivers:
            if w.get("n"):
                length[w["n"]] = length.get(w["n"], 0) + path_miles(w["pts"])
        rivers = [w for w in rivers if length.get(w.get("n"), 0) >= opt.get("rivermin", 30)]
    for w in rivers:                                     # rivers: dotted lines
        p = [xy(a, b) for a, b in w["pts"]]
        for (x0, y0), (x1, y1) in zip(p, p[1:]):
            if colour:
                c.line(x0, y0, x1, y1, I("river"), width=3 if bold else 2)
            else:
                c.line(x0, y0, x1, y1, dash=(2, 4) if bold else (2, 2), width=2 if bold else 1)

    spots = {}                                           # road number -> candidate label positions
    for kind in ("p", "t", "m"):                         # motorways last so they sit on top
        if ("m", "t", "p").index(kind) >= road_level or (kind == "p" and ppm < 3):
            continue
        for r in osm.get("roads") or []:
            if r.get("k") != kind:
                continue
            p = [xy(a, b) for a, b in r["pts"]]
            for (x0, y0), (x1, y1) in zip(p, p[1:]):
                if colour:
                    c.line(x0, y0, x1, y1, I(kind), width=(3 if bold else 2) if kind != "p" else 1)
                else:
                    c.line(x0, y0, x1, y1, width=2 if kind == "m" and bold else 1,
                           dash=None if kind == "m" or bold else (4, 1) if kind == "t" else (3, 2))
            vis = [q for q in p if inside(q[0], q[1], 14)]
            if r.get("ref") and vis and opt.get("labels", 1):
                spots.setdefault((("m", "t", "p").index(kind), r["ref"]), []).extend(vis)

    taken = []
    if home:                                             # home: a cross; labels keep clear of it
        hx, hy = home
        c.rect(hx - 6, hy - 6, 13, 13, 0)
        c.line(hx - 5, hy, hx + 5, hy, I("home")); c.line(hx, hy - 5, hx, hy + 5, I("home"))
        c.line(hx - 5, hy - 1, hx + 5, hy - 1, I("home")); c.line(hx - 1, hy - 5, hx - 1, hy + 5, I("home"))
        taken.append((hx - 7, hy - 7, hx + 8, hy + 8))
    for (_rank, ref), vis in sorted(spots.items())[:40]:  # one number per road, near the middle of what is visible
        mx_ = sum(q[0] for q in vis) / len(vis); my_ = sum(q[1] for q in vis) / len(vis)
        x, y = min(vis, key=lambda q: (q[0] - mx_) ** 2 + (q[1] - my_) ** 2)
        w = len(ref) * CW + 3
        box = (int(x - w / 2), int(y - 5), int(x - w / 2) + w, int(y - 5) + CH + 2)
        if not overlaps(box, taken):
            taken.append(box)
            c.label(box[0], box[1], ref)

    places = sorted((osm.get("places") or []) if opt.get("towns", 1) else [], key=lambda q: (q.get("k") != "city", -q.get("pop", 0)))
    for pl in places[:60]:
        x, y = (int(round(v)) for v in xy(pl["lat"], pl["lon"]))
        name = pl["name"].upper().encode("ascii", "replace").decode()[:18]
        tw = len(name) * CW
        for bx in (x + 4, x - 4 - tw):                    # name to the right of the dot, else to the left
            box = (bx - 1, y - 4, bx + tw, y + 5)
            if inside(box[0], box[1], 1) and inside(box[2], box[3], 1) and not overlaps(box, taken):
                taken.append(box)
                c.rect(box[0], box[1], tw + 1, 9, 0)
                c.text(bx, y - 3, name, I("town"))
                c.rect(x - 2, y - 2, 5, 5, 0); c.rect(x - 1, y - 1, 3, 3, I("town"))
                break

    if not osm.get("places"):                            # county names only when there are no town names
        for cx, cy, name in county_names:
            tw = len(name) * CW
            box = (int(cx - tw / 2), int(cy - 4), int(cx + tw / 2), int(cy + 5))
            if name and inside(box[0], box[1], 2) and inside(box[2], box[3], 2) and not overlaps(box, taken):
                taken.append(box)
                c.text(box[0], box[1] + 1, name, I("county") if colour else 1)


BASE_CACHE = {}        # drawn base maps, so a request only has to add the objects


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def marker(c, x, y, kind, v=1):
    """A small symbol on a background patch: triangle = traffic, square = fire/EMS, circle = weather,
    diamond = other, hollow square = somebody else's APRS object or station."""
    c.rect(x - 5, y - 6, 11, 12, 0)
    if kind == "traffic":
        for i in range(10):
            c.line(x - i // 2 - 1, y - 5 + i, x + i // 2 + 1, y - 5 + i, v)
    elif kind == "fire":
        c.rect(x - 4, y - 4, 9, 9, v)
    elif kind == "aprs":
        c.frame(x - 4, y - 4, 9, 9, v); c.frame(x - 3, y - 3, 7, 7, v)
    elif kind == "weather":
        for yy in range(-5, 6):
            for xx in range(-5, 6):
                d = xx * xx + yy * yy
                if 14 <= d <= 30 or d <= 3:
                    c.set(x + xx, y + yy, v)
    else:
        for i in range(-5, 6):
            c.line(x - (5 - abs(i)), y + i, x + (5 - abs(i)), y + i, v)


def kind_of(o):
    name, src = o.get("name", ""), o.get("source", "")
    if src == "aprs":
        return "aprs"
    if src in ("nws", "nhc"):
        return "weather"
    if name.startswith(("TRAF", "WORK")) or src in ("njta", "nj511", "pa511"):
        return "traffic"
    if name.startswith(("FIRE", "EMS")):
        return "fire"
    return "other"


SHORT_NAMES = (("TRAF", "TRAFFIC"), ("FIRE", "FIRE"), ("WORK", "WORK"))


def display_name(o):
    """Label for an object: our own traffic/fire/roadwork objects are just that word (their names
    are generated ids); everything else keeps its callsign or object name."""
    name = str(o.get("name", ""))
    if o.get("source") != "aprs":
        for prefix, word in SHORT_NAMES:
            if name.startswith(prefix):
                return word
    return name


def wrap(text, width, lines):
    words, out, cur = text.split(), [], ""
    for w in words:
        while len(w) > width:
            if cur:
                out.append(cur); cur = ""
            out.append(w[:width]); w = w[width:]
        if len(cur) + len(w) + (1 if cur else 0) <= width:
            cur = f"{cur} {w}".strip()
        else:
            out.append(cur); cur = w
    if cur:
        out.append(cur)
    if len(out) > lines:
        out = out[:lines]
        out[-1] = out[-1][:width - 2] + ".."
    return out


def number(value, default, lo, hi):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if lo <= v <= hi and v == v else default


def parse_home(text):
    """(lat, lon) from text such as '40.12,-75.34', '40.12, -75.34', '40.12 -75.34' or
    '40.12N 75.34W' (quotes and degree signs are ignored).  Raises ValueError."""
    t = str(text or "")
    for odd, plain in (("\u2212", "-"), ("\u2013", "-"), ("\u2014", "-"), ("\u2010", "-"), ("\u2011", "-"),
                       ("\u201c", ""), ("\u201d", ""), ("\u2018", ""), ("\u2019", ""), ("\u00a0", " "),
                       ("\ufeff", ""), ("\u00b0", " ")):
        t = t.replace(odd, plain)                     # minus signs and quotes from copied text
    t = t.strip().strip("'\"").upper().strip()
    if not t or re.sub(r"[-+\d.\sNSEW,;/]", "", t):
        raise ValueError(f"cannot read a latitude and longitude from {text!r}"
                         + (" (it contains characters other than digits, '-', ',' and N/S/E/W)"
                            if re.search(r"[^\x20-\x7e]", str(text or "")) else ""))
    nums = list(re.finditer(r"[-+]?\d+(?:\.\d+)?", t))
    letters = list(re.finditer(r"[NSEW]", t))
    if len(nums) != 2 or len(letters) not in (0, 2):
        raise ValueError(f"cannot read a latitude and longitude from {text!r}")
    hemi = ["", ""]
    if letters:                                       # 40.12N 75.34W (suffix) or N40.12 W75.34 (prefix)
        suffix = t[-1] in "NSEW"
        for L in letters:
            owner = [i for i, n in enumerate(nums) if (n.end() <= L.start() if suffix else n.start() >= L.end())]
            if not owner:
                raise ValueError(f"cannot read a latitude and longitude from {text!r}")
            i = owner[-1] if suffix else owner[0]
            if hemi[i]:
                raise ValueError(f"cannot read a latitude and longitude from {text!r}")
            hemi[i] = L.group()
    vals = [-abs(float(n.group())) if h in ("S", "W") else float(n.group()) for n, h in zip(nums, hemi)]
    lat, lon = vals
    if hemi[0] in ("E", "W") or hemi[1] in ("N", "S") or (abs(lat) > 90 >= abs(lon) and not hemi[0]):
        lat, lon = lon, lat                           # written longitude first
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError(f"{text!r} is outside the range of a latitude and longitude")
    return lat, lon


_HOME_WARNED = []


def home_position():
    text = os.environ.get("APRS_HOME") or ""
    if not text.strip():
        return DEFAULT_HOME
    try:
        return parse_home(text)
    except ValueError as e:
        if not _HOME_WARNED:
            _HOME_WARNED.append(1)
            print(f"# APRS_HOME: {e}; using {DEFAULT_HOME[0]},{DEFAULT_HOME[1]} instead", file=sys.stderr, flush=True)
        return DEFAULT_HOME


def render(objects, params, basemap=None, now=None, colour=False, symbols=None, messages=None):
    """Draw the picture.  objects: the web UI's object records.  Returns a Canvas (before rotation)."""
    zoom = int(number(params.get("zoom"), 1, 1, 4))
    w = int(number(params.get("w"), 800 if colour else 272, 64, 2000)) // zoom
    h = int(number(params.get("h"), 480 if colour else 792, 64, 2000)) // zoom
    ink = COLOUR_INK if colour else {}

    def I(role):
        return ink.get(role, 1)
    hlat, hlon = home_position()
    lat0 = number(params.get("lat"), hlat, -89, 89)
    lon0 = number(params.get("lon"), hlon, -180, 180)
    miles = number(params.get("miles"), 15, 0.5, 2000)
    want_list = str(params.get("list", "1")) != "0"
    c = Canvas(w, h)

    bar = 13
    list_h = list_w = 0
    if want_list and w >= h * 1.2 and w >= 360:          # wide picture: list down the right-hand side
        list_w = max(170, min(int(w * 0.36), 330))
    elif want_list and h >= 200:
        list_h = max(CH * 4, min(int(h * 0.30), h - bar - 120))
    mx, my, mw, mh = 0, bar, w - list_w, h - bar - list_h

    # equirectangular projection: `miles` from the centre to the nearer edge of the map
    ppm = (min(mw, mh) / 2.0) / miles                   # pixels per mile
    kx = 69.172 * math.cos(math.radians(lat0)) * ppm     # pixels per degree of longitude
    ky = 69.0 * ppm

    def xy(lat, lon):
        return mx + mw / 2.0 + (lon - lon0) * kx, my + mh / 2.0 - (lat - lat0) * ky

    def inside(x, y, margin=0):
        return mx + margin <= x < mx + mw - margin and my + margin <= y < my + mh - margin

    north, south = lat0 + mh / 2.0 / ky, lat0 - mh / 2.0 / ky
    west, east = lon0 - mw / 2.0 / kx, lon0 + mw / 2.0 / kx

    # ---- map ----
    c.clip = (mx, my, mx + mw, my + mh)
    detail = int(number(params.get("detail"), 2, 0, 2))
    hx, hy = (int(round(v)) for v in xy(hlat, hlon))
    home = (hx, hy) if inside(hx, hy, 6) else None
    opt = {"roads": int(number(params.get("roads"), 3 if colour else 2, 0, 3)), "rivers": int(number(params.get("rivers"), 1, 0, 2)),
           "rivermin": number(params.get("rivermin"), 2 * miles, 0, 5000),
           "weight": int(number(params.get("weight"), 1, 1, 2)),
           "labels": int(number(params.get("labels"), 1, 0, 1)), "towns": int(number(params.get("towns"), 1, 0, 1))}
    if not (basemap and detail):
        draw_base(c, {}, xy, inside, ppm, 0, home, {"ink": ink})
    else:
        layers = basemap.layers(south, west, north, east, step=(north - south) / mh / 2)
        bkey = (w, h, round(lat0, 5), round(lon0, 5), miles, list_h, list_w, detail, basemap.version, id(basemap),
                colour, tuple(sorted(opt.items())))
        if bkey not in BASE_CACHE:
            draw_base(c, layers, xy, inside, ppm, detail, home, dict(opt, ink=ink))
            if len(BASE_CACHE) >= 8:
                BASE_CACHE.clear()
            BASE_CACHE[bkey] = bytes(c.px)
        else:
            c.px[:] = BASE_CACHE[bkey]

    for o in objects:                                    # warning outlines
        s = o.get("shape")
        if not s or len(s.get("points", [])) < 2:
            continue
        pts = [xy(a, b) for a, b in s["points"]]
        segs = list(zip(pts, pts[1:] + (pts[:1] if s.get("closed") else [])))
        dash = None if s.get("style") == "solid" else (6, 4)
        for (x0, y0), (x1, y1) in segs:
            c.line(x0, y0, x1, y1, I(s.get("colour") if s.get("colour") in ink else "ink"), dash=dash, width=2)

    if str(params.get("paths", "1")) != "0":             # where moving objects have been
        for o in objects:
            pts = [xy(a, b) for a, b in o.get("path") or []]
            v = I(o.get("source") if o.get("source") in ink else "other")
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                c.line(x0, y0, x1, y1, v, dash=(2, 2), width=2 if colour else 1)

    shown = []
    kc = math.cos(math.radians(lat0))                      # list order: nearest to the centre of the map first
    for o in sorted(objects, key=lambda o: (o["lat"] - lat0) ** 2 + ((o["lon"] - lon0) * kc) ** 2):
        x, y = xy(o["lat"], o["lon"])
        if inside(x, y, 7):
            shown.append((o, int(round(x)), int(round(y))))
    use_sym = bool(symbols) and str(params.get("symbols", "1" if colour else "0")) != "0" and symbols.ready()
    reach = {}                                           # how far each marker extends from its centre
    for n, (o, x, y) in enumerate(shown, 1):
        icon = symbols.icon(o.get("table"), o.get("code")) if use_sym else None
        if icon and colour:
            c.paste(icon, x, y)
        elif icon:
            c.rect(x - icon[0] // 2, y - icon[0] // 2, icon[0], icon[0], 0)
            c.paste_mono(icon, x, y)
        else:
            marker(c, x, y, kind_of(o), I(o.get("source") if o.get("source") in ink else "other"))
        reach[n] = icon[0] // 2 + 2 if icon else 8
    by_name = str(params.get("names", "1" if colour else "0")) != "0"     # label with the callsign, not a number
    tags = {n: (display_name(o) if by_name else str(n)) for n, (o, _x, _y) in enumerate(shown, 1)}
    taken = [(x - reach[n] + 2, y - reach[n] + 2, x + reach[n] - 2, y + reach[n] - 2)      # keep labels off the markers
             for n, (o, x, y) in enumerate(shown, 1)] if by_name else []
    for n, (o, x, y) in enumerate(shown, 1):             # labels, placed where they do not collide
        lw, lh = len(tags[n]) * CW + 3, CH + 2
        r = reach[n]
        for dx, dy in ((r, -5), (-r - lw, -5), (-lw // 2, -r - lh), (-lw // 2, r), (r, 4), (-r - lw, 4)):
            bx, by = x + dx, y + dy
            box = (bx, by, bx + lw, by + lh)
            if inside(bx, by) and inside(bx + lw, by + lh) and not overlaps(box, taken):
                break
        else:
            bx, by = min(max(x + r, mx), mx + mw - lw), min(max(y - 5, my), my + mh - lh)
            box = (bx, by, bx + lw, by + lh)
        taken.append(box)
        c.label(bx, by, tags[n])

    # scale bar
    step = next((s for s in (1, 2, 5, 10, 20, 50, 100, 200, 500) if s * ppm >= 40), 500)
    sw = int(step * ppm)
    sx, sy = mx + 6, my + mh - 8
    c.rect(sx - 3, sy - 11, sw + len(f"{step} MI") * CW + 12, 17, 0)
    c.line(sx, sy, sx + sw, sy, width=2); c.line(sx, sy - 4, sx, sy); c.line(sx + sw, sy - 4, sx + sw, sy)
    c.text(sx + sw + 5, sy - 6, f"{step} MI")
    c.clip = (0, 0, w, h)
    c.frame(mx, my, mw, mh)

    # ---- title bar ----
    c.rect(0, 0, w, bar, I("bar"))
    stamp = time.strftime("%a %H:%M", time.localtime(now)).upper()
    count = f"{len(shown)} ON MAP" + (f" +{len(objects) - len(shown)}" if len(objects) > len(shown) else "")
    bt = ink.get("bartext", 0)
    c.text(3, 3, "APRS", bt)
    c.text(max(3 + 5 * CW, (w - len(stamp) * CW) // 2), 3, stamp, bt)
    c.text(w - len(count) * CW - 3, 3, count, bt)

    # ---- list ----
    if list_h or list_w:
        lx, ly = (mx + mw + 5, bar + 4) if list_w else (3, my + mh + 4)
        lwid, bottom = (list_w - 8 if list_w else w - 6), h - 2
        pad = 8 if colour else 0                             # room for a colour key square
        cols = max(8, (lwid - pad) // CW)
        # latest APRS messages: a small section at the foot of the list
        want = int(number(params.get("messages"), 4 if colour else 0, 0, 12))
        msgs = list(messages or [])[:want]
        if msgs and bottom - ly > CH * 8:
            mcols = max(8, lwid // CW)
            block = []
            for m in msgs:
                stamp = time.strftime("%H:%M", time.localtime(m.get("time", 0)))
                block.append(wrap(f"{stamp} {m.get('src', '')}>{m.get('to', '')} {m.get('text', '')}", mcols, 2))
            room = (bottom - ly) // 2                        # never more than half the list
            while block and CH + 3 + sum(len(b) * CH + 1 for b in block) > room:
                block.pop()
            if block:
                top = bottom - (CH + 3 + sum(len(b) * CH + 1 for b in block))
                c.rect(lx - 2, top, lwid + 4, CH + 1, I("bar"))
                c.text(lx, top + 1, "MESSAGES", ink.get("bartext", 0))
                yy = top + CH + 3
                for b in block:
                    for i, ln in enumerate(b):
                        c.text(lx + (0 if i == 0 else CW), yy, ln[:mcols - (0 if i == 0 else 1)],
                               I("ink") if i or not colour else I("town"))
                        yy += CH
                    yy += 1
                bottom = top - 3
        y = ly
        if not shown:
            for ln in wrap("NO ACTIVE OBJECTS ON THE MAP", cols, 2):
                c.text(lx, y, ln); y += CH
        for n, (o, _x, _y) in enumerate(shown, 1):
            text = re.sub(r"^\d{3}/\d{3}(?:/[A-Z]{2}/\S+)?\s*", "", o.get("comment", ""))
            lines = wrap(f"{tags[n]} {text}" if by_name else f"{n} {o['name']} {text}", cols, 2)
            if y + len(lines) * CH > bottom:
                c.rect(lx - 2, y, lwid + 4, bottom - y + 2, 0)
                if y + CH <= h:
                    c.text(lx, y, f"+{len(shown) - n + 1} MORE")
                break
            if colour:
                c.rect(lx, y, 5, 7, I(o.get("source") if o.get("source") in ink else "other"))
            for i, ln in enumerate(lines):
                ind = 0 if i == 0 else (1 if by_name else len(str(n)) + 1) * CW
                c.text(lx + pad + ind, y, ln[:cols - ind // CW])
                y += CH
            y += 2
    return c


def image(objects, params, basemap=None, fmt="png", symbols=None, messages=None):
    """(bytes, content type) for the request.  fmt: png, bmp (black and white) or colour."""
    colour = fmt == "colour"
    c = render(objects, params, basemap, colour=colour, symbols=symbols, messages=messages)
    c = c.scaled(int(number(params.get("zoom"), 1, 1, 4)))
    c = c.rotated(int(number(params.get("rotate"), 0, 0, 270)) // 90 * 90)
    if colour:
        theme = DARK if str(params.get("dark", "0")) == "1" else LIGHT
        colours = [theme[r] for r in ROLES]
        return (c.png_rgb(colours) if c.over else c.png_palette(colours)), "image/png"
    invert = str(params.get("invert", "0")) == "1"
    if fmt == "bmp":
        return c.bmp(invert), "image/bmp"
    return c.png(invert, gray=str(params.get("gray", "0")) == "1"), "image/png"
