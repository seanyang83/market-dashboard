#!/usr/bin/env python3
"""삼성전자·SK하이닉스 주가 사이클(고점 → 저점 → 전고점 회복) 표를 semi_cycles.json 으로 저장한다 (위기 상황판 '반도체 사이클 참고'용).

사용:  python3 build_semi_cycles.py          # 네이버 일봉(수정주가, 1996~)을 받아 새로 만든다
       python3 build_semi_cycles.py --check  # 저장하지 않고 표만 출력

정의(종가 기준):
- 고점: 아래 PEAKS 에 직접 적은 '큰 고점'(지그재그 35%/30% 이상 등락에서 골랐다). 새 사이클이 생기면 PEAKS 에 추가한다.
- 저점: 그 고점 이후 '전고점(그 고점의 종가)을 다시 넘기 전'까지의 가장 낮은 종가. 회복 못 했으면 지금까지의 최저.
- 회복: 고점 이후 처음으로 종가가 그 고점 종가를 넘은 날. 저점→회복, 고점→회복 기간을 년으로.
- 상승: 직전 사이클의 저점(첫 줄은 START)에서 이번 고점까지.
- 삼성전자 2024.7 고점(87,800)은 2021.1 고점(91,000)을 못 넘은 고점이라 2021.1 사이클 안에 묶인다.
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "semi_cycles.json")
STOCKS = [
    {"code": "000660", "name": "SK하이닉스", "start": "20040102",
     "peaks": ["20060918", "20110421", "20140708", "20180523", "20210225", "20240711", "20260622"]},
    {"code": "005930", "name": "삼성전자", "start": "20041213",
     "peaks": ["20060131", "20080515", "20110128", "20130102", "20171101", "20210111", "20260618"]},
]


def closes(code):
    u = "https://api.finance.naver.com/siseJson.naver?symbol=%s&requestType=1&startTime=19960101&endTime=%s&timeframe=day" % (
        code, (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y%m%d"))
    t = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=90).read().decode("utf-8")
    return [(d, int(c)) for d, o, h, l, c in re.findall(r'\["(\d{8})",\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)', t)]


def yrs(a, b):
    return round((datetime.strptime(b, "%Y%m%d") - datetime.strptime(a, "%Y%m%d")).days / 365.25, 1)


def iso(d):
    return "%s-%s-%s" % (d[:4], d[4:6], d[6:])


def build_stock(spec):
    s = closes(spec["code"])
    idx = {d: i for i, (d, _) in enumerate(s)}
    prev = idx[spec["start"]]          # 직전 저점(첫 줄은 START)
    rows, cur = [], None
    for k, pk in enumerate(spec["peaks"]):
        i = idx[pk]
        P = s[i][1]
        j = next((x for x in range(i + 1, len(s)) if s[x][1] > P), None)
        end = j if j is not None else len(s) - 1
        m = min(range(i, end + 1), key=lambda x: s[x][1])
        base = {
            "peakDate": iso(s[i][0]), "peakClose": P,
            "riseFromDate": iso(s[prev][0]), "riseFromClose": s[prev][1],
            "risePct": round((P / s[prev][1] - 1) * 100), "riseYears": yrs(s[prev][0], s[i][0]),
            "lowDate": iso(s[m][0]), "lowClose": s[m][1],
            "lowPct": round((s[m][1] / P - 1) * 100), "fallYears": yrs(s[i][0], s[m][0]),
        }
        if j is None:                    # 아직 전고점 회복 못 한 진행 중 사이클
            cur = dict(base, lastDate=iso(s[-1][0]), lastClose=s[-1][1], sincePeakYears=yrs(s[i][0], s[-1][0]))
        else:
            rows.append(dict(base, recDate=iso(s[j][0]), recYears=yrs(s[m][0], s[j][0]), totalYears=yrs(s[i][0], s[j][0])))
        prev = m
    return {"code": spec["code"], "name": spec["name"], "rows": rows, "current": cur}


if __name__ == "__main__":
    res = {"asOf": None, "stocks": [build_stock(sp) for sp in STOCKS]}
    res["asOf"] = max(st["current"]["lastDate"] for st in res["stocks"] if st["current"])
    for st in res["stocks"]:
        print("==", st["name"])
        for r in st["rows"]:
            print(" %s 고점 %s | 상승 %+d%% %.1f년 | 저점 %s %d%% (고점후 %.1f년) | 저점→회복 %.1f년 (%s) | 고점→회복 %.1f년" % (
                r["peakDate"], format(r["peakClose"], ","), r["risePct"], r["riseYears"], r["lowDate"], r["lowPct"], r["fallYears"], r["recYears"], r["recDate"], r["totalYears"]))
        c = st["current"]
        if c:
            print(" 진행 %s 고점 %s | 상승 %+d%% %.1f년 | 최저 %s %d%% | 최근 %s %s (고점후 %.1f년)" % (
                c["peakDate"], format(c["peakClose"], ","), c["risePct"], c["riseYears"], c["lowDate"], c["lowPct"], c["lastDate"], format(c["lastClose"], ","), c["sincePeakYears"]))
    if "--check" not in sys.argv:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, separators=(",", ":"))
        print("저장:", OUT, os.path.getsize(OUT), "bytes")
