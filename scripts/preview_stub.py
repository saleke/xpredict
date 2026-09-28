"""Serve the real web/ app against the generated forecast board.

The production server must not serve out-of-window fixtures (the 48h horizon
is a hard product cap and the bundled fixtures are dated outside it), so for
VISUAL VALIDATION ONLY this stub mounts the real index.html/app.js/css and
answers the three /api endpoints with pre-generated JSON built at a
fixture-compatible clock. Nothing here touches production code paths.
"""
import http.server
import json
import os
import socketserver

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
PORT = 8092

data = {k: json.load(open(f"/tmp/opencode/{k}.json")) for k in
        ("forecast", "tiers", "dashboard")}


class StubHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def do_GET(self):
        if self.path.startswith("/api/forecast"):
            body = json.dumps(data["forecast"]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/tiers"):
            body = json.dumps(data["tiers"]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/dashboard"):
            body = json.dumps(data["dashboard"]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/backtest") or self.path.startswith("/api/"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return super().do_GET()

    def log_message(self, *args):
        pass


with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), StubHandler) as httpd:
    print(f"stub app at http://127.0.0.1:{PORT}")
    httpd.serve_forever()