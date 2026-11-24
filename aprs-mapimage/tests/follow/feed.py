import socketserver, time, sys, threading
phase = sys.argv[2]
LINES = ["K3XYZ-9>APRS,qAR:!4030.00N/07430.00W>moving", "N3AAA>APRS,qAR:!4007.00N/07520.00W-home"] if phase == "first" else []
LATER = ["K3XYZ-9>APRS,qAR:!4036.00N/07430.00W>moved north"] if phase == "first" else []
class H(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"# fake\r\n"); l = self.rfile.readline().decode().strip()
        open("logins.txt", "a").write(l + "\n")
        self.wfile.write(b"# logresp\r\n")
        for x in LINES: self.wfile.write(x.encode() + b"\r\n")
        self.wfile.flush()
        try:
            n = 0
            while True:
                time.sleep(1); n += 1
                if n == 6:
                    for x in LATER: self.wfile.write(x.encode() + b"\r\n")
                self.wfile.write(b"#\r\n"); self.wfile.flush()
        except OSError: pass
s = socketserver.ThreadingTCPServer(("127.0.0.1", int(sys.argv[1])), H); s.daemon_threads = True; s.serve_forever()
