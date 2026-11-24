import json, time
now = time.time()
json.dump({"saved": now, "last_seen": {"N3OLD-7": {"name": "N3OLD-7", "kind": "station", "lat": 40.05, "lon": -75.25,
           "heard": now - 5 * 3600, "table": "/", "code": ">", "comment": "parked at the mall"}}}, open("data/last_seen.json", "w"))
