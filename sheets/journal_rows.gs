/**
 * 매매일지 탭에 날짜 줄을 미리 자동으로 만들어 둔다 (Apps Script, 시트 확장 프로그램 > Apps Script 에 snapshot.gs 와 같은 프로젝트로 붙인다).
 *
 * 일지는 "최신 날짜가 위" 순서다. 1~2행(머리글·메모 줄)은 건드리지 않고, 3행부터 찾은 맨 위 날짜 줄 위에
 * (= 메모 줄 바로 아래) 새 줄을 끼워 넣어 오늘 + AHEAD_DAYS 일까지 평일(공휴일 제외)을 채운다.
 * 새 줄은 바로 아래 줄(이전 맨 위)의 서식·드롭다운(Stage)을 그대로 복사하고, 날짜 칸과 FILL_COLS 수식 칸만 채운다.
 *
 * 쓰는 법:
 *   1) 먼저 previewJournalRows 를 실행해 로그에 "추가할 날짜"가 맞게 나오는지 본다 (시트는 바뀌지 않는다).
 *   2) 맞으면 ensureJournalRows 를 한 번 실행한다.
 *   3) setupJournalTrigger 를 한 번 실행하면 매일 아침 6시경 자동으로 부족한 날짜를 채운다.
 *
 * 공휴일은 코드가 알 수 없으므로 HOLIDAYS 에 적어 둔다(휴장일 줄은 직접 지워도 된다 — 이미 지난 날짜는 다시 만들지 않는다).
 */
var JOURNAL_TAB = '일지';          // 일지 탭 이름
var AHEAD_DAYS = 14;              // 오늘로부터 며칠 앞까지(2주) 날짜 줄을 만들어 둘지
var MEMO_ROWS = 2;                // 이 행까지(머리글·메모 줄)는 날짜 줄이 아니므로 건드리지 않는다 — 날짜 줄은 그 아래부터
var SCAN_ROWS = 60;               // 메모 줄 아래로 이 줄 수 안에서 맨 위 날짜 줄을 찾는다
var HOLIDAYS = ['2026-12-25', '2026-12-31', '2027-01-01'];   // 증시 휴장일(yyyy-mm-dd). 필요하면 더 적는다
// 새 줄에 넣을 스냅샷 수식 칸. 비워 두면([]) 수식은 넣지 않는다. header 는 `스냅샷` 탭 머리글 이름.
var FILL_COLS = [
  { col: 'C', header: '코스피 등락%' },
  { col: 'K', header: '종베 매크로%' }
];

function dayNum_(y, m, d) { return Math.round(Date.UTC(y, m - 1, d) / 86400000); }
function fromNum_(n) {
  var t = new Date(n * 86400000);
  return { y: t.getUTCFullYear(), m: t.getUTCMonth() + 1, d: t.getUTCDate(), dow: t.getUTCDay() };
}
function pad_(n) { return (n < 10 ? '0' : '') + n; }
function iso_(n) { var p = fromNum_(n); return p.y + '-' + pad_(p.m) + '-' + pad_(p.d); }

/** 날짜 칸 값(글자 '26.11.02(월)' 또는 날짜 값) -> {num, isDate}, 날짜가 아니면 null */
function parseCell_(v) {
  if (v instanceof Date) {
    var s = Utilities.formatDate(v, 'Asia/Seoul', 'yyyy-MM-dd').split('-');
    return { num: dayNum_(+s[0], +s[1], +s[2]), isDate: true };
  }
  var m = /^(\d{2})\.(\d{2})\.(\d{2})/.exec(String(v).trim());
  if (!m) return null;
  return { num: dayNum_(2000 + (+m[1]), +m[2], +m[3]), isDate: false };
}

function labelFor_(n) {                      // '26.11.19(수)'
  var p = fromNum_(n);
  return String(p.y).slice(2) + '.' + pad_(p.m) + '.' + pad_(p.d) + '(' + '일월화수목금토'.charAt(p.dow) + ')';
}

