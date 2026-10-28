/**
 * 대시보드 하루 스냅샷 -> 구글 시트 `스냅샷` 탭 (한 날짜 = 한 줄)
 *
 * 설치(한 번만):
 *  1) 시트 메뉴 확장 프로그램 > Apps Script 에 이 코드를 통째로 붙여넣고 저장
 *  2) 왼쪽 톱니(프로젝트 설정) > 스크립트 속성 에 두 개 추가
 *       SNAPSHOT_URL = https://market-dashboard-gqxq.onrender.com/api/ops/snapshot
 *       SNAPSHOT_KEY = (텔레그램 요약에 쓰는 TELEGRAM_SUMMARY_KEY 값)
 *  3) 위 함수 선택 목록에서 setupTriggers 를 한 번 실행(권한 허용) -> 매일 15:30 이후와 17시경 자동 실행
 *  4) snapshotNow 를 직접 한 번 실행해 `스냅샷` 탭에 오늘 줄이 생기는지 확인
 *
 * 장 마감 전(15:30 이전)이거나 휴장일이면 아무것도 쓰지 않는다. 같은 날짜 줄이 이미 있으면 값을 갱신한다
 * (15:45경 1차 기록, 17시경 2차로 정배열 칸 등을 채운다).
 */
var TAB = '스냅샷';
var COLS = [
  '날짜', '기록시각', '종베 매크로%', '종베 종목%', '종베 종목',
  '코스피 종가', '코스피 Stage', '코스피 20일선대비%', '코스피 60일고점대비%', '코스피 올해낙폭%', '코스피 올해MDD%',
  '코스닥 종가', '코스닥 Stage', '코스닥 20일선대비%', '코스닥 60일고점대비%', '코스닥 올해낙폭%', '코스닥 올해MDD%',
  '신용 코스피낙폭%', '신용 코스닥낙폭%', '신용 기준일',
  '20Down%', '거래대금 코스피(조)', '거래대금 코스닥(조)',
  '정배열 종목수', '정배열 상위업종', '정배열 기준'
];

function get_(o, path) {
  var cur = o;
  for (var i = 0; i < path.length; i++) {
    if (cur === null || cur === undefined) return '';
    cur = cur[path[i]];
  }
  return (cur === null || cur === undefined) ? '' : cur;
}

function fetchSnapshot_() {
  var p = PropertiesService.getScriptProperties();
  var url = p.getProperty('SNAPSHOT_URL'), key = p.getProperty('SNAPSHOT_KEY');
  if (!url || !key) throw new Error('스크립트 속성 SNAPSHOT_URL / SNAPSHOT_KEY 를 먼저 설정하세요');
  var last = '';
  for (var i = 0; i < 4; i++) {                       // 서버가 깨어나는 중일 수 있어 몇 번 더 시도
    try {
      var res = UrlFetchApp.fetch(url + '?key=' + encodeURIComponent(key), { muteHttpExceptions: true });
      if (res.getResponseCode() === 200) return JSON.parse(res.getContentText());
      last = 'HTTP ' + res.getResponseCode();
    } catch (e) { last = String(e); }
    Utilities.sleep(20000);
  }
  throw new Error('스냅샷을 받지 못했습니다: ' + last);
}

