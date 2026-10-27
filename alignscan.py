"""주봉 4-13-26-52 정배열 종목 스캔: 종목 목록을 받아 종목마다 주봉을 받아 정배열 여부와 연속 주수를 판정한다.

정배열 판정은 leader._weekly_analysis와 같은 규칙(4주선>13주선>26주선>52주선)이다. 종목마다 받자마자 판정하고
원본 주봉은 버려서 메모리를 거의 쓰지 않는다. 파싱은 ast.literal_eval(8ms) 대신 json.loads(0.4ms)를 쓴다.
"""
import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import leader

LOOKBACK_DAYS = 2500   # leader.WEEKLY_LOOKBACK_DAYS와 같은 길이(~357주) — 정배열 연속 주수가 길어도 센다
WORKERS = 6
MIN_BARS = 52          # 52주선을 만들 수 있어야 판정 가능


def _weekly_closes(code, start, end):
    params = {"symbol": code, "requestType": "1", "timeframe": "week", "startTime": start, "endTime": end}
    url = f"{leader.SISE_URL}?{urllib.parse.urlencode(params)}"
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=leader.HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            rows = json.loads(body.strip().replace("'", '"'))[1:]
            return [(str(r[0]), float(r[4])) for r in rows if r[4] is not None]
        except Exception as e:
            last_err = e
            time.sleep(0.5 * (attempt + 1))
    raise last_err


def judge(weekly):
    """[(YYYYMMDD, 종가)] -> 정배열이면 {"weeks","since","gainSincePct","vsMa4Pct","truncated"}, 아니면 None.
    52주선이 없으면(상장 1년 미만) 'short'."""
    closes = [c for _, c in weekly]
    if len(closes) < MIN_BARS:
        return "short"
    mas = {p: leader._rolling(closes, p) for p in leader.MA_PERIODS}
    last = len(closes) - 1

    def aligned(i):
        v = [mas[p][i] for p in leader.MA_PERIODS]
        return all(x is not None for x in v) and v[0] > v[1] > v[2] > v[3]

    streak = 0
    while last - streak >= 0 and aligned(last - streak):
        streak += 1
    if not streak:
        return None
    start = last - streak + 1
    d = weekly[start][0]
    return {
        "weeks": streak,
        "since": f"{d[0:4]}-{d[4:6]}-{d[6:8]}",
        "gainSincePct": round((closes[last] / closes[start] - 1) * 100, 1),
        "vsMa4Pct": round((closes[last] / mas[4][last] - 1) * 100, 1),
        "truncated": start == 51,   # 데이터 맨 앞(52주선이 처음 생기는 주)부터 계속 정배열 — 실제 주수는 이보다 길 수 있음
    }


def scan(stocks, progress=None):
    """stocks: [(code, name, price, marketCapEok, market)] -> 결과 dict. progress(done, total) 콜백(선택)."""
    end = datetime.now(timezone.utc) + timedelta(hours=9)
    start = (end - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    end = end.strftime("%Y%m%d")
    total = len(stocks)
    state = {"done": 0}

    def one(item):
        code, name, price, cap, market = item
        try:
            r = judge(_weekly_closes(code, start, end))
        except Exception:
            r = "fail"
        state["done"] += 1
        if progress and state["done"] % 100 == 0:
            progress(state["done"], total)
        if isinstance(r, dict):
            return {"code": code, "name": name, "market": market, "price": price, "marketCap": cap, **r}
        return r

    items, short, failed = [], 0, 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for r in ex.map(one, stocks):
            if isinstance(r, dict):
                items.append(r)
            elif r == "short":
                short += 1
            elif r == "fail":
                failed += 1
    items.sort(key=lambda x: (-x["weeks"], -(x["marketCap"] or 0)))
    return {"items": items, "total": total, "short": short, "failed": failed}
