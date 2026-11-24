import os as _os, sys as _sys
APP = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "app")
import sys, os, json, time, threading, socketserver, urllib.request, urllib.error, io, collections
sys.path.insert(0, APP)
port_aprs = int(sys.argv[1]); phase = sys.argv[2]
os.environ.update(DATA_DIR=os.path.abspath("data"), APRS_HOME="40.12,-75.34", LISTEN_MILES="20", APRS_IS_HOST="127.0.0.1",
                  APRS_IS_PORT=str(port_aprs), APRS_BACKFILL="0", EINK_SYMBOLS="0", EINK_OSM="0", EINK_COUNTIES="0",
                  PORT="8790", APRSFI_KEY="testkey", PUBLIC_URL="https://maps.example.org/", TZ="America/New_York")
import server
real_urlopen = urllib.request.urlopen
CALLS = collections.Counter(); UA = []
class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): pass
def fake_urlopen(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else req
    if "api.aprs.fi" in url:
        q = dict(urllib.parse.parse_qsl(url.split("?", 1)[1]))
        CALLS[q["name"]] += 1; UA.append(req.get_header("User-agent"))
        assert q["apikey"] == "testkey" and q["what"] == "loc"
        entries = [{"name": "W1FAR-9", "type": "l", "lat": "41.7000", "lng": "-72.6000", "time": str(int(time.time() - 7200)),
                    "lasttime": str(int(time.time() - 3 * 3600)), "symbol": "/>", "comment": "heading home", "srccall": "W1FAR-9"}] \
                  if q["name"] == "W1FAR-9" else []
        return Resp(json.dumps({"command": "get", "result": "ok", "found": len(entries), "what": "loc", "entries": entries}).encode())
    return real_urlopen(req, timeout=timeout)
server.urllib.request.urlopen = fake_urlopen
B = "http://127.0.0.1:8790"
def js(u): return json.loads(real_urlopen(B + u, timeout=30).read())
def get(u): return real_urlopen(B + u, timeout=30).read()
def test():
    try:
        time.sleep(2)
        if phase == "first":
            for name in ("K3XYZ-9", "N3OLD-7", "W1FAR-9", "W1AW", "TRAF0M1ZB"):
                j = js("/api/follow?call=" + name.lower())
                print(f"{name:10s} found={j['found']} source={j['source']} age={j['age']}")
            assert js("/api/follow?call=K3XYZ-9")["source"] == "live"
            assert js("/api/follow?call=N3OLD-7")["source"] == "history"
            assert js("/api/follow?call=W1FAR-9")["source"] == "aprs.fi"
            print("aprs.fi asked:", dict(CALLS), "| user agent:", UA[0])
            assert CALLS["K3XYZ-9"] == 0 and CALLS["W1FAR-9"] == 1 and CALLS["W1AW"] == 1, "cached and only when needed"
            open("old-history.png", "wb").write(get("/map.png?follow=N3OLD-7&miles=6&w=800&h=480"))
            open("old-aprsfi.png", "wb").write(get("/map.png?follow=W1FAR-9&miles=6&w=800&h=480"))
            open("old-eink.png", "wb").write(get("/eink.png?follow=N3OLD-7&miles=6&w=792&h=272&names=1"))
            time.sleep(2.5)
            print("filter:", open("logins.txt").read().strip().splitlines()[-1].split("filter ")[1])
            try: get("/api/follow?call=bad%20name!")
            except urllib.error.HTTPError as e: print("bad name ->", e.code)
        else:
            j = js("/api/follow?call=K3XYZ-9"); print("after restart, K3XYZ-9:", j["source"], j["age"])
            assert j["found"] and j["source"] in ("live", "history")
            assert js("/api/follow?call=N3OLD-7")["source"] == "history"
        print("PHASE OK", phase)
    except BaseException:
        import traceback; traceback.print_exc()
    finally:
        server.save_state(); server.save_history(); os._exit(0)
threading.Thread(target=test, daemon=True).start()
server.main()
