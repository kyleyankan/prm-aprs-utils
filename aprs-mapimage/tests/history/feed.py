import socketserver, time, sys
LINES = ["K3XYZ-9>APRS,qAR:!4005.50N/07515.25W>mobile"] if sys.argv[2] == "first" else []
class H(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"# fake\r\n"); open("logins.txt", "a").write(self.rfile.readline().decode().strip() + "\n")
        for x in LINES: self.wfile.write(x.encode() + b"\r\n")
        self.wfile.flush()
        try:
            while True: time.sleep(1); self.wfile.write(b"#\r\n"); self.wfile.flush()
        except OSError: pass
s = socketserver.ThreadingTCPServer(("127.0.0.1", int(sys.argv[1])), H); s.daemon_threads = True; s.serve_forever()
