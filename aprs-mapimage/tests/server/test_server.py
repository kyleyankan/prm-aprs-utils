import os as _os, sys as _sys
APP = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "app")
import sys, time, json, socketserver, threading, urllib.request, urllib.error, subprocess, os, signal, io
from PIL import Image
LINES=["K3XYZ-9>APRS,qAR:!4005.50N/07515.25W>mobile one","W3ABC-1>APRS,qAC:;HAMFEST  *191500z4010.00N/07520.00WEHamfest today",
 "N3AAA>APRS,qAR:!4012.00N/07530.00W-home station","K3XYZ-9>APRS,qAR:!4006.50N/07514.25W>mobile two",
 "K3ABC-7>APRS,qAR::N3DEF-9  :Running late, be there at 6{42"]
logins=[]
class H(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"# aprsc\r\n"); logins.append(self.rfile.readline().decode().strip()); self.wfile.write(b"# logresp N0CALL unverified\r\n")
        for l in LINES: self.wfile.write(l.encode()+b"\r\n")
        self.wfile.flush()
        try:
            while True:
                time.sleep(1); self.wfile.write(b"# keepalive\r\n"); self.wfile.flush()
        except OSError: pass
srv=socketserver.ThreadingTCPServer(("127.0.0.1",0),H); srv.daemon_threads=True; port=srv.server_address[1]
threading.Thread(target=srv.serve_forever,daemon=True).start()
D=os.path.abspath("data")
env=dict(os.environ,DATA_DIR=D,APRS_HOME="40.12,-75.34",LISTEN_MILES="40",APRS_IS_HOST="127.0.0.1",APRS_IS_PORT=str(port),
         APRS_BACKFILL="0",EINK_OSM="0",EINK_COUNTIES="0",EINK_SYMBOLS="0",PORT="8777",TZ="America/New_York")
def start(): return subprocess.Popen([sys.executable,os.path.join(APP, "server.py")],env=env,stderr=subprocess.PIPE,text=True,cwd="/tmp")
B="http://127.0.0.1:8777"
def get(u):
    r=urllib.request.urlopen(B+u); return r.status, r.headers.get("Content-Type"), r.read()
def req(method,u,body=None):
    r=urllib.request.Request(B+u,data=json.dumps(body).encode() if body is not None else None,method=method,headers={"Content-Type":"application/json"})
    try: x=urllib.request.urlopen(r); return x.status, json.loads(x.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())
p=start(); time.sleep(2.5)
try:
    st=json.loads(get("/api/status")[2]); print("status:",{k:st[k] for k in ("connected","stations_heard","objects_heard","areas","callsign")})
    assert st["connected"] and st["stations_heard"]==2 and st["objects_heard"]==1, st
    print("login:", logins[0]); assert "pass -1" in logins[0] and "filter r/40.1200/-75.3400/64" in logins[0] and "aprs-mapimage" in logins[0]
    a=json.loads(get("/api/aprs")[2]); paths={o["name"]:len(o["path"]) for o in a["objects"]}; print("heard:",paths,"| messages:",[m["text"] for m in a["messages"]])
    assert paths=={"HAMFEST":0,"K3XYZ-9":2,"N3AAA":0} and a["messages"][0]["text"].startswith("Running late")
    for u in ("/map.png","/eink.png","/eink.bmp","/map.png?w=272&h=792&miles=10&dark=1","/eink.png?rotate=90&invert=1","/map.png?stations=0","/map.png?w=abc&miles=-5"):
        s,ct,d=get(u); im=Image.open(io.BytesIO(d)); print(f"{u:42s} {s} {ct:10s} {im.size} {im.mode}")
    open("map.png","wb").write(get("/map.png?miles=20")[2])
    t0=time.time(); get("/map.png?miles=33"); t1=time.time(); get("/map.png?miles=33"); t2=time.time(); print(f"render {t1-t0:.3f}s, cached {t2-t1:.3f}s")
    print("html:", get("/")[0], b"__HOME__" not in get("/")[2])
    # presets
    print(req("POST","/api/presets",{"name":"Kitchen","fmt":"mono","params":{}}))
    s,j=req("POST","/api/presets",{"name":"kitchen-eink","fmt":"mono","params":{"w":"272","h":"792","lat":"39.95","lon":"-75.10","miles":"12","evil":"x","roads":"1;rm -rf"}}); print(s,j)
    assert j["url"]=="/p/kitchen-eink.png"
    pre=json.loads(get("/api/presets")[2]); print("saved params:",pre["kitchen-eink"]["params"]); assert "evil" not in pre["kitchen-eink"]["params"] and "roads" not in pre["kitchen-eink"]["params"]
    s,ct,d=get("/p/kitchen-eink.png"); im=Image.open(io.BytesIO(d)); print("preset picture:",s,ct,im.size,im.mode); assert im.size==(272,792) and im.mode=="1"
    s,ct,d=get("/p/kitchen-eink.bmp"); assert ct=="image/bmp"
    s,ct,d=get("/p/kitchen-eink.png?rotate=90"); assert Image.open(io.BytesIO(d)).size==(792,272)
    time.sleep(2.5); print("logins:", len(logins), "| filter now:", logins[-1].split("filter ")[1]); assert len(logins)==2 and len(logins[-1].split("filter ")[1].split())==2
    st=json.loads(get("/api/status")[2]); print("areas now:",st["areas"])
    try: get("/p/nope.png")
    except urllib.error.HTTPError as e: print("missing preset ->",e.code)
    print(req("DELETE","/api/presets/kitchen-eink")); time.sleep(2.5); assert len(logins[-1].split("filter ")[1].split())==1, logins[-1]; print("after delete, filter:", logins[-1].split("filter ")[1])
    req("POST","/api/presets",{"name":"desk","fmt":"colour","params":{"miles":"20"}}); time.sleep(2)
finally:
    p.send_signal(signal.SIGTERM); p.wait(10); err=p.stderr.read(); print([l for l in err.splitlines() if "state saved" in l or "Traceback" in l or "error" in l.lower()])
LINES.clear()
p=start(); time.sleep(2)
try:
    st=json.loads(get("/api/status")[2]); print("after restart:",st["stations_heard"],"stations,",st["objects_heard"],"objects, areas",len(st["areas"]))
    assert st["stations_heard"]==2 and st["objects_heard"]==1 and len(st["areas"])==2
finally:
    p.terminate(); p.wait(10)
print("MAPIMAGE OK")
