#!/usr/bin/env python3
"""장중 가집계 수급(est)이 확정값(post/final)과 얼마나 맞는지 집계한다.

사용: python3 flowcheck_report.py [CSV경로]
CSV가 없으면 data 브랜치(origin/data)의 data/flow_check.csv 를 읽는다(먼저 `git fetch origin data`).
기준: 외국인·기관 각각 부호(+/-)가 같은지. 어느 한쪽이 0이거나 비어 있으면 부호 비교에서 뺀다.
"""
import csv
import io
import subprocess
import sys
from collections import defaultdict

NXT_BIG = {"000660", "005930"}   # NXT 거래가 큰 종목(나머지와 나눠서 본다)


def load(path=None):
    if path:
        text = open(path, encoding="utf-8").read()
    else:
        text = subprocess.run(["git", "show", "origin/data:data/flow_check.csv"], capture_output=True,
                              text=True, check=True).stdout
    return list(csv.DictReader(io.StringIO(text)))


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def sign(v):
    return None if v is None or v == 0 else (1 if v > 0 else -1)


def pct(a, b):
    return "-" if b == 0 else f"{a / b * 100:.0f}% ({a}/{b})"


def main():
    rows = load(sys.argv[1] if len(sys.argv) > 1 else None)
    by = defaultdict(dict)   # (date, code) -> {slot: row}
    for r in rows:
        by[(r["date"], r["code"])][r["slot"]] = r
    days = sorted({d for d, _ in by})
    print(f"기록 일수 {len(days)}일 ({days[0] if days else '-'} ~ {days[-1] if days else '-'}), 종목-일 {len(by)}건\n")
    pairs = (("est", "post"), ("est", "final"), ("post", "final"))
    for who, field in (("외국인", "foreign"), ("기관", "inst")):
        print(f"[{who}] 부호 일치율")
        for a, b in pairs:
            groups = {"전체": [0, 0], "NXT 큰 종목(하이닉스·삼성전자)": [0, 0], "그 외": [0, 0]}
            miss = 0
            for (d, code), slots in by.items():
                if a not in slots or b not in slots:
                    continue
                sa, sb = sign(to_int(slots[a][field])), sign(to_int(slots[b][field]))
                if sa is None or sb is None:
                    miss += 1
                    continue
                for g in ("전체", "NXT 큰 종목(하이닉스·삼성전자)" if code in NXT_BIG else "그 외"):
                    groups[g][1] += 1
                    groups[g][0] += (sa == sb)
            line = ", ".join(f"{g} {pct(*v)}" for g, v in groups.items())
            print(f"  {a}→{b}: {line}  | 값 없음/0 제외 {miss}건")
        print()
    # 수량이 크게 틀린 날(부호 불일치 중 final 기준 금액이 큰 순)
    bad = []
    for (d, code), slots in by.items():
        if "est" in slots and "final" in slots:
            e, f = to_int(slots["est"]["foreign"]), to_int(slots["final"]["foreign"])
            if sign(e) and sign(f) and sign(e) != sign(f):
                bad.append((abs(f), d, code, slots["est"]["name"] or slots["final"]["name"], e, f))
    bad.sort(reverse=True)
    print("외국인 부호 불일치(est→final), 확정 수량 큰 순 상위 10")
    for _, d, code, name, e, f in bad[:10]:
        print(f"  {d} {code} {name}: 가집계 {e:+,} → 확정 {f:+,}")
    if not bad:
        print("  없음")
    print("\n판단 기준(미리 정함): 일치율 90%+ 그대로 사용 / 70~90% '참고용' 표시 / 70% 미만 장중 외국인 판정 미사용")
    print("주의: 같은 날 종목들은 시장 흐름에 같이 움직여 실제 독립 표본은 건수보다 적다. 10거래일은 감 잡기, 20거래일에 결론.")


if __name__ == "__main__":
    main()
