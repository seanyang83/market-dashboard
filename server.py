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
import ast
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

# Render wipes the local disk on every new deploy, so KIS_TOKEN_FILE alone
# only survives a sleep/wake cycle, not a redeploy - and each redeploy that
# misses it re-issues a token, which is what was triggering KIS's SMS
# notices during active development (many deploys in one day). Render env
# vars, in contrast, are stored on the platform and are already present in
# os.environ on every fresh process, so reading them costs nothing extra;
# only *writing* a newly-issued token back needs a Render API call, done
# here so the next deploy (within the token's ~24h validity) can reuse it.
RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "")
RENDER_SERVICE_ID = os.environ.get("RENDER_SERVICE_ID", "")
RENDER_API_BASE = "https://api.render.com/v1"


def _render_set_env_var(key, value):
    if not RENDER_API_KEY or not RENDER_SERVICE_ID:
        return
    try:
        url = f"{RENDER_API_BASE}/services/{RENDER_SERVICE_ID}/env-vars/{key}"
        req = urllib.request.Request(
            url,
            data=json.dumps({"value": value}).encode(),
            method="PUT",
            headers={
                "Authorization": f"Bearer {RENDER_API_KEY}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass  # persisting for next time is a bonus - never block on it


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

        env_token = os.environ.get("KIS_CACHED_TOKEN")
        env_expires_at = os.environ.get("KIS_CACHED_TOKEN_EXPIRES_AT")
        if env_token and env_expires_at:
            try:
                if now < float(env_expires_at) - 300:
                    _kis_token_cache["token"] = env_token
                    _kis_token_cache["expires_at"] = float(env_expires_at)
                    return env_token
            except ValueError:
                pass

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
        _render_set_env_var("KIS_CACHED_TOKEN", token)
        _render_set_env_var("KIS_CACHED_TOKEN_EXPIRES_AT", str(expires_at))
        return token


def kis_get(path, tr_id, params, retries=2):
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
    # KIS occasionally answers with a bare HTTP 5xx (no JSON body) under
    # load rather than an actual data problem - retry a couple of times
    # with a short backoff before giving up, so a momentary blip doesn't
    # surface as an error to the user.
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == retries:
                raise
            time.sleep(0.4 * (attempt + 1))

NAVER_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com"}
NAVER_QUOTE_URL = "https://polling.finance.naver.com/api/realtime/domestic/index/KOSPI"
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

# Daily-close history (KOSPI index or any KRX stock code) via Naver's chart
# JSON API. Replaces the old sise_day.naver / sise_index_day.naver HTML
# pages: those were paginated at ~10 rows/page (15+ sequential requests to
# cover a 120-day MA) and Naver retired them outright - both now return
# HTTP 410 Gone (confirmed 2026-09-18) - so this is a hard requirement, not
# just a speed-up. A single request covers the whole lookback window here.
NAVER_SISEJSON_URL = "https://api.finance.naver.com/siseJson.naver"
HISTORY_LOOKBACK_DAYS = 300
HISTORY_CACHE_TTL = 60
_history_cache = {}  # symbol -> {"data": [...], "ts": epoch}
_history_cache_lock = threading.Lock()


def naver_daily_closes(symbol):
    now = time.time()
    with _history_cache_lock:
        cached = _history_cache.get(symbol)
        if cached and now - cached["ts"] < HISTORY_CACHE_TTL:
            return cached["data"]

    end_dt = datetime.now(timezone.utc) + timedelta(hours=9)
    start_dt = end_dt - timedelta(days=HISTORY_LOOKBACK_DAYS)
    params = {
        "symbol": symbol,
        "requestType": "1",
        "startTime": start_dt.strftime("%Y%m%d"),
        "endTime": end_dt.strftime("%Y%m%d"),
        "timeframe": "day",
    }
    url = f"{NAVER_SISEJSON_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=NAVER_HEADERS)
    with urllib.request.urlopen(req, timeout=8) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    rows = ast.literal_eval(body.strip())
    closes = []
    for r in rows[1:]:
        date_str = str(r[0])
        closes.append({
            "date": f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}",
            "close": float(r[4]),
        })
    if len(closes) < 20:
        raise ValueError("insufficient history rows")

    with _history_cache_lock:
        _history_cache[symbol] = {"data": closes, "ts": now}
    return closes

# --- Single-stock checklist feature ---
NAVER_STOCK_SEARCH_URL = "https://ac.stock.naver.com/ac?q={query}&target=stock"
NAVER_STOCK_QUOTE_URL = "https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
NAVER_STOCK_INVESTOR_URL = (
    "https://stock.naver.com/api/domestic/detail/{code}/trend?tradeType=KRX&startIdx=0&pageSize=1"
)
STOCK_CODE_RE = re.compile(r"^[0-9A-Z]{6}$")

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
        elif self.path.startswith("/api/telegram/send-summary"):
            self.handle_telegram_send_summary()
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
        try:
            closes = naver_daily_closes("KOSPI")
            self.send_json(200, {"closes": closes})
        except Exception as e:
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
        try:
            closes = naver_daily_closes(code)
            self.send_json(200, {"closes": closes})
        except Exception as e:
            self.send_json(502, {"error": str(e)})

    def handle_stock_investors(self):
        code = self.require_stock_code()
        if not code:
            return
        if KIS_APP_KEY and KIS_APP_SECRET:
            try:
                self.send_json(200, self._fetch_investors_kis(code))
                return
            except Exception:
                pass  # fall through to the Naver-based approximation below
        try:
            self.send_json(200, self._fetch_investors_naver(code))
        except Exception as e:
            self.send_json(502, {"error": str(e)})

    def _fetch_investors_kis(self, code):
        # Real net-buy amounts for the prior trading day, straight from KRX
        # (via KIS), rather than Naver's share quantity approximated by
        # qty x close price. FID_INPUT_DATE_1 left blank returns the most
        # recently settled business day - during market hours that's
        # yesterday, but after today's own close (~15:40) it becomes today.
        # output2 is a descending list of days, so when the first row turns
        # out to be today, the row right after it is the actual prior day -
        # always use that one, regardless of what time this is called.
        data = kis_get(
            "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily",
            "FHPTJ04160001",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": code,
                "FID_INPUT_DATE_1": "",
                "FID_ORG_ADJ_PRC": "",
                "FID_ETC_CLS_CODE": "",
            },
        )
        rows = data.get("output2") or []
        if not rows:
            raise ValueError("데이터 없음")
        today = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y%m%d")
        trading_rows = [r for r in rows if r.get("stck_bsop_date") != today]
        if not trading_rows:
            trading_rows = rows
        row = trading_rows[0]

        def to_entry(r):
            return {
                "date": r.get("stck_bsop_date"),
                "individual": int(r.get("prsn_ntby_tr_pbmn", 0)) * 1_000_000,
                "foreign": int(r.get("frgn_ntby_tr_pbmn", 0)) * 1_000_000,
                "institution": int(r.get("orgn_ntby_tr_pbmn", 0)) * 1_000_000,
            }

        # A single call already returns ~30 days in output2 (descending), so
        # a week's history costs nothing extra - just keep more of what we
        # already fetched.
        entry = to_entry(row)
        entry["individualQty"] = int(row.get("prsn_ntby_qty", 0))
        entry["foreignQty"] = int(row.get("frgn_ntby_qty", 0))
        entry["institutionQty"] = int(row.get("orgn_ntby_qty", 0))
        entry["source"] = "kis"
        entry["history"] = [to_entry(r) for r in trading_rows[:7]]
        return entry

    def _fetch_investors_naver(self, code):
        req = urllib.request.Request(NAVER_STOCK_INVESTOR_URL.format(code=code), headers=NAVER_HEADERS)
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
        # This fallback path only ever gets called if the KIS call above
        # failed, and Naver's endpoint only gives one day - no week history.
        return {
            "date": row.get("bizdate"),
            "individual": individual_qty * close_price,
            "foreign": foreign_qty * close_price,
            "institution": institution_qty * close_price,
            "individualQty": individual_qty,
            "foreignQty": foreign_qty,
            "institutionQty": institution_qty,
            "source": "naver",
            "history": [{
                "date": row.get("bizdate"),
                "individual": individual_qty * close_price,
                "foreign": foreign_qty * close_price,
                "institution": institution_qty * close_price,
            }],
        }

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
        try:
            data = kis_get(
                "/uapi/domestic-stock/v1/quotations/program-trade-by-stock",
                "FHPPG04650101",
                # "J" (KRX only) can even show the opposite sign from what a
                # user sees in their own MTS app, which defaults to "UN"
                # (통합 = KRX+NXT combined) - match that so the numbers agree.
                {"FID_COND_MRKT_DIV_CODE": "UN", "FID_INPUT_ISCD": code},
            )
            rows = data.get("output") or []
            if not rows:
                raise ValueError("데이터 없음")
            latest = rows[0]
            self.send_json(200, {
                "time": latest.get("bsop_hour"),
                "netQty": int(latest.get("whol_smtn_ntby_qty", 0)),
                "netAmount": int(latest.get("whol_smtn_ntby_tr_pbmn", 0)),
            })
        except Exception as e:
            self.send_json(502, {"error": str(e)})

    def handle_telegram_send_summary(self):
        if not TELEGRAM_SUMMARY_KEY or self.query_param("key") != TELEGRAM_SUMMARY_KEY:
            self.send_json(403, {"error": "forbidden"})
            return
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            self.send_json(503, {"error": "텔레그램 설정이 안 되어 있습니다"})
            return
        try:
            text = build_dashboard_summary_text()
            send_telegram_message(text)
            self.send_json(200, {"ok": True})
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


