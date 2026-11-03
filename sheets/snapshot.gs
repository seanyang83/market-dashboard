/**
 * 대시보드 하루 스냅샷 -> 구글 시트 `스냅샷` 탭 (한 날짜 = 한 줄)
 *
 * 설치(한 번만):
 *  1) 시트 메뉴 확장 프로그램 > Apps Script 에 이 코드를 통째로 붙여넣고 저장
 *  2) 왼쪽 톱니(프로젝트 설정) > 스크립트 속성 에 두 개 추가
 *       SNAPSHOT_URL = https://market-dashboard-gqxq.onrender.com/api/ops/snapshot
 *       SNAPSHOT_KEY = (텔레그램 요약에 쓰는 TELEGRAM_SUMMARY_KEY 값)
 *  3) 위 함수 선택 목록에서 setupTriggers 를 한 번 실행(권한 허용) -> 매일 16시경 하루 한 번 자동 실행
 *  4) snapshotNow 를 직접 한 번 실행해 `스냅샷` 탭에 오늘 줄이 생기는지 확인
 *
 * 장 마감 전(15:30 이전)이거나 휴장일이면 아무것도 쓰지 않는다. 하루 한 번(16시경) 기록한다 — 서버의 정배열 스캔이
 * 15:50부터 돌아 16시 전에 끝나므로, 아직 오늘 스캔이 아니면 몇 번 기다렸다가 쓴다. 같은 날짜 줄이 이미 있으면
 * (직접 다시 실행한 경우) 값이 있는 칸만 갱신한다.
 */
var TAB = '스냅샷';
var COLS = [
  '날짜', '기록시각', '종베 매크로%', '종베 종목%', '종베 종목',
  '코스피 종가', '코스피 Stage', '코스피 20일선대비%', '코스피 60일고점대비%', '코스피 올해낙폭%', '코스피 올해MDD%',
  '코스닥 종가', '코스닥 Stage', '코스닥 20일선대비%', '코스닥 60일고점대비%', '코스닥 올해낙폭%', '코스닥 올해MDD%',
  '신용 코스피낙폭%', '신용 코스닥낙폭%', '신용 기준일',
  '20Down%', '거래대금 코스피(조)', '거래대금 코스닥(조)',
  '정배열 종목수', '정배열 상위업종', '정배열 기준',
  '코스피 등락%', '코스닥 등락%',
  'TOP10 요약', 'TOP10 Hit수'
];

// 거래대금 TOP10 전용 탭(하루 10줄, 같은 날짜를 다시 실행하면 그날 줄을 지우고 다시 쓴다). 종목코드는 글자로 저장한다.
var TOP_TAB = '거래대금TOP10';
var TOP_COLS = ['날짜', '순위', '종목코드', '종목명', '현재가', '등락%', '거래대금(억)', '외국인', '기관', '프로그램', 'Hit', '기록시각'];

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
  // 정배열은 서버가 15:50부터 스캔해 하루 한 번 만든다. 아직 오늘 결과가 아니면(스캔 중) 최대 150초 기다린다.
  for (var w = 0; w < 5 && get_(s, ['align', 'finalFor']) !== today; w++) {
    Utilities.sleep(30000);
    try { s = fetchSnapshot_(); kospi = get_(s, ['indices', 'kospi']); } catch (e) { break; }
  }

  var ss = SpreadsheetApp.getActive();
  var sh = ss.getSheetByName(TAB) || ss.insertSheet(TAB);
  if (sh.getLastRow() === 0 || sh.getRange(1, 1).getValue() !== COLS[0]) {
    sh.getRange(1, 1, 1, COLS.length).setValues([COLS]).setFontWeight('bold').setBackground('#ffff00');
    sh.setFrozenRows(1);
  } else if (sh.getRange(1, COLS.length).getValue() !== COLS[COLS.length - 1]) {
    // 코드에 칸이 추가된 경우: 맨 뒤에 붙은 머리글만 채운다(기존 칸 위치는 그대로)
    sh.getRange(1, 1, 1, COLS.length).setValues([COLS]).setFontWeight('bold').setBackground('#ffff00');
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
    '정배열 기준': get_(s, ['align', 'builtAt']),
    '코스피 등락%': get_(s, ['indices', 'kospi', 'chgPct']),
    '코스닥 등락%': get_(s, ['indices', 'kosdaq', 'chgPct']),
    'TOP10 요약': topSummary_(s.volumeRank),
    'TOP10 Hit수': s.volumeRank ? s.volumeRank.filter(function (r) { return r.hit; }).length : ''
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
  try { writeTop10_(ss, s, d); } catch (e) { Logger.log('TOP10 기록 실패: ' + e); }
  if (s.errors && Object.keys(s.errors).length) Logger.log('일부 항목 실패: ' + JSON.stringify(s.errors));
  Logger.log('기록 완료: ' + s.date + ' (' + (isNew ? '새 줄' : '갱신') + ')');
}

function topSummary_(rows) {
  if (!rows || !rows.length) return '';
  return rows.map(function (r) { return r.rank + ' ' + r.name + '(' + (r.changePct > 0 ? '+' : '') + r.changePct + ')'; }).join(' · ');
}

function yn_(v) { return v === null || v === undefined ? '' : (v > 0 ? 'Y' : 'N'); }

