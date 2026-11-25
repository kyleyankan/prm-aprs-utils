# CLAUDE.md

This repository is **prm-aprs-utils**: related amateur-radio (APRS) projects by K3YLE, one per
folder. The feeder's folder is `aprs-incident-feeder/` (it was `aprs-pi/` until 2026-11-25, and on
the Pi itself it is still deployed in `~/aprs-pi`). Read `HANDOFF.md` for the history, the
decisions behind the code and the open tasks before making changes. `aprs-incident-feeder/ARCHITECTURE.md`
explains how the Pi's services fit together and where each feed's data comes from; keep it in step
with the code.

| Folder | What it is | Where it runs |
|---|---|---|
| `aprs-incident-feeder/` | Scripts that turn county dispatch, 511 traffic, NWS weather and river gauge feeds into APRS objects (sent as **K3YLE-15** by one sender, `aprs_send.py`), plus a supervisor, a web map/editor, an APRS-IS listener and e-ink/colour picture endpoints | A Raspberry Pi on the home network, copied over by hand (no Docker) |
| `aprs-incident-rf-bridge/` | One stand-alone script, `aprs_radio.py`, that other operators run: it polls the Pi's `/api/packets` and transmits the objects near them through their own KISS TNC | Anyone's machine beside a radio; nothing of ours runs it |
| `aprs-mapimage/` | A stand-alone Docker service that listens to APRS-IS and draws map pictures (PNG/BMP) for e-paper and dashboards, with a builder web page and saved presets | Docker; live at https://aprs.phlm.sh/ (host port 8083 -> container 8080) |

## Ground rules

- **Python 3 standard library only.** No pip dependencies in either project. The Pi runs stock
  Raspberry Pi OS; the Docker image is `python:3.12-slim` plus `tzdata`. If something seems to need a
  library, write it with the standard library (the PNG/BMP writer, the map renderer and the HTTP
  servers already are).
- **Never commit or print secrets**: `aprs-incident-feeder/aprs.env`, `aprs-mapimage/.env`, API keys (Google,
  aprs.fi), the PennDOT RCRS login, the map server's admin password or picture key. Credentials come
  from the environment / `aprs.env` only, never from the command line (it shows in `ps`).
- **The owner's e-mail address is private.** Never put it in code, examples or docs, and never send it
  to an outside service without asking (`NWS_CONTACT` is set by the owner in `aprs.env`).
- **Never transmit while testing.** `SEND=0` (the default) prints packets only; `aprs_radio.py`
  prints only unless given `--transmit`. Tests use fake
  APRS-IS servers on localhost; nothing in a test may log in to the real APRS-IS with a passcode.
- **Respect the data sources.** If a site refuses automated access, do not work around it.
  aprs.fi API: only on demand, answers cached (30 min default), back off after errors, a User-Agent
  with app name, version and home page, and a visible link crediting aprs.fi. No background polling.
- **One sender.** Only `aprs-incident-feeder/aprs_send.py` logs in to APRS-IS to send. Feed scripts and the web
  editor hand packets to it with `aprs_send.Outbox` (files in `data/outbox/`); never add another
  sending connection. It sends an object when it is new, changed or killed, or last sent 26 minutes
  or more ago. On its own account it does three things only: kills an object nobody has handed
  over for 2 hours, holds back the less important of two reports of one crash (county dispatch
  against a road agency), and sends a bulletin for a critical object.
- **Importance rules live in one file**, `aprs-incident-feeder/aprs_priority.py` (levels critical, high, medium,
  low). The sender's minimum level, the web map and `/api/packets` all use it; change the rules
  there and add a case to `tests/test_priority.py`.
- **The Pi's web service is public only at `/api/packets`, `/map`, `/api/public` and `/docs`** (the
  read-only map and its data, and the documentation pages in `aprs-incident-feeder/public_docs/`, which are
  public as written, so nothing private goes in them; nothing there may change anything or show gateways or services). `https://aprs-incidents.phlm.sh` is a
  reverse proxy in front of it; requests it passes on (they carry `X-Forwarded-For`) get 403 for
  everything else, because the page has no login and its edits are transmitted. Any new address
  added to `aprs_web.py` is internal unless it is deliberately added to `PUBLIC_PATHS`.