# --- Telegram summary broadcast ---
# Lets a scheduled job (GitHub Actions cron) pull a text summary of what the
# dashboard currently shows and post it to a Telegram channel, so checking
# in doesn't require opening the page. Reuses the exact same JSON endpoints
# the frontend calls (via localhost) rather than re-scraping anything, so
# there's exactly one place each data source is fetched from.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_SUMMARY_KEY = os.environ.get("TELEGRAM_SUMMARY_KEY", "")
TELEGRAM_SUMMARY_STOCK_CODE = os.environ.get("TELEGRAM_SUMMARY_STOCK_CODE", "000660")

SIGNAL_EMOJI = {"green": "🟢", "yellow": "🟡", "red": "🔴", None: "⚪"}


def _score_for(signal):
    return {"green": 20, "yellow": 10, "red": 0}.get(signal, 10)


def _local_get(path):
    url = f"http://127.0.0.1:{PORT}{path}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read())


def _dir_of(diff):
    if diff > 0.0005:
        return "up"
    if diff < -0.0005:
        return "down"
    return "flat"


def _signal_for_dir(dir_, sentiment):
    if dir_ == "flat":
        return "yellow"
    favorable = (dir_ == "down") if sentiment == "inverse" else (dir_ == "up")
    return "green" if favorable else "red"