/** 거래대금 TOP10을 `거래대금TOP10` 탭에 쓴다. 같은 날짜 줄이 있으면 먼저 지운다(날짜 칸은 날짜 값). */
function writeTop10_(ss, s, d) {
  var rows = s.volumeRank;
  if (!rows || !rows.length) { Logger.log('TOP10 없음(서버 오류?) — 건너뜀: ' + JSON.stringify(s.errors || {})); return; }
  var sh = ss.getSheetByName(TOP_TAB) || ss.insertSheet(TOP_TAB);
  if (sh.getLastRow() === 0) {
    sh.getRange(1, 1, 1, TOP_COLS.length).setValues([TOP_COLS]).setFontWeight('bold').setBackground('#ffff00');
    sh.setFrozenRows(1);
  }
  var last = sh.getLastRow();
  if (last > 1) {                                       // 같은 날짜 줄 삭제(아래에서부터)
    var dates = sh.getRange(2, 1, last - 1, 1).getValues();
    for (var i = dates.length - 1; i >= 0; i--) {
      var x = dates[i][0];
      if (x instanceof Date && Utilities.formatDate(x, 'Asia/Seoul', 'yyyy-MM-dd') === s.date) sh.deleteRow(i + 2);
    }
  }
  var start = sh.getLastRow() + 1;
  var values = rows.map(function (r) {
    return [d, r.rank, String(r.code), r.name, r.price, r.changePct, r.tradingValueEok,
            yn_(r.foreign), yn_(r.institution), yn_(r.program), r.hit ? 'Y' : 'N', s.asOf];
  });
  sh.getRange(start, 3, values.length, 1).setNumberFormat('@');          // 종목코드 000660 의 앞자리 0 보존
  sh.getRange(start, 1, values.length, TOP_COLS.length).setValues(values);
  sh.getRange(start, 1, values.length, 1).setNumberFormat('yyyy-mm-dd');
  sh.getRange(start, 3, values.length, 1).setNumberFormat('@');
  Logger.log('TOP10 기록: ' + s.date + ' ' + values.length + '줄');
}

/**
 * 한 번만(필요할 때) 실행: `TOP10보기` 탭을 만든다 — 가로 최근 10일 × 세로 1~10위 표, 맨 아래 신규 진입·이탈.
 * 어제 TOP10에 없던 종목은 주황 배경, 외국인·기관·프로그램이 모두 순매수(Hit)인 종목은 굵은 초록 글자.
 * 데이터는 `거래대금TOP10` 탭에서 수식으로 읽으므로 새 날짜가 쌓이면 저절로 갱신된다.
 */
function buildTop10View() {
  var ss = SpreadsheetApp.getActive();
  var sh = ss.getSheetByName('TOP10보기') || ss.insertSheet('TOP10보기');
  sh.clear();
  sh.clearConditionalFormatRules();
  var T = "'" + TOP_TAB + "'!";
  var cols = 10;                                         // 최근 10일(B~K열)
  sh.getRange('A1').setValue('순위');
  for (var c = 0; c < cols; c++) {
    var L = String.fromCharCode(66 + c);                 // B..K
    sh.getRange(L + '1').setFormula('=IFERROR(LARGE(UNIQUE(' + T + '$A$2:$A),COLUMNS($B$1:' + L + '$1)),"")').setNumberFormat('mm-dd');
    for (var r = 2; r <= 11; r++) {
      sh.getRange(L + r).setFormula('=IFERROR(INDEX(FILTER(' + T + '$D$2:$D,' + T + '$A$2:$A=' + L + '$1,' + T + '$B$2:$B=$A' + r + '),1),"")');
    }
  }
  for (var k = 1; k <= 10; k++) sh.getRange('A' + (k + 1)).setValue(k);
  sh.getRange('A13').setValue('오늘 신규 진입');
  sh.getRange('B13').setFormula('=IF($C$1="","-",IFERROR(TEXTJOIN(", ",TRUE,FILTER($B$2:$B$11,$B$2:$B$11<>"",ARRAYFORMULA(ISNA(MATCH($B$2:$B$11,$C$2:$C$11,0))))),"-"))');
  sh.getRange('A14').setValue('오늘 이탈');
  sh.getRange('B14').setFormula('=IF($C$1="","-",IFERROR(TEXTJOIN(", ",TRUE,FILTER($C$2:$C$11,$C$2:$C$11<>"",ARRAYFORMULA(ISNA(MATCH($C$2:$C$11,$B$2:$B$11,0))))),"-"))');
  sh.getRange('A1:K1').setFontWeight('bold').setBackground('#ffff00');
  sh.getRange('A13:A14').setFontWeight('bold');
  sh.setFrozenRows(1);
  sh.setColumnWidth(1, 110);
  sh.setColumnWidths(2, cols, 110);
  var rules = [];
  rules.push(SpreadsheetApp.newConditionalFormatRule()    // 어제 TOP10에 없던 신규 진입
    .whenFormulaSatisfied('=AND(B2<>"",C$1<>"",COUNTIF(C$2:C$11,B2)=0)')
    .setBackground('#ffd9a0').setRanges([sh.getRange('B2:J11')]).build());
  rules.push(SpreadsheetApp.newConditionalFormatRule()    // Hit(외·기·프 모두 순매수)
    .whenFormulaSatisfied('=COUNTIFS(INDIRECT("\'' + TOP_TAB + '\'!$A:$A"),B$1,INDIRECT("\'' + TOP_TAB + '\'!$D:$D"),B2,INDIRECT("\'' + TOP_TAB + '\'!$K:$K"),"Y")>0')
    .setBold(true).setFontColor('#0b8043').setRanges([sh.getRange('B2:K11')]).build());
  sh.setConditionalFormatRules(rules);
  Logger.log('TOP10보기 탭을 만들었습니다');
}

/** 한 번만 실행: 매일 16시경(구글 트리거는 몇 분 오차가 있다) snapshotNow 를 부르는 트리거 하나를 만든다. 기존 트리거는 지운다. */
function setupTriggers() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'snapshotNow') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('snapshotNow').timeBased().everyDays(1).atHour(16).nearMinute(10).inTimezone('Asia/Seoul').create();
}
