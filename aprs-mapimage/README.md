# APRS map images

A small Docker service that listens to APRS-IS (receive only) and draws pictures of what is
on the air around a point you choose: stations, objects and items, their recent paths, and the
latest messages, over a base map of county lines, roads, rivers and towns.

It is made for e-paper displays (ESPHome, Inkplate, TRMNL-style screens) and dashboards that
fetch a picture from an address every few minutes. A builder page lets you choose the
options, see a live preview, and save the result under a short fixed address.

## Start it

```sh
cp .env.example .env        # set APRS_HOME, CALLSIGN, TZ (and NWS_CONTACT for county lines)
docker compose up -d --build
```

Open `http://<host>:8083/`.

Without compose:

```sh
docker build -t aprs-mapimage .
docker run -d --name aprs-mapimage --restart unless-stopped --init \
  --env-file .env -p 8083:8080 -v mapimage-data:/data aprs-mapimage
```

It works on x86 and on a Raspberry Pi (arm64), using only the Python standard library.

## Addresses

| Address | Picture |
|---|---|
| `/map.png` | Colour, 800×480 by default |
| `/eink.png` | 1-bit black and white, 272×792 by default |
| `/eink.bmp` | The same as a 1-bit BMP, for displays that cannot decode PNG |
| `/p/NAME.png` (or `.bmp`) | A saved preset. Options added to the address override the preset's (e.g. `?rotate=90`) |
| `/api/status` | Listener status |
| `/healthz` | Docker health check; always open |
| `/api/aprs` | Everything heard, as JSON |
| `/api/presets` | Saved presets (GET; POST `{name, fmt, params}`; DELETE `/api/presets/NAME`) |

Picture options, all optional:

| Option | Meaning |
|---|---|
| `w`, `h` | Size in pixels (64–2000) |
| `lat`, `lon` | Centre (default `APRS_HOME`) |
| `miles` | Centre to the nearer edge of the map |
| `rotate` | 0, 90, 180 or 270 |
| `zoom` | Draw text and symbols 1–4× larger |
| `stations`, `objects` | 0 to leave out stations, or objects and items |
| `wx` | 1 to show weather stations (stations with the APRS weather symbol); hidden by default |
| `paths`, `symbols`, `names`, `list` | 0 or 1 |
| `messages` | How many recent messages to list |
| `follow` | A callsign with an optional SSID (e.g. `K3XYZ-9`): centre on it wherever it was last heard |
| `detail` | 0 none, 1 county lines, 2 roads, rivers and towns |
| `roads` | 0–3 |
| `rivers` | 0–2 |
| `towns`, `labels` | 0 or 1 |
| `water` | 0 to leave out the filled lakes, bays and sea |
| `weight` | 1 or 2 |
| `dark` | Colour pictures only |
| `invert`, `gray` | Black and white pictures only |

Pictures are cached for 15 seconds, so many displays can share one address.

## Following a station

Put a callsign with an optional SSID in **Follow a station** on the builder page, or add
`follow=K3XYZ-9` to a picture address. The picture is centred wherever that station was last
heard, and moves with it. The title bar shows the callsign; if it has not been heard for half an
hour it adds how long ago (`K3XYZ-9 2H AGO`), and if it has never been heard it says
`NOT HEARD` and uses the picture's latitude and longitude (or home) instead.

The server asks APRS-IS for that station wherever it is (`b/` filter), and for everything
around it (`f/` filter), so other stations near it show up even far from home. The last
position is kept in `/data`, so a parked car stays on the map after a restart. Followed
stations in presets are listened for all the time. One typed into the builder or used in an
address is listened for until a day after the last picture asked for it.

### Old positions

The server keeps the last position of every station and object it hears for `HISTORY_DAYS`
(default 30), in `/data/last_seen.json`. Following something that has gone quiet still centres
on its last position: the marker gets a dashed box, its label says how old it is
(`K3XYZ-9 (5H AGO)`), and its text in the list starts `OLD POSITION`.