def _rolling_ma(values, period):
    out = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def _rank_index(price, ma_list):
    items = [("__price__", price)] + ma_list
    items.sort(key=lambda it: -it[1])
    return next(i for i, (label, _) in enumerate(items) if label == "__price__")


def _format_rank_chain(price, ma_list, price_label):
    items = [(price_label, price)] + [(label + "일선", val) for label, val in ma_list]
    items.sort(key=lambda it: -it[1])
    return ">".join(label for label, _ in items)


def _describe_rank_change(last, ma_list, prev_last, prev_ma_list):
    crossed_up, crossed_down = [], []
    for (label, val), (_, prev_val) in zip(ma_list, prev_ma_list):
        was_above = prev_last > prev_val
        is_above = last > val
        if is_above and not was_above:
            crossed_up.append(label + "일선")
        elif not is_above and was_above:
            crossed_down.append(label + "일선")
    if crossed_up and crossed_down:
        return f"{'·'.join(crossed_up)} 돌파, {'·'.join(crossed_down)} 이탈"
    if crossed_up:
        return f"{'·'.join(crossed_up)} 상향 돌파"
    if crossed_down:
        return f"{'·'.join(crossed_down)} 하향 돌파"
    steps = _rank_index(prev_last, prev_ma_list) - _rank_index(last, ma_list)
    if steps > 0:
        return f"어제보다 {steps}단계 상승"
    if steps < 0:
        return f"어제보다 {-steps}단계 하락"
    return "어제와 동일한 위치"


