# 종베 점수 기록

`data/scores.csv` — GitHub Actions(`score-log.yml`)가 거래일 15:10(close)·19:40(extended) KST에 자동 기록.

- macro/stock: 종가베팅 체크리스트 점수(%)
- macro_sig 키: tnx wti ndq btc kospiDir kospiMa / stock_sig 키: candle usSector krSector ma flow program
- slot: close(15:10)/extended(19:40)/manual(수동 테스트, 분석에서 제외)
- 신호 값: G=초록(좋음) Y=노랑 R=빨강(나쁨) N=데이터 없음
