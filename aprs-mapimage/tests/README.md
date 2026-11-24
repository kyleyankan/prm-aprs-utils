# Development test harnesses

These are the scripts used while building the map server. They run the real code against fakes:
a fake APRS-IS server on localhost, fake OpenStreetMap/NWS responses, and a fake aprs.fi. None of
them sends anything to the real APRS-IS or calls outside services, except `world/views.py`, which
only reads `app/world.json.gz`. They are harnesses, not a test suite yet (see HANDOFF.md, task 4).

Run each from its own folder; they write pictures and `data/` folders beside themselves.

| Folder | Run | Checks |
|---|---|---|
| `server/` | `python3 test_server.py` | listener, pictures, presets, listening areas, state across restart (ports 8777, fake APRS-IS) |
| `server/` | `python3 test_signin.py` | sign-in and picture key rules (port 8779) |
| `follow/` | `python3 feed.py 9611 first &` then `python3 run.py 9611 first`, and again with `second` | tile prefetch, following a moving station, restart from disk |
| `history/` | `python3 seed.py`, `python3 feed.py 9640 first &`, `python3 run.py 9640 first` (then `second`) | live / history / aprs.fi lookups, caching, old-position pictures |
| `water/` | `python3 coast.py`, `python3 marsh.py`, `mkdir tdata && python3 tiles_water.py` | sea, bay, lake and river fill; water layer download and upgrade |
| `world/` | `mkdir data && python3 views.py` | Natural Earth layer at several scales and places |
| `ui/` | start a server, then `node builder_ui.js` / `node follow_ui.js` (Playwright; ports in the files) | builder page in headless Chromium with a stand-in for Leaflet |
