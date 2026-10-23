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
import rsirule
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


def _fetch_market_trading_value_history(market, days=8):
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
    # 지수 등락과 신용융자 잔고는 거래대금 표에 같이 보여주는 부가 정보 - 하나가 실패해도 표는 나오게 따로 감싼다.
    try:
        result["index"] = {"kospi": _fetch_index_closes("KOSPI"), "kosdaq": _fetch_index_closes("KOSDAQ")}
    except Exception as e:
        result["index"] = {"error": str(e)}
    try:
        result["credit"] = _fetch_market_credit()
    except Exception as e:
        result["credit"] = {"error": str(e)}
    with _market_trading_value_cache_lock:
        _market_trading_value_cache["data"] = result
        _market_trading_value_cache["ts"] = now
    return result


# --- 지수 일봉 종가 (네이버 siseJson) / 신용융자 잔고 (금융투자협회 FreeSIS) ---
# 신용융자 잔고는 코스피(유가증권)/코스닥을 나눠 공표되고 하루 늦게(T+1) 나온다. FreeSIS 화면이 쓰는 조회
# 주소(키 불필요)를 호출한다 - 공식 API가 아니라 바뀔 수 있어 실패하면 표에서 "—"로 보이게 둔다.
# 응답 컬럼(추정, 합계 검증: TMPV2 = TMPV3 + TMPV4): TMPV1 날짜, TMPV2 합계, TMPV3 유가증권(코스피),
# TMPV4 코스닥 — 단위 백만원. 네이버 증시자금동향의 신용잔고(합계)와 약 1% 이내로 비슷하게 움직임.
KOFIA_CREDIT_URL = "https://freesis.kofia.or.kr/meta/getMetaDataList.do"
CREDIT_CACHE_TTL = 1800
INDEX_CLOSES_CACHE_TTL = 60
_credit_cache = {"data": None, "ts": 0}
_index_closes_cache = {}
_market_extra_lock = threading.Lock()


def _fetch_index_closes(symbol, days=40):
    """{YYYYMMDD: 종가} - 오늘(장중)은 현재가. 네이버 siseJson 일봉."""
    now = time.time()
    with _market_extra_lock:
        hit = _index_closes_cache.get(symbol)
        if hit and now - hit[0] < INDEX_CLOSES_CACHE_TTL:
            return hit[1]
    end = datetime.now(timezone.utc) + timedelta(hours=9)
    start = end - timedelta(days=days)
    url = ("https://api.finance.naver.com/siseJson.naver?symbol=%s&requestType=1&timeframe=day&startTime=%s&endTime=%s"
           % (symbol, start.strftime("%Y%m%d"), end.strftime("%Y%m%d")))
    req = urllib.request.Request(url, headers=NAVER_HEADERS)
    with urllib.request.urlopen(req, timeout=8) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    import ast
    rows = ast.literal_eval(body.strip().replace("null", "None"))[1:]
    out = {str(r[0]): float(r[4]) for r in rows if r[4] is not None}
    with _market_extra_lock:
        _index_closes_cache[symbol] = (now, out)
    return out


