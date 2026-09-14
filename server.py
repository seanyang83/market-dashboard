#!/usr/bin/env python3
"""Local static file server + same-origin proxy for market data.

Browsers block direct client-side fetches to these providers (no CORS
headers), but server-to-server requests work fine. This server serves
index.html and proxies quotes on the page's behalf, so the browser only
ever talks to localhost (no CORS issue).

KOSPI uses Naver Finance instead of Yahoo: Yahoo's ^KS11 feed via the
unofficial chart API frequently stops updating for days at a time, while
Naver (a Korean provider) has genuinely live intraday data for domestic
indices.
"""
import http.server
import json
import os
import re
import socketserver
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8934))
YAHOO_HOSTS = ["https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"]
ALLOWED_RANGES = {"1d", "5d", "1mo", "2mo", "3mo", "6mo", "1y"}
ALLOWED_INTERVALS = {"1m", "2m", "5m", "15m", "1d"}

NAVER_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com"}
NAVER_QUOTE_URL = "https://polling.finance.naver.com/api/realtime/domestic/index/KOSPI"
NAVER_HISTORY_URL = "https://finance.naver.com/sise/sise_index_day.naver?code=KOSPI&page={page}"
NAVER_INVESTOR_URL = (
    "https://stock.naver.com/api/domestic/market/trend/daily"
    "?tradeType=KRX&marketType=KOSPI&startIdx=0&pageSize=1"
)
# KRX investor-type codes: 9000 individual, 8000/9001 foreign, everything else
# (securities/insurance/trust/pension/etc.) rolls up into "institution".
INVESTOR_INDIVIDUAL = {"9000"}
INVESTOR_FOREIGN = {"8000", "9001"}
NAVER_HISTORY_ROW_RE = re.compile(
    rb'<td class="date">(\d{4})\.(\d{2})\.(\d{2})</td>\s*<td class="number_1">([\d,]+\.\d+)</td>'
)
KOSPI_HISTORY_MIN_DAYS = 150
KOSPI_HISTORY_MAX_PAGES = 30
KOSPI_HISTORY_CACHE_TTL = 300
_kospi_history_cache = {"data": None, "ts": 0}


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/quote"):
            self.handle_quote()
        elif self.path.startswith("/api/kospi/quote"):
            self.handle_kospi_quote()
        elif self.path.startswith("/api/kospi/history"):
            self.handle_kospi_history()
        elif self.path.startswith("/api/kospi/investors"):
            self.handle_kospi_investors()
        else:
            super().do_GET()

    def handle_quote(self):
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        symbol = params.get("symbol", [""])[0]
        if not symbol:
            self.send_json(400, {"error": "missing symbol"})
            return
        range_ = params.get("range", ["1d"])[0]
        interval = params.get("interval", ["2m"])[0]
        if range_ not in ALLOWED_RANGES or interval not in ALLOWED_INTERVALS:
            self.send_json(400, {"error": "invalid range/interval"})
            return

        encoded_symbol = urllib.parse.quote(symbol, safe="")
        last_err = None
        for host in YAHOO_HOSTS:
            url = (
                f"{host}/v8/finance/chart/{encoded_symbol}"
                f"?range={range_}&interval={interval}&includePrePost=false"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            try:
                with urllib.request.urlopen(req, timeout=8) as resp:
                    body = resp.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return
            except (urllib.error.URLError, TimeoutError) as e:
                last_err = str(e)

        self.send_json(502, {"error": last_err or "upstream unavailable"})

    def handle_kospi_quote(self):
        req = urllib.request.Request(NAVER_QUOTE_URL, headers=NAVER_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
            d = data["datas"][0]
            price = float(d["closePriceRaw"])
            change = float(d["compareToPreviousClosePriceRaw"])
            epoch = None
            traded_at = d.get("localTradedAt")
            if traded_at:
                try:
                    epoch = int(datetime.fromisoformat(traded_at).timestamp())
                except ValueError:
                    epoch = None
            self.send_json(200, {
                "price": price,
                "prevClose": price - change,
                "open": float(d["openPriceRaw"]),
                "high": float(d["highPriceRaw"]),
                "low": float(d["lowPriceRaw"]),
                "time": epoch,
                "marketStatus": d.get("marketStatus"),
            })
        except Exception as e:
            self.send_json(502, {"error": str(e)})

    def handle_kospi_investors(self):
        req = urllib.request.Request(NAVER_INVESTOR_URL, headers=NAVER_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
            row = data["content"][0]
            amounts = {a["investorGubun"]: int(a["diffValue"]) for a in row["netAmounts"]}
            individual = sum(v for k, v in amounts.items() if k in INVESTOR_INDIVIDUAL)
            foreign = sum(v for k, v in amounts.items() if k in INVESTOR_FOREIGN)
            institution = sum(amounts.values()) - individual - foreign
            self.send_json(200, {
                "date": row.get("bizdate"),
                "individual": individual,
                "foreign": foreign,
                "institution": institution,
            })
        except Exception as e:
            self.send_json(502, {"error": str(e)})

    def handle_kospi_history(self):
        now = time.time()
        cached = _kospi_history_cache["data"]
        if cached and now - _kospi_history_cache["ts"] < KOSPI_HISTORY_CACHE_TTL:
            self.send_json(200, {"closes": cached})
            return
        try:
            by_date = {}
            for page in range(1, KOSPI_HISTORY_MAX_PAGES + 1):
                req = urllib.request.Request(NAVER_HISTORY_URL.format(page=page), headers=NAVER_HEADERS)
                with urllib.request.urlopen(req, timeout=8) as resp:
                    body = resp.read()
                matches = NAVER_HISTORY_ROW_RE.findall(body)
                if not matches:
                    break
                for y, m, d, price in matches:
                    date = f"{y.decode()}-{m.decode()}-{d.decode()}"
                    by_date[date] = float(price.decode().replace(",", ""))
                if len(by_date) >= KOSPI_HISTORY_MIN_DAYS:
                    break

            closes = [{"date": d, "close": by_date[d]} for d in sorted(by_date)]
            if len(closes) < 20:
                raise ValueError("insufficient history rows scraped")
            _kospi_history_cache["data"] = closes
            _kospi_history_cache["ts"] = now
            self.send_json(200, {"closes": closes})
        except Exception as e:
            if cached:
                self.send_json(200, {"closes": cached})
            else:
                self.send_json(502, {"error": str(e)})

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with Server((HOST, PORT), Handler) as httpd:
        print(f"Serving dashboard on http://{HOST}:{PORT}")
        httpd.serve_forever()
