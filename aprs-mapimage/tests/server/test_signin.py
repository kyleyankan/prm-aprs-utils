import os as _os, sys as _sys
APP = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "app")
import subprocess, sys, os, time, urllib.request, urllib.error, base64, json
def run(extra):
    d=f"/tmp/auth{abs(hash(str(extra)))}"; subprocess.run(["rm","-rf",d])
    env=dict(os.environ,DATA_DIR=d,APRS_HOME="40.12,-75.34",APRS_IS_HOST="127.0.0.1",APRS_IS_PORT="1",APRS_BACKFILL="0",EINK_OSM="0",EINK_COUNTIES="0",EINK_SYMBOLS="0",PORT="8779",**extra)
    return subprocess.Popen([sys.executable,os.path.join(APP, "server.py")],env=env,stderr=subprocess.PIPE,text=True)
def code(path,auth=None,method="GET",body=None):
    h={"Content-Type":"application/json"}
    if auth: h["Authorization"]="Basic "+base64.b64encode(auth.encode()).decode()
    r=urllib.request.Request("http://127.0.0.1:8779"+path,headers=h,method=method,data=json.dumps(body).encode() if body else None)
    try:
        x=urllib.request.urlopen(r); return x.status
    except urllib.error.HTTPError as e: return (e.code, e.headers.get("WWW-Authenticate"))[0]
cases=[("open",{}),("password",{"ADMIN_PASSWORD":"s3cret pass:ü"}),("password+key",{"ADMIN_USER":"kyle","ADMIN_PASSWORD":"pw","IMAGE_KEY":"abc123"})]
for name,extra in cases:
    p=run(extra); time.sleep(1.5)
    good="admin:s3cret pass:ü" if name=="password" else "kyle:pw"
    try:
        r={ "page":code("/"), "page signed":code("/",good), "page wrong":code("/","admin:nope"), "api":code("/api/presets"),
            "post":code("/api/presets",method="POST",body={"name":"x","params":{}}),
            "post signed":code("/api/presets",good,"POST",{"name":"x","params":{}}),
            "delete":code("/api/presets/x",method="DELETE"),
            "map":code("/map.png"),"map key":code("/map.png?key=abc123"),"map badkey":code("/map.png?key=zzz"),"map signed":code("/map.png",good),
            "preset key":code("/p/x.png?key=abc123"),"healthz":code("/healthz")}
        print(f"{name:13s}",r)
        print("   ", [l for l in p.stderr.readline() and [p.stderr.readline().strip()]][0])
    finally:
        p.terminate(); p.wait()
