# prm-aprs-utils

Amateur-radio (APRS) projects by K3YLE. Each folder is its own project with its own README, and
none of them needs anything outside the Python 3 standard library.

| Project | What it does | Start with |
|---|---|---|
| [`aprs-incident-feeder/`](aprs-incident-feeder/) | Turns county dispatch, 511 traffic, NWS weather and river gauge feeds into APRS objects, sent to APRS-IS as K3YLE-15. Runs on a Raspberry Pi, with a web map and editor and a public read-only map. | `README.md` there; `ARCHITECTURE.md` for how the services fit together |
| [`aprs-radio/`](aprs-radio/) | One script for other operators: it takes the feeder's objects near them and transmits them through their own KISS TNC. | `README.md` there |
| [`aprs-mapimage/`](aprs-mapimage/) | A Docker service that listens to APRS-IS and draws map pictures for e-paper displays and dashboards. | `README.md` there |

The projects are separate but related: `aprs-radio` reads the feeder's `/api/packets`, and the
feeder and the map service share two modules (`aprs_eink.py` and `aprs_listen.py`; the master copies
are in `aprs-mapimage/app/`, kept in step by `aprs-mapimage/tools/sync_shared.py`).

`HANDOFF.md` has the goals, architecture, decisions and open tasks across all three; `CLAUDE.md` has
the working rules for Claude Code.
