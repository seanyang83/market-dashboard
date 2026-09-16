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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8934))
YAHOO_HOSTS = ["https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"]
ALLOWED_RANGES = {"1d", "5d", "1mo", "2mo", "3mo", "6mo", "1y"}
ALLOWED_INTERVALS = {"1m", "2m", "5m", "15m", "1d"}

# --- Korea Investment & Securities (KIS) Open API: live per-stock signals ---
# Naver has no live per-stock investor-type flow (only settled prior-day data),
# but KIS's own "추정가집계"/program-trade endpoints update intraday.
KIS_APP_KEY = os.environ.get("KIS_APP_KEY", "")
KIS_APP_SECRET = os.environ.get("KIS_APP_SECRET", "")
KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"
# KIS rate-limits how often a new access token may be issued (frequent
# re-issuance can trigger a usage restriction), and each token is valid for
# ~24h. Cache it on disk too, so a process restart (local dev reload, Render
# redeploy/spin-down) reuses the still-valid token instead of requesting a
# fresh one every time.
KIS_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".kis_token_cache.json")
_kis_token_cache = {"token": None, "expires_at": 0}
_kis_token_lock = threading.Lock()

KST_OFFSET = timedelta(hours=9)


def kst_today():
    return (datetime.now(timezone.utc) + KST_OFFSET).strftime("%Y%m%d")


# Per-code, per-day cache of program-trade snapshots, bucketed to 30 minutes:
# {code: {"date": "20260916", "points": {"093000": {"time": "093012", ...}}}}
# KIS's program-trade endpoint only ever returns a short recent rolling
# window (~3-4 minutes), never the full trading day, so the only way to
# reconstruct a whole-day trend is to keep polling while the process is
# alive and accumulate what each call returns. Only a rough increasing/
# decreasing trend is needed (not tick-level detail), so only the latest
# value seen within each 30-minute bucket is kept - one point per bucket,
# ~13 for a full trading day, rather than thousands of raw ticks.
# Persisted to disk so a process restart (Render redeploy, or waking from
# an inactivity sleep) doesn't throw away the day's progress so far.
BUCKET_MINUTES = 30
PROGRAM_TRADE_HISTORY_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".program_trade_history.json"
)
_program_trade_history = {}
_program_trade_lock = threading.Lock()