def _ma_state(closes):
    """Returns (last, ma_list, prev_last, prev_ma_list, have_prev) or None if
    there isn't enough history for a 120-day MA."""
    if len(closes) < 120:
        return None
    periods = [5, 10, 20, 60, 120]
    arrs = {p: _rolling_ma(closes, p) for p in periods}
    last = closes[-1]
    ma_list = [(str(p), arrs[p][-1]) for p in periods]
    prev_idx = len(closes) - 2
    prev_last = closes[prev_idx] if prev_idx >= 0 else None
    prev_ma_list = [(str(p), arrs[p][prev_idx]) for p in periods] if prev_idx >= 0 else []
    have_prev = prev_idx >= 0 and prev_last is not None and all(v is not None for _, v in prev_ma_list)
    return last, ma_list, prev_last, prev_ma_list, have_prev


def _macro_instrument_line(label, symbol, sentiment, prefix, decimals):
    data = _local_get(f"/api/quote?symbol={urllib.parse.quote(symbol, safe='')}&range=1d&interval=2m")
    meta = data["chart"]["result"][0]["meta"]
    price = meta["regularMarketPrice"]
    prev = meta.get("previousClose") or meta.get("chartPreviousClose")
    diff = price - prev
    dir_ = _dir_of(diff)
    signal = _signal_for_dir(dir_, sentiment)
    pct = (diff / prev * 100) if prev else 0
    arrow = "▲" if dir_ == "up" else ("▼" if dir_ == "down" else "-")
    price_fmt = f"{price:.{decimals}f}" if decimals else f"{price:,.0f}"
    return signal, f"{SIGNAL_EMOJI[signal]} {label} {prefix}{price_fmt} ({arrow}{abs(pct):.2f}%)"


def _kospi_section():
    quote = _local_get("/api/kospi/quote")
    hist = _local_get("/api/kospi/history")
    closes = [c["close"] for c in hist.get("closes", []) if c.get("close") is not None]
    price, prev_close = quote["price"], quote["prevClose"]
    diff = price - prev_close
    dir_signal = _signal_for_dir(_dir_of(diff), "normal")

    pct = diff / prev_close * 100 if prev_close else 0
    arrow = "▲" if diff > 0 else ("▼" if diff < 0 else "-")

    state = _ma_state(closes)
    if not state:
        line = f"{SIGNAL_EMOJI[dir_signal]} 코스피 {price:,.2f} ({arrow}{abs(pct):.2f}%) 데이터 부족"
        return dir_signal, dir_signal, [line]
    last, ma_list, prev_last, prev_ma_list, have_prev = state
    ma5 = dict(ma_list)["5"]
    ma20 = dict(ma_list)["20"]
    if last >= ma5:
        zone = "5일선 위=2배 베팅"
    elif last > ma20:
        zone = "5일선 아래=현금 베팅"
    else:
        zone = "20일선 아래=저점 현금베팅"
    if last >= ma5:
        ma5_signal = "green"
    elif have_prev and _rank_index(last, ma_list) < _rank_index(prev_last, prev_ma_list):
        ma5_signal = "yellow"
    else:
        ma5_signal = "red"
    rank_text = _describe_rank_change(last, ma_list, prev_last, prev_ma_list) if have_prev else "데이터 부족"
    rank_chain = _format_rank_chain(last, ma_list, "지수")
    lines = [
        f"{SIGNAL_EMOJI[dir_signal]} 코스피 {price:,.2f} ({arrow}{abs(pct):.2f}%) · {zone}",
        f"{SIGNAL_EMOJI[ma5_signal]} 코스피 순위: {rank_chain} ({rank_text})",
    ]
    return dir_signal, ma5_signal, lines


