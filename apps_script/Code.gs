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
 *       ITEM_COLUMN      = column letter ("G") or header of the column holding the item text (default: first)
 *       HEADER_ROW       = row number holding the date headers (default: found automatically, the first row
 *                          within the top 10 that holds a date; otherwise 1)
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
 * Rows hidden in the sheet are skipped. In date_columns mode a row is checkable only when today's cell is a
 * checkbox; a formula cell (e.g. a TRUE/FALSE computed from another tab) is reported read-only with its value,
 * and a row with neither (a section heading, a dropdown) is passed through as text.
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

function isDateLike(v) {
  if (v instanceof Date) return true;
  var s = String(v).trim();
  return /^\d{4}-\d{2}-\d{2}$/.test(s) || /^\d{1,2}\/\d{1,2}(\/\d{2,4})?$/.test(s);
}
function headerRowIndex(values) {          // 0-based index of the header row
  var want = Number(cfg('HEADER_ROW', ''));
  if (want >= 1) return want - 1;
  for (var r = 0; r < Math.min(10, values.length); r++) {
    for (var c = 0; c < values[r].length; c++) if (isDateLike(values[r][c])) return r;
  }
  return 0;
}
function columnIndexFromLetter(s) {       // "G" -> 6; "" when not a letter reference
  if (!/^[A-Za-z]{1,3}$/.test(s)) return -1;
  var n = 0; s = s.toUpperCase();
  for (var i = 0; i < s.length; i++) n = n * 26 + (s.charCodeAt(i) - 64);
  return n - 1;
}

function readSheet(sheet) {
  if (!sheet) return { headers: [], rows: [], header_row: 1 };
  var values = sheet.getDataRange().getValues();
  if (values.length === 0) return { headers: [], rows: [], header_row: 1 };
  var hr = headerRowIndex(values);
  var headers = values[hr].map(function (h, c) {
    var s = h instanceof Date ? headerKey(h) : headerKey(String(h).trim());
    return s || columnLetter(c);              // an empty header cell: the column letter (e.g. "G")
  });
  var rows = [];
  for (var r = hr + 1; r < values.length; r++) {
    if (sheet.isRowHiddenByUser(r + 1)) continue;
    var row = {}, empty = true;
    for (var c = 0; c < headers.length; c++) {
      var v = values[r][c];
      if (v instanceof Date) v = Utilities.formatDate(v, Session.getScriptTimeZone(), 'yyyy-MM-dd');
      row[headers[c]] = v;
      if (v !== '' && v !== null) empty = false;
    }
    if (!empty) { row._row = r + 1; rows.push(row); }
  }
  return { headers: headers, rows: rows, header_row: hr + 1 };
}
function columnLetter(c) { var s = ''; c += 1; while (c > 0) { var m = (c - 1) % 26; s = String.fromCharCode(65 + m) + s; c = (c - m - 1) / 26; } return s; }

function itemColumnIndex(headers) {
  var want = cfg('ITEM_COLUMN', '');
  if (want) {
    var i = headers.indexOf(want); if (i >= 0) return i;
    var j = columnIndexFromLetter(want); if (j >= 0 && j < headers.length) return j;
  }
  return 0;
}
function cellKind(sheet, r, c) {            // what today's cell is: "checkbox" | "formula" | "choice" | "value"
  var range = sheet.getRange(r, c);
  if (range.getFormula()) return 'formula';
  var dv = range.getDataValidation();
  if (dv) {
    var t = dv.getCriteriaType();
    if (t === SpreadsheetApp.DataValidationCriteria.CHECKBOX) return 'checkbox';
    if (t === SpreadsheetApp.DataValidationCriteria.VALUE_IN_LIST || t === SpreadsheetApp.DataValidationCriteria.VALUE_IN_RANGE) return 'choice';
  }
  return 'value';
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
  var todayCol = data.headers.indexOf(today) + 1;
  data.rows.forEach(function (row) {
    row.item = String(row[itemHeader] || '').trim();
    var done = false, kind = 'checkbox';
    if (mode === 'date_columns') {
      done = row[today] === true || String(row[today]).toLowerCase() === 'true' || row[today] === 1 || String(row[today]) === 'x';
      kind = todayCol ? cellKind(sheet, row._row, todayCol) : 'value';
      if (kind === 'value' && (row[today] === true || row[today] === false)) kind = 'checkbox';   // plain TRUE/FALSE cells are fine to write
    }
    else if (mode === 'last_done') done = row['Last done'] === today || row['last_done'] === today;
    else if (mode === 'checkbox') done = row['Done'] === true || row['done'] === true;
    row.done_today = done;
    row.checkable = kind === 'checkbox';
    row.computed = kind === 'formula';
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
  var col, hr = data.header_row || 1;
  if (mode === 'date_columns') {
    col = data.headers.indexOf(today) + 1;
    if (col === 0) {                                   // add today's column at the right edge
      col = sheet.getLastColumn() + 1;
      sheet.getRange(hr, col).setValue(new Date());
      sheet.getRange(hr, col).setNumberFormat('yyyy-mm-dd');
    }
    var kind = cellKind(sheet, target._row, col);
    if (kind === 'formula') return { ok: false, error: 'that row is computed by a formula in the sheet' };
    if (kind === 'choice') return { ok: false, error: 'that row is a dropdown in the sheet, not a checkbox' };
    sheet.getRange(target._row, col).setValue(!undo);
  } else if (mode === 'last_done') {
    col = data.headers.indexOf('Last done') + 1 || data.headers.indexOf('last_done') + 1;
    if (col === 0) { col = sheet.getLastColumn() + 1; sheet.getRange(hr, col).setValue('Last done'); }
    sheet.getRange(target._row, col).setValue(undo ? '' : today);
  } else {
    col = data.headers.indexOf('Done') + 1 || data.headers.indexOf('done') + 1;
    if (col === 0) { col = sheet.getLastColumn() + 1; sheet.getRange(hr, col).setValue('Done'); }
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
