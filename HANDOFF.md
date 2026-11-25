# Handoff: APRS object feeds and APRS map images

Written 2026-11-24 when the work moved from a Claude chat to Claude Code. The chat built both
projects over about two weeks of November 2026; this file records what exists, why it is the way it
is, what was tried and dropped, and what is still open.

## 1. Goals

**aprs-pi — public incidents as APRS objects.** Put live public-safety, traffic and weather
information on the APRS map around southeastern Pennsylvania and South Jersey, as objects sent from
**K3YLE-15**: county fire/EMS/traffic calls, state 511 incidents, closures and road work, and NWS
warnings and storms. Each incident gets a stable name and a meaningful symbol, its text is kept
current, and it is killed when the incident ends.

**aprs-mapimage — pictures of what is on the air.** A small self-hosted service that draws what
APRS-IS carries around a chosen point as a picture an e-paper display or dashboard can fetch from a
fixed address: stations, objects, paths, messages, over a real base map, anywhere in the world.

## 2. Architecture

### aprs-pi (Raspberry Pi, no Docker)

```
feeds ──> aprs_<feed>.py (one process each) ──> data/outbox/ ──> aprs_send.py ──> APRS-IS
                 │  state: data/*_live_state.json            (one login, as K3YLE-15, only when SEND=1)
aprs_ctl.sh ─────┘  supervisor: starts/stops/restarts, crash back-off, logs, reads aprs.env before every launch
aprs_send.py  the only program that logs in to APRS-IS to send (service "send", always enabled).
              Sends an object when it is new, changed or killed, or last sent 26+ minutes ago.
aprs_priority.py  rates every object critical/high/medium/low; used by the sender, the web map and
              /api/packets. The minimum level for sending is kept in data/sender_settings.json.
aprs_web.py   web map/editor on :8080 + /eink.png /map.png pictures (aprs_eink.py) + other people's
              objects via aprs_listen.py; edits/deletes go through aprs_overrides.py and are handed
              to aprs_send.py. Also shows each object's level and send times, sets the minimum
              level, and serves /api/packets (packets near a point, for radios to transmit).
aprs_objects.py  stand-alone cleanup: find and kill leftover objects for a callsign (uses aprs.fi)
```

| Script | Source | Notes |
|---|---|---|
| `aprs_montco.py` | Montgomery County live CAD (RSS) | Geocoded (Google, cached); no incident ID, so names are a hash |
| `aprs_chesco.py` | Chester County WebCAD (public page, three tables) | Names from the CAD incident number tail |
| `aprs_bucks.py` | Bucks County 911 Incident Report (public Power BI) | Fire and traffic only; asks only for the fields the report shows; names `TAGB<hash>` |
| `aprs_nj511.py` | 511NJ RSS | Replaced `aprs_njta.py` (retired). County by point-in-polygon on NWS county zones (cached `nj_counties.json`); types incident, closure, delay by default; only events whose written dates include today; `--county Cape May` works unquoted |
| `aprs_pa511.py` | PennDOT RCRS (511PA) JSON | Free PennDOT login from `aprs.env`; names from EventID in base 36; killed when the road reopens; `--dump` shows raw fields |
| `aprs_nws.py` | api.weather.gov alerts, NHC | Storm objects with movement, multiline warning shapes, VTEC-based names (`SVRPHI123`); `parse_home()` accepts many lat/lon spellings |

Shared rules in every feed script: one poll every 2 minutes by default; the full set of wanted objects
is handed to `aprs_send.py` each poll (one file in `data/outbox/`); anything in the state file that is
no longer wanted gets a kill packet; objects older than 6 hours are killed even if still listed; a
failed fetch kills nothing and hands over nothing; an incident that fails to geocode this time is left
alone; state is written only after the hand-over file is written.

`aprs_send.py` (added 2026-11-24) decides what goes on the air. A packet is sent when the object is
new, its text after the time stamp changed, it is a kill, or it was last sent `--resend-minutes` (26)
or more ago. It only acts on what scripts hand over, so a stopped script's objects go quiet as they
did before. What is due while APRS-IS is unreachable waits in `data/sender_state.json` and goes out
after the next login; a waiting live object nobody has asked for in 30 minutes is dropped, a waiting
kill never is. Packets sent since the server was last heard from are sent again when a connection
turns out to be dead. A hand-over through files was chosen over a local socket: nothing listens on
a port, it needs no retry logic in the scripts, and hand-overs survive a restart of either side.
Still on their own connections: the receive-only listener in `aprs_web.py` (`aprs_listen.py`) and
the hand-run cleanup tool `aprs_objects.py`.

