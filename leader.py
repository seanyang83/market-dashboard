"""주도주 분석 (1단계): 분기 실적(영업이익률 변화율) + 주봉 4-13-26-52 정배열.

데이터는 전부 네이버에서 가져온다 (분기 실적/컨센서스: m.stock.naver.com,
주봉: api.finance.naver.com/siseJson.naver). 네이버 분기 실적은 최근 5개 분기 +
컨센서스 1개 분기뿐이라 "가속/둔화(변화율의 변화)"는 아직 판정하지 못하고,
컨센서스 분기와 직전 실제 분기의 전년동기 대비 변화율을 비교하는 정도다.
이력이 더 필요하면 DART(2단계)로 채울 것.
"""
import ast
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com"}
FINANCE_URL = "https://m.stock.naver.com/api/stock/{code}/finance/quarter"
SISE_URL = "https://api.finance.naver.com/siseJson.naver"
FINANCE_TTL = 6 * 3600  # 분기에 한 번 바뀌는 데이터
WEEKLY_TTL = 600
WEEKLY_LOOKBACK_DAYS = 2500  # ~357주: 52주선 + 정배열 지속 기간 계산용
CHART_WEEKS = 104
MA_PERIODS = (4, 13, 26, 52)

_cache = {}
_cache_lock = threading.Lock()


class NoFinance(Exception):
    """실적 데이터가 없는 종목(ETF, 신규상장 등)."""