function formulaFor_(header, r) {
  return '=LET(k,IF(ISNUMBER($A' + r + '),$A' + r + ',DATE(2000+LEFT($A' + r + ',2),MID($A' + r + ',4,2),MID($A' + r + ',7,2))),'
    + 'IFERROR(INDEX(스냅샷!$A:$AB,MATCH(k,스냅샷!$A:$A,0),MATCH("' + header + '",스냅샷!$1:$1,0))/100,""))';
}

function plan_(sh) {
  var first = MEMO_ROWS + 1;
  var last = Math.min(sh.getLastRow(), MEMO_ROWS + SCAN_ROWS);
  var vals = last >= first ? sh.getRange(first, 1, last - first + 1, 1).getValues() : [];
  var topRow = 0, top = null;
  for (var i = 0; i < vals.length; i++) {
    var p = parseCell_(vals[i][0]);
    if (p) { topRow = i + first; top = p; break; }
  }
  if (!top) throw new Error('일지 탭 A열(' + first + '~' + (MEMO_ROWS + SCAN_ROWS) + '행)에서 날짜 줄을 찾지 못했습니다');
  var t = Utilities.formatDate(new Date(), 'Asia/Seoul', 'yyyy-MM-dd').split('-');
  var until = dayNum_(+t[0], +t[1], +t[2]) + AHEAD_DAYS;
  var todo = [];
  for (var n = top.num + 1; n <= until; n++) {
    var dow = fromNum_(n).dow;
    if (dow === 0 || dow === 6) continue;
    if (HOLIDAYS.indexOf(iso_(n)) >= 0) continue;
    todo.push(n);
  }
  return { topRow: topRow, top: top, todo: todo };
}

function previewJournalRows() {
  var sh = SpreadsheetApp.getActive().getSheetByName(JOURNAL_TAB);
  if (!sh) throw new Error('탭 "' + JOURNAL_TAB + '" 을 찾지 못했습니다');
  var pl = plan_(sh);
  Logger.log('맨 위 날짜 줄: ' + pl.topRow + '행 (' + iso_(pl.top.num) + ', ' + (pl.top.isDate ? '날짜 값' : '글자') + ')');
  Logger.log(pl.todo.length ? '추가할 날짜 ' + pl.todo.length + '개: ' + pl.todo.map(labelFor_).join(', ') : '추가할 날짜 없음');
}

function ensureJournalRows() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) { Logger.log('다른 실행 중 — 건너뜀'); return; }
  try {
    var sh = SpreadsheetApp.getActive().getSheetByName(JOURNAL_TAB);
    if (!sh) throw new Error('탭 "' + JOURNAL_TAB + '" 을 찾지 못했습니다');
    var pl = plan_(sh);
    if (!pl.todo.length) { Logger.log('추가할 날짜 없음'); return; }
    var lastCol = sh.getLastColumn(), r = pl.topRow;
    pl.todo.forEach(function (n) {          // 오래된 날짜부터 차례로 맨 위에 끼워 넣으므로 결과는 최신이 위
      sh.insertRowBefore(r);                // 새 줄이 r행, 이전 맨 위는 r+1행으로 내려간다
      var tpl = sh.getRange(r + 1, 1, 1, lastCol), row = sh.getRange(r, 1, 1, lastCol);
      tpl.copyTo(row, SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
      tpl.copyTo(row, SpreadsheetApp.CopyPasteType.PASTE_DATA_VALIDATION, false);
      var a = sh.getRange(r, 1);
      if (pl.top.isDate) a.setValue(new Date(iso_(n) + 'T00:00:00+09:00'));
      else a.setValue(labelFor_(n));
      FILL_COLS.forEach(function (c) { sh.getRange(c.col + r).setFormula(formulaFor_(c.header, r)); });
    });
    Logger.log('일지에 날짜 ' + pl.todo.length + '개 추가: ' + pl.todo.map(labelFor_).join(', '));
  } finally {
    lock.releaseLock();
  }
}

/** 한 번만 실행: 매일 아침 6시경 ensureJournalRows 실행 */
function setupJournalTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'ensureJournalRows') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('ensureJournalRows').timeBased().everyDays(1).atHour(6).inTimezone('Asia/Seoul').create();
}
