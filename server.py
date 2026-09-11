#!/usr/bin/env python3
"""Local static file server + same-origin proxy for Yahoo Finance quotes.

Browsers block direct client-side fetches to Yahoo Finance (no CORS headers
on their API), but server-to-server requests work fine. This server serves
index.html and proxies /api/quote?symbol=... to Yahoo on the page's behalf,
so the browser only ever talks to localhost (no CORS issue).
"""
import http.server
import json
import os
import socketserver
import urllib.error
import urllib.parse
import urllib.request

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8934))
YAHOO_HOSTS = ["https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"]


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/quote"):
            self.handle_quote()
        else:
            super().do_GET()

    def handle_quote(self):
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        symbol = params.get("symbol", [""])[0]
        if not symbol:
            self.send_json(400, {"error": "missing symbol"})
            return

        encoded_symbol = urllib.parse.quote(symbol, safe="")
        last_err = None
        for host in YAHOO_HOSTS:
            url = (
                f"{host}/v8/finance/chart/{encoded_symbol}"
                "?range=1d&interval=2m&includePrePost=false"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            try:
                with urllib.request.urlopen(req, timeout=8) as resp:
                    body = resp.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            except (urllib.error.URLError, TimeoutError) as e:
                last_err = str(e)

        self.send_json(502, {"error": last_err or "upstream unavailable"})

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with Server((HOST, PORT), Handler) as httpd:
        print(f"Serving dashboard on http://{HOST}:{PORT}")
        httpd.serve_forever()
