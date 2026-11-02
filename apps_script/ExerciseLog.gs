/**
 * Task Tracker <- Exercise logging sheet (read-only).
 *
 * Paste this into the Apps Script project of the exercise logging spreadsheet (Extensions -> Apps Script),
 * add the manifest from apps_script/appsscript.json (scope: this spreadsheet only), set Script Properties
 *   SHEET_TOKEN     = the same random string as the app's "Sheet token" (or its own; see exercise_script_token)
 *   SESSIONS_SHEET  = tab name of the sessions log (default "Sessions")
 * and deploy as a Web app (Execute as: Me, Who has access: Anyone). Put the /exec URL in the app's Settings as
 * "Exercise log script URL". The app reads the Sessions tab and never writes to this file.
 */
var P = PropertiesService.getScriptProperties();
function cfg(k, d) { var v = P.getProperty(k); return (v === null || v === '') ? d : v; }
function json(o) { return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON); }

function readSessions(days) {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(cfg('SESSIONS_SHEET', 'Sessions'));
  if (!sheet) return { error: 'sessions sheet not found', rows: [] };
  var values = sheet.getDataRange().getValues();
  if (values.length < 2) return { headers: [], rows: [] };
  var headers = values[0].map(function (h) { return String(h).trim(); });
  var since = new Date(Date.now() - (days || 120) * 86400000);
  var rows = [];
  for (var r = 1; r < values.length; r++) {
    var row = {}, started = null;
    for (var c = 0; c < headers.length; c++) {
      var v = values[r][c];
      if (headers[c] === 'data_json') continue;                      // the raw blob is not needed
      if (v instanceof Date) { v = Utilities.formatDate(v, Session.getScriptTimeZone(), "yyyy-MM-dd'T'HH:mm:ss"); }
      row[headers[c]] = v;
    }
    if (row.started_at) started = new Date(row.started_at);
    if (!row.exercise && !row.category) continue;
    if (started && !isNaN(started) && started < since) continue;
    rows.push(row);
  }
  return { headers: headers, rows: rows };
}

function doGet(e) {
  var p = (e && e.parameter) || {};
  var want = cfg('SHEET_TOKEN', '');
  if (!want || p.token !== want) return json({ ok: false, error: 'unauthorized' });
  return json({ ok: true, sessions: readSessions(Number(p.days) || 120) });
}