def _stock_section(code):
    quote = _local_get(f"/api/stock/quote?code={code}")
    hist = _local_get(f"/api/stock/history?code={code}")
    theme = _local_get(f"/api/stock/theme?code={code}")
    try:
        live = _local_get(f"/api/stock/investor-live?code={code}")
    except Exception:
        live = None
    try:
        prog = _local_get(f"/api/stock/program-trade?code={code}")
    except Exception:
        prog = None

    name = quote.get("name") or code
    price, prev_close, open_ = quote["price"], quote["prevClose"], quote.get("open")
    diff = price - prev_close
    dir_ = _dir_of(diff)
    is_bull = open_ is not None and price > open_
    is_bear = open_ is not None and price < open_
    candle_label = "양봉" if is_bull else ("음봉" if is_bear else "보합")
    if dir_ == "up":
        candle_signal = "yellow" if is_bear else "green"
    elif dir_ == "down":
        candle_signal = "yellow" if is_bull else "red"
    else:
        candle_signal = "yellow"
    signals = [candle_signal]

    us_signal, us_line, kr_signal, kr_line = None, None, None, None
    info = theme.get("info") if theme.get("found") else None
    if info and info.get("usProxy"):
        try:
            us_data = _local_get(f"/api/quote?symbol={urllib.parse.quote(info['usProxy']['symbol'], safe='')}&range=1d&interval=2m")
            meta = us_data["chart"]["result"][0]["meta"]
            prev = meta.get("previousClose") or meta.get("chartPreviousClose")
            d = meta["regularMarketPrice"] - prev
            us_signal = _signal_for_dir(_dir_of(d), "normal")
            us_pct = d / prev * 100 if prev else 0
            us_arrow = "▲" if d > 0 else ("▼" if d < 0 else "-")
            us_line = f"{SIGNAL_EMOJI[us_signal]} 전일 미국 동일 산업군({info['usProxy']['name']}) {us_arrow}{abs(us_pct):.2f}%"
        except Exception:
            pass
    if info and info.get("krProxy"):
        try:
            kr_data = _local_get(f"/api/stock/quote?code={info['krProxy']['code']}")
            kr_prev = kr_data["prevClose"]
            d = kr_data["price"] - kr_prev
            kr_signal = _signal_for_dir(_dir_of(d), "normal")
            kr_pct = d / kr_prev * 100 if kr_prev else 0
            kr_arrow = "▲" if d > 0 else ("▼" if d < 0 else "-")
            kr_line = f"{SIGNAL_EMOJI[kr_signal]} 국내 동일 산업군({info['krProxy']['name']}) {kr_arrow}{abs(kr_pct):.2f}%"
        except Exception:
            pass
    if us_signal:
        signals.append(us_signal)
    if kr_signal:
        signals.append(kr_signal)

    closes = [c["close"] for c in hist.get("closes", []) if c.get("close") is not None]
    state = _ma_state(closes)
    chart_signal, rank_text = None, "데이터 부족"
    if state:
        last, ma_list, prev_last, prev_ma_list, have_prev = state
        ma5 = dict(ma_list)["5"]
        if last >= ma5:
            chart_signal = "green"
        elif have_prev and _rank_index(last, ma_list) < _rank_index(prev_last, prev_ma_list):
            chart_signal = "yellow"
        else:
            chart_signal = "red"
        if have_prev:
            rank_text = _describe_rank_change(last, ma_list, prev_last, prev_ma_list)
        rank_chain = _format_rank_chain(last, ma_list, name)
        signals.append(chart_signal)

    pct = diff / prev_close * 100 if prev_close else 0
    arrow = "▲" if diff > 0 else ("▼" if diff < 0 else "-")
    lines = [f"{SIGNAL_EMOJI[candle_signal]} {name} ₩{price:,.0f} ({arrow}{abs(pct):.2f}%, {candle_label})"]
    if us_line:
        lines.append(us_line)
    if kr_line:
        lines.append(kr_line)
    if chart_signal:
        lines.append(f"{SIGNAL_EMOJI[chart_signal]} 이평선 순위: {rank_chain} ({rank_text})")

    if live and not live.get("error"):
        fb, ib = live.get("foreignQty", 0) > 0, live.get("institutionQty", 0) > 0
        live_signal = "green" if fb and ib else ("red" if not fb and not ib else "yellow")
        signals.append(live_signal)
        lines.append(
            f"{SIGNAL_EMOJI[live_signal]} 수급: 외국인 {fmt_eok(live.get('foreignQty', 0) * price)} · "
            f"기관 {fmt_eok(live.get('institutionQty', 0) * price)}"
        )

    if prog and not prog.get("error"):
        amt = prog.get("netAmount", 0)
        prog_signal = "green" if amt > 0 else ("red" if amt < 0 else "yellow")
        signals.append(prog_signal)
        lines.append(f"{SIGNAL_EMOJI[prog_signal]} 프로그램매매: {fmt_eok(amt)}")

    score_pct = round(sum(_score_for(s) for s in signals) / (len(signals) * 20) * 100) if signals else None
    return score_pct, name, lines


