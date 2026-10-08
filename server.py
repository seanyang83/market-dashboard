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
import hashlib
import hmac
import http.server
import json
import leader
import os
import re
import secrets
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

# --- 국내휴장일조회: 설날/추석 같은 평일 공휴일에 텔레그램 메시지를 쉬기 위함 ---
# KIS 공식 문서가 "당사 원장서비스와 연관되어 있어 가급적 1일 1회 호출 부탁
# 드립니다"라고 명시한 API라서, 하루 한 번만 조회하고 그날 자정(KST)까지는
# 캐시된 값을 재사용한다 (정시 발송 5번 + 30분 체크 ~20여 번이 전부 같은
# 캐시를 봄).
_holiday_cache = {"date": None, "is_trading_day": True}
_holiday_cache_lock = threading.Lock()


def is_krx_trading_day():
    date_str = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y%m%d")
    with _holiday_cache_lock:
        if _holiday_cache["date"] == date_str:
            return _holiday_cache["is_trading_day"]
    is_trading = True  # 조회 실패 시 평소처럼 동작(실제 거래일에 조용해지는 것보다 안전)
    try:
        data = kis_get(
            "/uapi/domestic-stock/v1/quotations/chk-holiday",
            "CTCA0903R",
            {"BASS_DT": date_str, "CTX_AREA_NK": "", "CTX_AREA_FK": ""},
        )
        output = data.get("output")
        if isinstance(output, list):
            output = next((o for o in output if o.get("bass_dt") == date_str), output[0] if output else {})
        is_trading = (output or {}).get("opnd_yn") == "Y"
    except Exception:
        pass
    with _holiday_cache_lock:
        _holiday_cache["date"] = date_str
        _holiday_cache["is_trading_day"] = is_trading
    return is_trading


NAVER_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com"}
NAVER_QUOTE_URL = "https://polling.finance.naver.com/api/realtime/domestic/index/KOSPI"
# Investor-type codes in Naver's market trend payloads: 8000 개인, 9000 외국인
# (9001 = 외국인 기타, 키움 기준에선 뺌), and the institution sub-types below.
# 7000 is an always-zero placeholder and 7100 (기타법인) is its own bucket that
# the simplified 3-way view excludes.
INVESTOR_INSTITUTION = {"1000", "2000", "3000", "3100", "4000", "5000", "6000"}

# Market-wide (KOSPI/KOSDAQ) daily total trading value, last N trading days.
# trend/daily with a bigger pageSize - the
# per-investor-type buyPrice values already sum to the whole market's total
# 거래대금 for that day (verified against the real-time index quote's own
# accumulatedTradingValueRaw: matched within ~0.1%, the rest being normal
# snapshot-timing lag between the two calls).
NAVER_MARKET_TREND_URL = (
    "https://stock.naver.com/api/domestic/market/trend/daily"
    "?tradeType=KRX&marketType={market}&startIdx=0&pageSize={page_size}"
)
MARKET_TRADING_VALUE_CACHE_TTL = 20
_market_trading_value_cache = {"data": None, "ts": 0}
_market_trading_value_cache_lock = threading.Lock()


def _fetch_market_trading_value_history(market, days=7):
    url = NAVER_MARKET_TREND_URL.format(market=market, page_size=days)
    req = urllib.request.Request(url, headers=NAVER_HEADERS)
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read())
    history = []
    for day in data.get("content") or []:
        total = sum(int(a.get("buyPrice", 0) or 0) for a in day.get("netAmounts", []))
        history.append({"date": day.get("bizdate"), "value": total})
    return history


def _fetch_market_trading_value():
    now = time.time()
    with _market_trading_value_cache_lock:
        cached = _market_trading_value_cache["data"]
        if cached and now - _market_trading_value_cache["ts"] < MARKET_TRADING_VALUE_CACHE_TTL:
            return cached
    result = {
        "kospi": _fetch_market_trading_value_history("KOSPI"),
        "kosdaq": _fetch_market_trading_value_history("KOSDAQ"),
    }
    with _market_trading_value_cache_lock:
        _market_trading_value_cache["data"] = result
        _market_trading_value_cache["ts"] = now
    return result


# --- 코스피 당일 투자자별 순매수 추이 (키움 "당일추이"와 같은 데이터) ---
# 네이버 trend/time: 분 단위 누적 순매수. KRX와 NXT가 따로 내려와서 시각별로
# 합쳐야 키움(통합) 값과 맞는다 (11:06 실측: 개인 -871, 기관 +2,220 일치).
# 외국인은 9000만 센다 - 9001(외국인 기타)까지 넣으면 키움보다 ~72억 어긋남.
# startIdx가 페이지 번호. 처음 한 번만 하루치(pageSize 200, NXT는 08:00~20:00이라
# 200행을 넘을 수 있어 페이지를 넘겨 받음)를 받고 이후에는 최신 20행만 받아서
# 메모리의 기록에 이어붙인다.
FLOW_TREND_URL = (
    "https://stock.naver.com/api/domestic/market/trend/time"
    "?tradeType={trade_type}&marketType=KOSPI&startIdx={page}&pageSize={size}"
)
FLOW_TREND_CACHE_TTL = 60
_flow_trend = {"date": None, "rows": {"KRX": {}, "NXT": {}}, "result": None, "ts": 0}
_flow_trend_lock = threading.Lock()


def _flow_trend_fetch_page(trade_type, page, size):
    url = FLOW_TREND_URL.format(trade_type=trade_type, page=page, size=size)
    req = urllib.request.Request(url, headers=NAVER_HEADERS)
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read())