Added the same day, all described in `aprs-pi/ARCHITECTURE.md`:
- **Send times on the web map**: when each object last went to APRS-IS and when it is sent again.
- **`/api/packets`**: the packets last sent within a radius of a point, so a program beside a radio
  can put them on RF. `via=CALL` gives third-party form, chosen so this station stays the sender and
  igates do not pass the packet back to APRS-IS. No radio-side program exists yet.
- **Importance** (`aprs_priority.py`): a level from the name tag and the call type at the start of
  the text, and a score for ordering. Freshness moves only the score, never the level; otherwise an
  ageing incident would fall below the minimum and be killed. The rules were tuned against the
  wording in the Pi's logs and are only as good as that wording.
- **Minimum level for sending**, chosen on the web map. The sender holds back lower-rated objects
  and kills any that are already on the air. Hand-made objects, kills and status lines always go. Symbols come
from `SYM_*` settings (`./aprs_ctl.sh symbols`); comments are fitted to `COMMENT_LENGTH` by
abbreviating first and dropping the least important parts last; approximate geocodes end in `?`.

### aprs-mapimage (Docker)

```
APRS-IS (receive only, own SSID) ──> aprs_listen.py ──┐
OpenStreetMap Overpass + NWS zones ──> tiles.py ───────┤──> aprs_eink.py renderer ──> /map.png /eink.png /eink.bmp /p/NAME.png
Natural Earth (shipped world.json.gz) ──> world.py ───┘
aprs.fi (optional, on demand) ──> server.py locate()        builder page /  ·  /api/*  ·  presets
```

| File | Role |
|---|---|
| `app/server.py` | HTTP server (stdlib `ThreadingHTTPServer`), builder page (inline HTML/JS, Leaflet from unpkg), presets in `/data/presets.json`, follow logic, history store, aprs.fi lookup, sign-in, 15 s picture cache |
| `app/aprs_listen.py` | APRS-IS client: several `r/` areas (home + presets), `b/`/`f/`/`o/` filters for followed stations/objects, paths, messages, history backfill from the APRS-IS history port, state saved in `/data/aprs_heard.json` |
| `app/aprs_eink.py` | Pure-stdlib renderer (PNG/BMP writer, palette, fonts): base map, water fill, borders, symbols (hessu/aprs-symbols, cached), labels, object list, messages, old-position markers |
| `app/tiles.py` | Base map in 0.25° tiles from Overpass (roads, rivers, towns, water outlines, coastline) and NWS county zones, downloaded ahead of time by a queue; `/data/basemap_tiles.json` |
| `app/world.py` + `world.json.gz` | Natural Earth world layer: country/state borders always; highways, rivers, lakes, sea, cities when tiles don't cover the view |
| `tools/build_world.py` | Rebuilds `world.json.gz` from Natural Earth GeoJSON |
| `tests/` | Development harnesses (see its README) |

Key endpoints: `/` builder, `/map.png` colour, `/eink.png` 1-bit, `/eink.bmp`, `/p/NAME.png|bmp`
presets (query overrides), `/api/status`, `/api/aprs`, `/api/presets` (GET/POST/DELETE),
`/api/follow?call=`, `/healthz` (always open).

Settings (`.env`): `APRS_HOME`, `LISTEN_MILES`, `CALLSIGN` (own SSID), `TZ`, `NWS_CONTACT`,
`ADMIN_USER`/`ADMIN_PASSWORD` (Basic sign-in for page and API), `IMAGE_KEY` (`?key=` on pictures),
`HISTORY_DAYS` (30), `APRSFI_KEY`, `PUBLIC_URL`, `APRSFI_CACHE_MINUTES`, `PREFETCH_MILES`,
`FORGET_MINUTES`, `TRAIL_MINUTES`, `EINK_OSM`, `EINK_COUNTIES`, `EINK_SYMBOLS`, `WORLD_MAP`,
`APRS_BACKFILL`, `LOG_REQUESTS`.

## 3. Key decisions and why

- **Standard library only, both projects.** Simple installs on a Pi and a tiny Docker image; the
  renderer writes PNG/BMP itself.
