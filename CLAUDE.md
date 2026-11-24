# CLAUDE.md — 현재 상태 요약 (2026-11-24 확인)

상세 구조·이력은 `HANDOFF.md`(§1~43), 작업 규칙 전문은 `종베.md`. 이 파일은 지금 상태만 적는다. 시작할 때 `HANDOFF.md`와 `종베.md`도 읽는다.

## 서버·배포
- 저장소 `github.com/seanyang83/market-dashboard`, 브랜치 `main`, 로컬 `/Users/sean_yang/Claude/Code/YSH`.
- Render https://market-dashboard-gqxq.onrender.com, `main` push 시 자동 배포. 확인: `/healthz`의 commit == `git rev-parse --short=7 HEAD`.
- 배포 커밋은 `2baad4a`(11/12) 이후 바뀐 게 없다. 문서 커밋 3개와 기록판 작업(커밋 전)은 **2026-11-24 21시 이후 커밋·push 예정**(세션 예약; 안 됐으면 `git status`로 확인).
- 서버: `server.py` 하나(표준 라이브러리만, 추가 의존성은 `pypdf`). 로컬 실행은 `.claude/launch.json`의 `dashboard`(포트 8934). 로컬엔 한투 키가 없어 한투 API는 503. `server.py`를 고치면 서버를 재시작해야 반영된다.
- 환경변수(KIS, 대시보드 비밀번호, 텔레그램 등)는 Render에만 있다.

## 페이지 (호칭)
종베 `/`(index.html) · 주도주 `/leader` · 업종 `/sector` · 손절 계산기 `/risk` · 위기판 `/crisis` · 기록판 `/record`(전부 로그인 필요, 비로그인은 302). 텔레 = 텔레그램 발송.

## 경로·스크립트
- 키(저장소 밖, 출력·커밋 금지): `~/.config/jongbae/telegram_key`, `dart_key`. 백업: `~/.config/jongbae/backup/flow_check_log_20261112_1545.json`.
- `flowcheck_pull.py` 서버 수급 기록 → data 브랜치 `data/flow_check.csv`(`--dry-run`, `--clean`). `flowcheck_report.py` 집계.
- `build_crisis_history.py` → `crisis_history.json`. `build_semi_cycles.py` → `semi_cycles.json`(새 고점·회복 시 `peaks` 갱신 후 재실행).
- 스케줄 작업: `flowcheck-pull-daily`(평일 20:30). `pbr-bps-reminder`는 소멸/정리 대상.

## 기록판 `/record` (11/24 신설, HANDOFF §41~43)
- 이슈 타임라인: 기본 1년, 드래그 확대. 이슈는 `record_issues.json`에 항목 추가(`status`: draft·news·user). 위기판에서는 분리됨.
- 미국 선거 사이클(2000년 이후, 한국 선거는 제외): 기본 접힘, 눌러야 펴진다.
- 미국 금리·물가·나스닥과 코스피: FRED 6시리즈, 드래그 확대. 나스닥은 FRED 반영이 며칠 늦다. 필라델피아 반도체지수는 사용자 요청으로 뺐으니 다시 제안하지 않는다.
- 이슈와 등락의 인과를 단정하지 않고 매수·매도를 권하지 않는다.

## 현재 설정·동작
- 위기판: "내 포지션 시나리오" 제거. "반도체 사이클 참고" 카드(`/api/crisis/semis`) 있음. 약세장 유형 분류는 중단.
- 종베 거래대금 순위 Hit: 수급은 평일엔 통합(UN) 일별 오늘 행 우선, 값이 없거나 전부 0이면 가집계로 폴백(§40).
- 수급 점검: 서버가 15:10·15:50·20:10에 수집, 메모리+`flow_check_log.json`(재시작 시 소멸). 중간 보고 ~11/25, 결론 ~12/9.
- 한투 재무 API(손익·재무상태표)는 11/11부터 오류, 원인 미상(§38).

## 주의사항
1. 15:00~21:00 `main` push 금지(재시작으로 수급 기록 소실). 불가피하면 먼저 `python3 flowcheck_pull.py`.
2. 비밀값은 화면·답변·커밋·메모리에 쓰지 않는다. 텔레 테스트 발송은 사용자가 요청할 때만(`force=1`, 실제 구독자 3명 채널).
3. 위기판은 수동 확인용: 알림 기능은 요청 전까지 만들지 않고, 매수·매도 권유 금지.
4. 두 기기에서 작업하면 작업 전 `git pull`.

## 열린 일
- 기록판 작업 커밋·push(21시 이후) 후 `/healthz` 커밋 확인.
- 기록판: 11/20·11/23 하락 원인은 사용자 확인 대기(11/24 상승은 사용자 기록). 2022년 금리 인상일·CPI 발표일을 이슈로 넣을지는 미정.
- 11/12 post·final 기록이 data 브랜치에 들어갔는지 확인(`git fetch origin data` 후 `flowcheck_pull.py --dry-run`).
- 맥북 분실 대비: push, 키 2개는 비밀번호 관리자에 따로 보관(텔레그램 키는 Render 환경변수에서 복구 가능, DART 키는 재발급 가능).