function snapshotNow() {
  var s = fetchSnapshot_();
  var today = s.date.replace(/-/g, '');
  var kospi = get_(s, ['indices', 'kospi']);
  if (!s.afterClose) { Logger.log('장 마감 전(' + s.asOf + ') — 건너뜀'); return; }
  if (!kospi || kospi.lastDate !== today) { Logger.log('휴장일이거나 오늘 종가가 아직 없음 — 건너뜀'); return; }

  var ss = SpreadsheetApp.getActive();
  var sh = ss.getSheetByName(TAB) || ss.insertSheet(TAB);
  if (sh.getLastRow() === 0 || sh.getRange(1, 1).getValue() !== COLS[0]) {
    sh.getRange(1, 1, 1, COLS.length).setValues([COLS]).setFontWeight('bold').setBackground('#ffff00');
    sh.setFrozenRows(1);
  }
  var d = new Date(s.date + 'T00:00:00+09:00');
  var v = {
    '날짜': d,
    '기록시각': s.asOf,
    '종베 매크로%': get_(s, ['scores', 'macro']),
    '종베 종목%': get_(s, ['scores', 'stock']),
    '종베 종목': get_(s, ['scores', 'stockName']),
    '코스피 종가': get_(s, ['indices', 'kospi', 'close']),
    '코스피 Stage': get_(s, ['indices', 'kospi', 'stage']),
    '코스피 20일선대비%': get_(s, ['indices', 'kospi', 'vsMa20Pct']),
    '코스피 60일고점대비%': get_(s, ['indices', 'kospi', 'dd60Pct']),
    '코스피 올해낙폭%': get_(s, ['mdd', 'kospi', 'index', 'now']),
    '코스피 올해MDD%': get_(s, ['mdd', 'kospi', 'index', 'mdd']),
    '코스닥 종가': get_(s, ['indices', 'kosdaq', 'close']),
    '코스닥 Stage': get_(s, ['indices', 'kosdaq', 'stage']),
    '코스닥 20일선대비%': get_(s, ['indices', 'kosdaq', 'vsMa20Pct']),
    '코스닥 60일고점대비%': get_(s, ['indices', 'kosdaq', 'dd60Pct']),
    '코스닥 올해낙폭%': get_(s, ['mdd', 'kosdaq', 'index', 'now']),
    '코스닥 올해MDD%': get_(s, ['mdd', 'kosdaq', 'index', 'mdd']),
    '신용 코스피낙폭%': get_(s, ['mdd', 'kospi', 'credit', 'now']),
    '신용 코스닥낙폭%': get_(s, ['mdd', 'kosdaq', 'credit', 'now']),
    '신용 기준일': get_(s, ['mdd', 'kospi', 'credit', 'lastDate']),
    '20Down%': get_(s, ['down20', 'ratio']),
    '거래대금 코스피(조)': get_(s, ['trading', 'kospi']),
    '거래대금 코스닥(조)': get_(s, ['trading', 'kosdaq']),
    '정배열 종목수': get_(s, ['align', 'aligned']),
    '정배열 상위업종': get_(s, ['align', 'topSectors']),
    '정배열 기준': get_(s, ['align', 'builtAt'])
  };

  // 같은 날짜 줄 찾기 (날짜 칸은 날짜 값)
  var last = sh.getLastRow(), row = 0;
  if (last > 1) {
    var dates = sh.getRange(2, 1, last - 1, 1).getValues();
    for (var i = 0; i < dates.length; i++) {
      var x = dates[i][0];
      if (x instanceof Date && Utilities.formatDate(x, 'Asia/Seoul', 'yyyy-MM-dd') === s.date) { row = i + 2; break; }
    }
  }
  var isNew = row === 0;
  if (isNew) row = last + 1;
  // 새 줄이면 전부, 기존 줄이면 값이 있는 칸만 갱신(17시 2차 실행이 앞서 쓴 값을 빈 값으로 덮지 않게)
  for (var c = 0; c < COLS.length; c++) {
    var val = v[COLS[c]];
    if (isNew || (val !== '' && val !== undefined)) sh.getRange(row, c + 1).setValue(val);
  }
  sh.getRange(row, 1).setNumberFormat('yyyy-mm-dd');
  if (s.errors && Object.keys(s.errors).length) Logger.log('일부 항목 실패: ' + JSON.stringify(s.errors));
  Logger.log('기록 완료: ' + s.date + ' (' + (isNew ? '새 줄' : '갱신') + ')');
}

/** 한 번만 실행: 평일 15:30~16:00 사이와 16:45~17:15 사이에 snapshotNow 를 부르는 트리거를 만든다. */
function setupTriggers() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'snapshotNow') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('snapshotNow').timeBased().everyDays(1).atHour(15).nearMinute(45).inTimezone('Asia/Seoul').create();
  ScriptApp.newTrigger('snapshotNow').timeBased().everyDays(1).atHour(17).nearMinute(0).inTimezone('Asia/Seoul').create();
}
