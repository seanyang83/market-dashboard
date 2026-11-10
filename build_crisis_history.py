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


DISP_CRISIS = -0.25        # 이격도 통계 대상: 직전 400일 고점 대비 -25% 이하인 날
DISP_QUANT = (1, 2, 3, 5, 10, 15, 20, 25, 30, 40, 50, 60, 70, 80, 90, 95)
DISP_TH = (-8, -10, -12, -15)   # 20일 이격도 기준(%)


def _median(a):
    b = sorted(a)
    m = len(b) // 2
    return b[m] if len(b) % 2 else (b[m - 1] + b[m]) / 2


def disparity_stats(dates, vals, pk, dd):
    """위기일(-25% 이하)에서 이평선 이격도 분포와, 20일 이격도가 크게 벌어졌을 때 이후 5·10·20거래일 성과."""
    n = len(vals)

    def disp(t, k):
        return None if t < k - 1 else vals[t] / (sum(vals[t - k + 1:t + 1]) / k) - 1

    crisis = [t for t in range(260, n) if dd[t] <= DISP_CRISIS]
    out = {"crisisDays": len(crisis), "crisisDef": "직전 400일 고점 대비 -25% 이하", "quantiles": {}, "stats20": []}
    for k in (20, 60, 120):
        d = sorted(disp(t, k) * 100 for t in crisis if disp(t, k) is not None)
        out["quantiles"][str(k)] = [[q, round(d[min(len(d) - 1, int(len(d) * q / 100))], 1)] for q in DISP_QUANT]

    def clusters(ts, gap=10):
        cl = []
        for t in ts:
            if not cl or t - cl[-1][-1] > gap:
                cl.append([t])
            else:
                cl[-1].append(t)
        return cl

    def row(label, ts):
        ts = [t for t in ts if t + 20 < n]
        if not ts:
            return None
        fw = lambda t, h: vals[t + h] / vals[t] - 1
        newlow = lambda t: min(vals[t + 1:t + 21]) < min(vals[pk[t]:t + 1])
        first = [c[0] for c in clusters(ts)]
        pc = lambda xs: round(100 * sum(xs) / len(xs))
        return {"label": label, "days": len(ts), "clusters": len(first),
                "up5": pc([fw(t, 5) > 0 for t in ts]), "up10": pc([fw(t, 10) > 0 for t in ts]),
                "med5": round(_median([fw(t, 5) for t in ts]) * 100, 1), "med10": round(_median([fw(t, 10) for t in ts]) * 100, 1),
                "newLow20": pc([newlow(t) for t in ts]),
                "cUp5": sum(fw(t, 5) > 0 for t in first), "cUp10": sum(fw(t, 10) > 0 for t in first),
                "cNewLow20": sum(newlow(t) for t in first)}

    out["stats20"].append(row("전체", crisis))
    for th in DISP_TH:
        out["stats20"].append(dict(row("%d%% 이하" % th, [t for t in crisis if disp(t, 20) is not None and disp(t, 20) <= th / 100]), th=th))
    return out


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
    disparity = disparity_stats(dates, vals, pk, dd)
    return {"asOf": iso(dates[-1]), "threshold": int(THRESH * 100), "disparity": disparity, "peakWindowDays": PEAK_DAYS, "curveLen": CURVE_LEN,
            "note": "1990-01-04 이전 일봉이 없어 1990년 위기의 고점은 실제보다 낮을 수 있음", "episodes": eps}


if __name__ == "__main__":
    res = build(load_rows(sys.argv))
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, separators=(",", ":"))
    for e in res["episodes"]:
        print(e["peakDate"], e["troughDate"], e["troughDd"], e["daysToTrough"], e["daysTo30"], e["extraAfter30"], e["recoveryDays"], e["name"])
    print("저장:", OUT, os.path.getsize(OUT), "bytes")