def _flow_trend_row_values(row):
    t = {a["investorGubun"]: int(a["diffValue"]) for a in row.get("netAmounts", [])}
    return (
        t.get("8000", 0),
        t.get("9000", 0),
        sum(v for k, v in t.items() if k in INVESTOR_INSTITUTION),
    )


def _flow_trend_refresh(trade_type):
    store = _flow_trend["rows"][trade_type]
    if not store:
        size = 200
        first = _flow_trend_fetch_page(trade_type, 0, size)
        pages = int(first.get("totalPages") or 1)
        contents = [first.get("content", [])]
        for p in range(1, pages):
            contents.append(_flow_trend_fetch_page(trade_type, p, size).get("content", []))
        rows = [r for c in contents for r in c]
    else:
        rows = _flow_trend_fetch_page(trade_type, 0, 20).get("content", [])
    for r in rows:
        if r.get("time"):
            store[(r["bizdate"], r["time"])] = _flow_trend_row_values(r)


def _build_flow_trend():
    with _flow_trend_lock:
        now = time.time()
        if _flow_trend["result"] and now - _flow_trend["ts"] < FLOW_TREND_CACHE_TTL:
            return _flow_trend["result"]
        today = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y%m%d")
        if _flow_trend["date"] != today:
            _flow_trend["date"] = today
            _flow_trend["rows"] = {"KRX": {}, "NXT": {}}
        for trade_type in ("KRX", "NXT"):
            _flow_trend_refresh(trade_type)

        stores = _flow_trend["rows"]
        latest = max((k[0] for st in stores.values() for k in st), default=None)
        if latest is None:
            raise ValueError("데이터 없음")
        times = sorted({k[1] for st in stores.values() for k in st if k[0] == latest})
        last = {"KRX": (0, 0, 0), "NXT": (0, 0, 0)}
        points = []
        for t in times:
            for tt in ("KRX", "NXT"):
                if (latest, t) in stores[tt]:
                    last[tt] = stores[tt][(latest, t)]
            total = [last["KRX"][i] + last["NXT"][i] for i in range(3)]
            points.append({
                "t": t,
                "individual": round(total[0] / 1e8, 1),
                "foreign": round(total[1] / 1e8, 1),
                "institution": round(total[2] / 1e8, 1),
            })
        result = {
            "date": latest,
            "points": points,
        }
        _flow_trend["result"] = result
        _flow_trend["ts"] = now
        return result


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

# --- 시장 등락 종목수 (코스피/코스닥 전체 체감 심리) ---
# KIS "국내업종 현재지수"[v1_국내주식-063] - 업종코드로 지수 자체를 조회하면
# (개별 종목이 아니라) 그 지수를 구성하는 전체 종목의 상승/보합/하락 집계도
# 같이 내려준다. 업종코드: 코스피 0001, 코스닥 1001.
MARKET_BREADTH_ISCD = {"kospi": "0001", "kosdaq": "1001"}
MARKET_BREADTH_CACHE_TTL = 20
_market_breadth_cache = {"data": None, "ts": 0}
_market_breadth_cache_lock = threading.Lock()


