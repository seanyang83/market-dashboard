"""120분봉 RSI 과매도 분할매수 규칙 (SK하이닉스 백테스트 기반, 참고용).

규칙
- 120분봉(키움식, 하루 4봉: 09:00~11:00, 11:00~13:00, 13:00~15:00, 15:00~15:30)의 RSI(14)가
  30 미만인 봉 = "과매도 신호". 신호 사이 간격이 16봉(4일) 이내면 같은 묶음.
- 묶음에서 2번째 신호에 절반, 3번째 신호에 나머지 절반 매수(1번째는 관찰만).
- 3번째 신호 후 20봉(5일) 안에 종가가 2번째 신호 가격을 넘으면 절반 매도.
- 나머지 절반은 3번째 신호 후 40봉(10일) 안에 종가가 1번째 신호 가격을 넘거나 RSI 50 이상이면 매도.
- 기간 안에 못 넘으면 정리 검토 알림.

순수 함수만 둔다(네트워크/전역 상태 없음) - 서버가 분봉을 모아 build_bars로 넘기고,
evaluate_bar가 "이번 봉에서 새로 생긴 이벤트"만 돌려준다. 같은 봉을 다시 평가해도
같은 결과라서 재시작 후에도 상태 저장 없이 이어서 동작한다(중복 방지는 서버가 봉 단위로).
"""

SLOTS = (("A", "0900", "1100"), ("B", "1100", "1300"), ("C", "1300", "1500"), ("D", "1500", "1531"))
SLOT_END = {"A": "11:00", "B": "13:00", "C": "15:00", "D": "15:30"}
RSI_PERIOD = 14
OVERSOLD = 30.0
CLUSTER_GAP = 16      # 봉 (4일)
FIRST_LEG = 20        # 봉 (5일): 2번째 가격 돌파 대기
SECOND_LEG = 40       # 봉 (10일): 나머지 절반 매도 대기
REST_RSI = 50.0


def _f(x):
    return float(x) if x not in (None, "") else None


def build_bars(day_rows, upto=None):
    """{YYYYMMDD: [[HHMM, 시, 고, 저, 종, 거래량], ...]} -> 120분봉 리스트.
    upto=(date, slot)이면 그 봉까지만(그 봉 포함) 만든다 - 진행 중인 봉을 빼기 위함."""
    bars = []
    for d in sorted(day_rows):
        rows = [r for r in day_rows[d] if r and r[4] not in (None, "")]
        if not rows:
            continue
        for slot, lo, hi in SLOTS:
            if upto and (d, slot) > upto:
                return bars
            seg = [r for r in rows if lo <= r[0] < hi]
            if seg:
                bars.append({
                    "date": d, "slot": slot,
                    "close": _f(seg[-1][4]),
                    "low": min(_f(r[3]) for r in seg if r[3] not in (None, "")),
                })
    return bars


def rsi_series(closes, n=RSI_PERIOD):
    """Wilder RSI. 처음 n개 변화는 단순평균으로 시작."""
    out = [None] * len(closes)
    g = l = 0.0
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gi, li = max(ch, 0.0), max(-ch, 0.0)
        if i <= n:
            g += gi
            l += li
            if i == n:
                g /= n
                l /= n
                out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
        else:
            g = (g * (n - 1) + gi) / n
            l = (l * (n - 1) + li) / n
            out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def clusters_of(rsi):
    sig = [i for i, r in enumerate(rsi) if r is not None and r < OVERSOLD]
    out = []
    for i in sig:
        if out and i - out[-1][-1] <= CLUSTER_GAP:
            out[-1].append(i)
        else:
            out.append([i])
    return out


def evaluate_bar(bars, j=None):
    """bars[j](기본: 마지막 봉)에서 새로 생긴 이벤트 목록.
    각 이벤트: {"kind", ...가격/횟수 정보}. kind: signal1 signal2 signal3 signalN
    sell_half sell_rest timeout1 timeout2."""
    if not bars:
        return [], None
    j = len(bars) - 1 if j is None else j
    closes = [b["close"] for b in bars[: j + 1]]
    rsi = rsi_series(closes)
    cur = {"rsi": rsi[j], "close": closes[j], "date": bars[j]["date"], "slot": bars[j]["slot"]}
    events = []
    for cl in clusters_of(rsi):
        i1 = cl[0]
        p1 = closes[i1]
        # 이번 봉이 신호 봉이면 몇 번째인지
        if j in cl:
            n = cl.index(j) + 1
            ev = {"kind": "signal1" if n == 1 else "signal2" if n == 2 else "signal3" if n == 3 else "signalN",
                  "n": n, "p1": p1}
            if n >= 2:
                ev["p2"] = closes[cl[1]]
            if n >= 3:
                ev["p3"] = closes[cl[2]]
                ev["avg"] = (ev["p2"] + ev["p3"]) / 2
            events.append(ev)
        if len(cl) < 3 or j <= cl[2]:
            continue
        i3 = cl[2]
        p2, p3 = closes[cl[1]], closes[i3]
        avg = (p2 + p3) / 2
        base = {"p1": p1, "p2": p2, "p3": p3, "avg": avg}
        # 1차 매도: 3번째 신호 후 처음으로 종가가 2번째 가격 위
        hit = next((k for k in range(i3 + 1, min(i3 + FIRST_LEG, j) + 1) if closes[k] > p2), None)
        if hit == j:
            events.append({"kind": "sell_half", "gain": (closes[j] / avg - 1) * 100, **base})
        elif hit is None and j == i3 + FIRST_LEG:
            events.append({"kind": "timeout1", "gain": (closes[j] / avg - 1) * 100, **base})
        # 2차 매도: 1차 매도 이후 종가가 1번째 가격 위이거나 RSI 50 이상
        if hit is not None and hit < j:
            for k in range(hit + 1, min(i3 + SECOND_LEG, j) + 1):
                reason = None
                if closes[k] > p1:
                    reason = "p1"
                elif rsi[k] is not None and rsi[k] >= REST_RSI:
                    reason = "rsi50"
                if reason:
                    if k == j:
                        events.append({"kind": "sell_rest", "reason": reason,
                                       "gain": (closes[j] / avg - 1) * 100, **base})
                    break
            else:
                if j == i3 + SECOND_LEG:
                    events.append({"kind": "timeout2", "gain": (closes[j] / avg - 1) * 100, **base})
    return events, cur


