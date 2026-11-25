# aprs-incident-feeder

Scripts that turn public dispatch, traffic, weather and river gauge feeds into APRS objects, sent to
APRS-IS under one callsign by a single sender. It runs on a Raspberry Pi with stock Raspberry Pi OS
and needs only the Python 3 standard library.

- [`README.txt`](README.txt): installing it on a Pi, every setting, and what each service does.
- [`ARCHITECTURE.md`](ARCHITECTURE.md): how the services fit together, with diagrams, and where
  each feed's data comes from.
- [`docs/article/`](docs/article/): a newsletter article that explains the feeder to other operators.
- [`docs/slides/`](docs/slides/): the club slide deck (open `index.html`).

| Service | Script | Source |
|---|---|---|
| `send` | `aprs_send.py` | the one program that logs in to APRS-IS; every other script hands its packets to it |
| `montco`, `chesco`, `bucks` | `aprs_montco.py`, `aprs_chesco.py`, `aprs_bucks.py` | county fire and traffic dispatch |
| `pa511`, `nj511` | `aprs_pa511.py`, `aprs_nj511.py` | PennDOT and 511NJ crashes, closures and roadwork |
| `nws` | `aprs_nws.py` | National Weather Service warnings, watches and tracked storms |
| `water` | `aprs_water.py` | NWS river and tide gauges that are flooding or at low water |
| `web` | `aprs_web.py` | the map and editor, the read-only map at `/map`, and `/api/packets` for radios |

```sh
./aprs_ctl.sh init      # create aprs.env from the defaults
./aprs_ctl.sh start     # start the supervisor and every enabled service
./aprs_ctl.sh status
cd tests && python3 -m unittest test_priority test_send test_web test_water
```

Without `SEND=1` in `aprs.env` nothing is transmitted: the packets are only printed.

The script that puts these objects on the air through a radio is a separate project,
[`../aprs-incident-rf-bridge/`](../aprs-incident-rf-bridge/).
