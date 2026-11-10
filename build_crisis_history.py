#!/usr/bin/env python3
"""과거 코스피 위기(고점 대비 -30% 이하)를 뽑아 crisis_history.json 으로 저장한다 (위기 상황판 '과거 위기 겹쳐 보기'용).

사용:  python3 build_crisis_history.py            # 네이버 일봉을 받아 새로 만든다
       python3 build_crisis_history.py kospi.json # [[YYYYMMDD, 종가], ...] 파일에서 만든다

정의(상황판 /crisis 와 같은 방식):
- 고점 = 그날 기준 직전 400달력일 최고 종가. 낙폭 = 종가 / 고점 - 1.
- 낙폭 -30% 이하인 날을 120거래일 이내로 이어지면 한 위기로 묶고, 묶음에서 낙폭이 가장 깊은 날을 저점,
  그 저점 날짜 기준 고점을 그 위기의 고점으로 삼는다.
- 저점이 확정된 지 120거래일이 안 된 위기(= 지금 진행 중인 위기)는 과거로 치지 않아 뺀다.
- 곡선 = 고점일부터 최대 600거래일까지의 고점 대비 낙폭(%, 소수 1자리).
주의: 코스피 일봉이 1990-01-04부터라 1990년 위기의 고점은 실제 고점보다 낮을 수 있다.
"""
import bisect
import json
import os
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "crisis_history.json")
PEAK_DAYS = 400
THRESH = -0.30
GAP = 120
CURVE_LEN = 600
SETTLE = 120
NAMES = {  # 저점 연월 → 이름 (널리 알려진 것만)
    "1998-06": "IMF 외환위기", "2000-12": "닷컴 거품", "2008-10": "글로벌 금융위기", "2020-03": "코로나",
}


def load_rows(argv):
    if len(argv) > 1:
        rows = json.load(open(argv[1]))
        return sorted((str(a), float(b)) for a, b in rows)
    sys.path.insert(0, HERE)
    import server
    return sorted(server._fetch_index_closes("KOSPI", 16000).items())


def build(rows):
    dates = [d for d, _ in rows]
    vals = [v for _, v in rows]
    n = len(rows)
    dts = [datetime.strptime(d, "%Y%m%d") for d in dates]
    pk = []
    for t in range(n):
        s = bisect.bisect_left(dts, dts[t] - timedelta(days=PEAK_DAYS))
        pk.append(max(range(s, t + 1), key=lambda i: vals[i]))
    dd = [vals[t] / vals[pk[t]] - 1 for t in range(n)]
    days = [t for t in range(n) if dd[t] <= THRESH]
    clusters = []
    for t in days:
        if not clusters or t - clusters[-1][-1] > GAP:
            clusters.append([t])
        else:
            clusters[-1].append(t)
    iso = lambda d: "%s-%s-%s" % (d[:4], d[4:6], d[6:])
    eps = []
    for c in clusters:
        tr = min(c, key=lambda t: dd[t])
        if tr > n - 1 - SETTLE:
            continue
        p = pk[tr]
        h20 = next(t for t in range(p, n) if vals[t] <= vals[p] * 0.8)
        h30 = next(t for t in range(p, n) if vals[t] <= vals[p] * 0.7)
        h40 = next((t for t in range(p, n) if vals[t] <= vals[p] * 0.6), None)
        rec = next((t for t in range(tr, n) if vals[t] >= vals[p]), None)
        end = min(n - 1, p + CURVE_LEN)
        ym = iso(dates[tr])[:7]
        eps.append({
            "id": dates[p],
            "name": NAMES.get(ym),
            "peakDate": iso(dates[p]), "peakClose": round(vals[p], 2),
            "troughDate": iso(dates[tr]), "troughClose": round(vals[tr], 2),
            "troughDd": round(dd[tr] * 100, 1), "daysToTrough": tr - p,
            "daysTo20": h20 - p, "daysTo30": h30 - p, "daysTo40": None if h40 is None else h40 - p,
            "daysFrom30ToTrough": tr - h30,
            "extraAfter30": round((vals[tr] / vals[h30] - 1) * 100, 1),
            "recoveryDays": None if rec is None else rec - tr,
            "curve": [round((vals[i] / vals[p] - 1) * 100, 1) for i in range(p, end + 1)],
        })
    return {"asOf": iso(dates[-1]), "threshold": int(THRESH * 100), "peakWindowDays": PEAK_DAYS, "curveLen": CURVE_LEN,
            "note": "1990-01-04 이전 일봉이 없어 1990년 위기의 고점은 실제보다 낮을 수 있음", "episodes": eps}


if __name__ == "__main__":
    res = build(load_rows(sys.argv))
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, separators=(",", ":"))
    for e in res["episodes"]:
        print(e["peakDate"], e["troughDate"], e["troughDd"], e["daysToTrough"], e["daysTo30"], e["extraAfter30"], e["recoveryDays"], e["name"])
    print("저장:", OUT, os.path.getsize(OUT), "bytes")