def _w(v):
    return f"{v:,.0f}"


def format_message(name, cur, events, tag=""):
    """텔레그램 평문. events가 비면 빈 문자열(보낼 게 없음)."""
    if not events:
        return ""
    d, slot = cur["date"], cur["slot"]
    when = f"{d[4:6]}/{d[6:8]} {SLOT_END[slot]}"
    head = [f"📉 RSI 과매도 규칙{tag} · {name} · {when} 120분봉 마감",
            f"종가 {_w(cur['close'])} · RSI {cur['rsi']:.1f}"]
    body = []
    for ev in events:
        k = ev["kind"]
        if k == "signal1":
            body += ["", "👀 1번째 과매도 신호 — 관찰 (2번째 신호부터 분할매수)",
                     f" · 1번째 신호가 {_w(ev['p1'])}"]
        elif k == "signal2":
            body += ["", "🛒 2번째 신호 — 1차 매수 (절반)",
                     f" · 1번째 {_w(ev['p1'])} · 2번째 {_w(ev['p2'])}",
                     f" · 다음: 3번째 신호 때 나머지 절반 매수",
                     f" · 2번째 가격 {_w(ev['p2'])} 위로 종가 마감하면 절반 매도"]
        elif k == "signal3":
            body += ["", "🛒🛒 3번째 신호 — 2차 매수 (나머지 절반)",
                     f" · 평단 {_w(ev['avg'])} (2번째 {_w(ev['p2'])} · 3번째 {_w(ev['p3'])})",
                     f" · 매도 ① {_w(ev['p2'])} 위로 종가 마감 시 절반",
                     f" · 매도 ② 나머지는 {_w(ev['p1'])} 위 종가 또는 RSI 50",
                     f" · 5일 안에 2번째 가격({_w(ev['p2'])})을 못 넘으면 정리 검토"]
        elif k == "signalN":
            body += ["", f"⚠️ {ev['n']}번째 신호 지속 — 규칙상 추가 매수 없음 (기존 매도 기준 유지)"]
        elif k == "sell_half":
            body += ["", f"💰 절반 매도 — 종가 {_w(cur['close'])} > 2번째 가격 {_w(ev['p2'])} (평단 대비 {ev['gain']:+.1f}%)",
                     f" · 나머지: {_w(ev['p1'])} 위 종가 또는 RSI 50에서 매도"]
        elif k == "sell_rest":
            why = f"종가 {_w(cur['close'])} > 1번째 가격 {_w(ev['p1'])}" if ev["reason"] == "p1" else "RSI 50 도달"
            body += ["", f"💰 나머지 매도 — {why} (평단 대비 {ev['gain']:+.1f}%)"]
        elif k == "timeout1":
            body += ["", f"⏰ 5일 경과 — 2번째 가격({_w(ev['p2'])})을 못 넘음, 전량 정리 검토 (평단 대비 {ev['gain']:+.1f}%)"]
        elif k == "timeout2":
            body += ["", f"⏰ 10일 경과 — 나머지 절반 정리 검토 (평단 대비 {ev['gain']:+.1f}%)"]
    foot = ["", "(120분봉 RSI14 · 과거 데이터 백테스트 기반 참고용)"]
    return "\n".join(head + body + foot)


def status_of(bars):
    """웹 표시용 현재 상태: 마지막 봉 RSI와 진행 중인 묶음의 신호 횟수."""
    if not bars:
        return None
    closes = [b["close"] for b in bars]
    rsi = rsi_series(closes)
    j = len(bars) - 1
    cl = next((c for c in reversed(clusters_of(rsi)) if j - c[-1] <= CLUSTER_GAP), None)
    return {
        "date": bars[j]["date"], "slot": bars[j]["slot"], "close": closes[j], "rsi": rsi[j],
        "signals": len(cl) if cl else 0,
        "oversold": bool(rsi[j] is not None and rsi[j] < OVERSOLD),
    }