def _load_program_trade_history():
    try:
        with open(PROGRAM_TRADE_HISTORY_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_program_trade_history():
    try:
        with open(PROGRAM_TRADE_HISTORY_FILE, "w") as f:
            json.dump(_program_trade_history, f)
    except OSError:
        pass


_program_trade_history = _load_program_trade_history()

# Whatever stock code a client most recently asked about, so the background
# poller (below) keeps building that stock's history even after the browser
# tab closes, as long as the process itself stays alive.
_last_requested_code = {"code": None}


def _bucket_key(bsop_hour):
    hh, mm = bsop_hour[0:2], int(bsop_hour[2:4])
    bucket_mm = "00" if mm < BUCKET_MINUTES else "30"
    return f"{hh}{bucket_mm}00"


def merge_program_trade_rows(code, rows):
    """Merge freshly-fetched ticks into the per-code/day history cache,
    keeping only the latest value seen within each 30-minute bucket."""
    today = kst_today()
    with _program_trade_lock:
        entry = _program_trade_history.setdefault(code, {"date": today, "points": {}})
        if entry["date"] != today:
            entry["date"] = today
            entry["points"] = {}
        changed = False
        for r in rows:
            t = r.get("bsop_hour")
            if not t:
                continue
            bucket = _bucket_key(t)
            existing = entry["points"].get(bucket)
            if existing is not None and existing.get("time", "") >= t:
                continue
            entry["points"][bucket] = {
                "time": t,
                "netQty": int(r.get("whol_smtn_ntby_qty", 0)),
                "netAmount": int(r.get("whol_smtn_ntby_tr_pbmn", 0)),
            }
            changed = True
        history = [entry["points"][b] for b in sorted(entry["points"])]
        if changed:
            _save_program_trade_history()
    return history


def _program_trade_background_loop():
    # Keeps accumulating history for the last-viewed stock even when no
    # browser is connected, so the day's chart stays complete as long as
    # this process is awake (see the GitHub Actions keep-alive workflow,
    # which pings the site during market hours so Render doesn't sleep it).
    # Only one snapshot per 30-minute bucket is kept, so polling every few
    # minutes (well under the bucket width) is more than enough - no need
    # to poll every few seconds for a rough increasing/decreasing trend.
    while True:
        time.sleep(300)
        code = _last_requested_code["code"]
        if not code or not KIS_APP_KEY or not KIS_APP_SECRET:
            continue
        try:
            data = kis_get(
                "/uapi/domestic-stock/v1/quotations/program-trade-by-stock",
                "FHPPG04650101",
                {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
            )
            rows = data.get("output") or []
            if rows:
                merge_program_trade_rows(code, rows)
        except Exception:
            pass


def _kis_load_cached_token():
    try:
        with open(KIS_TOKEN_FILE) as f:
            data = json.load(f)
        if data.get("token") and time.time() < data.get("expires_at", 0) - 300:
            return data
    except (OSError, ValueError):
        pass
    return None


def _kis_save_cached_token(token, expires_at):
    try:
        with open(KIS_TOKEN_FILE, "w") as f:
            json.dump({"token": token, "expires_at": expires_at}, f)
    except OSError:
        pass


def kis_get_token():
    now = time.time()
    with _kis_token_lock:
        if _kis_token_cache["token"] and now < _kis_token_cache["expires_at"] - 300:
            return _kis_token_cache["token"]

        cached = _kis_load_cached_token()
        if cached:
            _kis_token_cache["token"] = cached["token"]
            _kis_token_cache["expires_at"] = cached["expires_at"]
            return cached["token"]

        body = json.dumps({
            "grant_type": "client_credentials",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
        }).encode()
        req = urllib.request.Request(
            f"{KIS_BASE_URL}/oauth2/tokenP",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        token = data["access_token"]
        expires_at = now + int(data.get("expires_in", 86400))
        _kis_token_cache["token"] = token
        _kis_token_cache["expires_at"] = expires_at
        _kis_save_cached_token(token, expires_at)
        return token


def kis_get(path, tr_id, params):
    token = kis_get_token()
    url = f"{KIS_BASE_URL}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

NAVER_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com"}
NAVER_QUOTE_URL = "https://polling.finance.naver.com/api/realtime/domestic/index/KOSPI"
NAVER_HISTORY_URL = "https://finance.naver.com/sise/sise_index_day.naver?code=KOSPI&page={page}"
NAVER_INVESTOR_URL = (
    "https://stock.naver.com/api/domestic/market/trend/daily"
    "?tradeType=KRX&marketType=KOSPI&startIdx=0&pageSize=1"
)
# KRX investor-type codes. Verified against Naver's own displayed totals
# (stock.naver.com's investor widget): 8000 individual, 9000/9001 foreign,
# 1000/2000/3000/3100/4000/5000/6000 are the institution sub-types.
# 7000 is an always-zero placeholder and 7100 (기타법인, other corporations)
# is its own bucket that Naver's simplified 3-way view excludes entirely, so
# neither is counted here.
INVESTOR_INDIVIDUAL = {"8000"}
INVESTOR_FOREIGN = {"9000", "9001"}
INVESTOR_INSTITUTION = {"1000", "2000", "3000", "3100", "4000", "5000", "6000"}
NAVER_HISTORY_ROW_RE = re.compile(
    rb'<td class="date">(\d{4})\.(\d{2})\.(\d{2})</td>\s*<td class="number_1">([\d,]+\.\d+)</td>'
)
KOSPI_HISTORY_MIN_DAYS = 150
KOSPI_HISTORY_MAX_PAGES = 30
KOSPI_HISTORY_CACHE_TTL = 300
_kospi_history_cache = {"data": None, "ts": 0}

# --- Single-stock checklist feature ---
NAVER_STOCK_SEARCH_URL = "https://ac.stock.naver.com/ac?q={query}&target=stock"
NAVER_STOCK_QUOTE_URL = "https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
NAVER_STOCK_HISTORY_URL = "https://finance.naver.com/item/sise_day.naver?code={code}&page={page}"
NAVER_STOCK_INVESTOR_URL = (
    "https://stock.naver.com/api/domestic/detail/{code}/trend?tradeType=KRX&startIdx=0&pageSize=1"
)
STOCK_CODE_RE = re.compile(r"^[0-9A-Z]{6}$")
STOCK_HISTORY_ROW_RE = re.compile(
    rb'<td align="center"><span class="tah p10 gray03">(\d{4})\.(\d{2})\.(\d{2})</span></td>\s*'
    rb'<td class="num"><span class="tah p11">([\d,]+)</span></td>'
)
STOCK_HISTORY_MIN_DAYS = 150
STOCK_HISTORY_MAX_PAGES = 20
STOCK_HISTORY_CACHE_TTL = 300
_stock_history_cache = {}  # code -> {"data": [...], "ts": epoch}

# Naver has no clean public "theme" API, so this is a small curated map of
# well-known large-caps to a representative domestic ETF (today's sector
# move) and US ETF (yesterday's close, via the existing Yahoo proxy) per
# theme. Anything not listed here just skips the theme/sector checklist
# items rather than guessing.
STOCK_THEMES = {
    "000660": {"name": "SK하이닉스", "theme": "반도체",
               "krProxy": {"code": "091160", "name": "KODEX 반도체"},
               "usProxy": {"symbol": "SOXX", "name": "iShares Semiconductor ETF"}},
    "005930": {"name": "삼성전자", "theme": "반도체",
               "krProxy": {"code": "091160", "name": "KODEX 반도체"},
               "usProxy": {"symbol": "SOXX", "name": "iShares Semiconductor ETF"}},
    "373220": {"name": "LG에너지솔루션", "theme": "2차전지",
               "krProxy": {"code": "305540", "name": "TIGER 2차전지테마"},
               "usProxy": {"symbol": "LIT", "name": "Global X Lithium & Battery Tech ETF"}},
    "006400": {"name": "삼성SDI", "theme": "2차전지",
               "krProxy": {"code": "305540", "name": "TIGER 2차전지테마"},
               "usProxy": {"symbol": "LIT", "name": "Global X Lithium & Battery Tech ETF"}},
    "247540": {"name": "에코프로비엠", "theme": "2차전지",
               "krProxy": {"code": "305540", "name": "TIGER 2차전지테마"},
               "usProxy": {"symbol": "LIT", "name": "Global X Lithium & Battery Tech ETF"}},
    "086520": {"name": "에코프로", "theme": "2차전지",
               "krProxy": {"code": "305540", "name": "TIGER 2차전지테마"},
               "usProxy": {"symbol": "LIT", "name": "Global X Lithium & Battery Tech ETF"}},
    "005380": {"name": "현대차", "theme": "자동차",
               "krProxy": {"code": "091180", "name": "KODEX 자동차"},
               "usProxy": {"symbol": "CARZ", "name": "First Trust Future Vehicles & Tech ETF"}},
    "000270": {"name": "기아", "theme": "자동차",
               "krProxy": {"code": "091180", "name": "KODEX 자동차"},
               "usProxy": {"symbol": "CARZ", "name": "First Trust Future Vehicles & Tech ETF"}},
    "035420": {"name": "NAVER", "theme": "인터넷/플랫폼",
               "krProxy": {"code": "315270", "name": "TIGER 200커뮤니케이션서비스"},
               "usProxy": {"symbol": "XLK", "name": "Technology Select Sector SPDR"}},
    "035720": {"name": "카카오", "theme": "인터넷/플랫폼",
               "krProxy": {"code": "315270", "name": "TIGER 200커뮤니케이션서비스"},
               "usProxy": {"symbol": "XLK", "name": "Technology Select Sector SPDR"}},
    "068270": {"name": "셀트리온", "theme": "바이오",
               "krProxy": {"code": "266420", "name": "KODEX 헬스케어"},
               "usProxy": {"symbol": "XBI", "name": "SPDR S&P Biotech ETF"}},
    "207940": {"name": "삼성바이오로직스", "theme": "바이오",
               "krProxy": {"code": "266420", "name": "KODEX 헬스케어"},
               "usProxy": {"symbol": "XBI", "name": "SPDR S&P Biotech ETF"}},
    "105560": {"name": "KB금융", "theme": "금융",
               "krProxy": {"code": "091170", "name": "KODEX 은행"},
               "usProxy": {"symbol": "XLF", "name": "Financial Select Sector SPDR"}},
    "055550": {"name": "신한지주", "theme": "금융",
               "krProxy": {"code": "091170", "name": "KODEX 은행"},
               "usProxy": {"symbol": "XLF", "name": "Financial Select Sector SPDR"}},
    "012450": {"name": "한화에어로스페이스", "theme": "방산",
               "krProxy": {"code": "449450", "name": "PLUS K방산"},
               "usProxy": {"symbol": "ITA", "name": "iShares U.S. Aerospace & Defense ETF"}},
    "079550": {"name": "LIG넥스원", "theme": "방산",
               "krProxy": {"code": "449450", "name": "PLUS K방산"},
               "usProxy": {"symbol": "ITA", "name": "iShares U.S. Aerospace & Defense ETF"}},
    "042660": {"name": "한화오션", "theme": "조선",
               "krProxy": {"code": "0115D0", "name": "KODEX 조선TOP10"}, "usProxy": None},
    "329180": {"name": "HD현대중공업", "theme": "조선",
               "krProxy": {"code": "0115D0", "name": "KODEX 조선TOP10"}, "usProxy": None},
    "090430": {"name": "아모레퍼시픽", "theme": "화장품",
               "krProxy": {"code": "228790", "name": "TIGER 화장품"}, "usProxy": None},
    "009150": {"name": "삼성전기", "theme": "IT부품 (MLCC·반도체 기판)",
               "krProxy": {"code": "266370", "name": "KODEX IT"},
               "usProxy": {"symbol": "SOXX", "name": "iShares Semiconductor ETF"}},
    "000810": {"name": "삼성화재", "theme": "보험",
               "krProxy": {"code": "140700", "name": "KODEX 보험"},
               "usProxy": {"symbol": "KIE", "name": "SPDR S&P Insurance ETF"}},
    "032830": {"name": "삼성생명", "theme": "보험",
               "krProxy": {"code": "140700", "name": "KODEX 보험"},
               "usProxy": {"symbol": "KIE", "name": "SPDR S&P Insurance ETF"}},
}


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
        elif self.path.startswith("/api/stock/search"):
            self.handle_stock_search()
        elif self.path.startswith("/api/stock/quote"):
            self.handle_stock_quote()
        elif self.path.startswith("/api/stock/history"):
            self.handle_stock_history()
        elif self.path.startswith("/api/stock/investors"):
            self.handle_stock_investors()
        elif self.path.startswith("/api/stock/theme"):
            self.handle_stock_theme()
        elif self.path.startswith("/api/stock/investor-live"):
            self.handle_stock_investor_live()
        elif self.path.startswith("/api/stock/program-trade"):
            self.handle_stock_program_trade()
        else:
            super().do_GET()

    def query_param(self, name):
        query = urllib.parse.urlparse(self.path).query
        return urllib.parse.parse_qs(query).get(name, [""])[0]

    def require_stock_code(self):
        code = self.query_param("code").strip().upper()
        if not STOCK_CODE_RE.match(code):
            self.send_json(400, {"error": "invalid or missing code"})
            return None
        return code

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
            institution = sum(v for k, v in amounts.items() if k in INVESTOR_INSTITUTION)
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

    def handle_stock_search(self):
        q = self.query_param("q").strip()
        if not q:
            self.send_json(400, {"error": "missing q"})
            return
        url = NAVER_STOCK_SEARCH_URL.format(query=urllib.parse.quote(q))
        req = urllib.request.Request(url, headers=NAVER_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
            items = [
                {"code": it["code"], "name": it["name"], "market": it.get("typeName")}
                for it in data.get("items", [])
                if it.get("category") == "stock"
            ]
            self.send_json(200, {"items": items[:10]})
        except Exception as e:
            self.send_json(502, {"error": str(e)})

    def handle_stock_quote(self):
        code = self.require_stock_code()
        if not code:
            return
        req = urllib.request.Request(NAVER_STOCK_QUOTE_URL.format(code=code), headers=NAVER_HEADERS)
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
                "name": d.get("stockName"),
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

    def handle_stock_history(self):
        code = self.require_stock_code()
        if not code:
            return
        now = time.time()
        cache_entry = _stock_history_cache.get(code)
        if cache_entry and now - cache_entry["ts"] < STOCK_HISTORY_CACHE_TTL:
            self.send_json(200, {"closes": cache_entry["data"]})
            return
        try:
            by_date = {}
            for page in range(1, STOCK_HISTORY_MAX_PAGES + 1):
                url = NAVER_STOCK_HISTORY_URL.format(code=code, page=page)
                req = urllib.request.Request(url, headers=NAVER_HEADERS)
                with urllib.request.urlopen(req, timeout=8) as resp:
                    body = resp.read()
                matches = STOCK_HISTORY_ROW_RE.findall(body)
                if not matches:
                    break
                for y, m, d, price in matches:
                    date = f"{y.decode()}-{m.decode()}-{d.decode()}"
                    by_date[date] = float(price.decode().replace(",", ""))
                if len(by_date) >= STOCK_HISTORY_MIN_DAYS:
                    break

            closes = [{"date": d, "close": by_date[d]} for d in sorted(by_date)]
            if len(closes) < 20:
                raise ValueError("insufficient history rows scraped")
            _stock_history_cache[code] = {"data": closes, "ts": now}
            self.send_json(200, {"closes": closes})
        except Exception as e:
            if cache_entry:
                self.send_json(200, {"closes": cache_entry["data"]})
            else:
                self.send_json(502, {"error": str(e)})

    def handle_stock_investors(self):
        code = self.require_stock_code()
        if not code:
            return
        req = urllib.request.Request(NAVER_STOCK_INVESTOR_URL.format(code=code), headers=NAVER_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
            if not data:
                raise ValueError("no data for code")
            row = data[0]
            close_price = int(row["closePrice"])
            individual_qty = int(row["individualPureBuyQuant"])
            foreign_qty = int(row["foreignerPureBuyQuant"])
            institution_qty = int(row["organPureBuyQuant"])
            # Naver only gives net share quantity per investor type, not won
            # value, so approximate the money amount using that day's close
            # price (same convention the market-wide /kospi/investors uses).
            self.send_json(200, {
                "date": row.get("bizdate"),
                "individual": individual_qty * close_price,
                "foreign": foreign_qty * close_price,
                "institution": institution_qty * close_price,
                "individualQty": individual_qty,
                "foreignQty": foreign_qty,
                "institutionQty": institution_qty,
            })
        except Exception as e:
            self.send_json(502, {"error": str(e)})

    def handle_stock_theme(self):
        code = self.require_stock_code()
        if not code:
            return
        info = STOCK_THEMES.get(code)
        self.send_json(200, {"found": info is not None, "info": info})

    def handle_stock_investor_live(self):
        code = self.require_stock_code()
        if not code:
            return
        if not KIS_APP_KEY or not KIS_APP_SECRET:
            self.send_json(503, {"error": "KIS API 키가 설정되지 않았습니다"})
            return
        try:
            data = kis_get(
                "/uapi/domestic-stock/v1/quotations/investor-trend-estimate",
                "HHPTJ04160200",
                {"MKSC_SHRN_ISCD": code},
            )
            rows = data.get("output2") or []
            if not rows:
                raise ValueError("데이터 없음 (장 시작 전이거나 첫 집계 전일 수 있음)")
            latest = max(rows, key=lambda r: int(r.get("bsop_hour_gb", 0) or 0))
            foreign_qty = int(latest.get("frgn_fake_ntby_qty", 0))
            institution_qty = int(latest.get("orgn_fake_ntby_qty", 0))
            sum_qty = int(latest.get("sum_fake_ntby_qty", 0))
            self.send_json(200, {
                "checkpoint": latest.get("bsop_hour_gb"),
                "foreignQty": foreign_qty,
                "institutionQty": institution_qty,
                "sumQty": sum_qty,
                # KIS/KRX only estimate 외국인+기관 in real time; 개인 is not
                # separately tracked intraday. Since net buy quantity across
                # all investor types sums to ~0 for a given stock, 개인 is
                # approximated as the mirror image of the foreign+institution
                # total (ignores 기타법인, which is usually small).
                "individualQtyEstimated": -sum_qty,
            })
        except Exception as e:
            self.send_json(502, {"error": str(e)})

    def handle_stock_program_trade(self):
        code = self.require_stock_code()
        if not code:
            return
        if not KIS_APP_KEY or not KIS_APP_SECRET:
            self.send_json(503, {"error": "KIS API 키가 설정되지 않았습니다"})
            return
        _last_requested_code["code"] = code
        try:
            data = kis_get(
                "/uapi/domestic-stock/v1/quotations/program-trade-by-stock",
                "FHPPG04650101",
                {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
            )
            rows = data.get("output") or []
            if not rows:
                raise ValueError("데이터 없음")
            latest = rows[0]
            history = merge_program_trade_rows(code, rows)
            self.send_json(200, {
                "time": latest.get("bsop_hour"),
                "netQty": int(latest.get("whol_smtn_ntby_qty", 0)),
                "netAmount": int(latest.get("whol_smtn_ntby_tr_pbmn", 0)),
                "history": history,
            })
        except Exception as e:
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
    if KIS_APP_KEY and KIS_APP_SECRET:
        threading.Thread(target=_program_trade_background_loop, daemon=True).start()
    with Server((HOST, PORT), Handler) as httpd:
        print(f"Serving dashboard on http://{HOST}:{PORT}")
        httpd.serve_forever()
