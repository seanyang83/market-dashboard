"""지수 국면(Stage) 판정 — 순수 함수. 일지 시트의 Stage 칸(상승장/상승전환/횡보장/하락전환/하락장)과 같은 규칙.

종가 기준:
  1. 최근 20거래일 종가 고저폭 <= 7%            -> 횡보장
  2. 최근 60거래일 고점 대비 낙폭 > -5%  + 종가 >= 20일선 -> 상승장
  3. 낙폭 > -10% + 종가 < 20일선                 -> 하락전환 (고점 근처에서 20일선 이탈)
  4. 낙폭 <= -10% + 종가 < 20일선                -> 하락장
  5. 낙폭 <= -10% + 종가 >= 20일선               -> 상승전환 (크게 빠진 뒤 20일선 회복)
  6. 그 외(낙폭 -5~-10%, 20일선 위)               -> 횡보장
새 상태가 2거래일 연속 같을 때만 상태를 바꾼다(하루짜리 신호 무시).
"""

RANGE_DAYS, RANGE_MAX = 20, 0.07
HIGH_DAYS = 60
NEAR_HIGH, CORRECTION = -0.05, -0.10


def raw_label(closes, i):
    """closes[i]까지의 종가로 그날의 원시 상태. 데이터가 부족하면 None."""
    if i < HIGH_DAYS - 1:
        return None
    win20 = closes[i - RANGE_DAYS + 1:i + 1]
    if max(win20) / min(win20) - 1 <= RANGE_MAX:
        return "횡보장"
    dd = closes[i] / max(closes[i - HIGH_DAYS + 1:i + 1]) - 1
    above = closes[i] >= sum(win20) / RANGE_DAYS
    if dd > NEAR_HIGH and above:
        return "상승장"
    if dd > CORRECTION and not above:
        return "하락전환"
    if dd <= CORRECTION:
        return "상승전환" if above else "하락장"
    return "횡보장"


def label_series(closes):
    """날짜 오름차순 종가 리스트 -> 각 날의 확정 상태(2일 연속 확인) 리스트. 앞쪽 데이터 부족분은 None."""
    raws = [raw_label(closes, i) for i in range(len(closes))]
    out, cur = [], None
    for i, x in enumerate(raws):
        if x is None:
            out.append(None)
            continue
        if cur is None or (x != cur and raws[i - 1] == x):
            cur = x
        out.append(cur)
    return out


def detail(closes):
    """마지막 날 기준 설명 값: {stage, rawStage, close, ma20, vsMa20Pct, dd60Pct, range20Pct, since(index)}."""
    labels = label_series(closes)
    i = len(closes) - 1
    if labels[i] is None:
        return None
    ma20 = sum(closes[i - 19:i + 1]) / 20
    win20 = closes[i - 19:i + 1]
    since = i
    while since - 1 >= 0 and labels[since - 1] == labels[i]:
        since -= 1
    return {
        "stage": labels[i],
        "rawStage": raw_label(closes, i),
        "close": closes[i],
        "ma20": round(ma20, 2),
        "vsMa20Pct": round((closes[i] / ma20 - 1) * 100, 2),
        "dd60Pct": round((closes[i] / max(closes[i - HIGH_DAYS + 1:i + 1]) - 1) * 100, 2),
        "range20Pct": round((max(win20) / min(win20) - 1) * 100, 2),
        "sinceIndex": since,
    }
