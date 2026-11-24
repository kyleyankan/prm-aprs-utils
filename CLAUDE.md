# CLAUDE.md

Two related amateur-radio (APRS) projects by K3YLE. Read `HANDOFF.md` for the history, the
decisions behind the code and the open tasks before making changes.

| Folder | What it is | Where it runs |
|---|---|---|
| `aprs-pi/` | Scripts that turn county dispatch, 511 traffic and NWS weather feeds into APRS objects (sent as **K3YLE-15**), plus a supervisor, a web map/editor, an APRS-IS listener and e-ink/colour picture endpoints | A Raspberry Pi on the home network, copied over by hand (no Docker) |
| `aprs-mapimage/` | A stand-alone Docker service that listens to APRS-IS and draws map pictures (PNG/BMP) for e-paper and dashboards, with a builder web page and saved presets | Docker; live at https://aprs.phlm.sh/ (host port 8083 -> container 8080) |

## Ground rules

- **Python 3 standard library only.** No pip dependencies in either project. The Pi runs stock
  Raspberry Pi OS; the Docker image is `python:3.12-slim` plus `tzdata`. If something seems to need a
  library, write it with the standard library (the PNG/BMP writer, the map renderer and the HTTP
  servers already are).
- **Never commit or print secrets**: `aprs-pi/aprs.env`, `aprs-mapimage/.env`, API keys (Google,
  aprs.fi), the PennDOT RCRS login, the map server's admin password or picture key. Credentials come
  from the environment / `aprs.env` only, never from the command line (it shows in `ps`).
- **The owner's e-mail address is private.** Never put it in code, examples or docs, and never send it
  to an outside service without asking (`NWS_CONTACT` is set by the owner in `aprs.env`).
- **Never transmit while testing.** `SEND=0` (the default) prints packets only. Tests use fake
  APRS-IS servers on localhost; nothing in a test may log in to the real APRS-IS with a passcode.
- **Respect the data sources.** If a site refuses automated access, do not work around it.
  aprs.fi API: only on demand, answers cached (30 min default), back off after errors, a User-Agent
  with app name, version and home page, and a visible link crediting aprs.fi. No background polling.
- **APRS object rules:** names at most 9 characters and stable from poll to poll; comments fitted to
  `COMMENT_LENGTH` (67, or 43 for strict clients); an object that is no longer wanted is killed
  (`_` instead of `*`), never just abandoned; a failed fetch kills nothing.
- **The map server's `CALLSIGN` needs its own SSID** (e.g. `-13`): APRS-IS holds back packets from the
  callsign a client logs in as, so sharing K3YLE-15 hides the owner's own objects.
- Plain, specific wording in README files, log messages and the web pages.

## Commands

```sh
# aprs-pi (on the Pi, from aprs-pi/)
./aprs_ctl.sh init            # create aprs.env from defaults
./aprs_ctl.sh start|stop [svc]|restart [svc]|status
./aprs_ctl.sh logs nws -f
./aprs_ctl.sh symbols         # add missing SYM_* settings to aprs.env
python3 aprs_pa511.py --help  # every feed script has --help; pa511 also has --dump
python3 aprs_objects.py --callsign K3YLE-15 --aprsfi-key KEY   # find/kill leftover objects

# aprs-mapimage
cd aprs-mapimage && cp .env.example .env && docker compose up -d --build
cd aprs-mapimage/app && DATA_DIR=/tmp/mi APRS_HOME=40.12,-75.34 PORT=8790 python3 server.py   # without Docker
python3 aprs-mapimage/tools/build_world.py <natural-earth geojson folder> aprs-mapimage/app/world.json.gz
```

Tests: `aprs-mapimage/tests/README.md` (development harnesses with fake APRS-IS, fake map services
and fake aprs.fi; run each from its own folder).

## Layout notes

- `aprs_eink.py` (renderer) and `aprs_listen.py` (APRS-IS listener) exist in **both** projects and have
  diverged: the `aprs-mapimage/app/` copies are newer (multi-area listening, follow filters, water fill,
  world map, old-position markers). Change both deliberately, or consolidate (open task in HANDOFF.md).
- `aprs-mapimage/app/world.json.gz` is generated (Natural Earth, public domain); rebuild it with
  `tools/build_world.py`, don't edit it.
- Runtime data lives in `aprs-pi/data/` and the map server's `/data` volume; never commit it.