def _fetch_market_credit(days=40):
    """{YYYYMMDD: {"kospi": 원, "kosdaq": 원}} - 신용융자 잔고."""
    now = time.time()
    with _market_extra_lock:
        if _credit_cache["data"] and now - _credit_cache["ts"] < CREDIT_CACHE_TTL:
            return _credit_cache["data"]
    end = datetime.now(timezone.utc) + timedelta(hours=9)
    start = end - timedelta(days=days)
    body = json.dumps({"dmSearch": {
        "tmpV40": "1000000", "tmpV41": "1", "tmpV1": "D",
        "tmpV45": start.strftime("%Y%m%d"), "tmpV46": end.strftime("%Y%m%d"),
        "OBJ_NM": "STATSCU0100000070BO",
    }}).encode()
    req = urllib.request.Request(KOFIA_CREDIT_URL, data=body, headers={
        "Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
        "Referer": "https://freesis.kofia.or.kr/", "Origin": "https://freesis.kofia.or.kr",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    out = {}
    for r in data.get("ds1") or []:
        try:
            out[str(r["TMPV1"])] = {"kospi": int(r["TMPV3"]) * 1_000_000, "kosdaq": int(r["TMPV4"]) * 1_000_000}
        except (KeyError, TypeError, ValueError):
            continue
    if not out:
        raise ValueError("신용융자 잔고 데이터가 비어 있습니다")
    with _market_extra_lock:
        _credit_cache["data"], _credit_cache["ts"] = out, now
    return out


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


def _flow_trend_row_complete(row):
    """개인(8000)·외국인(9000) 항목이 모두 있는 행만 완성된 행으로 본다. 네이버가 최신 행을 채우는 중에
    일부 투자자 항목이 빠진 채 내려오면 0으로 읽혀 차트 끝점이 튄다(2026-10-19 15:06 NXT 개인·외국인이 0으로
    읽혀 합계가 KRX 단독 값으로 떨어짐)."""
    gubuns = {a.get("investorGubun") for a in row.get("netAmounts", [])}
    return "8000" in gubuns and "9000" in gubuns


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
    # 시각 오름차순으로 넣으면서 직전 값과 비교해 이상한 행(누적값이 갑자기 0으로 떨어지는 행)은 건너뛴다.
    # 누적 순매수가 정확히 0으로 되돌아오는 일은 사실상 없으므로, 개인/외국인이 0이 됐는데 직전에는 10억원
    # 넘게 있었다면 불완전한 행으로 보고 다음 새로고침(이미 받은 최신 20행을 다시 덮어씀)에서 바로잡는다.
    for r in sorted((r for r in rows if r.get("time")), key=lambda r: (r["bizdate"], r["time"])):
        if not _flow_trend_row_complete(r):
            continue
        key = (r["bizdate"], r["time"])
        vals = _flow_trend_row_values(r)
        earlier = [k for k in store if k[0] == key[0] and k[1] < key[1]]
        if earlier:
            prev = store[max(earlier)]
            if any(vals[i] == 0 and abs(prev[i]) > 1_000_000_000 for i in (0, 1)):
                continue
        store[key] = vals


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


# --- 거래대금 TOP10 종목의 수급 체크(외국인·기관·프로그램 순매수) ---
# "조건 충족" = 당일 상승 + 외국인 순매수 + 기관 순매수 + 프로그램 순매수가 모두 +. 값은 종목 체크리스트가
# 쓰는 것과 같은 한투 TR(외국인/기관 = 투자자 가집계 investor-trend-estimate, 없으면 종목별 일별 수급의
# 오늘 행 / 프로그램 = program-trade-by-stock 통합). TOP10이 종목마다 호출 2번(=20번)이라 30초 캐시.
VOLUME_RANK_FLOWS_TTL = 30
_volume_rank_flows_cache = {"data": None, "ts": 0, "key": None}
_volume_rank_flows_lock = threading.Lock()


def _stock_flow_signs(code):
    """-> {"foreignQty","institutionQty","programNetAmount"} (없으면 None 값). 한 종목 호출 2번."""
    out = {"foreignQty": None, "institutionQty": None, "programNetAmount": None}
    try:
        data = kis_get("/uapi/domestic-stock/v1/quotations/investor-trend-estimate", "HHPTJ04160200",
                       {"MKSC_SHRN_ISCD": code})
        rows = data.get("output2") or []
        if rows:
            latest = max(rows, key=lambda r: int(r.get("bsop_hour_gb", 0) or 0))
            out["foreignQty"] = int(latest.get("frgn_fake_ntby_qty", 0))
            out["institutionQty"] = int(latest.get("orgn_fake_ntby_qty", 0))
    except Exception:
        pass
    if out["foreignQty"] is None:
        # 가집계가 없는 시간(장 마감 후 등)에는 종목별 일별 수급의 오늘 행으로 대신한다.
        try:
            data = kis_get("/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily", "FHPTJ04160001",
                           {"FID_COND_MRKT_DIV_CODE": "UN", "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": "",
                            "FID_ORG_ADJ_PRC": "", "FID_ETC_CLS_CODE": ""})
            today = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y%m%d")
            row = next((r for r in (data.get("output2") or []) if r.get("stck_bsop_date") == today), None)
            if row:
                out["foreignQty"] = int(row.get("frgn_ntby_qty", 0))
                out["institutionQty"] = int(row.get("orgn_ntby_qty", 0))
        except Exception:
            pass
    try:
        data = kis_get("/uapi/domestic-stock/v1/quotations/program-trade-by-stock", "FHPPG04650101",
                       {"FID_COND_MRKT_DIV_CODE": "UN", "FID_INPUT_ISCD": code})
        rows = data.get("output") or []
        if rows:
            out["programNetAmount"] = int(rows[0].get("whol_smtn_ntby_tr_pbmn", 0))
    except Exception:
        pass
    return out


def _fetch_volume_rank_flows():
    rank = _fetch_volume_rank()
    key = tuple(r["code"] for r in rank)
    now = time.time()
    with _volume_rank_flows_lock:
        c = _volume_rank_flows_cache
        if c["data"] and c["key"] == key and now - c["ts"] < VOLUME_RANK_FLOWS_TTL:
            return c["data"]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(lambda r: _stock_flow_signs(r["code"]), rank))
    flows = {}
    for r, f in zip(rank, results):
        vals = (f["foreignQty"], f["institutionQty"], f["programNetAmount"])
        known = all(v is not None for v in vals)
        flows[r["code"]] = {
            **f,
            "up": r["changePct"] > 0,
            "known": known,
            "pass": bool(known and r["changePct"] > 0 and all(v > 0 for v in vals)),
        }
    data = {"flows": flows, "asOf": (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%H:%M:%S")}
    with _volume_rank_flows_lock:
        _volume_rank_flows_cache.update(data=data, ts=now, key=key)
    return data


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


# --- 업종 월별 수익률 순위 (/sector 페이지) ---
# 한투 "국내업종 일자별지수"(FHPUP02120000, 월봉 100개)로 업종지수 월 종가를 모아 최근 SECTOR_MONTHS개월 수익률을 만든다.
# 업종 구분은 KRX 업종지수(코스피 23개, 코스닥 19개). 종합/대형주 같은 규모 지수와 "제조"(여러 업종의 합) 같은 합계 지수는 뺐다.
# 한투 코스닥 업종 코드는 코스피와 앞 자리만 다른 별칭이 섞여 있어서(예: 1005는 코스피 음식료와 같은 값), 한투 업종 목록에
# 실제로 나오는 코드만 쓴다.
SECTOR_MONTHS = 36
SECTOR_DEFS = {
    "KOSPI": [("0005", "음식료·담배"), ("0006", "섬유·의류"), ("0007", "종이·목재"), ("0008", "화학"), ("0009", "제약"),
              ("0010", "비금속"), ("0011", "금속"), ("0012", "기계·장비"), ("0013", "전기·전자"), ("0014", "의료·정밀기기"),
              ("0015", "운송장비·부품"), ("0016", "유통"), ("0017", "전기·가스"), ("0018", "건설"), ("0019", "운송·창고"),
              ("0020", "통신"), ("0021", "금융"), ("0024", "증권"), ("0025", "보험"), ("0026", "일반서비스"),
              ("0028", "부동산"), ("0029", "IT 서비스"), ("0030", "오락·문화")],
    "KOSDAQ": [("1006", "일반서비스"), ("1010", "건설"), ("1011", "유통"), ("1013", "운송·창고"), ("1014", "금융"),
               ("1015", "오락·문화"), ("1019", "음식료·담배"), ("1020", "섬유·의류"), ("1021", "종이·목재"),
               ("1022", "출판·매체복제"), ("1023", "화학"), ("1024", "제약"), ("1025", "비금속"), ("1026", "금속"),
               ("1027", "기계·장비"), ("1028", "전기·전자"), ("1029", "의료·정밀기기"), ("1030", "운송장비·부품"),
               ("1031", "기타제조")],
}
SECTOR_CACHE_TTL = 6 * 3600
_sector_cache = {"data": None, "ts": 0.0, "building": False, "error": None}
_sector_lock = threading.Lock()


def _fetch_sector_month_closes(code):
    """{'YYYYMM': 월 종가(이번 달은 최신가)} - 한투 월봉(최신순 100개)."""
    today = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y%m%d")
    data = kis_get("/uapi/domestic-stock/v1/quotations/inquire-index-daily-price", "FHPUP02120000",
                   {"FID_PERIOD_DIV_CODE": "M", "FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code,
                    "FID_INPUT_DATE_1": today})
    out = {}
    for r in data.get("output2") or []:
        d, c = r.get("stck_bsop_date"), r.get("bstp_nmix_prpr")
        if d and c and d[:6] not in out:
            out[d[:6]] = float(c)
    return out


def _build_sector_data():
    now = datetime.now(timezone.utc) + timedelta(hours=9)
    fetched = {}
    for market, defs in SECTOR_DEFS.items():
        for code, name in defs:
            try:
                fetched[(market, code)] = _fetch_sector_month_closes(code)
            except Exception as e:
                print(f"sector {market} {code} failed: {e}")
            time.sleep(0.12)
    # 기준 월 목록: 월봉이 가장 길게 있는 업종의 최근 SECTOR_MONTHS개월
    longest = max((sorted(c) for c in fetched.values()), key=len, default=[])
    if len(longest) < SECTOR_MONTHS + 1:
        raise ValueError("업종 데이터를 만들지 못했습니다")
    months = longest[-(SECTOR_MONTHS + 1):][1:]
    markets = {}
    for market, defs in SECTOR_DEFS.items():
        rows = []
        for code, name in defs:
            closes = fetched.get((market, code))
            if not closes:
                continue
            keys = sorted(closes)[-(SECTOR_MONTHS + 1):]
            real = len(keys) - 1
            # 2024-08 KRX 업종 개편으로 새로 생긴 업종(부동산·IT 서비스·오락문화)은 월봉이 28개뿐이라 앞쪽 달은 비워 둔다.
            if real < 12 or keys[1:] != months[-real:]:
                print(f"sector {market} {code} {name}: 월봉 {real}개/월 구간 불일치로 제외")
                continue
            rets = [None] * (SECTOR_MONTHS - real) + [
                round((closes[keys[i]] / closes[keys[i - 1]] - 1) * 100, 2) for i in range(1, len(keys))]
            rows.append({"code": code, "name": name, "rets": rets, "close": closes[keys[-1]]})
        markets[market] = rows
    if not any(markets.values()):
        raise ValueError("업종 데이터를 만들지 못했습니다")
    return {"asOf": now.strftime("%Y-%m-%d %H:%M"), "months": months, "partialMonth": now.strftime("%Y%m"),
            "markets": markets}


def _sector_refresh_async():
    with _sector_lock:
        if _sector_cache["building"]:
            return
        _sector_cache["building"] = True

    def work():
        try:
            data = _build_sector_data()
            with _sector_lock:
                _sector_cache.update(data=data, ts=time.time(), error=None)
        except Exception as e:
            with _sector_lock:
                _sector_cache["error"] = str(e)[:200]
            print(f"sector build failed: {e}")
        finally:
            with _sector_lock:
                _sector_cache["building"] = False

    threading.Thread(target=work, daemon=True).start()


def sector_monthly_data():
    """(data, 상태메시지). 캐시가 있으면 바로 주고, 오래됐으면 뒤에서 갱신한다."""
    with _sector_lock:
        data, ts, building, err = (_sector_cache[k] for k in ("data", "ts", "building", "error"))
    if data is not None:
        if time.time() - ts > SECTOR_CACHE_TTL and not building:
            _sector_refresh_async()
        return data, ""
    if not building:
        _sector_refresh_async()
    return None, err or "업종 데이터를 모으는 중입니다 (처음 한 번은 30초쯤 걸립니다)"


# --- 세분 업종(네이버 79개 업종) 월별 수익률 ---
# KRX 업종지수는 큰 단위(전기·전자 안에 반도체와 가전이 같이 있음)라서, 책처럼 "반도체/화장품/조선" 단위로 보려고
# 네이버 금융의 79개 업종(WICS 분류와 같은 이름)을 쓴다. 네이버는 업종지수의 과거 수익률을 주지 않으므로
# 업종별 시가총액 상위 FINE_TOP_N개 종목(우선주·스팩 제외)의 월 수익률을 동일가중으로 평균해 업종 수익률을 만든다.
# 각 종목의 월 수익률도 같이 보내서 화면에서 "그 달 대표 상승 종목"을 보여 준다. 현재 구성 종목 기준이라 생존 편향이 조금 있다.
FINE_TOP_N = 10
FINE_MIN_STOCKS = 3
FINE_RET_CLIP = (-50.0, 150.0)      # 종목 한 개의 극단값이 업종 평균을 흔들지 않게 자른다(%)
FINE_SKIP_GROUPS = {25}              # '기타'(1,500여 종목 잡다한 묶음)
FINE_CACHE_TTL = 12 * 3600
NAVER_M_INDUSTRY_URL = "https://m.stock.naver.com/api/stocks/industry"
_fine_cache = {"data": None, "ts": 0.0, "building": False, "error": None, "progress": ""}
_fine_lock = threading.Lock()


def _naver_get_json(url, retries=2):
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception:
            if attempt == retries:
                raise
            time.sleep(0.5 * (attempt + 1))


def _fine_top_stocks(group_no, total):
    """업종 구성 종목 중 시가총액 상위 FINE_TOP_N개(보통주만)."""
    stocks, page = [], 1
    while len(stocks) < total and page <= 5:
        data = _naver_get_json(f"{NAVER_M_INDUSTRY_URL}/{group_no}?page={page}&pageSize=100")
        got = data.get("stocks") or []
        stocks.extend(got)
        if len(got) < 100:
            break
        page += 1
    out = []
    for s in stocks:
        code, name = str(s.get("itemCode") or ""), str(s.get("stockName") or "")
        if s.get("stockEndType") != "stock" or not re.fullmatch(r"\d{5}0", code) or "스팩" in name:
            continue                # 우선주(코드 끝자리가 0이 아님)·코넥스·스팩 제외
        try:
            mv = float(str(s.get("marketValue") or "0").replace(",", ""))
        except ValueError:
            mv = 0.0
        out.append((mv, code, name))
    out.sort(reverse=True)
    return [(code, name) for _, code, name in out[:FINE_TOP_N]]


def _fine_month_closes(code, start_ym):
    start = start_ym + "01"
    end = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y%m%d")
    url = f"{NAVER_SISEJSON_URL}?symbol={code}&requestType=1&startTime={start}&endTime={end}&timeframe=month"
    req = urllib.request.Request(url, headers=NAVER_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    out = {}
    for r in ast.literal_eval(body.strip())[1:]:
        out[str(r[0])[:6]] = float(r[4])
    return out


def _calendar_months(n):
    """오늘이 속한 달부터 거꾸로 n개월(오름차순 'YYYYMM')."""
    now = datetime.now(timezone.utc) + timedelta(hours=9)
    y, m, out = now.year, now.month, []
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


def _build_fine_data():
    ext = _calendar_months(SECTOR_MONTHS + 1)      # 첫 달 수익률을 구하려면 그 직전 달 종가가 필요해서 한 달 더 앞까지
    months = ext[1:]
    prev_of = {ext[i]: ext[i - 1] for i in range(1, len(ext))}
    groups = _naver_get_json(f"{NAVER_M_INDUSTRY_URL}?page=1&pageSize=100").get("groups") or []
    industries, done = [], 0
    for g in groups:
        no, name, total = g.get("no"), g.get("name"), int(g.get("totalCount") or 0)
        done += 1
        if no in FINE_SKIP_GROUPS or total < FINE_MIN_STOCKS:
            continue
        with _fine_lock:
            _fine_cache["progress"] = f"{done}/{len(groups)} {name}"
        try:
            top = _fine_top_stocks(no, total)
        except Exception as e:
            print(f"fine group {no} {name} failed: {e}")
            continue
        stock_rows = []
        for code, sname in top:
            try:
                closes = _fine_month_closes(code, ext[0])
            except Exception as e:
                print(f"fine stock {code} failed: {e}")
                continue
            time.sleep(0.03)
            rets = []
            for mth in months:
                c1, c0 = closes.get(mth), closes.get(prev_of[mth])
                rets.append(round((c1 / c0 - 1) * 100, 2) if c1 and c0 else None)
            if sum(1 for r in rets if r is not None) >= 6:
                stock_rows.append({"code": code, "name": sname, "rets": rets})
        if len(stock_rows) < FINE_MIN_STOCKS:
            continue
        lo, hi = FINE_RET_CLIP
        ind_rets = []
        for i in range(len(months)):
            vals = [min(hi, max(lo, s["rets"][i])) for s in stock_rows if s["rets"][i] is not None]
            ind_rets.append(round(sum(vals) / len(vals), 2) if len(vals) >= FINE_MIN_STOCKS else None)
        industries.append({"no": no, "name": name, "total": total, "n": len(stock_rows), "rets": ind_rets, "stocks": stock_rows})
    if not industries:
        raise ValueError("세분 업종 데이터를 만들지 못했습니다")
    now = datetime.now(timezone.utc) + timedelta(hours=9)
    return {"asOf": now.strftime("%Y-%m-%d %H:%M"), "months": months, "partialMonth": now.strftime("%Y%m"),
            "topN": FINE_TOP_N, "industries": industries}


def _fine_refresh_async():
    with _fine_lock:
        if _fine_cache["building"]:
            return
        _fine_cache["building"] = True

    def work():
        try:
            data = _build_fine_data()
            with _fine_lock:
                _fine_cache.update(data=data, ts=time.time(), error=None, progress="")
        except Exception as e:
            with _fine_lock:
                _fine_cache["error"] = str(e)[:200]
            print(f"fine sector build failed: {e}")
        finally:
            with _fine_lock:
                _fine_cache["building"] = False

    threading.Thread(target=work, daemon=True).start()


def fine_sector_data():
    with _fine_lock:
        data, ts, building, err, prog = (_fine_cache[k] for k in ("data", "ts", "building", "error", "progress"))
    if data is not None:
        if time.time() - ts > FINE_CACHE_TTL and not building:
            _fine_refresh_async()
        return data, ""
    if not building:
        _fine_refresh_async()
    return None, err or ("세분 업종을 모으는 중입니다" + (f" ({prog})" if prog else "") + " — 처음 한 번은 2~3분 걸립니다")


# 종목 일별 수급(최근 7거래일 표)은 "오늘을 뺀 지난 거래일"만 담기 때문에 하루 안에는 값이 변하지 않는다
# (어제 값은 어제 20시 NXT 마감 뒤 확정). 그래서 KST 날짜가 바뀔 때까지 종목별로 메모리에 보관하고,
# 한투 호출(1.3초)은 종목당 하루 한 번만 한다. 한투 성공 응답만 저장(네이버 대체 응답은 저장 안 함).
# 서버가 재시작되면 비워지고 첫 요청 때 다시 채운다(Render 디스크는 재배포 때 지워져 파일 저장은 안 함).
_investors_day_cache = {}   # code -> {"date": "YYYYMMDD(KST)", "data": {...}}
_investors_day_lock = threading.Lock()


def _investors_kis_cached(code, fetch):
    today = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y%m%d")
    with _investors_day_lock:
        hit = _investors_day_cache.get(code)
        if hit and hit["date"] == today:
            return hit["data"]
    data = fetch(code)
    if data.get("source") == "kis":
        with _investors_day_lock:
            _investors_day_cache[code] = {"date": today, "data": data}
    return data


# 장 마감 후(15:40~) 종목 "오늘 수급": 장중 가집계(investor-trend-estimate)는 KRX 정규장 기준이라 NXT 체결이 빠지고
# 외국인 부호가 키움(통합 일별)과 반대로 나오기도 한다. 그래서 15:40 이후에는 일별 투자자(통합 UN)의 오늘 행을 쓴다.
# 이 값은 NXT가 끝나는 20:00까지 조금씩 바뀌는 잠정치다. 한투 호출이 1.3초라 30초 캐시 + 오래된 값 먼저 응답(백그라운드 갱신).
INVESTOR_TODAY_FRESH = 30
INVESTOR_TODAY_STALE_OK = 600
_investor_today_cache = {}   # code -> {"ts", "date", "row"}
_investor_today_lock = threading.Lock()
_investor_today_refreshing = set()


def _investor_today_fetch(code):
    data = kis_get("/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily", "FHPTJ04160001",
                   {"FID_COND_MRKT_DIV_CODE": "UN", "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": "",
                    "FID_ORG_ADJ_PRC": "", "FID_ETC_CLS_CODE": ""})
    rows = data.get("output2") or []
    today = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y%m%d")
    row = next((r for r in rows[:2] if r.get("stck_bsop_date") == today), None)
    entry = {"ts": time.time(), "date": today, "row": row}
    with _investor_today_lock:
        _investor_today_cache[code] = entry
        _investor_today_refreshing.discard(code)
    return entry


def investor_today_row(code):
    """오늘 일별 수급 행(없으면 None). 오래된 캐시가 있으면 먼저 돌려주고 뒤에서 갱신한다."""
    today = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y%m%d")
    now = time.time()
    with _investor_today_lock:
        hit = _investor_today_cache.get(code)
        if hit and hit["date"] == today:
            age = now - hit["ts"]
            if age < INVESTOR_TODAY_FRESH:
                return hit["row"]
            if age < INVESTOR_TODAY_STALE_OK:
                if code not in _investor_today_refreshing:
                    _investor_today_refreshing.add(code)
                    threading.Thread(target=lambda: _safe(_investor_today_fetch, code), daemon=True).start()
                return hit["row"]
    return _investor_today_fetch(code)["row"]


def _safe(fn, *a):
    try:
        fn(*a)
    except Exception:
        with _investor_today_lock:
            _investor_today_refreshing.clear()


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
        elif self.path.startswith("/api/rsi-rule/add"):
            self.handle_rsi_watch_add()
        elif self.path.startswith("/api/rsi-rule/remove"):
            self.handle_rsi_watch_remove()
        elif self.path.startswith("/api/rsi-rule/watch"):
            self.handle_rsi_watch_get()
        elif self.path.startswith("/api/ops/rsi-rule"):
            self.handle_ops_rsi_rule()
        elif self.path.startswith("/api/watchlist/add"):
            self.handle_watchlist_add()
        elif self.path.startswith("/api/watchlist/remove"):
            self.handle_watchlist_remove()
        elif self.path.startswith("/api/watchlist"):
            self.handle_watchlist_get()
        elif self.path.startswith("/api/market/volume-rank-flows"):
            self.handle_volume_rank_flows()
        elif self.path.startswith("/api/market/volume-rank"):
            self.handle_volume_rank()
        elif self.path.startswith("/api/kospi/flow-trend"):
            self.handle_kospi_flow_trend()
        elif self.path.startswith("/api/market/trading-value"):
            self.handle_market_trading_value()
        elif self.path.startswith("/api/market/breadth"):
            self.handle_market_breadth()
        elif self.path.startswith("/api/ops/score-snapshot"):
            self.handle_ops_score_snapshot()
        elif self.path.startswith("/api/ops/stock-flow"):
            self.handle_ops_stock_flow()
        elif self.path.startswith("/api/ops/regime"):
            self.handle_ops_regime()
        elif self.path.startswith("/api/ops/timing"):
            self.handle_ops_timing()
        elif self.path.startswith("/api/ops/kis-minute"):
            self.handle_ops_kis_minute()
        elif self.path.startswith("/api/ops/kis-income"):
            self.handle_ops_kis_income()
        elif self.path.startswith("/api/ops/kis-raw"):
            self.handle_ops_kis_raw()
        elif self.path.startswith("/api/ops/sectors-fine"):
            if not TELEGRAM_SUMMARY_KEY or self.query_param("key") != TELEGRAM_SUMMARY_KEY:
                self.send_json(403, {"error": "forbidden"})
            else:
                self.handle_sectors_fine()
        elif self.path.startswith("/api/ops/sectors"):
            # 점검용: /sector 페이지가 쓰는 업종 월별 수익률을 로그인 없이 키로 확인
            if not TELEGRAM_SUMMARY_KEY or self.query_param("key") != TELEGRAM_SUMMARY_KEY:
                self.send_json(403, {"error": "forbidden"})
            else:
                self.handle_sectors_monthly()
        elif self.path.startswith("/api/sectors/monthly"):
            self.handle_sectors_monthly()
        elif self.path.startswith("/api/sectors/fine"):
            self.handle_sectors_fine()
        elif self.path.startswith("/api/leader"):
            self.handle_leader()
        elif urllib.parse.urlparse(self.path).path in ("/sector", "/sector.html"):
            self.path = "/sector.html"
            super().do_GET()
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
                self.send_json(200, _investors_kis_cached(code, self._fetch_investors_kis))
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
        now_kst = datetime.now(timezone.utc) + timedelta(hours=9)
        if now_kst.weekday() < 5 and now_kst.hour * 60 + now_kst.minute >= 15 * 60 + 40:
            try:
                row = investor_today_row(code)
            except Exception:
                row = None
            if row:
                self.send_json(200, {
                    "source": "daily",
                    "checkpoint": "daily",
                    "foreignQty": int(row.get("frgn_ntby_qty", 0)),
                    "institutionQty": int(row.get("orgn_ntby_qty", 0)),
                    "sumQty": int(row.get("frgn_ntby_qty", 0)) + int(row.get("orgn_ntby_qty", 0)),
                    "individualQtyEstimated": int(row.get("prsn_ntby_qty", 0)),   # 일별은 개인 실제값이 있다
                    "amountsM": {"individual": row.get("prsn_ntby_tr_pbmn"), "foreign": row.get("frgn_ntby_tr_pbmn"),
                                 "institution": row.get("orgn_ntby_tr_pbmn")},
                })
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

    def handle_volume_rank_flows(self):
        if not KIS_APP_KEY or not KIS_APP_SECRET:
            self.send_json(503, {"error": "KIS API 키가 설정되지 않았습니다"})
            return
        try:
            self.send_json(200, _fetch_volume_rank_flows())
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
        # 서버 안 스케줄러(_summary_scheduler)가 이미 이 시각 요약을 보냈으면(GitHub 크론이 늦게 와도)
        # 중복 발송하지 않는다. 수동 테스트는 force=1로 우회(이 경우 시각 기록도 안 남김).
        force = self.query_param("force") == "1"
        if not force:
            now = datetime.now(timezone.utc) + timedelta(hours=9)
            slot, fresh = claim_summary_slot(now, window=150)
            if slot and not fresh:
                self.send_json(200, {"ok": True, "skipped": "already_sent", "slot": slot})
                return
            if not slot:
                # 어느 정시 슬롯에도 속하지 않는 시각의 자동 호출(GitHub 크론이 몇 시간씩 늦게 도는 경우,
                # 2026-10-13 19:40 호출이 23:04에 도착)은 보내지 않는다. 수동 테스트는 force=1.
                self.send_json(200, {"ok": True, "skipped": "outside_schedule"})
                return
        try:
            send_telegram_message(build_dashboard_summary_text())
            self.send_json(200, {"ok": True})
        except Exception as e:
            if not force and slot and fresh:
                release_summary_slot(now, slot)
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

    def handle_ops_score_snapshot(self):
        # 승률 검증용 점수 기록: 매크로/종목 체크 점수를 한 줄로 돌려준다(저장은 GitHub Actions가
        # data 브랜치 CSV에 붙임 - Render 무료 서버는 파일이 사라지므로 서버에 저장하지 않는다).
        if not TELEGRAM_SUMMARY_KEY or self.query_param("key") != TELEGRAM_SUMMARY_KEY:
            self.send_json(403, {"error": "forbidden"})
            return
        if not is_krx_trading_day():
            self.send_json(200, {"ok": True, "skipped": "market_holiday"})
            return
        try:
            slot = self.query_param("slot") or ""
            today = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d")
            # 서버 스케줄러가 15:10/19:40에 정시 계산해 둔 것이 있으면 그것을 돌려준다
            # (GitHub 크론이 늦게 와도 기록 시각이 어긋나지 않게).
            row = _score_snap.get((today, slot)) or build_score_snapshot(slot)
            self.send_json(200, {"ok": True, "row": row})
        except Exception as e:
            self.send_json(502, {"error": str(e)})

    def handle_ops_timing(self):
        # 점검용: 종베가 새로고침 때 부르는 종목 API들의 서버 응답시간(ms). 각 API를 1회 순차 호출(콜드),
        # 다시 1회(캐시), 마지막으로 브라우저처럼 6개를 동시에 호출한 총 시간을 돌려준다.
        if not TELEGRAM_SUMMARY_KEY or self.query_param("key") != TELEGRAM_SUMMARY_KEY:
            self.send_json(403, {"error": "forbidden"})
            return
        code = self.require_stock_code()
        if not code:
            return
        names = ["quote", "history", "investors", "theme", "investor-live", "program-trade"]
        # 종목 패널이 그 뒤에 순서대로 부르는 동일 산업군 조회(미국 ETF 시세, 국내 ETF 시세)도 같이 잰다.
        extra = {"us-proxy": "/api/quote?symbol=SOXX&range=1d&interval=2m",
                 "kr-proxy": "/api/stock/quote?code=091160"}
        out = {"code": code, "cold_ms": {}, "warm_ms": {}}

        def timed(n):
            t = time.time()
            try:
                _local_get(extra[n] if n in extra else f"/api/stock/{n}?code={code}")
                err = None
            except Exception as e:
                err = str(e)[:80]
            return round((time.time() - t) * 1000), err

        for n in names + list(extra):
            ms, err = timed(n)
            out["cold_ms"][n] = ms if not err else f"{ms} ERR {err}"
        for n in names + list(extra):
            ms, err = timed(n)
            out["warm_ms"][n] = ms if not err else f"{ms} ERR {err}"
        t0 = time.time()
        threads = [threading.Thread(target=timed, args=(n,)) for n in names]
        [t.start() for t in threads]
        [t.join() for t in threads]
        out["parallel_total_ms"] = round((time.time() - t0) * 1000)
        try:
            out["investor_live"] = _local_get(f"/api/stock/investor-live?code={code}")
        except Exception as e:
            out["investor_live"] = {"error": str(e)[:100]}
        self.send_json(200, out)

    def handle_ops_stock_flow(self):
        # 점검용: 종목 수급을 한투 원본 값으로 그대로 보여준다(키움 화면과 대조할 때 쓴다).
        # daily = 종목별 일별 투자자(통합, 백만원 단위 원본 포함), estimate = 장중 가집계(수량), program = 프로그램매매.
        if not TELEGRAM_SUMMARY_KEY or self.query_param("key") != TELEGRAM_SUMMARY_KEY:
            self.send_json(403, {"error": "forbidden"})
            return
        code = self.require_stock_code()
        if not code:
            return
        out = {"code": code}
        try:
            data = kis_get("/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily", "FHPTJ04160001",
                           {"FID_COND_MRKT_DIV_CODE": "UN", "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": "",
                            "FID_ORG_ADJ_PRC": "", "FID_ETC_CLS_CODE": ""})
            rows = (data.get("output2") or [])[:6]
            out["daily"] = [{"date": r.get("stck_bsop_date"), "close": r.get("stck_clpr"),
                             "indiv_amt_m": r.get("prsn_ntby_tr_pbmn"), "foreign_amt_m": r.get("frgn_ntby_tr_pbmn"),
                             "inst_amt_m": r.get("orgn_ntby_tr_pbmn"), "indiv_qty": r.get("prsn_ntby_qty"),
                             "foreign_qty": r.get("frgn_ntby_qty"), "inst_qty": r.get("orgn_ntby_qty")} for r in rows]
        except Exception as e:
            out["daily"] = {"error": str(e)}
        try:
            data = kis_get("/uapi/domestic-stock/v1/quotations/investor-trend-estimate", "HHPTJ04160200",
                           {"MKSC_SHRN_ISCD": code})
            out["estimate"] = [{"hour": r.get("bsop_hour_gb"), "foreign_qty": r.get("frgn_fake_ntby_qty"),
                                "inst_qty": r.get("orgn_fake_ntby_qty"), "sum_qty": r.get("sum_fake_ntby_qty")}
                               for r in (data.get("output2") or [])]
        except Exception as e:
            out["estimate"] = {"error": str(e)}
        try:
            data = kis_get("/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
                           {"FID_COND_MRKT_DIV_CODE": "UN", "FID_INPUT_ISCD": code})
            out["price"] = data["output"].get("stck_prpr")
        except Exception as e:
            out["price"] = {"error": str(e)}
        try:
            data = kis_get("/uapi/domestic-stock/v1/quotations/program-trade-by-stock", "FHPPG04650101",
                           {"FID_COND_MRKT_DIV_CODE": "UN", "FID_INPUT_ISCD": code})
            r = (data.get("output") or [{}])[0]
            out["program"] = {"time": r.get("bsop_hour"), "net_qty": r.get("whol_smtn_ntby_qty"), "net_amt": r.get("whol_smtn_ntby_tr_pbmn")}
        except Exception as e:
            out["program"] = {"error": str(e)}
        self.send_json(200, out)

    def handle_ops_regime(self):
        # 점검/테스트용: kind=morning|pre|change 메시지를 만들어 보여준다. send=1이면 텔레그램으로도 보낸다.
        if not TELEGRAM_SUMMARY_KEY or self.query_param("key") != TELEGRAM_SUMMARY_KEY:
            self.send_json(403, {"error": "forbidden"})
            return
        kind = self.query_param("kind") or "morning"
        if kind not in ("morning", "pre", "change"):
            self.send_json(400, {"error": "kind=morning|pre|change"})
            return
        try:
            if kind == "change":     # 전환이 없어도 형식 확인용으로 예시 메시지를 만든다
                info = compute_regime(include_today=True)
                if not info["changed"]:
                    info = {**info, "changed": True, "prev": "side" if info["regime"] != "side" else "up"}
                    text = build_regime_message("change", info) + "\n(형식 확인용 예시: 실제 전환이 아닙니다)"
                else:
                    text = build_regime_message("change", info)
            else:
                text = regime_message_for(kind)
            info = compute_regime(include_today=False)
            sent = False
            if self.query_param("send") == "1" and text:
                send_telegram_message("[테스트] " + text)
                sent = True
            self.send_json(200, {"text": text, "regime": info, "sent": sent})
        except Exception as e:
            self.send_json(502, {"error": str(e)})

    def handle_ops_kis_minute(self):
        # 운영/분석용: 한투 주식일별분봉조회(FHKST03010230)로 하루치 1분봉을 모아 돌려준다.
        # 한 번에 120개씩만 주므로 마감(15:30)부터 거꾸로 이어서 받는다. TELEGRAM_SUMMARY_KEY로 보호.
        if not TELEGRAM_SUMMARY_KEY or self.query_param("key") != TELEGRAM_SUMMARY_KEY:
            self.send_json(403, {"error": "forbidden"})
            return
        code = self.require_stock_code()
        date = self.query_param("date")
        if not code or not re.fullmatch(r"\d{8}", date or ""):
            if code:
                self.send_json(400, {"error": "date=YYYYMMDD 필요"})
            return
        try:
            mkt = (self.query_param("mkt") or "J").upper()
            self.send_json(200, {"code": code, "date": date, "mkt": mkt, "bars": _fetch_day_minutes(code, date, mkt)})
        except Exception as e:
            self.send_json(502, {"error": str(e)})

    def handle_sectors_fine(self):
        data, status = fine_sector_data()
        if data is None:
            self.send_json(503, {"error": status})
            return
        self.send_json(200, data)

    def handle_sectors_monthly(self):
        data, status = sector_monthly_data()
        if data is None:
            self.send_json(503, {"error": status})
            return
        self.send_json(200, data)

    def handle_ops_kis_raw(self):
        # 운영/탐색용: 한투 시세 조회 API(읽기 전용 GET)를 그대로 호출해 원본 응답 앞부분을 돌려준다.
        # 예) /api/ops/kis-raw?key=..&path=/uapi/domestic-stock/v1/quotations/inquire-index-daily-price&tr=FHPUP02120000&p.FID_INPUT_ISCD=0013...
        # path는 quotations 아래만 허용. TELEGRAM_SUMMARY_KEY로 보호.
        if not TELEGRAM_SUMMARY_KEY or self.query_param("key") != TELEGRAM_SUMMARY_KEY:
            self.send_json(403, {"error": "forbidden"})
            return
        path, tr = self.query_param("path"), self.query_param("tr")
        if not path.startswith("/uapi/domestic-stock/v1/quotations/") or not re.fullmatch(r"[A-Za-z0-9]{6,20}", tr or ""):
            self.send_json(400, {"error": "path(quotations 아래)와 tr 필요"})
            return
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        params = {k[2:]: v[0] for k, v in q.items() if k.startswith("p.")}
        limit = int(self.query_param("limit") or 5)
        try:
            data = kis_get(path, tr, params)
            out = {"rt_cd": data.get("rt_cd"), "msg1": data.get("msg1")}
            for k, v in data.items():
                if isinstance(v, list):
                    out[k] = {"count": len(v), "first": v[:limit], "last": v[-1:] if len(v) > limit else []}
                elif k not in out:
                    out[k] = v
            self.send_json(200, out)
        except Exception as e:
            self.send_json(502, {"error": str(e)[:300]})

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

    def handle_rsi_watch_get(self):
        items = []
        for code in get_rsi_watch_codes():
            try:
                name = _local_get(f"/api/stock/quote?code={code}").get("name") or code
            except Exception:
                name = code
            items.append({"code": code, "name": name, "status": rsi_rule_status(code)})
        self.send_json(200, {"items": items})

    def handle_rsi_watch_add(self):
        code = self.require_stock_code()
        if code:
            self.send_json(200, {"ok": True, "codes": add_rsi_watch_code(code)})

    def handle_rsi_watch_remove(self):
        code = self.require_stock_code()
        if code:
            self.send_json(200, {"ok": True, "codes": remove_rsi_watch_code(code)})

    def handle_ops_rsi_rule(self):
        # 점검/재현용: 특정 봉(asof=YYYYMMDD&slot=A~F)에서 규칙이 뭘 알렸을지 계산한다.
        # send=1이면 텔레그램으로도 보내며, 과거 재현(asof 지정)은 [테스트] 표시를 붙인다.
        if not TELEGRAM_SUMMARY_KEY or self.query_param("key") != TELEGRAM_SUMMARY_KEY:
            self.send_json(403, {"error": "forbidden"})
            return
        code = self.require_stock_code()
        if not code:
            return
        try:
            now = datetime.now(timezone.utc) + timedelta(hours=9)
            asof = self.query_param("asof") or now.strftime("%Y%m%d")
            slot = (self.query_param("slot") or "").upper()
            if slot not in ("A", "B", "C", "D", "E", "F"):
                done = _last_completed_slot(now) if asof == now.strftime("%Y%m%d") else "F"
                slot = done or "F"
            ndays = int(self.query_param("ndays") or RSI_HISTORY_DAYS)
            events, cur, name = rsi_rule_evaluate(code, asof, slot, ndays)
            tag = " [테스트·과거 재현]" if self.query_param("asof") else ""
            text = rsirule.format_message(name, cur, events, tag) if cur else ""
            sent = False
            if self.query_param("send") == "1" and text:
                send_telegram_message(text)
                sent = True
            self.send_json(200, {"cur": cur, "events": events, "text": text, "sent": sent})
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

SIGNAL_EMOJI = {"green": "🟢", "yellow": "🟡", "orange": "🟠", "red": "🔴", None: "⚪"}

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


def _kst_now():
    return datetime.now(timezone.utc) + timedelta(hours=9)


def _boot_silent_day(start_min, end_min):
    """서버가 [start_min, end_min](KST 분) 구간 중에 켜졌으면 오늘 날짜를, 아니면 None.
    재시작 직후에는 메모리의 '이미 보냈다/발동했다' 기록이 비어 있어, 그냥 평가하면 방금 전에 보낸 알림이
    다시 나간다(2026-10-13 배포 때 5일선 근접 알림과 정시 요약이 또 나옴). 이 날짜에 한해 첫 평가는
    상태만 갱신하고 발송하지 않는다."""
    now = _kst_now()
    mins = now.hour * 60 + now.minute
    return now.strftime("%Y%m%d") if now.weekday() < 5 and start_min <= mins <= end_min else None


_boot_silent = {"threshold": _boot_silent_day(9 * 60, 15 * 60 + 30), "ma": _boot_silent_day(9 * 60, 20 * 60)}


def _consume_boot_silent(key):
    """오늘 부팅 후 첫 평가면 True(한 번만)."""
    with _alert_state_lock:
        if _boot_silent.get(key) == _kst_now().strftime("%Y%m%d"):
            _boot_silent[key] = None
            return True
    return False


def check_threshold_alert():
    # 80%/20% 돌파 알림은 정규장(09:00~15:30)에만 - 이후 NXT 시간대에는 보내지
    # 않는다. (5일선 근접 알림은 별도로 20:00까지.)
    now = datetime.now(timezone.utc) + timedelta(hours=9)
    if not (9 * 60 <= now.hour * 60 + now.minute <= 15 * 60 + 30):
        return {"sent": False, "skipped": "outside_regular_session"}

    macro_pct, _, stock_pct, stock_name, _ = _compute_scores()

    macro_events = _update_alert_state("macro", macro_pct)
    stock_events = _update_alert_state("stock", stock_pct)
    silent = _consume_boot_silent("threshold")
    if not macro_events and not stock_events:
        return {"sent": False, "macroPct": macro_pct, "stockPct": stock_pct}
    if silent:
        return {"sent": False, "silent": "boot", "macroPct": macro_pct, "stockPct": stock_pct}

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
    lines.extend(_kospi_change_headline())
    if fire_banner:
        lines.append(fire_banner)

    text = "\n".join(lines)
    send_telegram_message(text)
    return {"sent": True, "text": text}


# --- 120분봉 RSI 과매도 분할매수 규칙 알림 (rsirule.py) ---
# 감시 종목은 5일선 감시와 같은 방식으로 웹에서 추가/삭제하고 Render 환경변수
# (TELEGRAM_RSI_WATCH_CODES)에 써둔다. 120분봉은 한투 1분봉(주식일별분봉조회)으로
# 키움식(09:00~11:00, 11:00~13:00, 13:00~15:00, 15:00~15:30)으로 직접 만든다.
# 지난 거래일 분봉은 바뀌지 않으므로 메모리+파일(.rsi_min_cache.json)에 캐시한다.
RSI_HISTORY_DAYS = 50   # 거래일 기준 워밍업(RSI 수렴 + 묶음/보유 구간 추적). 통합 하루 6봉이라 ~300봉
_rsi_watch_lock = threading.Lock()
_rsi_watch_codes = [c.strip() for c in os.environ.get("TELEGRAM_RSI_WATCH_CODES", "000660").split(",") if c.strip()]
RSI_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".rsi_min_cache_un.json")
_rsi_day_cache = {}          # {code: {YYYYMMDD: rows}}
_rsi_cache_lock = threading.Lock()
_rsi_fetch_lock = threading.Lock()   # 분봉 수집은 한 번에 하나씩 (한투 호출 폭주 방지)
_rsi_status_cache = {}       # {code: (ts, status)}
_rsi_refreshing = set()
_rsi_done = set()            # 이미 평가한 (날짜, 봉, 종목)
_rsi_done_lock = threading.Lock()
RSI_EVAL_WINDOWS = {         # 봉 마감 +2분부터 40분 동안 평가 (재시작/지연 시 따라잡기)
    "A": (10 * 60 + 2, 10 * 60 + 42),
    "B": (12 * 60 + 2, 12 * 60 + 42),
    "C": (14 * 60 + 2, 14 * 60 + 42),
    "D": (16 * 60 + 2, 16 * 60 + 42),
    "E": (18 * 60 + 2, 18 * 60 + 42),
    "F": (20 * 60 + 2, 20 * 60 + 42),
}
_SLOT_LAST_MINUTE = {"A": "0956", "B": "1156", "C": "1356", "D": "1556", "E": "1756", "F": "1956"}   # 이 분 이후 분봉이 있어야 마감된 것으로 본다
_SLOT_END_MIN = {"A": 10 * 60, "B": 12 * 60, "C": 14 * 60, "D": 16 * 60, "E": 18 * 60, "F": 20 * 60}


def get_rsi_watch_codes():
    with _rsi_watch_lock:
        return list(_rsi_watch_codes)


def add_rsi_watch_code(code):
    with _rsi_watch_lock:
        if code not in _rsi_watch_codes:
            _rsi_watch_codes.append(code)
        codes = list(_rsi_watch_codes)
    _render_set_env_var("TELEGRAM_RSI_WATCH_CODES", ",".join(codes))
    _rsi_refresh_async(code)   # 분봉 이력을 미리 받아둔다
    return codes


def remove_rsi_watch_code(code):
    with _rsi_watch_lock:
        if code in _rsi_watch_codes:
            _rsi_watch_codes.remove(code)
        codes = list(_rsi_watch_codes)
    _render_set_env_var("TELEGRAM_RSI_WATCH_CODES", ",".join(codes))
    return codes


def _fetch_day_minutes(code, date, mkt="UN"):
    """하루치 1분봉 [[HHMM, 시, 고, 저, 종, 거래량], ...]. 기본은 통합(UN=KRX+NXT, 08:00~20:00,
    키움 통합 차트와 같음). 한 번에 120개씩만 주므로 마감부터 거꾸로 이어서 받는다."""
    rows, hour = {}, ("200000" if mkt != "J" else "153000")
    for _ in range(10):
        data = kis_get(
            "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice",
            "FHKST03010230",
            {"FID_COND_MRKT_DIV_CODE": mkt, "FID_INPUT_ISCD": code, "FID_INPUT_HOUR_1": hour,
             "FID_INPUT_DATE_1": date, "FID_PW_DATA_INCU_YN": "N", "FID_FAKE_TICK_INCU_YN": ""},
        )
        chunk = [r for r in (data.get("output2") or []) if r.get("stck_bsop_date") == date and r.get("stck_cntg_hour")]
        if not chunk:
            break
        for r in chunk:
            rows[r["stck_cntg_hour"][:4]] = [
                r["stck_cntg_hour"][:4], r.get("stck_oprc"), r.get("stck_hgpr"),
                r.get("stck_lwpr"), r.get("stck_prpr"), r.get("cntg_vol"),
            ]
        oldest = min(r["stck_cntg_hour"] for r in chunk)
        if oldest <= ("080000" if mkt != "J" else "090000") or len(chunk) < 100:
            break
        hh, mm = int(oldest[:2]), int(oldest[2:4])
        hh, mm = (hh, mm - 1) if mm else (hh - 1, 59)
        hour = f"{hh:02d}{mm:02d}00"
    return [rows[k] for k in sorted(rows)]


def _rsi_cache_load():
    try:
        with open(RSI_CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        with _rsi_cache_lock:
            for code, days in data.items():
                _rsi_day_cache.setdefault(code, {}).update(days)
    except Exception:
        pass


def _rsi_cache_save():
    try:
        with _rsi_cache_lock:
            snapshot = json.dumps(_rsi_day_cache)
        with open(RSI_CACHE_FILE, "w", encoding="utf-8") as f:
            f.write(snapshot)
    except Exception:
        pass


def _weekdays_back(upto, n):
    """upto(YYYYMMDD) 포함해 거꾸로 평일 n개 (공휴일은 분봉이 비어 있어 건너뛴다)."""
    d = datetime.strptime(upto, "%Y%m%d")
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return sorted(out)


def _rsi_history(code, upto, ndays):
    """{날짜: 1분봉 rows}. 오늘 이전 날짜는 캐시(휴장일은 빈 리스트)하고, 오늘은 매번 새로 받는다."""
    today = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y%m%d")
    out, fetched = {}, False
    with _rsi_fetch_lock:
        for d in _weekdays_back(upto, ndays):
            with _rsi_cache_lock:
                cached = _rsi_day_cache.get(code, {}).get(d)
            if cached is not None and d < today:
                out[d] = cached
                continue
            rows = _fetch_day_minutes(code, d)
            out[d] = rows
            fetched = True
            if d < today:
                with _rsi_cache_lock:
                    _rsi_day_cache.setdefault(code, {})[d] = rows
    if fetched:
        _rsi_cache_save()
    return out


def _last_completed_slot(now):
    """오늘 정규장에서 마감이 확정된 마지막 120분봉(없으면 None)."""
    mins = now.hour * 60 + now.minute
    done = [s for s, end in _SLOT_END_MIN.items() if mins >= end + 1]
    return done[-1] if done else None


def rsi_rule_evaluate(code, date, slot, ndays=RSI_HISTORY_DAYS):
    """date/slot 봉이 막 마감했다고 보고 규칙 이벤트를 계산. -> (events, cur, name)"""
    rows = _rsi_history(code, date, ndays)
    bars = rsirule.build_bars(rows, upto=(date, slot))
    if not bars or (bars[-1]["date"], bars[-1]["slot"]) != (date, slot):
        raise ValueError(f"{date} {slot} 봉 데이터가 없습니다")
    today = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y%m%d")
    if date == today and max((r[0] for r in rows.get(date, [])), default="") < _SLOT_LAST_MINUTE[slot]:
        raise ValueError(f"{date} {slot} 봉이 아직 완성되지 않았습니다")
    events, cur = rsirule.evaluate_bar(bars)
    try:
        name = _local_get(f"/api/stock/quote?code={code}").get("name") or code
    except Exception:
        name = code
    return events, cur, name


def rsi_rule_status(code):
    """웹 표시용 현재 상태. 이력이 아직 없으면 백그라운드로 받기 시작하고 None."""
    cached = _rsi_status_cache.get(code)
    if cached and time.time() - cached[0] < 300:
        return cached[1]
    _rsi_refresh_async(code)
    return cached[1] if cached else None


def _rsi_refresh_async(code):
    with _rsi_done_lock:
        if code in _rsi_refreshing:
            return
        _rsi_refreshing.add(code)

    def work():
        try:
            now = datetime.now(timezone.utc) + timedelta(hours=9)
            today = now.strftime("%Y%m%d")
            rows = _rsi_history(code, today, RSI_HISTORY_DAYS)
            slot = _last_completed_slot(now) if now.weekday() < 5 else "F"
            bars = rsirule.build_bars(rows, upto=(today, slot or ""))
            _rsi_status_cache[code] = (time.time(), rsirule.status_of(bars))
        except Exception as e:
            print(f"rsi status refresh failed for {code}: {e}")
        finally:
            with _rsi_done_lock:
                _rsi_refreshing.discard(code)

    threading.Thread(target=work, daemon=True).start()


def check_rsi_rule_alerts():
    """120분봉이 막 마감한 시각이면 감시 종목마다 규칙 이벤트를 평가해 텔레그램으로 보낸다.
    같은 (날짜, 봉, 종목)은 한 번만 - 스케줄러 스레드와 30분 알림 크론이 둘 다 불러도 안전하다."""
    if os.environ.get("RSI_RULE_ALERTS_OFF", "1") == "1":   # 기본 꺼짐(2026-10-13 사용자 요청). 켜려면 환경변수 RSI_RULE_ALERTS_OFF=0
        return {"checked": False, "off": True}
    now = datetime.now(timezone.utc) + timedelta(hours=9)
    if now.weekday() >= 5 or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {"checked": False}
    mins = now.hour * 60 + now.minute
    due = [s for s, (a, b) in RSI_EVAL_WINDOWS.items() if a <= mins <= b]
    if not due or not is_krx_trading_day():
        return {"checked": False}
    date = now.strftime("%Y%m%d")
    sent = []
    for slot in due:
        for code in get_rsi_watch_codes():
            key = (date, slot, code)
            with _rsi_done_lock:
                if key in _rsi_done:
                    continue
            try:
                events, cur, name = rsi_rule_evaluate(code, date, slot)
                text = rsirule.format_message(name, cur, events)
                if text:
                    send_telegram_message(text)
                    sent.append({"code": code, "slot": slot, "events": [e["kind"] for e in events]})
                with _rsi_done_lock:
                    _rsi_done.add(key)
                _rsi_status_cache[code] = (time.time(), rsirule.status_of(
                    rsirule.build_bars(_rsi_history(code, date, RSI_HISTORY_DAYS), upto=(date, slot))))
            except Exception as e:
                print(f"rsi rule check failed for {code} {slot}: {e}")
    return {"checked": True, "sent": sent}


# --- 정시 요약 / 점수 기록을 서버 안 스케줄러로 (Render 유료 인스턴스 = 항상 켜짐) ---
# GitHub Actions 크론은 10~30분 늦거나 아예 안 도는 날이 있어서(2026-10-13 아침 요약 누락),
# 서버가 직접 시각을 맞춘다. GitHub 크론은 백업으로 남겨두되, 이미 보낸 슬롯이면 서버가 건너뛴다.
SUMMARY_SLOTS = ((9, 10), (12, 10), (15, 10), (17, 10), (19, 40))   # KST
SCORE_LOG_SLOTS = {"1510": "close", "1940": "extended"}
SUMMARY_WINDOW_MIN = 30     # 슬롯 시각 + 30분 안이면 그 슬롯으로 본다(재시작/지연 따라잡기)
_summary_done = set()       # (YYYYMMDD, "HHMM") - 보냈거나 보내는 중
_summary_attempts = {}
_summary_lock = threading.Lock()
_score_snap = {}            # {("YYYY-MM-DD", "close"|"extended"): row}


def _summary_slot_due(now, window=SUMMARY_WINDOW_MIN):
    """지금 시각이 속한 정시 슬롯 = 가장 최근 슬롯 시각부터 window분 안이면 그 슬롯, 아니면 None."""
    mins = now.hour * 60 + now.minute
    best = None
    for h, m in SUMMARY_SLOTS:
        if h * 60 + m <= mins <= h * 60 + m + window:
            best = f"{h:02d}{m:02d}"
    return best


def claim_summary_slot(now, window=SUMMARY_WINDOW_MIN):
    """지금 시각의 정시 슬롯을 선점한다. -> (slot, fresh). slot이 None이면 정시 구간이 아님.
    fresh가 False면 이미 보낸 슬롯. GitHub 크론 호출은 지연(최대 1시간쯤)을 감안해 window를 크게 준다."""
    slot = _summary_slot_due(now, window)
    if not slot:
        return None, False
    key = (now.strftime("%Y%m%d"), slot)
    with _summary_lock:
        if key in _summary_done:
            return slot, False
        _summary_done.add(key)
    return slot, True


def release_summary_slot(now, slot):
    with _summary_lock:
        _summary_done.discard((now.strftime("%Y%m%d"), slot))


def _summary_scheduler():
    # 서버가 정시 슬롯 구간(슬롯 시각 +30분) 안에 켜졌다면 그 슬롯은 이미 처리됐다고 본다 - 재시작 때마다
    # 방금 보낸 정시 요약이 다시 나가는 걸 막는다(메모리 기록이 비기 때문). 그 사이 정말 서버가 죽어 있었다면
    # 그 슬롯은 건너뛰게 되지만, 항상 켜진 유료 인스턴스에서는 드물다.
    boot = _kst_now()
    if boot.weekday() < 5:
        slot = _summary_slot_due(boot)
        if slot:
            with _summary_lock:
                _summary_done.add((boot.strftime("%Y%m%d"), slot))
            print(f"summary slot {slot} marked as handled at boot")
    while True:
        try:
            now = datetime.now(timezone.utc) + timedelta(hours=9)
            if now.weekday() < 5 and _summary_slot_due(now) and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID \
                    and is_krx_trading_day():
                slot, fresh = claim_summary_slot(now)
                if slot and fresh:
                    key = (now.strftime("%Y%m%d"), slot)
                    try:
                        # 점수 기록용 스냅샷은 정시에 계산해 둔다(요약과 같은 점수, 60초 캐시 공유).
                        if slot in SCORE_LOG_SLOTS:
                            row = build_score_snapshot(SCORE_LOG_SLOTS[slot])
                            _score_snap[(row["date"], row["slot"])] = row
                        send_telegram_message(build_dashboard_summary_text())
                        print(f"summary sent {key}")
                    except Exception as e:
                        release_summary_slot(now, slot)
                        n = _summary_attempts.get(key, 0) + 1
                        _summary_attempts[key] = n
                        print(f"summary failed {key} attempt {n}: {e}")
                        if n >= 3:                  # 계속 실패하면 이 슬롯은 포기(GitHub 백업이 시도할 수 있음)
                            with _summary_lock:
                                _summary_done.add(key)
                        time.sleep(60)
        except Exception as e:
            print(f"summary scheduler error: {e}")
        time.sleep(20)


# --- 임계값(80/20%) 알림 + 5일선 근접 알림을 서버 안 스케줄러로 (30분 간격, 09:00~20:00) ---
# 둘 다 "하루 한 번/상태 기계"로 중복을 막으므로 GitHub 크론(threshold-alert.yml)이 백업으로 같이 불러도
# 같은 알림이 두 번 나가지 않는다. 서버가 켜지는 순간 이번 30분 구간을 아직 안 돌았으면 바로 한 번 돈다(따라잡기).
_alert_last_slot = {"key": None}


def run_alert_checks():
    """임계값 알림 + 5일선 근접 알림 한 번. 정규장 제한 등은 각 함수가 알아서 처리한다."""
    out = {}
    try:
        out["threshold"] = check_threshold_alert()
    except Exception as e:
        out["threshold"] = {"error": str(e)}
        print(f"threshold alert failed: {e}")
    try:
        out["ma"] = check_ma_proximity_alert()
    except Exception as e:
        out["ma"] = {"error": str(e)}
        print(f"ma alert failed: {e}")
    return out


def _alert_scheduler():
    while True:
        try:
            now = datetime.now(timezone.utc) + timedelta(hours=9)
            mins = now.hour * 60 + now.minute
            if now.weekday() < 5 and 9 * 60 <= mins <= 20 * 60 and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                key = (now.strftime("%Y%m%d"), mins // 30)
                if key != _alert_last_slot["key"] and is_krx_trading_day():
                    _alert_last_slot["key"] = key
                    run_alert_checks()
        except Exception as e:
            print(f"alert scheduler error: {e}")
        time.sleep(20)


# --- 장세(상승/횡보/하락) 규칙 복기 알림 ---
# 사용자의 매매 규칙(매매일지로 정리한 것)을 지수 장세에 맞춰 가끔 텔레그램으로 되새겨 준다.
# 장세는 코스피 일봉 종가로 판정한다(장중 값은 흔들려서 쓰지 않음, 확정된 날만).
#  - 하락장: 최근 60거래일 고점 대비 -7% 이하 (사용자 지정)
#  - 상승장: 하락장이 아니고, 지수>5일선·20일선 이고 5일선>20일선>60일선
#  - 횡보장: 그 외
# 하루 판정이 왔다갔다 하지 않게 **이틀 연속 같은 판정일 때만 전환**한다. 판정은 일봉 이력에서 매번 다시
# 계산하므로(상태 저장 없음) 서버가 재시작돼도 같은 결과.
# 알림: ① 아침 08:40(전일 종가 기준) ② 오후 15:00(오늘 장중가 기준, 신호등 포함). 장세 전환 단독 알림은 없음.
REGIME_DROP_PCT = -7.0
REGIME_HIGH_WINDOW = 60
REGIME_LABEL = {"up": "상승장", "side": "횡보장", "down": "하락장"}
REGIME_EMOJI = {"up": "📈", "side": "↔️", "down": "📉"}
# 장세별 규칙 문구(사용자 확정, 2026-10-23). 각 장세는 (제목, [규칙...]) 묶음의 목록이다. 아침(중단)/15:00/장세 전환 알림이 같은 문구를 쓴다.
REGIME_RULES = {
    "down": [
        ("하락장 규칙", [
            "하락할 때 반등매매를 노리고 들어가지 않는다. 수익본적이 거의 없다. (욕심 = 손실)",
            "하락장에는 돌파매매 안 통한다.",
        ]),
        ("하락장 반등을 노려본다면!", [
            "추세하락 반등 신호: 신용매물 감소(신용은 하루 뒤 15시 집계) + 개인 매물 1조 이상 + 외국인 매수",
            "일봉 반등은 지수 3일선 위 + 종목 3·5일선 위로 올라탈 때",
            "하락 후 반등 양봉이 크게 나와 전일 하락분 위로 올라왔다면 종베 금지",
            "하락 후 반등 양봉이 나와도 퐁당퐁당 확률 높음. 이때는 저점 음봉에 분할 매수 해볼것(비중 약하게)",
        ]),
    ],
    "side": [
        ("횡보장 규칙", [
            "주도주 - 15분봉 60,120선 골든크로스 되돌림시 현금 베팅",
            "지수 반등후 추세를 보고 상승, 하락 채널 확인 후 진입한다",
            "채널 상단에서는 비중 축소, 채널 하단에서는 현금비중.",
            "하단 지지를 못할경우 칼손절",
            "전고 갱신후 3·5일선 지지 일봉이 나온 뒤, 종베 -> 상승 확률이 높다",
            "손실 본 날 복수매매 금지",
        ]),
    ],
    "up": [
        ("상승장 규칙", [
            "이평선이 정배열이고 지수가 5일선 위일때 -> 주도주에 비중베팅",
            "고가 형성 후 3·5일선 지지 일봉이 나오면 비중베팅, 또 신고가 상승 확률이 높다",
            "종목이 신고가 + 장후 NXT 상승 패턴이면 종베",
            "장중 매수는 3분봉 20선에서",
            "올라갈 때는 돌파턴, 당일 흐름을 빠르게 파악",
        ]),
        ("주도주 첫하락", [
            "주도주 첫 하락은 3일선(10분봉 117선)·5일선(15분봉 120선) 반등을 노린다",
            "주도주 - 15분봉 60,120선 골든크로스 되돌림시 베팅",
        ]),
    ],
}
# 장세 알림은 하루 두 번(2026-10-23 확정): 아침 08:40(전일 종가 기준 장세) + 오후 15:00(오늘 장중 코스피 기준 장세·신호등).
# 장세 전환만 따로 보내던 알림은 없앴다(전환이 있으면 두 알림에 "🔔 장세 전환" 줄이 붙는다).
REGIME_SLOTS = {"morning": (8 * 60 + 40, 9 * 60 + 10), "pre": (15 * 60, 15 * 60 + 30)}
_regime_done = set()
_regime_lock = threading.Lock()


def _regime_of(closes):
    """closes(오름차순, 마지막이 기준일)로 그날의 장세 판정. 데이터가 모자라면 None."""
    if len(closes) < 61:
        return None
    last = closes[-1]
    high = max(closes[-REGIME_HIGH_WINDOW:])
    dd = (last / high - 1) * 100
    if dd <= REGIME_DROP_PCT:
        return "down"
    ma5, ma20, ma60 = (_rolling_ma(closes, n)[-1] for n in (5, 20, 60))
    if last > ma5 and last > ma20 and ma5 > ma20 > ma60:
        return "up"
    return "side"


def compute_regime(include_today=False):
    """확정된 일봉으로 장세를 계산. include_today=False면 오늘 봉(장중)은 뺀다."""
    rows = naver_daily_closes("KOSPI")
    today = _kst_now().strftime("%Y-%m-%d")
    if not include_today:
        rows = [r for r in rows if r["date"] != today]
    closes = [r["close"] for r in rows]
    dates = [r["date"] for r in rows]
    # 최근 15거래일의 하루짜리 판정 → 이틀 연속 같은 판정일 때만 전환
    n = min(15, len(closes) - 60)
    if n < 2:
        raise ValueError("일봉 이력이 모자랍니다")
    daily = [(dates[i], _regime_of(closes[: i + 1])) for i in range(len(closes) - n, len(closes))]
    confirmed, history = daily[0][1], []
    for k in range(1, len(daily)):
        prev_confirmed = confirmed
        if daily[k][1] != confirmed and daily[k][1] == daily[k - 1][1]:
            confirmed = daily[k][1]
        history.append((daily[k][0], confirmed, prev_confirmed))
    last_date, regime, prev_regime = history[-1]
    changed = regime != prev_regime
    last = closes[-1]
    high = max(closes[-REGIME_HIGH_WINDOW:])
    return {
        "regime": regime, "changed": changed, "prev": prev_regime, "date": last_date,
        "close": last, "high": high, "drawdown": (last / high - 1) * 100,
        "dayRegime": daily[-1][1],
    }


def jongbae_state(include_today=True):
    """종베 직전 신호등(코스피 5일선·20일선 기준). 매매일지 분석 결과: 5일선이 종베 가부를 가르고 20일선은 그 안에서 기대의 크기를 가른다.
    level: green(베팅가능 = 5일선 위 & 20일선 위) / yellow(소액 = 5일선 위 & 20일선 아래) / red(쉼 = 5일선 아래).
    마지막 값이 오늘이면 장중가로 판정(종가 확정 전 근사), 아니면 전일 종가 기준. 못 만들면 None."""
    rows = naver_daily_closes("KOSPI")
    today = _kst_now().strftime("%Y-%m-%d")
    if not include_today:       # 아침 알림: 오늘 행을 빼고 전일 종가까지 확정된 값으로 판정
        rows = [r for r in rows if r["date"] != today]
    closes = [r["close"] for r in rows if r.get("close") is not None]
    if len(closes) < 20:
        return None
    live = include_today and rows[-1]["date"] == today
    price = closes[-1]
    ma5 = sum(closes[-5:]) / 5
    ma20 = sum(closes[-20:]) / 20
    if price < ma5:
        level, emoji, label = "red", "🔴", "쉼 (5일선 아래)"
    elif price < ma20:
        level, emoji, label = "yellow", "🟡", "소액 (5일선 위, 20일선 아래)"
    else:
        level, emoji, label = "green", "🟢", "베팅가능 (5일선·20일선 위)"
    return {"level": level, "emoji": emoji, "label": label, "price": price, "ma5": ma5, "ma20": ma20,
            "basis": "장중가" if live else "전일 종가"}


def jongbae_light(state=None):
    s = state or jongbae_state()
    if not s:
        return None
    return [f"🚦 종베 신호등: {s['emoji']} {s['label']}",
            f"   코스피 {s['price']:,.0f} ({s['basis']}) · 5일선 {s['ma5']:,.0f} · 20일선 {s['ma20']:,.0f}"]


def _rules_lines(regime):
    out = []
    for title, items in REGIME_RULES[regime]:
        if out:
            out.append("")
        out.append(f"📌 {title}")
        out.extend(f" · {t}" for t in items)
    return out


def build_jongbae_pre_message(info, state, today_live=None):
    """15:00 종베 직전 알림(짧은 형태): 신호등 + 장세 한 줄 + 장세별 규칙."""
    r = info["regime"]
    dd = today_live["drawdown"] if today_live else info["drawdown"]
    lines = [f"🧭 종베 직전 15:00 · {_kst_now().strftime('%m/%d')}",
             f"🚦 {state['emoji']} {state['label']}"]
    if info["changed"]:
        lines.append(f"🔔 장세 전환: {REGIME_LABEL[info['prev']]} → {REGIME_LABEL[r]}")
    lines.append(f"{REGIME_EMOJI[r]} {REGIME_LABEL[r]} · 코스피 {state['price']:,.0f} · 고점 대비 {dd:+.1f}%")
    lines.append("")
    lines.extend(_rules_lines(r))
    return "\n".join(lines)


def build_regime_message(kind, info, today_live=None):
    """kind: morning | pre | change. 평문."""
    now = _kst_now()
    r = info["regime"]
    head = {"morning": "🧭 종베규칙 · 아침 08:40", "pre": "🧭 종베규칙 · 종베 직전 15:00", "change": "🔔 종베규칙 · 장세 전환"}[kind]
    if kind == "pre":
        try:
            state = jongbae_state()
        except Exception:
            state = None
        if state:
            return build_jongbae_pre_message(info, state, today_live)
    lines = [f"{head} · {now.strftime('%m/%d')}"]
    if kind == "morning":
        try:
            ms = jongbae_state(include_today=False)
        except Exception:
            ms = None
        if ms:
            lines.append(f"🚦 {ms['emoji']} {ms['label']} (전일 종가 기준)")
    if kind == "change" or info["changed"]:
        lines.append(f"{REGIME_EMOJI[info['prev']]} {REGIME_LABEL[info['prev']]} → {REGIME_EMOJI[r]} {REGIME_LABEL[r]} 전환 ({info['date'][5:].replace('-', '/')} 종가 기준)")
    else:
        lines.append(f"{REGIME_EMOJI[r]} 지금은 {REGIME_LABEL[r]}")
    lines.append(f"코스피 {info['close']:,.0f} · 60일 고점 {info['high']:,.0f} 대비 {info['drawdown']:+.1f}%")
    if today_live is not None and kind == "pre":
        lines.append(f"오늘 장중 {today_live['close']:,.0f} ({today_live['pct']:+.2f}%) · 고점 대비 {today_live['drawdown']:+.1f}%")
    lines.append("")
    lines.extend(_rules_lines(r))
    return "\n".join(lines)


def _regime_today_live():
    rows = naver_daily_closes("KOSPI")
    today = _kst_now().strftime("%Y-%m-%d")
    if len(rows) < 2 or rows[-1]["date"] != today:
        return None
    closes = [r["close"] for r in rows]
    high = max(closes[-REGIME_HIGH_WINDOW:])
    return {"close": closes[-1], "pct": (closes[-1] / closes[-2] - 1) * 100, "drawdown": (closes[-1] / high - 1) * 100}


def regime_message_for(kind):
    """kind에 맞는 메시지(없으면 None). change는 오늘 마감으로 전환이 확정된 때만."""
    if kind == "change":
        info = compute_regime(include_today=True)
        if not info["changed"] or info["date"] != _kst_now().strftime("%Y-%m-%d"):
            return None
        return build_regime_message("change", info)
    if kind == "pre":
        # 15:00: 오늘 장중 코스피를 오늘 종가 자리에 넣고 판정(네이버 일봉의 마지막 행이 장중가)
        return build_regime_message(kind, compute_regime(include_today=True), None)
    return build_regime_message(kind, compute_regime(include_today=False), None)   # 아침: 전일 종가까지 확정된 일봉


def _regime_slot_due(now):
    mins = now.hour * 60 + now.minute
    return [k for k, (a, b) in REGIME_SLOTS.items() if a <= mins <= b]


def _regime_scheduler():
    boot = _kst_now()
    if boot.weekday() < 5:     # 재시작 직후에는 이미 열려 있는 구간을 처리된 것으로 본다(중복 방지)
        with _regime_lock:
            for k in _regime_slot_due(boot):
                _regime_done.add((boot.strftime("%Y%m%d"), k))
    while True:
        try:
            now = _kst_now()
            if now.weekday() < 5 and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                for kind in _regime_slot_due(now):
                    key = (now.strftime("%Y%m%d"), kind)
                    with _regime_lock:
                        if key in _regime_done:
                            continue
                        _regime_done.add(key)
                    if not is_krx_trading_day():
                        continue
                    try:
                        text = regime_message_for(kind)
                        if text:
                            send_telegram_message(text)
                            print(f"regime message sent: {kind}")
                    except Exception as e:
                        with _regime_lock:
                            _regime_done.discard(key)    # 다음 루프에 다시 시도(창 안에서만)
                        print(f"regime message failed {kind}: {e}")
                        time.sleep(60)
        except Exception as e:
            print(f"regime scheduler error: {e}")
        time.sleep(20)


def _rsi_scheduler():
    # 서버가 켜지자마자 분봉 이력을 받아둔다(처음 60거래일은 한투 호출이 많아 1~3분 걸려서,
    # 첫 봉 마감 때 받기 시작하면 알림이 늦는다).
    for code in get_rsi_watch_codes():
        _rsi_refresh_async(code)
    while True:
        try:
            check_rsi_rule_alerts()
        except Exception as e:
            print(f"rsi scheduler error: {e}")
        time.sleep(20)


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
    silent = _consume_boot_silent("ma")
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
    if silent:
        return {"sent": False, "silent": "boot", "detail": detail}

    text = "\n".join([f"📍 5일선 근접 알림 · {now.strftime('%m/%d %H:%M')}", "", *events])
    send_telegram_message(text)
    return {"sent": True, "text": text, "detail": detail}


# 신호등별 가중치(%) - 합계 각각 100. index.html의 WEIGHTS와 반드시 같은 값으로
# 유지할 것(화면과 텔레그램의 매크로/종목 체크 %가 어긋나지 않게).
SCORE_WEIGHTS = {
    "macro": {"ndq": 20, "kospiMa5": 15, "kospiDir": 15, "kospiFlow": 15, "tnx": 15, "wti": 10, "btc": 10},
    "stock": {"program": 20, "liveFlow": 20, "chart": 20, "candle": 15, "krSector": 15, "usSector": 10},
}


def _score_for(signal):
    return {"green": 20, "yellow": 10, "orange": 5, "red": 0}.get(signal, 10)


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
    periods = [3, 5, 10, 20, 60, 120]   # 3일선 추가(2026-10-14, 화면의 이평선 순위/신호와 같게 유지)
    arrs = {p: _rolling_ma(closes, p) for p in periods}
    last = closes[-1]
    ma_list = [(str(p), arrs[p][-1]) for p in periods]
    prev_idx = len(closes) - 2
    prev_last = closes[prev_idx] if prev_idx >= 0 else None
    prev_ma_list = [(str(p), arrs[p][prev_idx]) for p in periods] if prev_idx >= 0 else []
    have_prev = prev_idx >= 0 and prev_last is not None and all(v is not None for _, v in prev_ma_list)
    return last, ma_list, prev_last, prev_ma_list, have_prev


_MA_ORDER = ("3", "5", "10", "20", "60", "120")


def _ma_pair_states(ma_list):
    """이웃한 이평선끼리(3-5, 5-10, ...) 정배열(단기선이 위)이면 True. 화면(index.html maPairStates)과 같은 규칙."""
    v = dict(ma_list)
    return [(a, b, v[a] > v[b]) for a, b in zip(_MA_ORDER, _MA_ORDER[1:])]


def _align_summary(states):
    """'전부 역배열 (3<5<...)' 또는 '🔺3>5 정배열 · 🔽5<10<20<60<120 역배열' (정배열 🔺빨강, 역배열 🔽파랑)."""
    if all(not up for _, _, up in states):
        return f"🔽전부 역배열 ({'<'.join(_MA_ORDER)})"
    if all(up for _, _, up in states):
        return f"🔺전부 정배열 ({'>'.join(_MA_ORDER)})"
    parts, i = [], 0
    while i < len(states):
        j = i
        while j + 1 < len(states) and states[j + 1][2] == states[i][2]:
            j += 1
        up = states[i][2]
        names = [states[i][0]] + [s[1] for s in states[i:j + 1]]
        parts.append(f"{'🔺' if up else '🔽'}{('>' if up else '<').join(names)} {'정배열' if up else '역배열'}")
        i = j + 1
    return " · ".join(parts)


def _align_changes(states, prev_states):
    """어제와 달라진 이웃 관계만: '3<5 역배열 전환' / '3>5 정배열 전환'."""
    return [f"{a}{'>' if up else '<'}{b} {'정배열' if up else '역배열'} 전환"
            for (a, b, up), (_, _, pup) in zip(states, prev_states) if up != pup]


def _kospi_change_headline():
    """상단 강조 줄들: 종베 코스피 카드의 변화 뱃지(예: 3일선·5일선 하향 돌파, 3<5 역배열 전환)와
    오늘 이평선 배열 요약(오늘: 🔽전부 역배열 ...). 줄 리스트, 못 만들면 []."""
    try:
        hist = _local_get("/api/kospi/history")
        closes = [c["close"] for c in hist.get("closes", []) if c.get("close") is not None]
        state = _ma_state(closes)
        if not state:
            return []
        last, ma_list, prev_last, prev_ma_list, have_prev = state
        out = [""]   # 매크로/종목 % 줄과 한 줄 띄움
        if have_prev:
            items = [_describe_rank_change(last, ma_list, prev_last, prev_ma_list)]
            items += _align_changes(_ma_pair_states(ma_list), _ma_pair_states(prev_ma_list))
            out.append("📢 코스피 변화: " + " / ".join(items))
        out.append(f"오늘: {_align_summary(_ma_pair_states(ma_list))}")
        return out
    except Exception:
        return []


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

    def fmt(name, b):
        # 폰 한 줄(한글 22자 안팎)에 들어가게: 상승 🔺 / 하락 ⬇️ (한국 관례 색: 빨강 상승, 파랑 하락)
        return f"{name} 🔺{b['up']} ⬇️{b['down']} (보합{b['flat']})"

    return [fmt("코스피", k), fmt("코스닥", q)]


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
    lines.append(_dual_buy_hit_line(rows))
    return lines


def _dual_buy_hit_line(rank_rows):
    """TOP10 중 당일 상승 + 외국인·기관·프로그램 순매수가 모두 +인 종목 한 줄(웹 "조건 충족만 보기"와 같은 기준)."""
    try:
        flows = _fetch_volume_rank_flows()["flows"]
    except Exception:
        return "🎯 양매수·프로그램 수급 히트: 확인 실패"
    if not any(f.get("known") for f in flows.values()):
        return "🎯 양매수·프로그램 수급 히트: 수급 데이터 없음"
    hits = [f"{r['name']} +{abs(r.get('changePct', 0)):.2f}%" for r in rank_rows if flows.get(r["code"], {}).get("pass")]
    return "🎯 양매수·프로그램 수급 히트: " + (", ".join(hits) if hits else "없음")


_scores_cache = {"ts": 0, "val": None}
_scores_cache_lock = threading.Lock()


def _compute_scores():
    """60초 캐시 - 같은 시각에 정시 요약과 점수 기록이 같이 계산해도 외부 호출이 두 배가 되지 않게."""
    with _scores_cache_lock:
        if _scores_cache["val"] is not None and time.time() - _scores_cache["ts"] < 60:
            return _scores_cache["val"]
        val = _compute_scores_uncached()
        _scores_cache["val"], _scores_cache["ts"] = val, time.time()
        return val


def _kospi_flow_line():
    """당일 코스피 수급(KRX+NXT 통합 누적 순매수, 키움 당일추이와 같은 값)의 신호등.
    외국인·기관 둘 다 순매수 = 초록, 외국인만 = 노랑, 기관만 = 주황(노랑보다 낮게), 둘 다 순매도(개인이 받아냄) = 빨강."""
    last = _build_flow_trend()["points"][-1]
    foreign, inst, indiv = last["foreign"], last["institution"], last["individual"]
    if foreign > 0 and inst > 0:
        sig = "green"
    elif foreign > 0:
        sig = "yellow"
    elif inst > 0:
        sig = "orange"
    else:
        sig = "red"
    f = lambda v: f"{'+' if v > 0 else ''}{round(v):,}억"
    return sig, f"{SIGNAL_EMOJI[sig]} 코스피 수급: 외국인 {f(foreign)} · 기관 {f(inst)} · 개인 {f(indiv)}"


def _compute_scores_uncached():
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

    try:
        flow_sig, flow_line = _kospi_flow_line()
        macro_signals.append((flow_sig, SCORE_WEIGHTS["macro"]["kospiFlow"]))
        macro_lines.append(flow_line)
    except Exception:
        macro_lines.append("⚪ 코스피 수급 데이터 없음")

    macro_lines.extend(_market_breadth_lines())

    macro_pct = _weighted_pct(macro_signals) or 0

    try:
        stock_pct, stock_name, stock_lines = _stock_section(TELEGRAM_SUMMARY_STOCK_CODE)
    except Exception as e:
        stock_pct, stock_name, stock_lines = None, TELEGRAM_SUMMARY_STOCK_CODE, [f"⚪ 종목 체크 데이터 없음 ({e})"]

    return macro_pct, macro_lines, stock_pct, stock_name, stock_lines


_SIGNAL_LETTER = {"🟢": "G", "🟡": "Y", "🟠": "O", "🔴": "R", "⚪": "N"}


_MACRO_KEYS = (("tnx", "미국채10Y"), ("wti", "WTI"), ("ndq", "나스닥100선물"), ("btc", "비트코인"),
               ("kospiMa", "코스피 순위"), ("kospiFlow", "코스피 수급"), ("kospiDir", "코스피 "))
_STOCK_KEYS = (("usSector", "미국 동일"), ("krSector", "국내 동일"), ("ma", "이평선 순위"),
               ("flow", "수급:"), ("program", "프로그램매매"))


def _signal_map(lines, keys, fallback_key=None):
    """신호 이모지(🟢🟡🔴⚪)로 시작하는 줄을 키별 G/Y/R/N 문자열("tnx:R wti:G ...")로.
    섹터 ETF처럼 없는 항목이 있어도 위치가 밀리지 않도록 키를 붙인다."""
    out = {}
    for l in lines:
        if not l or l[0] not in _SIGNAL_LETTER:
            continue
        key = next((k for k, s in keys if s in l), fallback_key)
        if key and key not in out:
            out[key] = _SIGNAL_LETTER[l[0]]
    return " ".join(f"{k}:{v}" for k, v in out.items())


def build_score_snapshot(slot):
    """점수 기록 한 줄. macro_sig 키: tnx wti ndq btc kospiDir kospiMa. stock_sig 키: candle usSector
    krSector ma flow program (없는 항목은 생략)."""
    now = datetime.now(timezone.utc) + timedelta(hours=9)
    macro_pct, macro_lines, stock_pct, stock_name, stock_lines = _compute_scores()
    return {
        "date": now.strftime("%Y-%m-%d"),
        "slot": slot if slot in ("close", "extended") else "manual",
        "time": now.strftime("%H:%M"),
        "macro": macro_pct,
        "stock": stock_pct,
        "stock_code": TELEGRAM_SUMMARY_STOCK_CODE,
        "stock_name": stock_name,
        "macro_sig": _signal_map(macro_lines, _MACRO_KEYS),
        "stock_sig": _signal_map(stock_lines, _STOCK_KEYS, "candle"),
    }


def _compact_summary_lines(macro_lines, stock_lines):
    """정시 요약만 폰 화면(한 줄≈한글 22자)에 맞게 다듬는다. 점수 기록(_signal_map)은 원본 줄을 쓰므로 영향 없음.
    - 순위 체인의 "일선" 생략, 코스피 순위 줄의 괄호 설명 생략(상단 "📢 코스피 변화"와 중복)
    - 코스피 지수 줄의 구간 문구(20일선 아래=저점 현금베팅 등) 생략
    - 코스피 수급은 "수급(억) 외국인 -4,628 · …", 동일 산업군은 미국/국내 각각 한 줄(ETF 이름 생략)"""
    def trim(line):
        line = re.sub(r"(순위: )([^(]+)", lambda m: m.group(1) + re.sub(r"(\d+)일선", r"\1", m.group(2)), line)
        if "코스피 순위:" in line:
            line = re.sub(r"\s*\(.*\)\s*$", "", line)
        elif "코스피 수급:" in line:
            line = re.sub(r"(\d)억", r"\1", line.replace("코스피 수급: ", "수급(억) "))
        elif " · " in line and "코스피 " in line:
            line = re.sub(r" · \d+일선 (위|아래)=.*$", "", line)
        line = re.sub(r"전일 미국 동일 산업군\(.*?\)", "미국 동일산업군(전일)", line)
        line = re.sub(r"국내 동일 산업군\(.*?\)", "국내 동일산업군", line)
        return line

    return [trim(l) for l in macro_lines], [trim(l) for l in stock_lines]


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
    summary_lines.extend(_kospi_change_headline())
    if fire_banner:
        summary_lines.append(fire_banner)

    # 거래대금 순위는 KIS 호출이 두 번(KRX+NXT) 더 들어가는 무거운 섹션이라,
    # 하루 중 장 시작(09:10)과 마감 전(15:10) 체크포인트에만 같이 보낸다.
    macro_lines, stock_lines = _compact_summary_lines(macro_lines, stock_lines)
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
    _rsi_cache_load()
    # 120분봉 RSI 규칙 알림은 사용자 요청으로 중단(2026-10-13): 키움 차트와 신호가 안 맞는다고 판단.
    # threading.Thread(target=_rsi_scheduler, daemon=True).start()   # ← 다시 켜려면 주석 해제
    if KIS_APP_KEY and KIS_APP_SECRET:
        _sector_refresh_async()      # 업종 페이지 첫 방문이 느리지 않게 부팅 때 미리 모아 둔다
    _fine_refresh_async()
    threading.Thread(target=_summary_scheduler, daemon=True).start()
    threading.Thread(target=_alert_scheduler, daemon=True).start()
    threading.Thread(target=_regime_scheduler, daemon=True).start()
    with Server((HOST, PORT), Handler) as httpd:
        print(f"Serving dashboard on http://{HOST}:{PORT}")
        httpd.serve_forever()
