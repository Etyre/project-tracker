/**
 * Task Tracker <-> Google Sheets bridge (daily checklist + exercise sheet), read AND write.
 *
 * Setup:
 *  1. Open the spreadsheet that holds your daily checklist (and, if it is the same file, the exercise sheet)
 *     -> Extensions -> Apps Script. Replace the contents with this file.
 *  2. Project Settings (gear) -> Script Properties. Add:
 *       SHEET_TOKEN      = a long random string (`openssl rand -hex 24`)
 *       CHECKLIST_SHEET  = tab name of the daily checklist            (default "Checklist")
 *       EXERCISE_SHEET   = tab name of the exercise log                (default "Exercise")
 *       EXERCISE_SHEET_ID= spreadsheet id if the exercise log is in a different file (optional)
 *       ITEM_COLUMN      = header of the column holding the item text  (default: first column)
 *       MODE             = "date_columns" | "last_done" | "checkbox"   (default "date_columns")
 *  3. Deploy -> New deployment -> Web app. Execute as: Me. Who has access: Anyone.
 *     (The token check is what stops strangers; sheet sharing is unaffected.)
 *  4. Put the /exec URL in config.toml ([sheets] apps_script_url) and the token in the env as SHEETS_TOKEN
 *     (or Keychain: security add-generic-password -s TaskTracker -a sheets -w).
 *  5. After editing this script: Deploy -> Manage deployments -> Edit -> New version.
 *
 * Checklist layouts supported (MODE):
 *   date_columns : one row per item, one column per date in the header row (YYYY-MM-DD, M/D, or a real date
 *                  cell). Marking done writes TRUE in today's column (created at the right edge if missing).
 *   last_done    : one row per item with a "Last done" column; marking done writes today's date there.
 *   checkbox     : one row per item with a "Done" checkbox column that YOU reset each day; marking done ticks it.
 * Every other column (e.g. "Frequency", "Minutes", "When") is passed through to the scheduler untouched.
 */
var P = PropertiesService.getScriptProperties();

function cfg(k, d) { var v = P.getProperty(k); return (v === null || v === '') ? d : v; }
function json(o) { return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON); }
function tokenOk(t) { var want = cfg('SHEET_TOKEN', ''); return want && t === want; }

function checklistSheet() {
  return SpreadsheetApp.getActiveSpreadsheet().getSheetByName(cfg('CHECKLIST_SHEET', 'Checklist'));
}
function exerciseSheet() {
  var id = cfg('EXERCISE_SHEET_ID', '');
  var ss = id ? SpreadsheetApp.openById(id) : SpreadsheetApp.getActiveSpreadsheet();
  return ss.getSheetByName(cfg('EXERCISE_SHEET', 'Exercise'));
}

function todayKey() {
  return Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd');
}
function headerKey(v) {
  if (v instanceof Date) return Utilities.formatDate(v, Session.getScriptTimeZone(), 'yyyy-MM-dd');
  var s = String(v).trim();
  var m = s.match(/^(\d{1,2})\/(\d{1,2})(?:\/(\d{2,4}))?$/);     // M/D or M/D/YY
  if (m) {
    var y = m[3] ? (m[3].length === 2 ? 2000 + Number(m[3]) : Number(m[3])) : new Date().getFullYear();
    return y + '-' + ('0' + m[1]).slice(-2) + '-' + ('0' + m[2]).slice(-2);
  }
  return s;
}

function readSheet(sheet) {
  if (!sheet) return { headers: [], rows: [] };
  var values = sheet.getDataRange().getValues();
  if (values.length === 0) return { headers: [], rows: [] };
  var headers = values[0].map(function (h) { return h instanceof Date ? headerKey(h) : String(h).trim(); });
  var rows = [];
  for (var r = 1; r < values.length; r++) {
    var row = {}, empty = true;
    for (var c = 0; c < headers.length; c++) {
      var v = values[r][c];
      if (v instanceof Date) v = Utilities.formatDate(v, Session.getScriptTimeZone(), 'yyyy-MM-dd');
      row[headers[c] || ('col' + c)] = v;
      if (v !== '' && v !== null) empty = false;
    }
    if (!empty) { row._row = r + 1; rows.push(row); }
  }
  return { headers: headers, rows: rows };
}