- **APRS object rules:** names at most 9 characters and stable from poll to poll; comments fitted to
  `COMMENT_LENGTH` (67, or 43 for strict clients); an object that is no longer wanted is killed
  (`_` instead of `*`), never just abandoned; a failed fetch kills nothing (the sender's maximum age
  takes a silent source's objects off after 2 hours).
- **The map server's `CALLSIGN` needs its own SSID** (e.g. `-13`): APRS-IS holds back packets from the
  callsign a client logs in as, so sharing K3YLE-15 hides the owner's own objects.
- Plain, specific wording in README files, log messages and the web pages.

## Commands

```sh
# aprs-incident-feeder (on the Pi, from aprs-incident-feeder/)
./aprs_ctl.sh init            # create aprs.env from defaults
./aprs_ctl.sh start|stop [svc]|restart [svc]|status
./aprs_ctl.sh logs nws -f
./aprs_ctl.sh symbols         # add missing SYM_* settings to aprs.env
python3 aprs_pa511.py --help  # every feed script has --help; pa511 also has --dump
./aprs_ctl.sh logs send -f    # what was really sent to APRS-IS, and why
cd data && python3 ../aprs_send.py --callsign K3YLE-15 --list   # each object: last change, last send
cd tests && python3 -m unittest test_priority test_send test_web test_water  # rating, sender, web API and river feed tests (fake servers on localhost)
python3 aprs_objects.py --callsign K3YLE-15 --aprsfi-key KEY   # find/kill leftover objects

# aprs-incident-rf-bridge (prints only; --transmit keys a radio through a KISS TNC, so never add it in a test)
python3 aprs-incident-rf-bridge/aprs_radio.py --server https://aprs-incidents.phlm.sh --callsign W3XYZ-10 --lat 40.12 --lon -75.34 --once
cd aprs-incident-rf-bridge/tests && python3 -m unittest test_radio
sudo sh aprs-incident-rf-bridge/systemd/install.sh   # on a Linux machine: install it as a systemd service (settings in /etc/aprs-incident-rf-bridge/aprs-incident-rf-bridge.conf, see aprs-incident-rf-bridge/INSTALL.md)

# aprs-mapimage
cd aprs-mapimage && cp .env.example .env && docker compose up -d --build
cd aprs-mapimage/app && DATA_DIR=/tmp/mi APRS_HOME=40.12,-75.34 PORT=8790 python3 server.py   # without Docker
python3 aprs-mapimage/tools/build_world.py <natural-earth geojson folder> aprs-mapimage/app/world.json.gz
python3 aprs-mapimage/tools/sync_shared.py           # copy the shared modules into aprs-incident-feeder/
python3 aprs-mapimage/tools/sync_shared.py --check   # exit 1 if the two copies differ
```

Tests: `aprs-mapimage/tests/README.md` (development harnesses with fake APRS-IS, fake map services
and fake aprs.fi; run each from its own folder) and `aprs-incident-feeder/tests/test_priority.py`, `test_send.py`, `test_web.py`, `test_water.py` (unittest). Use a
made-up callsign such as `TEST-13` with a fake server: `--send` refuses `N0CALL`.

## Layout notes

- `aprs_eink.py` (renderer) and `aprs_listen.py` (APRS-IS listener) are shared by both projects. The
  master copies are in `aprs-mapimage/app/`; `aprs-incident-feeder/` holds byte-identical copies. Edit the master,
  run `python3 aprs-mapimage/tools/sync_shared.py`, and commit both copies together. Never edit the
  `aprs-incident-feeder/` copies directly. Anything changed in these two files must keep `aprs-incident-feeder/aprs_web.py` working.
- `aprs-mapimage/app/world.json.gz` is generated (Natural Earth, public domain); rebuild it with
  `tools/build_world.py`, don't edit it.
- Runtime data lives in `aprs-incident-feeder/data/` and the map server's `/data` volume; never commit it.
