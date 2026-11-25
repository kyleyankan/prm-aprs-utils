# prm-aprs-utils

Amateur-radio (APRS) projects by K3YLE. Each folder is its own project with its own README, and
none of them needs anything outside the Python 3 standard library.

| Project | What it does | Start with |
|---|---|---|
| [`aprs-incident-feeder/`](aprs-incident-feeder/) | Turns county dispatch, 511 traffic, NWS weather and river gauge feeds into APRS objects, sent to APRS-IS as K3YLE-15. Runs on a Raspberry Pi, with a web map and editor and a public read-only map. | `README.md` there; `ARCHITECTURE.md` for how the services fit together |
| [`aprs-incident-rf-bridge/`](aprs-incident-rf-bridge/) | One script for other operators: it takes the feeder's objects near them and transmits them through their own KISS TNC. Its installer sets it up as a systemd service. | `README.md` there; `INSTALL.md` to run it as a service |
| [`aprs-mapimage/`](aprs-mapimage/) | A Docker service that listens to APRS-IS and draws map pictures for e-paper displays and dashboards. | `README.md` there |

The projects are separate but related: `aprs-incident-rf-bridge` reads the feeder's `/api/packets`, and the
feeder and the map service share two modules (`aprs_eink.py` and `aprs_listen.py`; the master copies
are in `aprs-mapimage/app/`, kept in step by `aprs-mapimage/tools/sync_shared.py`).

## See it running

| What | Where |
|---|---|
| The feeder's live public map (filter by source and importance) | <https://aprs-incidents.phlm.sh> |
| Documentation for the public: the sources, the processing, deploying it with your radio | <https://aprs-incidents.phlm.sh/docs> |
| The objects on APRS | [K3YLE-15 on aprs.fi](https://aprs.fi/info/a/K3YLE-15) |
| The club talk and the newsletter article | [`docs/slides/`](aprs-incident-feeder/docs/slides/) and [`docs/article/`](aprs-incident-feeder/docs/article/) in the feeder's folder |

The documentation pages are the Markdown files in
[`aprs-incident-feeder/public_docs/`](aprs-incident-feeder/public_docs/); the feeder's web service
serves them at `/docs`.

`HANDOFF.md` has the goals, architecture, decisions and open tasks across all three; `CLAUDE.md` has
the working rules for Claude Code.