For stations and objects this server never heard, set `APRSFI_KEY` to an
[aprs.fi](https://aprs.fi) API key (free, under Account settings). Following then falls back to
aprs.fi's last known position, marked `VIA APRS.FI`. As aprs.fi's API rules ask, it is only
asked when a picture or the builder needs a position, answers are cached for 30 minutes, it
backs off after errors, and the builder links to aprs.fi whenever a position came from there.
Set `PUBLIC_URL` to this server's address; it is sent to aprs.fi as the app's home page.

You can follow objects and items by name too (up to 9 characters, e.g. `TRAF0M1ZB`).

## Base map

Roads, rivers, towns, county lines and water are downloaded ahead of time, in tiles of a quarter
degree (about 17 × 13 miles), and kept in the `/data` volume:

1. the area around `APRS_HOME` that the builder shows first,
2. every preset's area,
3. the area around each followed station (checked every two minutes as it moves),
4. everything within `PREFETCH_MILES` of `APRS_HOME` (default `LISTEN_MILES`, at most 60).

The first start downloads for a few minutes. The status line on the builder page shows the
progress, and pictures fill in as tiles arrive. After that, pictures of these areas have their
base map from the first request. A picture somewhere new fetches its tiles at the front of the
queue. Views wider than about 100 miles use only tiles already on disk.

### The whole world

A worldwide base map from [Natural Earth](https://www.naturalearthdata.com) (public domain) ships
with the app (`app/world.json.gz`, 4.4 MB), so every picture has a map under it, anywhere, with
nothing to download:

- country borders and state/province borders, always drawn (heavier purple for countries);
- major and secondary highways with their route numbers, rivers, lakes, land and sea, and cities.

Detail follows the scale, like a web map: a continental view shows the main highways and big
cities, a regional one adds secondary highways and towns. The detailed OpenStreetMap layers take
over once every tile of a view is downloaded; until then, and for views too wide for tiles (about
100 miles and more), the Natural Earth layers are used. With `EINK_OSM=0` the server never contacts
OpenStreetMap and draws from Natural Earth alone. `WORLD_MAP=0` turns the world map off.

`tools/build_world.py` rebuilds the file from Natural Earth's GeoJSON (instructions inside).

### Water

Lakes, reservoirs, wide rivers and bays are filled from their OpenStreetMap outlines (ponds smaller
than about 300 m are left out). OpenStreetMap has no outline for the ocean, only the coastline,
which always has the sea on its right-hand side, so the sea is filled from the coastline. Colour
pictures use a deeper blue for sea and bays and a lighter one for lakes and rivers, with a thin
shoreline. Black-and-white pictures use a light dot pattern, denser for the sea.

If part of the coastline is missing (its tiles are still downloading), the sea near the gap is
left unfilled rather than guessed, and fills in once the tiles arrive.

## Sign-in

Set `ADMIN_PASSWORD` (and optionally `ADMIN_USER`, default `admin`) in `.env`, then run
`docker compose up -d` again. The builder page and the API then ask for that username and
password; your browser asks once and remembers it until it closes.

Displays fetching pictures usually cannot sign in, so pictures stay open unless you also set
`IMAGE_KEY`. With a key, every picture address needs `?key=<IMAGE_KEY>` (or the sign-in).
The builder adds the key to the addresses it shows you, so Copy gives a working address.
Changing the key breaks the addresses your displays already use.

This is plain HTTP Basic sign-in: the password crosses the network unencrypted unless you
put HTTPS in front (for example a reverse proxy such as Caddy or Nginx Proxy Manager). It
keeps casual visitors out of a home network; it is not meant for the open internet.

## Listening area

The server listens within `LISTEN_MILES` of `APRS_HOME`. Every saved preset adds its own
area, unless that area is already covered, so a preset somewhere else fills in by itself
once it is saved. The builder warns you when the current view is outside every area being
listened to. What it hears is saved in the `/data` volume and reloaded on restart. On a cold
start the last half hour or so is fetched from the APRS-IS history port.

## Notes

- Receive only: the server logs in with passcode -1 and never transmits.
- Give `CALLSIGN` an SSID of its own (e.g. `-13`). APRS-IS servers want every connected program
  to use a different callsign-SSID; they drop one of two clients sharing one, and they hold back
  packets from the logged-in callsign. If it matches the callsign your own scripts send objects
  from, those objects will not appear here.
- No sign-in unless you set one (see below).
- The base map comes from OpenStreetMap (Overpass) and the county lines from the National
  Weather Service. Both are fetched once per area and cached in `/data`. APRS symbol
  pictures come from github.com/hessu/aprs-symbols. To run fully offline, set `EINK_OSM=0`
  and `EINK_SYMBOLS=0`.
- The builder page loads Leaflet and the map tiles from the internet in your browser. The
  pictures themselves do not need them.
