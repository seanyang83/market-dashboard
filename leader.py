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
DISCLOSURE_URL = "https://m.stock.naver.com/api/stock/{code}/disclosure?page=1&pageSize=500"
SISE_URL = "https://api.finance.naver.com/siseJson.naver"
FINANCE_TTL = 6 * 3600  # 분기에 한 번 바뀌는 데이터
WEEKLY_TTL = 600
WEEKLY_LOOKBACK_DAYS = 2500  # ~357주: 52주선 + 정배열 지속 기간 계산용
CHART_WEEKS = 104
MA_PERIODS = (4, 13, 26, 52)
LOW_BASE_OPM = 5.0

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
            "netIncome": val("지배주주순이익", key),
            "netIsParent": True,
        })
    return quarters


def _month_end(key):
    y, m = int(key[:4]), int(key[4:6])
    return datetime(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)


def _fetch_release_dates(code, keys):
    """네이버 공시 목록에서 분기별 실적 발표일(영업(잠정)실적 공정공시의 최초 공시일)을 찾는다.
    정정 공시는 제외하고, 잠정실적이 없으면 매출액·손익구조 변동 공시로 대신한다.
    공시일이 분기말 이후 110일 안이면 그 분기의 것으로 본다. -> {key: "YYYY-MM-DD"}"""
    url = DISCLOSURE_URL.format(code=code)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=12) as resp:
        rows = json.loads(resp.read())
    ends = {k: _month_end(k) for k in keys}
    primary, fallback = {}, {}
    for r in rows:
        title = r.get("title") or ""
        if "정정" in title:
            continue
        if "영업" in title and "잠정" in title and "실적" in title:
            bucket = primary
        elif "손익구조" in title and "자회사" not in title:
            bucket = fallback
        else:
            continue
        try:
            d = datetime.strptime(r["datetime"][:10], "%Y-%m-%d")
        except (KeyError, ValueError):
            continue
        cands = [k for k, e in ends.items() if e < d <= e + timedelta(days=110)]
        if not cands:
            continue
        k = max(cands)
        day = d.strftime("%Y-%m-%d")
        if k not in bucket or day < bucket[k]:
            bucket[k] = day
    return {k: primary.get(k) or fallback.get(k) for k in keys if primary.get(k) or fallback.get(k)}


def _growth(cur, base):
    """영업이익 증가율(%)과 상태 라벨. -> (rate, state)
    base > 0: 증가율을 계산하고, 이번에 적자면 state는 "적자전환".
    base <= 0(적자/0): 증가율은 의미가 없어 None, 이번에 흑자면 "흑자전환", 아니면 "적자지속"."""
    if cur is None or base is None:
        return None, None
    if base > 0:
        return round((cur - base) / base * 100, 1), ("적자전환" if cur < 0 else None)
    if cur > 0:
        return None, "흑자전환"
    return (None, None) if cur == 0 and base == 0 else (None, "적자지속")


def _compute_rates(quarters):
    """분기 리스트(키 오름차순)에 영업이익 증가율(전년동기 YoY / 전분기 QoQ)과 그 가속도
    (증가율의 변화, %p)를 채운다. 비교 분기는 인덱스가 아니라 키(YYYYMM) 산술로 찾는다.
    영업이익률(opm)과 그 변화는 성장의 질을 보는 보조 지표로 같이 남긴다."""
    by_key = {q["key"]: q for q in quarters}

    def low_base(prev):
        # 비교 기준 영업이익률이 5% 미만이면 증가율이 수백~수천 %로 부풀어 의미가 없다.
        return bool(prev and prev["opm"] is not None and 0 < prev["opm"] < LOW_BASE_OPM)

    for q in quarters:
        prev_q = by_key.get(_shift_key(q["key"], -3))
        prev_y = by_key.get(_shift_key(q["key"], -12))
        q["opYoyRate"], q["opYoyState"] = _growth(q["opIncome"], prev_y["opIncome"]) if prev_y else (None, None)
        q["opQoqRate"], q["opQoqState"] = _growth(q["opIncome"], prev_q["opIncome"]) if prev_q else (None, None)
        q["opYoyLowBase"] = low_base(prev_y)
        q["opQoqLowBase"] = low_base(prev_q)
        q["revenueYoyRate"] = _rate(q["revenue"], prev_y["revenue"]) if prev_y else None
        q["netYoyRate"], q["netYoyState"] = _growth(q["netIncome"], prev_y["netIncome"]) if prev_y else (None, None)
        # 한투 값(당기순이익, 지배 아님)이 한쪽이라도 섞이면 표에 †로 표시
        q["netYoyMixed"] = bool(prev_y and not (q.get("netIsParent") and prev_y.get("netIsParent")))
        q["opmYoyPp"] = _pp(q["opm"], prev_y["opm"]) if prev_y else None
        q["opmQoqPp"] = _pp(q["opm"], prev_q["opm"]) if prev_q else None
    for q in quarters:
        prev_q = by_key.get(_shift_key(q["key"], -3))
        cur, prev = q["revenueYoyRate"], prev_q["revenueYoyRate"] if prev_q else None
        q["revenueYoyAccel"] = round(cur - prev, 1) if cur is not None and prev is not None else None
        for name in ("Yoy", "Qoq"):
            cur = q[f"op{name}Rate"]
            prev = prev_q[f"op{name}Rate"] if prev_q else None
            usable = (cur is not None and prev is not None
                      and not q[f"op{name}LowBase"] and not prev_q[f"op{name}LowBase"])
            q[f"op{name}Accel"] = round(cur - prev, 1) if usable else None
    return quarters