def _fetch_market_breadth_one(iscd):
    data = kis_get(
        "/uapi/domestic-stock/v1/quotations/inquire-index-price",
        "FHPUP02100000",
        {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": iscd},
    )
    out = data.get("output") or {}
    return {
        "up": int(out.get("ascn_issu_cnt", 0) or 0),
        "flat": int(out.get("stnr_issu_cnt", 0) or 0),
        "down": int(out.get("down_issu_cnt", 0) or 0),
        "limitUp": int(out.get("uplm_issu_cnt", 0) or 0),
        "limitDown": int(out.get("lslm_issu_cnt", 0) or 0),
    }


def _fetch_market_breadth():
    now = time.time()
    with _market_breadth_cache_lock:
        cached = _market_breadth_cache["data"]
        if cached and now - _market_breadth_cache["ts"] < MARKET_BREADTH_CACHE_TTL:
            return cached
    result = {name: _fetch_market_breadth_one(iscd) for name, iscd in MARKET_BREADTH_ISCD.items()}
    with _market_breadth_cache_lock:
        _market_breadth_cache["data"] = result
        _market_breadth_cache["ts"] = now
    return result


# --- 거래대금 순위 TOP10 ---
# KIS "거래량순위"[v1_국내주식-047] API, FID_BLNG_CLS_CODE="3"(거래금액순)으로
# 거래대금 기준 정렬. 최대 30건까지 지원(다음 조회 불가)하므로 top10엔 충분.
#
# 이 TR은 공식 문서상 FID_COND_MRKT_DIV_CODE가 "J"(KRX)/"NX"(NXT)만 지원하고
# "UN"(통합)은 빈 결과를 반환한다(program-trade-by-stock 등 다른 TR과 다름,
# 실측 확인함) - 그래서 KRX/NXT 두 번 조회해서 종목코드 기준으로 거래대금·
# 거래량을 합산한다. NXT 체결분을 빼먹으면 대형주(NXT 거래 비중이 큰 종목)일
# 수록 실제보다 낮게 나온다(삼성전자 실측 대비 약 56%로 확인됨).
VOLUME_RANK_CACHE_TTL = 20
_volume_rank_cache = {"data": None, "ts": 0}
_volume_rank_cache_lock = threading.Lock()

SIGN_DIR = {"1": "up", "2": "up", "3": "flat", "4": "down", "5": "down"}


def _fetch_volume_rank_rows(mrkt_div_code):
    data = kis_get(
        "/uapi/domestic-stock/v1/quotations/volume-rank",
        "FHPST01710000",
        {
            "FID_COND_MRKT_DIV_CODE": mrkt_div_code,
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": "0000",
            "FID_DIV_CLS_CODE": "0",
            "FID_BLNG_CLS_CODE": "3",
            "FID_TRGT_CLS_CODE": "111111111",
            # 자릿수 순서: 투자위험/경고/주의, 관리종목, 정리매매, 불성실공시,
            # 우선주, 거래정지, ETF, ETN, 신용주문불가, SPAC - 관리종목/거래정지/
            # ETF/ETN만 제외(2,6,7,8번째 자리 "1")하고 우선주 등은 그대로 포함.
            "FID_TRGT_EXLS_CLS_CODE": "0100011100",
            "FID_INPUT_PRICE_1": "",
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",
        },
    )
    return data.get("output") or []


def _fetch_volume_rank():
    now = time.time()
    with _volume_rank_cache_lock:
        cached = _volume_rank_cache["data"]
        if cached and now - _volume_rank_cache["ts"] < VOLUME_RANK_CACHE_TTL:
            return cached

    krx_rows = _fetch_volume_rank_rows("J")
    try:
        nxt_rows = _fetch_volume_rank_rows("NX")
    except Exception:
        nxt_rows = []  # NXT 조회 실패해도 KRX만으로는 계속 보여줌

    merged = {}
    for r in krx_rows + nxt_rows:
        code = r.get("mksc_shrn_iscd")
        if not code:
            continue
        entry = merged.setdefault(code, {
            "name": r.get("hts_kor_isnm"), "price": 0.0,
            "sign": "3", "pct": 0.0, "tr_pbmn": 0, "vol": 0,
        })
        entry["price"] = float(r.get("stck_prpr", 0) or 0)
        entry["sign"] = r.get("prdy_vrss_sign", entry["sign"])
        entry["pct"] = abs(float(r.get("prdy_ctrt", 0) or 0))
        entry["tr_pbmn"] += int(r.get("acml_tr_pbmn", 0) or 0)
        entry["vol"] += int(r.get("acml_vol", 0) or 0)

    ranked = sorted(merged.items(), key=lambda kv: -kv[1]["tr_pbmn"])[:10]
    result = []
    for i, (code, e) in enumerate(ranked, start=1):
        dir_ = SIGN_DIR.get(e["sign"], "flat")
        result.append({
            "rank": i,
            "code": code,
            "name": e["name"],
            "price": e["price"],
            "changePct": e["pct"] if dir_ != "down" else -e["pct"],
            "dir": dir_,
            "tradingValue": e["tr_pbmn"],
            "volume": e["vol"],
        })

    with _volume_rank_cache_lock:
        _volume_rank_cache["data"] = result
        _volume_rank_cache["ts"] = now
    return result


# --- 대시보드 접근 비밀번호 ---
# DASHBOARD_PASSWORD(Render 환경변수)를 설정하면 페이지와 /api/* 전부 로그인이
# 필요해진다. 한 번 로그인하면 쿠키를 30일간 기억해서 휴대폰에서도 매번 칠
# 필요가 없고, /login?key=비밀번호 링크를 북마크해두면 만료/삭제 후에도 한 번
# 탭으로 다시 로그인된다. 환경변수가 비어 있으면 인증을 끈다(설정 전에 배포돼도
# 잠기지 않게). 텔레그램 발송 엔드포인트는 자체 키(TELEGRAM_SUMMARY_KEY)로
# 보호되고, 서버가 자기 자신을 호출하는 내부 요청은 프로세스마다 새로 만든
# INTERNAL_TOKEN 헤더로 통과시킨다.
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
AUTH_COOKIE = "dash_auth"
AUTH_COOKIE_MAX_AGE = 30 * 24 * 3600
KEY_PROTECTED_PREFIXES = (
    "/api/ops/",
    "/api/telegram/send-summary",
    "/api/telegram/check-alert",
    "/api/telegram/announce",
)
INTERNAL_TOKEN = secrets.token_hex(16)
LOGIN_MAX_FAILS = 10
LOGIN_FAIL_WINDOW = 600
_login_fail_times = []
_login_fail_lock = threading.Lock()

LOGIN_PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>로그인</title>
<style>
  :root { color-scheme: light dark; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Pretendard,"Apple SD Gothic Neo",sans-serif;
         background:#f4f5f7; color:#14161a; }
  @media (prefers-color-scheme: dark) { body { background:#0b0d10; color:#f2f3f5; } }
  form { width:min(320px, 86vw); display:flex; flex-direction:column; gap:12px; }
  h1 { font-size:18px; margin:0 0 4px; }
  input, button { font-size:16px; padding:12px; border-radius:10px; border:1px solid #8884; background:transparent; color:inherit; }
  button { background:#2563eb; color:#fff; border:0; font-weight:700; }
  .err { color:#dc2626; font-size:13px; min-height:1em; }
</style></head><body>
<form method="post" action="/login">
  <h1>종가베팅 체크리스트</h1>
  <input type="password" name="password" placeholder="비밀번호" autocomplete="current-password" autofocus>
  <div class="err">__ERROR__</div>
  <button type="submit">들어가기</button>
</form></body></html>"""


def _auth_token():
    return hmac.new(DASHBOARD_PASSWORD.encode(), b"dashboard-auth-v1", hashlib.sha256).hexdigest()


def _login_allowed():
    now = time.time()
    with _login_fail_lock:
        _login_fail_times[:] = [t for t in _login_fail_times if now - t < LOGIN_FAIL_WINDOW]
        return len(_login_fail_times) < LOGIN_MAX_FAILS


def _check_password(candidate):
    if not _login_allowed():
        return False
    ok = hmac.compare_digest(candidate.encode(), DASHBOARD_PASSWORD.encode())
    if not ok:
        with _login_fail_lock:
            _login_fail_times.append(time.time())
    return ok


class Handler(http.server.SimpleHTTPRequestHandler):
    def _has_valid_cookie(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == AUTH_COOKIE and hmac.compare_digest(v.encode(), _auth_token().encode()):
                return True
        return False

    def _authorized(self, path):
        if not DASHBOARD_PASSWORD:
            return True
        if hmac.compare_digest(self.headers.get("X-Internal-Token", "").encode(), INTERNAL_TOKEN.encode()):
            return True
        if path.startswith(KEY_PROTECTED_PREFIXES):
            return True
        return self._has_valid_cookie()

    def _login_redirect(self, set_cookie):
        self.send_response(302)
        self.send_header("Location", "/")
        if set_cookie:
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            self.send_header(
                "Set-Cookie",
                f"{AUTH_COOKIE}={_auth_token()}; Max-Age={AUTH_COOKIE_MAX_AGE}; Path=/; HttpOnly; SameSite=Lax{secure}",
            )
        self.end_headers()

    def _send_login_page(self, error="", status=200):
        body = LOGIN_PAGE.replace("__ERROR__", error).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def handle_login_get(self):
        if not DASHBOARD_PASSWORD:
            self._login_redirect(False)
            return
        key = self.query_param("key")
        if key:
            if _check_password(key):
                self._login_redirect(True)
            else:
                self._send_login_page("비밀번호가 맞지 않습니다", 401)
            return
        self._send_login_page()

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/login" or not DASHBOARD_PASSWORD:
            self.send_json(404, {"error": "not found"})
            return
        length = min(int(self.headers.get("Content-Length") or 0), 4096)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode(errors="replace"))
        if _check_password(form.get("password", [""])[0]):
            self._login_redirect(True)
        elif not _login_allowed():
            self._send_login_page("시도 횟수가 많아 잠시 후 다시 시도해주세요", 429)
        else:
            self._send_login_page("비밀번호가 맞지 않습니다", 401)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/login":
            self.handle_login_get()
            return
        if path == "/healthz":
            # Render가 배포마다 넣어주는 커밋 해시 - 배포가 실제로 반영됐는지
            # 확인하는 용도 (저장소가 public이라 노출돼도 무방).
            self.send_json(200, {"ok": True, "commit": os.environ.get("RENDER_GIT_COMMIT", "")[:7] or None})
            return
        if not self._authorized(path):
            if path.startswith("/api/"):
                self.send_json(401, {"error": "unauthorized"})
            else:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
            return
        if self.path.startswith("/api/quote"):
            self.handle_quote()
        elif self.path.startswith("/api/kospi/quote"):
            self.handle_kospi_quote()
        elif self.path.startswith("/api/kospi/history"):
            self.handle_kospi_history()
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
        elif self.path.startswith("/api/telegram/check-alert"):
            self.handle_check_threshold_alert()
        elif self.path.startswith("/api/telegram/announce"):
            self.handle_telegram_announce()
        elif self.path.startswith("/api/watchlist/add"):
            self.handle_watchlist_add()
        elif self.path.startswith("/api/watchlist/remove"):
            self.handle_watchlist_remove()
        elif self.path.startswith("/api/watchlist"):
            self.handle_watchlist_get()
        elif self.path.startswith("/api/market/volume-rank"):
            self.handle_volume_rank()
        elif self.path.startswith("/api/kospi/flow-trend"):
            self.handle_kospi_flow_trend()
        elif self.path.startswith("/api/market/trading-value"):
            self.handle_market_trading_value()
        elif self.path.startswith("/api/market/breadth"):
            self.handle_market_breadth()
        elif self.path.startswith("/api/ops/kis-income"):
            self.handle_ops_kis_income()
        elif self.path.startswith("/api/leader"):
            self.handle_leader()
        elif urllib.parse.urlparse(self.path).path in ("/leader", "/leader.html"):
            self.path = "/leader.html"
            super().do_GET()
        elif urllib.parse.urlparse(self.path).path in ("/", "/index.html"):
            super().do_GET()
        else:
            # SimpleHTTPRequestHandler would otherwise serve every file in
            # this folder (server.py, HANDOFF.md, .kis_token_cache.json...).
            self.send_json(404, {"error": "not found"})

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

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
                # 이 TR은 공식 문서상 "UN"(통합)을 지원한다 - "J"(KRX 단독)로 두면
                # NXT 체결분이 빠져 종목별 수급이 실제(키움 MTS 통합 기준)와
                # 어긋난다(부호까지 반대로 나오는 경우도 확인됨).
                "FID_COND_MRKT_DIV_CODE": "UN",
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
        # price (same qty x close convention as the old market-wide investor card).
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

    def handle_kospi_flow_trend(self):
        try:
            self.send_json(200, _build_flow_trend())
        except Exception as e:
            self.send_json(502, {"error": str(e)})

    def handle_market_trading_value(self):
        try:
            self.send_json(200, _fetch_market_trading_value())
        except Exception as e:
            self.send_json(502, {"error": str(e)})

    def handle_market_breadth(self):
        if not KIS_APP_KEY or not KIS_APP_SECRET:
            self.send_json(503, {"error": "KIS API 키가 설정되지 않았습니다"})
            return
        try:
            self.send_json(200, _fetch_market_breadth())
        except Exception as e:
            self.send_json(502, {"error": str(e)})

    def handle_volume_rank(self):
        if not KIS_APP_KEY or not KIS_APP_SECRET:
            self.send_json(503, {"error": "KIS API 키가 설정되지 않았습니다"})
            return
        try:
            self.send_json(200, {"rows": _fetch_volume_rank()})
        except Exception as e:
            self.send_json(502, {"error": str(e)})

    def handle_telegram_send_summary(self):
        if not TELEGRAM_SUMMARY_KEY or self.query_param("key") != TELEGRAM_SUMMARY_KEY:
            self.send_json(403, {"error": "forbidden"})
            return
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            self.send_json(503, {"error": "텔레그램 설정이 안 되어 있습니다"})
            return
        if not is_krx_trading_day():
            self.send_json(200, {"ok": True, "skipped": "market_holiday"})
            return
        try:
            text = build_dashboard_summary_text()
            send_telegram_message(text)
            self.send_json(200, {"ok": True})
        except Exception as e:
            self.send_json(502, {"error": str(e)})

    def handle_check_threshold_alert(self):
        if not TELEGRAM_SUMMARY_KEY or self.query_param("key") != TELEGRAM_SUMMARY_KEY:
            self.send_json(403, {"error": "forbidden"})
            return
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            self.send_json(503, {"error": "텔레그램 설정이 안 되어 있습니다"})
            return
        # 정시 요약(09:10~19:40)과 같은 시간대에만 - cron 지연이나 수동 호출로
        # 장 밖 시간에 알림이 나가지 않도록 서버에서도 한 번 더 막는다.
        now = datetime.now(timezone.utc) + timedelta(hours=9)
        if not (9 * 60 <= now.hour * 60 + now.minute <= 20 * 60):
            self.send_json(200, {"sent": False, "skipped": "outside_hours"})
            return
        if not is_krx_trading_day():
            self.send_json(200, {"sent": False, "skipped": "market_holiday"})
            return
        try:
            result = check_threshold_alert()
            result["ma"] = check_ma_proximity_alert()
            self.send_json(200, result)
        except Exception as e:
            self.send_json(502, {"error": str(e)})

    def handle_telegram_announce(self):
        # 자동 발송(정시 요약/임계값 알림)과 별개로, 채널에 한 번 보낼 공지
        # 텍스트를 수동으로 보낼 때 쓰는 용도. 같은 키로 보호.
        if not TELEGRAM_SUMMARY_KEY or self.query_param("key") != TELEGRAM_SUMMARY_KEY:
            self.send_json(403, {"error": "forbidden"})
            return
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            self.send_json(503, {"error": "텔레그램 설정이 안 되어 있습니다"})
            return
        text = self.query_param("text")
        if not text:
            self.send_json(400, {"error": "missing text"})
            return
        try:
            send_telegram_message(text)
            self.send_json(200, {"ok": True})
        except Exception as e:
            self.send_json(502, {"error": str(e)})

    def handle_ops_kis_income(self):
        # 운영 확인용: 한투 손익계산서 원본 응답. TELEGRAM_SUMMARY_KEY로 보호.
        if not TELEGRAM_SUMMARY_KEY or self.query_param("key") != TELEGRAM_SUMMARY_KEY:
            self.send_json(403, {"error": "forbidden"})
            return
        code = self.require_stock_code()
        if not code:
            return
        try:
            self.send_json(200, leader.kis_income_raw(code, kis_get, self.query_param("div") or "1"))
        except Exception as e:
            self.send_json(502, {"error": str(e)})

    def handle_leader(self):
        code = self.require_stock_code()
        if not code:
            return
        try:
            self.send_json(200, leader.analyze(code, kis_get if KIS_APP_KEY and KIS_APP_SECRET else None))
        except leader.NoFinance as e:
            self.send_json(404, {"error": str(e)})
        except Exception as e:
            self.send_json(502, {"error": str(e)})

    def handle_watchlist_get(self):
        items = []
        for code in get_ma_watch_codes():
            try:
                q = _local_get(f"/api/stock/quote?code={code}")
                items.append({"code": code, "name": q.get("name") or code})
            except Exception:
                items.append({"code": code, "name": code})
        self.send_json(200, {"items": items})

    def handle_watchlist_add(self):
        code = self.require_stock_code()
        if not code:
            return
        self.send_json(200, {"ok": True, "codes": add_ma_watch_code(code)})

    def handle_watchlist_remove(self):
        code = self.require_stock_code()
        if not code:
            return
        self.send_json(200, {"ok": True, "codes": remove_ma_watch_code(code)})

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

# 매크로/종목 체크 % 에 따라 🔥 개수와 강조 문구를 정하는 단계.
# - 한쪽만 기준을 넘으면: 그 줄만 80%+ 🔥1개, 100% 🔥3개.
# - 둘 다 80%+: 양쪽 다 🔥3개 + "불장!!" 문구.
# - 둘 다 100%: 양쪽 다 🔥5개 + "종가베팅 순간이 왔습니다" 문구.
# 매 체크포인트마다 다시 떨어졌다 올랐다 할 수 있는 값이라 그 이상의 강조
# (굵게/채널 고정 등)는 하지 않는다.
ALERT_THRESHOLD = 80


def _fire_tier(macro_pct, stock_pct):
    both = stock_pct is not None

    def solo(pct):
        if pct is None:
            return 0
        if pct >= 100:
            return 3
        if pct >= ALERT_THRESHOLD:
            return 1
        return 0

    if both and macro_pct >= 100 and stock_pct >= 100:
        return 5, 5, "종가베팅 순간이 왔습니다"
    if both and macro_pct >= ALERT_THRESHOLD and stock_pct >= ALERT_THRESHOLD:
        return 3, 3, "불장!!"
    return solo(macro_pct), solo(stock_pct), None


# --- 실시간 임계값 돌파 알림 (정시 발송과 별개) ---
# 매크로/종목 체크 %가 80% 이상이거나 20% 이하일 때 즉시 텔레그램으로 알린다.
# 각각(매크로, 종목) 방향별(80%↑, 20%↓)로 하루(KST)에 한 번만 보낸다 - 값이
# 내려갔다 다시 올라와도 같은 날엔 재알림하지 않는다 (예전엔 히스테리시스로
# 다시 알렸는데 너무 자주 와서 하루 한 번으로 바꿈). 날짜 기록은 프로세스
# 메모리에만 있어서 재배포되면 초기화됨 (개인용 도구라 감수).
ALERT_LOW_THRESHOLD = 20

_alert_day = {
    "macro": {"high": None, "low": None},
    "stock": {"high": None, "low": None},
}
_alert_state_lock = threading.Lock()


def _update_alert_state(key, pct):
    events = []
    if pct is None:
        return events
    today = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y%m%d")
    with _alert_state_lock:
        sent = _alert_day[key]
        if pct >= ALERT_THRESHOLD and sent["high"] != today:
            sent["high"] = today
            events.append("high_cross")
        if pct <= ALERT_LOW_THRESHOLD and sent["low"] != today:
            sent["low"] = today
            events.append("low_cross")
    return events


def check_threshold_alert():
    # 80%/20% 돌파 알림은 정규장(09:00~15:30)에만 - 이후 NXT 시간대에는 보내지
    # 않는다. (5일선 근접 알림은 별도로 20:00까지.)
    now = datetime.now(timezone.utc) + timedelta(hours=9)
    if not (9 * 60 <= now.hour * 60 + now.minute <= 15 * 60 + 30):
        return {"sent": False, "skipped": "outside_regular_session"}

    macro_pct, _, stock_pct, stock_name, _ = _compute_scores()

    macro_events = _update_alert_state("macro", macro_pct)
    stock_events = _update_alert_state("stock", stock_pct)
    if not macro_events and not stock_events:
        return {"sent": False, "macroPct": macro_pct, "stockPct": stock_pct}

    now = datetime.now(timezone.utc) + timedelta(hours=9)
    lines = [f"⏰ 실시간 알림 · {now.strftime('%m/%d %H:%M')}"]
    for events, pct, label in [
        (macro_events, macro_pct, "매크로 체크"),
        (stock_events, stock_pct, f"종목체크({stock_name})" if stock_pct is not None else "종목 체크"),
    ]:
        for ev in events:
            if ev == "high_cross":
                lines.append(f"🔥 {label} {pct}% - {ALERT_THRESHOLD}% 돌파")
            elif ev == "low_cross":
                lines.append(f"⚠️ {label} {pct}% - {ALERT_LOW_THRESHOLD}% 이하로 하락")

    macro_fire, stock_fire, fire_banner = _fire_tier(macro_pct, stock_pct)
    lines.append("")
    lines.append(f"매크로 체크 {macro_pct}%" + (" " + "🔥" * macro_fire if macro_fire else ""))
    if stock_pct is not None:
        lines.append(f"종목체크({stock_name}) {stock_pct}%" + (" " + "🔥" * stock_fire if stock_fire else ""))
    if fire_banner:
        lines.append(fire_banner)

    text = "\n".join(lines)
    send_telegram_message(text)
    return {"sent": True, "text": text}


# --- 5일선 근접 알림 (09:00~20:00, KRX 거래시간) ---
# 종목이 "5일선 위"에 있다가 5일선 쪽으로 가까워질 때만 알린다 (5일선 아래에서
# 올라오는 경우는 제외 - 사용자가 명시적으로 그건 보고 싶지 않다고 함). 감시
# 종목 목록은 웹페이지 하단에서 추가/삭제 가능 - 메모리에 들고 있다가 바뀔
# 때마다 Render 환경변수(TELEGRAM_MA_WATCH_CODES)에도 써둬서 재배포돼도
# 유지된다 (KIS 토큰 캐싱과 같은 패턴). 히스테리시스 1%p: 5일선 위로 1%
# 이내까지 가까워지면 알리고, 2% 넘게 다시 멀어지거나(또는 5일선 아래로
# 내려가거나) 해야 리셋. 5일선 아래로 내려갔다가 올라오는 건 "위에서 접근"이
# 아니므로, 한 번 2% 넘게 위로 올라간 뒤에야 다시 알린다.
MA_NEAR_PCT = 1.0
MA_RESET_PCT = 2.0

_ma_watch_lock = threading.Lock()
_ma_watch_codes = [c.strip() for c in os.environ.get("TELEGRAM_MA_WATCH_CODES", "000660,036540").split(",") if c.strip()]

_ma_alert_state = {}
_ma_alert_state_lock = threading.Lock()


def get_ma_watch_codes():
    with _ma_watch_lock:
        return list(_ma_watch_codes)


def add_ma_watch_code(code):
    with _ma_watch_lock:
        if code not in _ma_watch_codes:
            _ma_watch_codes.append(code)
        codes = list(_ma_watch_codes)
    _render_set_env_var("TELEGRAM_MA_WATCH_CODES", ",".join(codes))
    return codes


def remove_ma_watch_code(code):
    with _ma_watch_lock:
        if code in _ma_watch_codes:
            _ma_watch_codes.remove(code)
        with _ma_alert_state_lock:
            _ma_alert_state.pop(code, None)
        codes = list(_ma_watch_codes)
    _render_set_env_var("TELEGRAM_MA_WATCH_CODES", ",".join(codes))
    return codes


def _update_ma_near_state(code, diff_pct):
    """diff_pct = (price - ma5) / ma5 * 100. True only when price *comes down
    toward* MA5 from above (a rebound up from below the line must not fire).
    States: None (unknown, e.g. just after a restart), "armed" (was clearly
    above), "fired" (alerted, waiting to get clearly above again), "below"
    (under the line - a bounce back above it doesn't count as approaching
    from above until it has first gone clearly above)."""
    with _ma_alert_state_lock:
        state = _ma_alert_state.get(code)
        if diff_pct <= 0:
            _ma_alert_state[code] = "below"
        elif diff_pct > MA_RESET_PCT:
            _ma_alert_state[code] = "armed"
        elif diff_pct <= MA_NEAR_PCT and state in (None, "armed"):
            _ma_alert_state[code] = "fired"
            return True
    return False


def _kis_current_price(code):
    # 주식현재가 시세: UN = KRX+NXT 통합이라 15:30 이후 NXT 거래 구간도 반영된다.
    data = kis_get(
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        "FHKST01010100",
        {"FID_COND_MRKT_DIV_CODE": "UN", "FID_INPUT_ISCD": code},
    )
    return float(data["output"]["stck_prpr"])


def check_ma_proximity_alert():
    now = datetime.now(timezone.utc) + timedelta(hours=9)
    market_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
    market_close = now.replace(hour=20, minute=0, second=59, microsecond=0)
    if not (market_open <= now <= market_close):
        return {"checked": False}

    today = now.strftime("%Y-%m-%d")
    events = []
    detail = {}
    for code in get_ma_watch_codes():
        try:
            quote = _local_get(f"/api/stock/quote?code={code}")
            hist = _local_get(f"/api/stock/history?code={code}")
            rows = [c for c in hist.get("closes", []) if c.get("close") is not None]
            closes = [c["close"] for c in rows]
            if len(closes) < 5:
                continue
            # 현재가는 한투(통합)에서 받고, 실패하면 네이버 값으로 대신한다.
            # 5일선은 네이버 일봉 종가로 계산하되 오늘 봉은 그 현재가로 바꿔 넣는다.
            source = "kis"
            try:
                price = _kis_current_price(code)
            except Exception:
                price, source = quote["price"], "naver"
            if rows[-1].get("date") == today:
                closes[-1] = price
            else:
                closes.append(price)
            ma5 = _rolling_ma(closes, 5)[-1]
            name = quote.get("name") or code
            diff_pct = (price - ma5) / ma5 * 100
            detail[code] = {"price": price, "ma5": round(ma5, 1), "diffPct": round(diff_pct, 2), "source": source}
            if _update_ma_near_state(code, diff_pct):
                events.append(f"📍 {name} 5일선 근접 - 현재가 {price:,.0f} / 5일선 {ma5:,.0f} (+{diff_pct:.2f}%)")
        except Exception:
            continue

    if not events:
        return {"sent": False, "detail": detail}

    text = "\n".join([f"📍 5일선 근접 알림 · {now.strftime('%m/%d %H:%M')}", "", *events])
    send_telegram_message(text)
    return {"sent": True, "text": text, "detail": detail}


# 신호등별 가중치(%) - 합계 각각 100. index.html의 WEIGHTS와 반드시 같은 값으로
# 유지할 것(화면과 텔레그램의 매크로/종목 체크 %가 어긋나지 않게).
SCORE_WEIGHTS = {
    "macro": {"ndq": 25, "kospiMa5": 20, "kospiDir": 20, "tnx": 15, "wti": 10, "btc": 10},
    "stock": {"program": 20, "liveFlow": 20, "chart": 20, "candle": 15, "krSector": 15, "usSector": 10},
}


def _score_for(signal):
    return {"green": 20, "yellow": 10, "red": 0}.get(signal, 10)


def _weighted_pct(items):
    """items: [(signal, weight)] - 데이터 없는 신호는 아예 넣지 않으면 나머지
    비중으로 다시 계산된다."""
    max_score = sum(w * 20 for _, w in items)
    if not max_score:
        return None
    return round(sum(w * _score_for(s) for s, w in items) / max_score * 100)


def _local_get(path):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", headers={"X-Internal-Token": INTERNAL_TOKEN})
    with urllib.request.urlopen(req, timeout=15) as resp:
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
        worse = have_prev and _rank_index(last, ma_list) > _rank_index(prev_last, prev_ma_list)
        ma5_signal = "yellow" if worse else "green"
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
    signals = [(candle_signal, SCORE_WEIGHTS["stock"]["candle"])]

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
        signals.append((us_signal, SCORE_WEIGHTS["stock"]["usSector"]))
    if kr_signal:
        signals.append((kr_signal, SCORE_WEIGHTS["stock"]["krSector"]))

    closes = [c["close"] for c in hist.get("closes", []) if c.get("close") is not None]
    state = _ma_state(closes)
    chart_signal, rank_text = None, "데이터 부족"
    if state:
        last, ma_list, prev_last, prev_ma_list, have_prev = state
        ma5 = dict(ma_list)["5"]
        if last >= ma5:
            worse = have_prev and _rank_index(last, ma_list) > _rank_index(prev_last, prev_ma_list)
            chart_signal = "yellow" if worse else "green"
        elif have_prev and _rank_index(last, ma_list) < _rank_index(prev_last, prev_ma_list):
            chart_signal = "yellow"
        else:
            chart_signal = "red"
        if have_prev:
            rank_text = _describe_rank_change(last, ma_list, prev_last, prev_ma_list)
        rank_chain = _format_rank_chain(last, ma_list, name)
        signals.append((chart_signal, SCORE_WEIGHTS["stock"]["chart"]))

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
        signals.append((live_signal, SCORE_WEIGHTS["stock"]["liveFlow"]))
        lines.append(
            f"{SIGNAL_EMOJI[live_signal]} 수급: 외국인 {fmt_eok(live.get('foreignQty', 0) * price)} · "
            f"기관 {fmt_eok(live.get('institutionQty', 0) * price)}"
        )

    if prog and not prog.get("error"):
        amt = prog.get("netAmount", 0)
        prog_signal = "green" if amt > 0 else ("red" if amt < 0 else "yellow")
        signals.append((prog_signal, SCORE_WEIGHTS["stock"]["program"]))
        lines.append(f"{SIGNAL_EMOJI[prog_signal]} 프로그램매매: {fmt_eok(amt)}")

    score_pct = _weighted_pct(signals)
    return score_pct, name, lines


def fmt_eok(won):
    return f"{'+' if won >= 0 else ''}{round(won / 1e8):,}억"


def fmt_eok_abs(won):
    return f"{round(won / 1e8):,}억"


def _market_breadth_lines():
    try:
        data = _local_get("/api/market/breadth")
        k, q = data["kospi"], data["kosdaq"]
    except Exception:
        return ["등락 종목수 데이터 없음"]

    def fmt(b):
        bigger, smaller = ("상승", "하락") if b["up"] >= b["down"] else ("하락", "상승")
        return f"{bigger} > {smaller} ({b['up']},{b['flat']},{b['down']})"

    return [
        f"코스피 등락: {fmt(k)}",
        f"코스닥 등락: {fmt(q)}",
    ]


def _volume_rank_lines():
    try:
        rows = _local_get("/api/market/volume-rank").get("rows") or []
    except Exception:
        rows = []
    if not rows:
        return ["📊 거래대금 순위 데이터 없음"]
    lines = ["📊 거래대금 순위 TOP10"]
    for r in rows:
        arrow = "▲" if r.get("dir") == "up" else ("▼" if r.get("dir") == "down" else "-")
        pct = abs(r.get("changePct", 0))
        lines.append(f"{r['rank']}. {r['name']} {fmt_eok_abs(r['tradingValue'])} ({arrow}{pct:.2f}%)")
    return lines


def _compute_scores():
    macro_signals = []
    macro_lines = []
    for key, label, symbol, sentiment, prefix, decimals in [
        ("tnx", "미국채10Y", "^TNX", "inverse", "", 3),
        ("wti", "WTI", "CL=F", "inverse", "$", 2),
        ("ndq", "나스닥100선물", "NQ=F", "normal", "$", 0),
        ("btc", "비트코인", "BTC-USD", "normal", "$", 0),
    ]:
        try:
            sig, line = _macro_instrument_line(label, symbol, sentiment, prefix, decimals)
            macro_signals.append((sig, SCORE_WEIGHTS["macro"][key]))
            macro_lines.append(line)
        except Exception:
            macro_lines.append(f"⚪ {label} 데이터 없음")

    try:
        dir_sig, ma5_sig, kospi_lines = _kospi_section()
        macro_signals.extend([(dir_sig, SCORE_WEIGHTS["macro"]["kospiDir"]), (ma5_sig, SCORE_WEIGHTS["macro"]["kospiMa5"])])
        macro_lines.extend(kospi_lines)
    except Exception as e:
        macro_lines.append(f"⚪ 코스피 데이터 없음 ({e})")

    macro_lines.extend(_market_breadth_lines())

    macro_pct = _weighted_pct(macro_signals) or 0

    try:
        stock_pct, stock_name, stock_lines = _stock_section(TELEGRAM_SUMMARY_STOCK_CODE)
    except Exception as e:
        stock_pct, stock_name, stock_lines = None, TELEGRAM_SUMMARY_STOCK_CODE, [f"⚪ 종목 체크 데이터 없음 ({e})"]

    return macro_pct, macro_lines, stock_pct, stock_name, stock_lines


def build_dashboard_summary_text():
    now = datetime.now(timezone.utc) + timedelta(hours=9)
    macro_pct, macro_lines, stock_pct, stock_name, stock_lines = _compute_scores()

    macro_fire, stock_fire, fire_banner = _fire_tier(macro_pct, stock_pct)

    header = f"📊 종가베팅 체크리스트 · {now.strftime('%m/%d %H:%M')}"

    summary_lines = [f"매크로 체크 {macro_pct}%" + (" " + "🔥" * macro_fire if macro_fire else "")]
    if stock_pct is not None:
        summary_lines.append(f"종목체크({stock_name}) {stock_pct}%" + (" " + "🔥" * stock_fire if stock_fire else ""))
    else:
        summary_lines.append("종목 체크 데이터 없음")
    if fire_banner:
        summary_lines.append(fire_banner)

    # 거래대금 순위는 KIS 호출이 두 번(KRX+NXT) 더 들어가는 무거운 섹션이라,
    # 하루 중 장 시작(09:10)과 마감 전(15:10) 체크포인트에만 같이 보낸다.
    blocks = [macro_lines, stock_lines]
    if now.hour in (9, 15):
        blocks.append(_volume_rank_lines())

    parts = [header, *summary_lines]
    for block in blocks:
        parts.append("")
        parts.extend(block)
    parts.append("")
    parts.append("----")
    return "\n".join(parts)


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
