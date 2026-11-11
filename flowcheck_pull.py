#!/usr/bin/env python3
"""서버가 쌓은 수급 점검 기록(/api/ops/flow-check-log)을 data 브랜치 data/flow_check.csv 로 옮긴다.

사용(저녁 20:30 이후 하루 한 번, 점검 기간 동안):
    python3 flowcheck_pull.py              # 서버 기록을 받아 data 브랜치 CSV 에 합치고 push
    python3 flowcheck_pull.py --dry-run    # 합친 결과만 보여 주고 push 하지 않음
    python3 flowcheck_pull.py --clean      # 기존 CSV 에서 시각이 슬롯 시간대를 벗어난 줄(= GitHub 예약 실행이 늦게 돌아 잘못 기록된 줄)을 지운다

키는 ~/.config/jongbae/telegram_key (TELEGRAM_SUMMARY_KEY). 화면에 찍지 않는다.
작업 디렉터리를 건드리지 않으려고 임시 git worktree 에서 data 브랜치를 수정한다.
같은 date+slot+code 줄은 서버 값으로 덮어쓴다. 빈 값(foreign 없음)인 줄은 기존에 값이 있으면 덮지 않는다.
"""
import csv
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

BASE = "https://market-dashboard-gqxq.onrender.com"
KEY_FILE = os.path.expanduser("~/.config/jongbae/telegram_key")
COLS = ["date", "slot", "time", "code", "name", "foreign", "inst", "checkpoint"]
ORDER = {"est": 0, "post": 1, "final": 2}
# 슬롯별 허용 시각(KST 분): 이 밖에서 기록된 줄은 점검에 쓸 수 없다(est=15:10 가집계, post=15:50, final=20:10)
WINDOW = {"est": (15 * 60 + 5, 15 * 60 + 40), "post": (15 * 60 + 45, 17 * 60), "final": (20 * 60 + 5, 21 * 60 + 30)}
HERE = os.path.dirname(os.path.abspath(__file__))


def git(*a, cwd=HERE, check=True):
    return subprocess.run(("git",) + a, cwd=cwd, check=check, capture_output=True, text=True)


def minutes(t):
    h, m = (t or "0:0").split(":")[:2]
    return int(h) * 60 + int(m)


def in_window(r):
    lo, hi = WINDOW.get(r["slot"], (0, 0))
    return lo <= minutes(r.get("time")) <= hi


def fetch_server_rows():
    key = open(KEY_FILE).read().strip()
    url = BASE + "/api/ops/flow-check-log?key=" + urllib.parse.quote(key)
    with urllib.request.urlopen(url, timeout=60) as resp:
        d = json.load(resp)
    print("서버 기록 %d줄 (부팅 %s, 마지막 실행 %s)" % (len(d["rows"]), d.get("bootAt"), d.get("lastRun")))
    return d["rows"]


def main():
    dry, clean = "--dry-run" in sys.argv, "--clean" in sys.argv
    git("fetch", "origin", "data")
    tmp = tempfile.mkdtemp(prefix="flowpull-")
    wt = os.path.join(tmp, "wt")
    git("worktree", "add", "--detach", wt, "origin/data")
    try:
        path = os.path.join(wt, "data", "flow_check.csv")
        old = []
        if os.path.exists(path):
            old = list(csv.DictReader(open(path, encoding="utf-8")))
        rows = list(old)
        if clean:
            keep = [r for r in rows if in_window(r)]
            print("정리: %d줄 중 시각이 슬롯 밖인 %d줄 삭제" % (len(rows), len(rows) - len(keep)))
            rows = keep
        else:
            new = [{c: ("" if r.get(c) is None else r.get(c)) for c in COLS} for r in fetch_server_rows()]
            new = [r for r in new if in_window(r)]
            have = {(r["date"], r["slot"], r["code"]): r for r in rows}
            for r in new:
                k = (r["date"], r["slot"], r["code"])
                if k in have and have[k]["foreign"] not in ("", None) and r["foreign"] in ("", None):
                    continue   # 값 있는 줄을 빈 줄로 덮지 않는다
                have[k] = r
            rows = list(have.values())
        rows.sort(key=lambda r: (r["date"], ORDER.get(r["slot"], 9), r["code"]))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLS)
            w.writeheader()
            w.writerows({c: r.get(c, "") for c in COLS} for r in rows)
        git("add", "data/flow_check.csv", cwd=wt)
        if git("diff", "--cached", "--quiet", cwd=wt, check=False).returncode == 0:
            print("변경 없음")
            return
        days = sorted({r["date"] for r in rows})
        print("결과: %d줄, 날짜 %s" % (len(rows), ", ".join(days)))
        if dry:
            print("--dry-run: push 하지 않음")
            return
        git("-c", "user.name=flow-pull", "-c", "user.email=flow-pull@users.noreply.github.com", "commit", "-m",
            "flow-check %s (%d줄)" % ("정리" if clean else "서버 기록 반영", len(rows)), cwd=wt)
        git("push", "origin", "HEAD:data", cwd=wt)
        print("data 브랜치에 반영했습니다")
    finally:
        git("worktree", "remove", "--force", wt, check=False)


if __name__ == "__main__":
    main()