def _fetch_kis_quarters(code, kis_get):
    """한투 손익계산서(분기)는 '연 단위 누적'이라 분기값으로 풀어서 돌려준다.
    같은 회계연도 안에서는 누적 매출이 늘어나므로, 직전 분기 누적보다 작아지면
    새 회계연도 1분기로 본다(결산월이 12월이 아닌 회사도 동작)."""
    data = kis_income_raw(code, kis_get)
    rows = data.get("output") or []
    ytd = {}
    for r in rows:
        key = str(r.get("stac_yymm") or "")
        rev, op = _num(r.get("sale_account")), _num(r.get("bsop_prti"))
        if len(key) == 6 and rev is not None:
            ytd[key] = (rev, op, _num(r.get("thtr_ntin")))
    out = []
    for key in sorted(ytd):
        rev, op, net = ytd[key]
        prev = ytd.get(_shift_key(key, -3))
        if prev is not None and rev >= prev[0]:
            rev_q = rev - prev[0]
            op_q = op - prev[1] if op is not None and prev[1] is not None else None
            net_q = net - prev[2] if net is not None and prev[2] is not None else None
        elif prev is None and key[4:6] != "03":
            continue  # 누적의 시작을 알 수 없는 맨 앞 분기
        else:
            rev_q, op_q, net_q = rev, op, net
        out.append({
            "key": key,
            "period": f"{key[:4]}.{key[4:6]}",
            "estimate": False,
            "revenue": rev_q,
            "opIncome": op_q,
            "opm": round(op_q / rev_q * 100, 2) if op_q is not None and rev_q else None,
            "netIncome": net_q,        # 한투는 지배/비지배 구분 없는 당기순이익
            "netIsParent": False,
        })
    return out