def fmt_eok(won):
    return f"{'+' if won >= 0 else ''}{round(won / 1e8):,}억"


def build_dashboard_summary_text():
    now = datetime.now(timezone.utc) + timedelta(hours=9)
    header = f"📊 종가베팅 체크리스트 · {now.strftime('%m/%d %H:%M')}"

    macro_signals = []
    macro_lines = []
    for label, symbol, sentiment, prefix, decimals in [
        ("미국채10Y", "^TNX", "inverse", "", 3),
        ("WTI", "CL=F", "inverse", "$", 2),
        ("나스닥100선물", "NQ=F", "normal", "$", 0),
        ("비트코인", "BTC-USD", "normal", "$", 0),
    ]:
        try:
            sig, line = _macro_instrument_line(label, symbol, sentiment, prefix, decimals)
            macro_signals.append(sig)
            macro_lines.append(line)
        except Exception:
            macro_lines.append(f"⚪ {label} 데이터 없음")

    try:
        dir_sig, ma5_sig, kospi_lines = _kospi_section()
        macro_signals.extend([dir_sig, ma5_sig])
        macro_lines.extend(kospi_lines)
    except Exception as e:
        macro_lines.append(f"⚪ 코스피 데이터 없음 ({e})")

    macro_pct = round(sum(_score_for(s) for s in macro_signals) / (len(macro_signals) * 20) * 100) if macro_signals else 0

    try:
        stock_pct, stock_name, stock_lines = _stock_section(TELEGRAM_SUMMARY_STOCK_CODE)
    except Exception as e:
        stock_pct, stock_name, stock_lines = None, TELEGRAM_SUMMARY_STOCK_CODE, [f"⚪ 종목 체크 데이터 없음 ({e})"]

    summary_lines = [f"매크로 체크 {macro_pct}%"]
    summary_lines.append(f"종목체크({stock_name}) {stock_pct}%" if stock_pct is not None else "종목 체크 데이터 없음")

    return "\n".join([header, *summary_lines, "", *macro_lines, "", *stock_lines, "", "----"])


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    body = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    if not result.get("ok"):
        raise ValueError(result.get("description", "telegram send failed"))


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with Server((HOST, PORT), Handler) as httpd:
        print(f"Serving dashboard on http://{HOST}:{PORT}")
        httpd.serve_forever()