- **aprs-pi runs directly on the Pi; aprs-mapimage is Docker.** The owner dropped Docker for the feed
  scripts ("deploy it ourselves onto a Raspberry Pi"); the map server was asked for as a container.
- **Stable names are the core rule.** Re-sending a name updates the marker; a new name makes a ghost.
  IDs where feeds have them, deterministic hashes where they don't, salted in sorted order on collisions.
- **Cautious kills.** Never kill on a failed fetch or geocode miss; persist state after sending; a
  replacement script kills its predecessor's objects (NJ 511 retired the NJ Turnpike script this way).
- **Supervisor re-reads `aprs.env` before every launch**, strips CRLF and checks syntax with `bash -n`,
  so a bad line names the file and line instead of crashing later.
- **Map server listens with its own SSID** and only warns (doesn't rewrite) when `CALLSIGN` has none:
  it can't know which SSIDs are free.
- **Tiles instead of one download per view** (0.25°, ~17 × 13 mi): a station being followed moves
  without refetching, and tiles can be downloaded before anyone asks (home, presets, followed stations,
  `PREFETCH_MILES`). Detailed OSM data is used only when **every** tile of the view is ready; otherwise
  the Natural Earth layer is used, so the two never overlap.
- **Sea from coastlines.** OSM has no ocean polygons. Coastline is drawn as walls, areas are flooded,
  and each coastline piece votes (sea on its right) only when its two sides fall in different areas;
  areas touching a loose coastline end (missing tile) are left unfilled rather than guessed. Bay
  outlines never paint over areas the coastline says are land. Natural Earth coastline direction isn't
  reliable, so its sea uses a point-in-land test against its land polygons instead.
- **Following** (`follow=CALL` or object name): centre on the last position; APRS-IS `b/` + `f/`
  (range around the station) + `o/` filters; presets follow permanently, ad hoc follows expire 24 h
  after the last request.
- **Old positions**: live → own history (every station/object heard, `HISTORY_DAYS`) → aprs.fi.
  Anything older than 30 minutes or not live is drawn with a dashed box, an age tag, and
  "OLD POSITION (VIA APRS.FI)" in the list.
- **Weather stations hidden by default** (`_` symbol stations; `wx=1` shows them); objects with the
  weather symbol are not affected.
- **Sign-in** is HTTP Basic for the page and API; pictures stay open or use `IMAGE_KEY`, because
  e-paper displays can't sign in. `/healthz` stays open for Docker.
- **Host port 8083** in docker-compose (container still 8080).

## 4. Rejected ideas and constraints to avoid

- Docker for the Pi project (owner's choice: run it directly).
- Keeping `aprs_njta.py` (retired for `aprs_nj511.py`; listed in `RETIRED` so the supervisor stops it).
- One base-map download per exact view (refetches whenever a followed station moves) → tiles.
- Using OSM ocean areas (they don't exist) or trusting Natural Earth coastline direction.
- Filling sea across a coastline gap by guesswork → leave it blank until the tile arrives.
- Silently appending an SSID to the map server's `CALLSIGN` → warn instead.
- Preloading or background-refreshing aprs.fi, or querying it from the planner thread (against their
  API rules) → only from picture/builder requests, cached.
- Putting the owner's e-mail anywhere, or sending it to services unasked.
- Getting around a site that blocks automated access (during development 511nj.org was read only
  through the owner's own browser, never by evading a block).
- Showing accented names as `?` → accents are stripped (QUEBEC), names with no Latin spelling skipped.
- Security posture: the Pi web UI is internal-only by the owner's decision (no login). The public map
  server is different and has sign-in plus a picture key.

## 5. Current state

Both projects are working and deployed by the owner: the feed scripts on the Pi (last packaged as
`aprs-pi-1119.zip`, which this folder matches), the map server at https://aprs.phlm.sh/.

```
aprs-pi/          aprs_ctl.sh, install-service.sh, README.txt, ARCHITECTURE.md, 6 feed scripts,
                  aprs_send.py, aprs_priority.py, aprs_web.py, aprs_eink.py, aprs_listen.py,
                  aprs_overrides.py, aprs_objects.py, tests/ (test_send, test_priority, test_web)
aprs-mapimage/    Dockerfile, docker-compose.yml, .env.example, .dockerignore, README.md,
                  app/ (server.py, aprs_listen.py, aprs_eink.py, tiles.py, world.py, world.json.gz),
                  tools/build_world.py, tools/sync_shared.py, tests/
```

Blog drafts (claude.ai docs, private to the owner): "Drawing the Airwaves: A Self-Hosted APRS Map
Image Server" (https://claude.ai/artifact/258poTWumCsSgrF8T1M297) and "From Dispatch Feeds to APRS
Objects" (https://claude.ai/artifact/CnXmEhpeNqWms87DNDqH8r).

**Deployed to the Pi on 2026-11-24** (`the Pi on the home network`, `~/aprs-pi`; the previous code and
data are in `~/aprs-pi-backup-20261124`): the single sender and the synced `aprs_eink.py` /
`aprs_listen.py`. Found during that deploy: APRS-IS servers close one of two connections that are
logged in with the same callsign and passcode. The web map's listener used to log in that way, so it
and the sender kept disconnecting each other; `aprs_web.py` now logs the listener in receive-only
(`pass -1`). The sender also re-sends a packet at most once after a dropped connection and waits
longer between reconnects when connections die within a minute. `aprs_ctl.sh` cannot be run on a
Mac (it needs Linux `stat -c` and `/proc`); test supervisor changes on the Pi.
Later the same day the send times, `/api/packets`, the importance ratings and the minimum level
were deployed too. The minimum was left at "everything"; raising it has only been tried in tests.
With the traffic of that day, "medium and above" would kill about 160 roadwork objects at once.

## 6. Remaining tasks

1. **Check the live paths the development sandbox could not reach**: aprs.fi lookups (needs
   `APRSFI_KEY` and `PUBLIC_URL` in `.env`), sea/water fill on the real Jersey Shore and Delaware Bay
   coastline, Natural Earth sea fill at the edges of real tiles.
2. **Map server sign-in**: the credentials the owner expected were rejected by the live server
   (pictures with the key worked). Check `ADMIN_USER`/`ADMIN_PASSWORD` in the server's `.env`.
3. **Done 2026-11-24: shared modules consolidated.** `aprs-mapimage/app/aprs_eink.py` and
   `aprs_listen.py` are the masters; `aprs-pi/` holds byte-identical copies made by
   `aprs-mapimage/tools/sync_shared.py` (`--check` fails if they differ; `tests/shared/` runs it).
   Symlinks were rejected (a broken link silently turns off pictures and listening, because
   `aprs_web.py` ignores the `ImportError`), and so was a top-level `shared/` folder (Docker would
   have to build from the repo root, putting `aprs-pi/aprs.env` in the build context).
   What changes on the Pi once the new copies are installed there: `/api/aprs` gains `areas`;
   `?title=` works; accented town names lose the accent instead of showing `?`; county lines are no
   longer drawn on very wide views (beyond about 226 miles on the default e-ink picture, about 390
   on colour). `aprs_heard.json` loads in both directions, so the old files can be put back.
   **Not yet copied to the Pi.**
4. **Turn `aprs-mapimage/tests/` into a real test suite** (pytest or unittest, ports chosen
   automatically, no fixed sandbox paths). aprs-pi has unittest files for the sender, the importance
   rules and the web API (`tests/`, fake APRS-IS on localhost); the feed scripts have none. Known harness problems:
   `follow/run.py` and `history/run.py` exit 0 even when an assertion fails (read the log for
   `PHASE OK`); `signin`, `coast`, `marsh` and `world` contain no assertions; `test_server.py` needs
   Pillow, which is not in the standard library; `test_signin.py` writes to `/tmp/auth*`; server
   start-up can stall on a slow reverse-DNS lookup of the machine's address.
5. **Radio side of `/api/packets`**: `aprs-radio/aprs_radio.py` (2026-11-24) polls it and sends to a
   KISS TNC over TCP or a serial port, with small default limits and print-only unless `--transmit`.
   It passes its tests against a fake feeder and fake TNC and reads the Pi's real list.
   **First on-air test, 2026-11-24 (evening, local):** run from the owner's Mac as `K3YLE-10`
   through the Pi's Direwolf (`--kiss-tcp <pi address>:9001 --kiss-port 0`; port 9001 is the
   all-channel KISS port, channel 0 is the 1200 baud radio, channel 11 is a network channel), with
   `--miles 10 --min low --max-objects 2 --once` and no path. Direwolf's console log shows PTT keyed
   and `[0L] K3YLE-10>APZIFD:}K3YLE-15>APRS,TCPIP,K3YLE-10*:;...` for two objects, then for their
   two kills (a second run with `--max-objects 0`). So the KISS framing, AX.25 addresses and
   third-party form are accepted by a real TNC and went out on RF. **Not shown yet: that another
   station decodes them** (nothing else was listening; igates do not pass third-party packets, so
   aprs.fi cannot confirm it). Nothing is left running. Still open:
   - Have a second receiver (handheld, another station's APRS display) confirm the objects appear
     and are removed by the kills.
   - **Reaching the feeder.** The owner put the web service behind a reverse proxy (Caddy) at
     `https://aprs-incidents.phlm.sh`. A test on 2026-11-24 showed the proxy passed everything
     through: the map, `/api/objects` and the POST addresses all answered from the internet, with
     no login, so anyone could have created, changed or killed objects under K3YLE-15.
     `aprs_web.py` now answers only `GET /api/packets` for requests the proxy passed on
     (`X-Forwarded-For` present) and 403 for the rest; `--proxied all` / `WEB_PROXIED=all` undoes
     it. This depends on the proxy adding that header. A path rule in the proxy itself would be a
     second layer and is still worth adding.
   - **Gateways layer** (same day): `/api/packets` requests are recorded (callsign given, point,
     radius, level, times, address; 7 days; `data/api_clients.json`) and drawn on the map.
   - `APZIFD` is an experimental destination; register a proper one if the script is published.
   - **Added 2026-11-25** (all in `aprs_send.py` and `aprs_web.py`, with tests; not yet deployed
     to the Pi when this was written):
     - *Maximum age*: the sender kills an object nobody has handed over for 2 hours
       (`--max-quiet-minutes`), after a 10-minute grace at start. This deliberately changes the old
       rule that the sender never kills on its own. The page marks a source with no good poll for
       10 minutes (state file not rewritten) and says when its objects go.
     - *Duplicates*: a county dispatch traffic object and a road-agency one within 0.3 miles
       (`--merge-miles`) are one incident; the higher level is sent, the county's on a tie, and
       the other is held with the same mechanism as the minimum level (`dup` in the record).
       Only TRAF objects, only across the two kinds of source. Not tried against live data: watch
       for wrong merges on dense roads and for two crashes at one junction.
     - *Bulletins*: `:BLNn     :text` for critical objects, slots BLN1-3 (`--bulletins`), kept in
       `sender_state.json`. Later the same day: `/api/packets` gained a `bulletins` list (the
       bulletin of each critical object within the radius; `bulletins=0` omits it; also in
       `format=text`), and `aprs_radio.py` 1.1 transmits them in third-party form, after kills and
       before objects, on the same new/changed/refresh rule, with no kill (`--no-bulletins` to
       skip). Not yet seen with a real critical object or on the air.
     - *Read-only map*: `/map` and `/api/public` are in `PUBLIC_PATHS`, and `/` through the proxy
       shows the map. It loads Leaflet and OpenStreetMap tiles from the internet, like the editor.
     - *Also 2026-11-25*: the read-only map has a Dark/Light button (same `aprs-theme` setting
       as the editor page), and each entry on the gateways layer can be removed from its popup
       (`POST /api/forget-client`, internal only; a station that asks again reappears). All of
       the above is deployed to the Pi. Seen live so far: the map, feed-health data, the packet
       list's `bulletins` field (empty). Not yet seen live: a merge, a bulletin, a stale kill.
       Noticed on the live map: Chester County lists one crash twice, as a TRAF and a FIRE call
       at the same junction; same-source pairs are not merged.
6. **Sharpen the importance rules where sources give real severity**: PennDOT (fatality, hazmat
   flags) and NWS (severity, certainty) could pass those through instead of relying on the text.
7. Offered but not built: colour the owner's own objects by feed in the map server (recognise
   K3YLE-15 objects and their name tags) and draw NWS warning outlines there (parse multiline objects).
8. Done: the repository is on GitHub (`origin`, branch `main`). Never commit `aprs.env` or `.env`.

## 7. Open questions

- Should old positions be shown for every object, not only followed ones?
- Blog article 1 has an open comment: name the specific feeds, or keep it generic?
- Memory: loading the world map peaks around 170 MB at start-up. Fine on a Pi 4 or server; is a
  smaller build (fewer roads at low zoom) wanted for small hosts?