def _merge_quarters(naver, kis):
    """네이버(최근 5개 분기+컨센서스)와 한투(최대 30개 분기)를 키로 합친다. 겹치는 분기는
    네이버 값을 쓴다(화면에서 사용자가 보는 값과 같게). 한투가 없으면 네이버만."""
    merged = {q["key"]: q for q in kis}
    for q in naver:
        merged[q["key"]] = q
    return [merged[k] for k in sorted(merged)]


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
        break_date, break_reason = None, None
        if end < last:
            # end = 마지막으로 정배열이던 주, end+1 = 처음으로 깨진 주.
            break_date = dates[end + 1]
            m = [mas[p][end + 1] for p in MA_PERIODS]
            names = ("4주", "13주", "26주", "52주")
            broken = [f"{names[k]}<{names[k + 1]}" for k in range(3) if m[k] <= m[k + 1]]
            break_reason = ", ".join(broken)
        run = {
            "breakDate": break_date, "breakReason": break_reason,
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


BASIS = {
    "yoy": {"name": "전년동기", "rate": "opYoyRate", "state": "opYoyState", "low": "opYoyLowBase", "accel": "opYoyAccel"},
    "qoq": {"name": "전분기", "rate": "opQoqRate", "state": "opQoqState", "low": "opQoqLowBase", "accel": "opQoqAccel"},
}


def _sp(period):
    """2026.06 -> 26.06"""
    return period[2:]


def _earnings_state(quarters, basis="yoy"):
    """영업이익 증가율(전년동기 또는 전분기)의 추이로 실적 상태를 판정한다.
    반환: (state, line, flags). state: 가속 / 둔화 / 피크아웃 / 음전환 / 악화 / 증가 / None.
    line은 화면에 그대로 쓰는 한 줄 요약, flags는 붉은 글씨로 강조할 주요 사항."""
    b = BASIS[basis]
    actual = [q for q in quarters if not q["estimate"] and q["opIncome"] is not None]
    est = next((q for q in quarters if q["estimate"] and q[b["rate"]] is not None), None)
    if not actual:
        return None, "실제 분기 실적 없음", []
    latest = actual[-1]
    g, gstate = latest[b["rate"]], latest[b["state"]]
    pn = _sp(latest["period"])

    if g is None:
        if gstate == "흑자전환":
            return "가속", f"{pn} 흑자전환", []
        if gstate == "적자전환":
            return "음전환", f"{pn} 적자전환", [("neg", "bad", "영업이익 적자전환")]
        if gstate == "적자지속":
            return "악화", f"{pn} 적자 지속", [("neg", "bad", "영업이익 적자 지속")]
        return None, f"{pn} 비교 분기 없음", []
    if latest[b["low"]]:
        return None, f"{pn} {g:+,.1f}% · 기저 낮아(※) 판정 제외", []

    series = [(q["period"], q[b["rate"]]) for q in actual if q[b["rate"]] is not None and not q[b["low"]]]
    prev_g = series[-2][1] if len(series) >= 2 and series[-2][0] != latest["period"] else None
    flags = []

    if g <= 0:
        state = "음전환" if prev_g is not None and prev_g > 0 else "악화"
        flags.append(("neg", "bad", "영업이익 감소 전환" if state == "음전환" else "영업이익 감소 지속"))
        return state, f"{pn} {g:+,.1f}%" + (f" (직전 {prev_g:+,.1f}%)" if prev_g is not None else ""), flags

    line = f"{pn} {g:+,.1f}%"
    if prev_g is None:
        state = "증가"
    else:
        accel = round(g - prev_g, 1)
        peak_period, peak = max(series[-8:], key=lambda x: x[1])
        line += f" (직전 {prev_g:+,.1f}%, 가속 {accel:+,.1f}%p)"
        prev_accel = round(prev_g - series[-3][1], 1) if len(series) >= 3 else None
        if accel >= 0:
            state = "가속"
        elif prev_g >= peak:
            state = "피크아웃"
            flags.append(("peak", "bad", f"피크아웃 (최고 {_sp(peak_period)} {peak:+,.0f}%)"))
        else:
            state = "둔화"
            flags.append(("slow", "warn", f"최고({_sp(peak_period)} {peak:+,.0f}%) 대비 둔화"))
    if est is not None:
        line += f" · 다음 E {est[b['rate']]:+,.1f}%"
        if est[b["rate"]] < g:
            flags.append(("cons", "warn", "컨센서스상 다음 분기 둔화 전망"))
    return state, line, flags


def _trend(ys, qs, weekly):
    """YoY/QoQ 실적 상태와 주봉 정배열을 합친 한 줄 추세 판단. -> (text, tone)
    tone: good / warn / bad / none"""
    good, slow, bad = ("가속", "증가"), ("피크아웃", "둔화"), ("음전환", "악화")
    if ys is None and qs is None:
        return "판단 보류 (실적 데이터 부족)", "none"
    if ys in bad or qs in bad:
        text, tone = "실적 악화 — 주도주 이탈 의심", "bad"
    elif ys in good and qs in good:
        text, tone = "상승 가속 — 실적 모멘텀 강함", "good"
    elif ys in good and qs in slow:
        text, tone = "고성장은 유지, 분기 증가 속도는 둔화 — 정점 접근", "warn"
    elif ys in slow and qs in good:
        text, tone = "전년 대비로는 둔화, 직전 분기보다는 재가속 — 단기 반등", "warn"
    elif ys in slow and qs in slow:
        text, tone = "성장 둔화 — 정점 통과 가능성", "bad"
    elif ys in good or qs in good:
        text, tone = "실적 개선 중 (한쪽 기준만 가속)", "good"
    else:
        text, tone = "방향성 불분명", "none"
    if weekly["aligned"]:
        text += f" · 정배열 {weekly['streakWeeks']}주째"
    else:
        text += " · 정배열 아님"
    return text, tone


def _judge(quarters, weekly, basis="yoy"):
    earn, line, flags = _earnings_state(quarters, basis)
    tag = "YoY" if basis == "yoy" else "QoQ"
    flags = [{"kind": k, "level": lv, "tag": tag, "text": t} for k, lv, t in flags]
    aligned, streak = weekly["aligned"], weekly["streakWeeks"]
    run = weekly.get("run")

    if earn in ("가속", "증가") and aligned:
        # 책 기준: 주도주 정배열은 보통 1년(52주)이 사실상 한계, 길어야 2년(104주).
        if streak <= 13:
            label = "① 태동"
        elif streak <= 39:
            label = "② 추세"
        elif streak <= 52:
            label = "② 추세 후반 (1년 임박)"
        elif streak <= 104:
            label = "③ 1년 초과 (막바지 주의)"
        else:
            label = "④ 2년 초과 (이례적 장기)"
    elif earn in ("피크아웃", "둔화") and aligned:
        label = "③ 둔화 주의"
    elif earn in ("음전환", "악화") and aligned:
        label = "④ 종료 경고"
    elif earn in ("가속", "증가"):
        label = "관찰 (정배열 전)"
    elif earn in ("피크아웃", "둔화"):
        label = "③~④ 조정·종료 의심"
    elif earn in ("음전환", "악화"):
        label = "④ 종료"
    else:
        label = "판정 보류"
    return {"label": label, "earnings": earn, "line": line, "flags": flags}


KIS_INCOME_PATH = "/uapi/domestic-stock/v1/finance/income-statement"


def kis_income_raw(code, kis_get, div="1"):
    """한투 국내주식 손익계산서(FHKST66430200). div "1"=분기(연 단위 누적), "0"=연간.
    확인/디버그용으로 원본 응답을 그대로 돌려준다."""
    return kis_get(
        KIS_INCOME_PATH,
        "FHKST66430200",
        {"FID_DIV_CLS_CODE": div, "fid_cond_mrkt_div_code": "J", "fid_input_iscd": code},
    )


def analyze(code, kis_get=None):
    """kis_get: server.py의 kis_get. 있으면 한투 손익계산서로 최대 30개 분기 이력을 붙인다."""
    naver = _cached(("fin", code), FINANCE_TTL, lambda: _fetch_finance(code))
    kis, history_note = [], None
    if kis_get is not None:
        try:
            kis = _cached(("kis", code), FINANCE_TTL, lambda: _fetch_kis_quarters(code, kis_get))
        except Exception as e:
            history_note = f"한투 분기 이력을 불러오지 못해 최근 5개 분기만 사용했습니다 ({e})"
    # 한투 누적값은 사업 분할/재분류로 소급 수정된 분기가 반영되지 않을 수 있다.
    # 네이버와 겹치는 분기에서 매출이 3% 넘게 어긋나면 경고를 붙인다.
    kis_by = {q["key"]: q for q in kis}
    mismatched = []
    for q in naver:
        k = kis_by.get(q["key"])
        if not q["estimate"] and k and q["revenue"] and k["revenue"] is not None:
            if abs(k["revenue"] - q["revenue"]) / q["revenue"] > 0.03:
                mismatched.append(q["period"])
    quarters = _compute_rates(_merge_quarters(naver, kis))
    weekly_raw = _cached(("wk", code), WEEKLY_TTL, lambda: _fetch_weekly_closes(code))
    weekly = _weekly_analysis(weekly_raw)
    try:
        dates = _cached(("rel", code), FINANCE_TTL, lambda: _fetch_release_dates(code, [q["key"] for q in quarters]))
    except Exception:
        dates = {}  # 발표일은 부가 정보 — 실패해도 분석은 계속
    for q in quarters:
        q["releaseDate"] = dates.get(q["key"])
    stage = _judge(quarters, weekly, "yoy")
    stage_qoq = _judge(quarters, weekly, "qoq")
    notes = [history_note] if history_note else []
    if mismatched:
        notes.append(f"한투 이력과 네이버 값이 {', '.join(mismatched)}에서 어긋남 (소급 수정 종목일 수 있어 과거 증가율은 참고만)")
    text, tone = _trend(stage["earnings"], stage_qoq["earnings"], weekly)
    flags, cons_tags = [], []
    for f in stage["flags"] + stage_qoq["flags"]:
        if f["kind"] == "cons":
            cons_tags.append(f["tag"])
        else:
            flags.append({"level": f["level"], "text": f"{f['tag']} {f['text']}"})
    if cons_tags:
        flags.append({"level": "warn", "text": f"컨센서스상 다음 분기 둔화 전망 ({'·'.join(cons_tags)})"})
    streak = weekly["streakWeeks"]
    if weekly["aligned"] and streak > 104:
        flags.append({"level": "bad", "text": f"정배열 {streak}주째 — 2년 초과, 주도주 통상 한계 넘음"})
    elif weekly["aligned"] and streak > 52:
        flags.append({"level": "warn", "text": f"정배열 {streak}주째 — 1년 초과, 막바지 구간"})
    elif weekly["aligned"] and streak >= 40:
        flags.append({"level": "warn", "text": f"정배열 {streak}주째 — 1년 임박"})
    run = weekly.get("run")
    if not weekly["aligned"] and run and run.get("breakDate"):
        flags.append({"level": "warn", "text": f"정배열 이탈 ({run['breakReason']})"})
    flags.sort(key=lambda f: f["level"] != "bad")
    return {
        "code": code,
        "quarters": quarters,
        "weekly": weekly,
        # 분기 실적 차트와 나란히 그리는 주가(주봉 종가). 표시 분기(최근 12개+E)를 덮도록 4년치.
        "priceWeekly": {"dates": [d for d, _ in weekly_raw[-210:]], "close": [c for _, c in weekly_raw[-210:]]},
        "stage": stage,
        "stageQoq": stage_qoq,
        "trend": {"text": text, "tone": tone, "flags": flags, "notes": notes},
    }