def _cached(key, ttl, loader):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = loader()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def _num(text):
    if text is None:
        return None
    text = str(text).replace(",", "").strip()
    if text in ("", "-", "N/A"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _rate(cur, base):
    """변화율(%). 기준값이 0 이하이면(적자/0) 의미가 없어 None."""
    if cur is None or base is None or base <= 0:
        return None
    return round((cur - base) / base * 100, 1)


def _pp(cur, base):
    if cur is None or base is None:
        return None
    return round(cur - base, 2)


def _shift_key(key, months):
    """'202606' -> months만큼 이동한 'YYYYMM'."""
    y, m = int(key[:4]), int(key[4:6])
    idx = y * 12 + (m - 1) + months
    return f"{idx // 12:04d}{idx % 12 + 1:02d}"


def _fetch_finance(code):
    url = FINANCE_URL.format(code=code)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise NoFinance(f"실적 데이터를 찾을 수 없습니다 (HTTP {e.code})")
    info = (data or {}).get("financeInfo") or {}
    titles = info.get("trTitleList") or []
    rows = {r["title"]: r.get("columns", {}) for r in info.get("rowList") or []}
    if not titles or "영업이익률" not in rows:
        raise NoFinance("분기 실적이 없는 종목입니다 (ETF·신규상장 등)")

    def val(row, key):
        return _num((rows.get(row, {}).get(key) or {}).get("value"))

    quarters = []
    for t in titles:
        key = t["key"]
        quarters.append({
            "key": key,
            "period": t["title"].rstrip("."),
            "estimate": t.get("isConsensus") == "Y",
            "revenue": val("매출액", key),
            "opIncome": val("영업이익", key),
            "opm": val("영업이익률", key),
        })
    by_key = {q["key"]: q for q in quarters}
    for q in quarters:
        prev_q = by_key.get(_shift_key(q["key"], -3))
        prev_y = by_key.get(_shift_key(q["key"], -12))
        q["opmQoqRate"] = _rate(q["opm"], prev_q["opm"]) if prev_q else None
        q["opmQoqPp"] = _pp(q["opm"], prev_q["opm"]) if prev_q else None
        q["opmYoyRate"] = _rate(q["opm"], prev_y["opm"]) if prev_y else None
        q["opmYoyPp"] = _pp(q["opm"], prev_y["opm"]) if prev_y else None
        q["revenueYoyRate"] = _rate(q["revenue"], prev_y["revenue"]) if prev_y else None
        q["opIncomeYoyRate"] = _rate(q["opIncome"], prev_y["opIncome"]) if prev_y else None
    return quarters


def _fetch_weekly_closes(code):
    end = datetime.now(timezone.utc) + timedelta(hours=9)
    start = end - timedelta(days=WEEKLY_LOOKBACK_DAYS)
    params = {
        "symbol": code, "requestType": "1", "timeframe": "week",
        "startTime": start.strftime("%Y%m%d"), "endTime": end.strftime("%Y%m%d"),
    }
    req = urllib.request.Request(f"{SISE_URL}?{urllib.parse.urlencode(params)}", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    rows = ast.literal_eval(body.strip().replace("null", "None"))[1:]
    out = []
    for r in rows:
        d = str(r[0])
        if r[4] is not None:
            out.append((f"{d[0:4]}-{d[4:6]}-{d[6:8]}", float(r[4])))
    if len(out) < 10:
        raise NoFinance("주봉 데이터가 부족합니다")
    return out


def _rolling(values, period):
    out = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def _weekly_analysis(weekly):
    dates = [d for d, _ in weekly]
    closes = [c for _, c in weekly]
    mas = {p: _rolling(closes, p) for p in MA_PERIODS}

    def aligned(i):
        vals = [mas[p][i] for p in MA_PERIODS]
        return all(v is not None for v in vals) and vals[0] > vals[1] > vals[2] > vals[3]

    last = len(closes) - 1
    streak = 0
    while last - streak >= 0 and aligned(last - streak):
        streak += 1
    since = dates[last - streak + 1] if streak else None

    # 가장 최근 정배열 구간: 지금 진행 중이면 그 구간, 이미 깨졌으면 마지막으로 정배열이었던
    # 구간. 시작 이후 최고점까지의 상승률을 본다(최고점은 구간이 끝난 뒤 포함 현재까지).
    run = None
    end = last
    while end >= 0 and not aligned(end):
        end -= 1
    if end >= 0:
        start = end
        while start - 1 >= 0 and aligned(start - 1):
            start -= 1
        peak = max(range(start, last + 1), key=lambda i: closes[i])
        run = {
            "active": end == last,
            "weeks": end - start + 1,
            "startDate": dates[start], "startClose": closes[start],
            "endDate": dates[end],
            "peakDate": dates[peak], "peakClose": closes[peak],
            "peakGainPct": round((closes[peak] / closes[start] - 1) * 100, 1),
            "nowGainPct": round((closes[last] / closes[start] - 1) * 100, 1),
            "fromPeakPct": round((closes[last] / closes[peak] - 1) * 100, 1),
        }

    # 정배열 시작 이후 처음으로 4주선이 13주선을 아래로 뚫은 주(데드크로스).
    if run:
        for i in range(start + 1, last + 1):
            a0, b0, a1, b1 = mas[4][i - 1], mas[13][i - 1], mas[4][i], mas[13][i]
            if None in (a0, b0, a1, b1):
                continue
            if a0 >= b0 and a1 < b1:
                run["deadCross"] = {
                    "date": dates[i], "close": closes[i],
                    "weeksAfterStart": i - start,
                    "weeksFromPeak": i - peak,  # 음수면 고점 전, 양수면 고점 후
                    "gainFromStartPct": round((closes[i] / closes[start] - 1) * 100, 1),
                    "fromPeakPct": round((closes[i] / closes[peak] - 1) * 100, 1),
                    "_i": i,
                }
                break
        else:
            run["deadCross"] = None

    # 차트는 기본 104주, 정배열 시작점이 더 과거면 거기까지 늘려서 시작 표시가 보이게 한다.
    n = min(len(closes), max(CHART_WEEKS, (last - start + 10) if run else 0))
    offset = len(closes) - n
    if run:
        run["startIdx"] = start - offset
        run["peakIdx"] = peak - offset
        run["endIdx"] = end - offset
        dc = run.get("deadCross")
        if dc:
            dc["idx"] = dc.pop("_i") - offset
    sl = slice(offset, None)
    ma_last = {p: mas[p][last] for p in MA_PERIODS}
    return {
        "aligned": aligned(last),
        "streakWeeks": streak,
        "since": since,
        "run": run,
        "price": closes[last],
        "asOf": dates[last],
        "mas": {str(p): round(v, 1) if v is not None else None for p, v in ma_last.items()},
        "priceVsMa4Pct": round((closes[last] / ma_last[4] - 1) * 100, 1) if ma_last[4] else None,
        "priceVsMa52x": round(closes[last] / ma_last[52], 2) if ma_last[52] else None,
        "series": {
            "dates": dates[sl],
            "close": closes[sl],
            **{f"ma{p}": [None if v is None else round(v, 1) for v in mas[p][sl]] for p in MA_PERIODS},
        },
    }


def _judge(quarters, weekly):
    """1단계 잠정 판정. 이력이 5개 분기뿐이라 '가속/둔화'는 컨센서스 분기와 직전
    실제 분기의 전년동기 대비 변화율을 비교하는 것으로 갈음한다."""
    actual = [q for q in quarters if not q["estimate"] and q["opm"] is not None]
    est = next((q for q in quarters if q["estimate"] and q["opmYoyRate"] is not None), None)
    latest = actual[-1] if actual else None
    yoy = latest["opmYoyRate"] if latest else None
    reasons = []

    if latest is None:
        earn = None
        reasons.append("실제 분기 실적이 없어 실적 상태를 판단할 수 없습니다")
    elif yoy is None:
        earn = None
        reasons.append(f"{latest['period']} 영업이익률의 전년동기 대비 변화율을 구할 수 없습니다 (비교 기간 없음 또는 적자 기저)")
    else:
        reasons.append(f"{latest['period']} 영업이익률 전년동기 대비 {yoy:+.1f}%")
        if yoy <= 0:
            earn = "악화"
        elif est is not None:
            earn = "가속" if est["opmYoyRate"] > yoy else "둔화"
            reasons.append(
                f"컨센서스 {est['period']} 전년동기 대비 {est['opmYoyRate']:+.1f}% → "
                f"직전 분기보다 {'높아져 가속' if earn == '가속' else '낮아져 둔화'} 전망"
            )
        else:
            earn = "증가"
            reasons.append("컨센서스가 없어 가속/둔화는 판단하지 못했습니다")

    aligned, streak = weekly["aligned"], weekly["streakWeeks"]
    if aligned:
        reasons.append(f"주봉 4>13>26>52 정배열 {streak}주째 ({weekly['since']}부터)")
    else:
        reasons.append("주봉 4-13-26-52 정배열이 아닙니다")

    if earn in ("가속", "증가") and aligned:
        if streak <= 13:
            label = "① 태동 (실적 증가 + 정배열 진입 초기)"
        elif streak <= 104:
            label = "② 추세 (실적 증가 + 정배열 유지)"
        else:
            label = "② 추세 장기화 — 정배열 2년 초과, 둔화 여부 주시"
    elif earn in ("둔화", "악화") and aligned:
        label = "③ 둔화 주의 (정배열 유지 중이지만 이익 증가세 둔화/악화)"
    elif earn in ("가속", "증가"):
        label = "관찰 (실적은 좋아지지만 아직 정배열 아님)"
    elif earn == "둔화":
        label = "③~④ 둔화 + 정배열 이탈 (조정 또는 종료 구간 의심)"
    elif earn == "악화":
        label = "④ 종료 (이익 악화 + 정배열 아님)"
    else:
        label = "판정 보류 (실적 데이터 부족)"
    return {"label": label, "earnings": earn, "reasons": reasons}


def analyze(code):
    quarters = _cached(("fin", code), FINANCE_TTL, lambda: _fetch_finance(code))
    weekly_raw = _cached(("wk", code), WEEKLY_TTL, lambda: _fetch_weekly_closes(code))
    weekly = _weekly_analysis(weekly_raw)
    return {
        "code": code,
        "quarters": quarters,
        "weekly": weekly,
        "stage": _judge(quarters, weekly),
    }