function itemColumnIndex(headers) {
  var want = cfg('ITEM_COLUMN', '');
  if (want) { var i = headers.indexOf(want); if (i >= 0) return i; }
  return 0;
}

function checklistWithStatus() {
  var sheet = checklistSheet();
  var data = readSheet(sheet);
  var mode = cfg('MODE', 'date_columns');
  var ic = itemColumnIndex(data.headers);
  var itemHeader = data.headers[ic] || 'col0';
  var today = todayKey();
  data.item_column = itemHeader;
  data.mode = mode;
  data.rows.forEach(function (row) {
    row.item = String(row[itemHeader] || '').trim();
    var done = false;
    if (mode === 'date_columns') done = row[today] === true || String(row[today]).toLowerCase() === 'true' || row[today] === 1 || String(row[today]) === 'x';
    else if (mode === 'last_done') done = row['Last done'] === today || row['last_done'] === today;
    else if (mode === 'checkbox') done = row['Done'] === true || row['done'] === true;
    row.done_today = done;
  });
  data.rows = data.rows.filter(function (r) { return r.item; });
  return data;
}

function doGet(e) {
  var p = (e && e.parameter) || {};
  if (!tokenOk(p.token)) return json({ ok: false, error: 'unauthorized' });
  return json({ ok: true, today: todayKey(), checklist: checklistWithStatus(), exercise: readSheet(exerciseSheet()) });
}

function markDone(item, undo) {
  var sheet = checklistSheet();
  if (!sheet) return { ok: false, error: 'checklist sheet not found' };
  var data = readSheet(sheet);
  var ic = itemColumnIndex(data.headers);
  var mode = cfg('MODE', 'date_columns');
  var today = todayKey();
  var target = null;
  data.rows.forEach(function (row) {
    if (!target && String(row[data.headers[ic]] || '').trim().toLowerCase() === String(item).trim().toLowerCase()) target = row;
  });
  if (!target) return { ok: false, error: 'item not found: ' + item };
  var col;
  if (mode === 'date_columns') {
    col = data.headers.indexOf(today) + 1;
    if (col === 0) {                                   // add today's column at the right edge
      col = sheet.getLastColumn() + 1;
      sheet.getRange(1, col).setValue(today);
    }
    sheet.getRange(target._row, col).setValue(!undo);
  } else if (mode === 'last_done') {
    col = data.headers.indexOf('Last done') + 1 || data.headers.indexOf('last_done') + 1;
    if (col === 0) { col = sheet.getLastColumn() + 1; sheet.getRange(1, col).setValue('Last done'); }
    sheet.getRange(target._row, col).setValue(undo ? '' : today);
  } else {
    col = data.headers.indexOf('Done') + 1 || data.headers.indexOf('done') + 1;
    if (col === 0) { col = sheet.getLastColumn() + 1; sheet.getRange(1, col).setValue('Done'); }
    sheet.getRange(target._row, col).setValue(!undo);
  }
  return { ok: true, item: item, row: target._row, column: col, date: today };
}

function doPost(e) {
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    var data = JSON.parse(e.postData.contents || '{}');
    if (!tokenOk(data.token)) return json({ ok: false, error: 'unauthorized' });
    if (data.action === 'done') return json(markDone(data.item, false));
    if (data.action === 'undo') return json(markDone(data.item, true));
    return json({ ok: false, error: 'unknown action' });
  } catch (err) {
    return json({ ok: false, error: String(err) });
  } finally {
    lock.releaseLock();
  }
}
