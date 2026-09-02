/**
 * Commission Receipts Sync — fully automatic.
 * Scans Gmail for affiliate payments (Wise/PayPal), logs them to a Sheet,
 * AND pushes them straight into the Commission Tracker (Supabase).
 * Runs on a weekly trigger — no app needed.
 *
 * OPTIONAL: Store a service_role key in Script Properties (gear → Script Properties)
 *   as SB_SERVICE_ROLE for elevated Supabase access. Without it the script
 *   falls back to the anon key below, which has read/write access to commissions.
 */
const LOOKBACK = '120d';
const SHEET_NAME = 'Commission Receipts';
// Supabase anon key — public, same key used in the frontend dashboard.
const SB_ANON_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inlnb3Rrd2h2eWRtaXNya3lvZmVjIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODM1NDcyNDMsImV4cCI6MjA5OTEyMzI0M30.Ya5cYDBgof2yyT78TIQj20VWndLn1qTyaWXe36Dwchg';

const SB_URL   = 'https://ygotkwhvydmisrkyofec.supabase.co';
const OWNER_ID = '1e86ded9-ad34-431d-b60f-59ad5d80d754';        // admin@cymate.io
const EXPECTED_EQUALS_RECEIVED = { smartlead: true }; // payout = commission
// These vendors are managed via affiliate dashboard scraper — Gmail sync only fills received if row already exists
const DASHBOARD_MANAGED = { zapmail: true, heyreach: true };

const PAYER_MAP = [
  { id: 'zapmail',   re: /rapidify|zapmail/i },
  { id: 'smartlead', re: /521\s*code|smart\s*lead/i },
  { id: 'heyreach',  re: /tolt|hey\s*reach/i },
  { id: 'icypeas',   re: /icypeas/i },
  { id: 'inboxkit',  re: /inbox\s*kit|enrich\s*labs/i },
  { id: 'leadmagic', re: /lead\s*magic/i },
];
const PAYER_BLOCKLIST = /cymate/i;
// Mercury transfers for these vendors are internal account movements, not affiliate commissions
const MERCURY_VENDOR_BLOCKLIST = { zapmail: true, heyreach: true, smartlead: true };

const QUERIES = [
  'from:(noreply@wise.com OR noreply@transferwise.com OR no-reply@wise.com) subject:"Money received from" newer_than:' + LOOKBACK,
  'from:(service@paypal.com OR service@paypal.com.sg) newer_than:' + LOOKBACK,
  'from:(noreply@mercury.com OR no-reply@mercury.com OR notifications@mercury.com) newer_than:' + LOOKBACK,
];

// Called by the dashboard's "Refresh & Sync" button (deployed as a Web App).
function doGet(e) {
  var added = syncReceipts();
  return ContentService.createTextOutput(JSON.stringify({ ok: true, added: added }))
    .setMimeType(ContentService.MimeType.JSON);
}

function syncReceipts() {
  const sheet = getOrCreateSheet_();
  const seen = getSeenMessageIds_(sheet);
  let added = 0, pushed = 0;
  QUERIES.forEach(function (q) {
    GmailApp.search(q, 0, 100).forEach(function (thread) {
      thread.getMessages().forEach(function (msg) {
        const id = msg.getId();
        if (seen[id]) return;
        const p = parseMessage_(msg);
        if (!p) return;
        sheet.appendRow([p.date, p.source, p.payer, p.amount, p.currency, p.vendor, p.month, msg.getSubject(), id]);
        seen[id] = true; added++;
        if (pushToSupabase_(p)) pushed++;
      });
    });
  });
  Logger.log('Sync complete. Added ' + added + ' receipt(s), pushed ' + pushed + ' to tracker.');
  return added;
}

// Push one receipt into the Commission Tracker (Supabase). Returns true on success.
function pushToSupabase_(p) {
  const key = PropertiesService.getScriptProperties().getProperty('SB_SERVICE_ROLE') || SB_ANON_KEY;

  // Dashboard-managed vendors: only update received on existing row, never create new rows
  if (DASHBOARD_MANAGED[p.vendor]) {
    const check = UrlFetchApp.fetch(
      SB_URL + '/rest/v1/commissions?vendor=eq.' + p.vendor + '&month=eq.' + p.month + '&user_id=eq.' + OWNER_ID + '&received=is.null&select=id',
      { headers: { apikey: key, Authorization: 'Bearer ' + key }, muteHttpExceptions: true }
    );
    const rows = JSON.parse(check.getContentText());
    if (!rows || rows.length === 0) return false; // row doesn't exist or received already set
    const res = UrlFetchApp.fetch(SB_URL + '/rest/v1/commissions?id=eq.' + rows[0].id, {
      method: 'patch', contentType: 'application/json',
      headers: { apikey: key, Authorization: 'Bearer ' + key },
      payload: JSON.stringify({ received: p.amount }), muteHttpExceptions: true,
    });
    return res.getResponseCode() < 300;
  }

  // For other vendors (smartlead, icypeas, etc.): check if a row already exists for this
  // vendor+month. If so, add to the existing amounts (handles multiple payouts per month)
  // rather than inserting a duplicate row.
  const existResp = UrlFetchApp.fetch(
    SB_URL + '/rest/v1/commissions?vendor=eq.' + p.vendor + '&month=eq.' + p.month + '&user_id=eq.' + OWNER_ID + '&select=id,expected,received',
    { headers: { apikey: key, Authorization: 'Bearer ' + key }, muteHttpExceptions: true }
  );
  const existingRows = JSON.parse(existResp.getContentText());

  if (existingRows && existingRows.length > 0) {
    const row = existingRows[0];
    const patch = {
      received: (parseFloat(row.received) || 0) + p.amount,
      updated_at: new Date().toISOString(),
    };
    if (EXPECTED_EQUALS_RECEIVED[p.vendor]) patch.expected = (parseFloat(row.expected) || 0) + p.amount;
    const res = UrlFetchApp.fetch(SB_URL + '/rest/v1/commissions?id=eq.' + row.id, {
      method: 'patch', contentType: 'application/json',
      headers: { apikey: key, Authorization: 'Bearer ' + key },
      payload: JSON.stringify(patch), muteHttpExceptions: true,
    });
    return res.getResponseCode() < 300;
  }

  const newRow = { vendor: p.vendor, month: p.month, user_id: OWNER_ID, received: p.amount, updated_at: new Date().toISOString() };
  if (EXPECTED_EQUALS_RECEIVED[p.vendor]) newRow.expected = p.amount;
  const res = UrlFetchApp.fetch(SB_URL + '/rest/v1/commissions', {
    method: 'post', contentType: 'application/json',
    headers: { apikey: key, Authorization: 'Bearer ' + key },
    payload: JSON.stringify(newRow), muteHttpExceptions: true,
  });
  return res.getResponseCode() < 300;
}

function parseMessage_(msg) {
  const subject = msg.getSubject() || '', body = msg.getPlainBody() || '', from = msg.getFrom() || '', date = msg.getDate();
  const source = /wise|transferwise/i.test(from) ? 'Wise' : /mercury/i.test(from) ? 'Mercury' : 'PayPal';
  const payer = extractPayer_(subject, body);
  if (payer && PAYER_BLOCKLIST.test(payer)) return null;
  const haystack = subject + '\n' + (payer || '') + '\n' + body;
  let vendor = '';
  for (var i = 0; i < PAYER_MAP.length; i++) { if (PAYER_MAP[i].re.test(haystack)) { vendor = PAYER_MAP[i].id; break; } }
  if (!vendor) return null;
  // Mercury rows for zapmail/heyreach/smartlead are account transfers, not commissions
  if (source === 'Mercury' && MERCURY_VENDOR_BLOCKLIST[vendor]) return null;
  const amt = extractAmount_(subject + '\n' + body);
  if (!amt) return null;
  // Inboxkit (Mercury) payments arrive ~1 month after the payout is requested — use prior month
  var commissionDate = new Date(date);
  if (vendor === 'inboxkit') commissionDate.setMonth(commissionDate.getMonth() - 1);
  return {
    date: Utilities.formatDate(date, Session.getScriptTimeZone(), 'yyyy-MM-dd'),
    source: source, payer: payer || vendor, amount: amt.value, currency: amt.currency, vendor: vendor,
    month: Utilities.formatDate(commissionDate, Session.getScriptTimeZone(), 'yyyy-MM'),
  };
}

function extractAmount_(text) {
  var m = text.match(/([0-9][0-9,]*\.[0-9]{2})\s*(USD|EUR|GBP)/i);
  if (m) return { value: toNumber_(m[1]), currency: m[2].toUpperCase() };
  m = text.match(/(USD|EUR|GBP)\s*([0-9][0-9,]*\.[0-9]{2})/i);
  if (m) return { value: toNumber_(m[2]), currency: m[1].toUpperCase() };
  m = text.match(/\$\s*([0-9][0-9,]*\.[0-9]{2})/);
  if (m) return { value: toNumber_(m[1]), currency: 'USD' };
  return null;
}
function toNumber_(s) { return parseFloat(String(s).replace(/,/g, '')); }

function extractPayer_(subject, body) {
  // Mercury: "You received a transfer of $X from Enrich Labs LLC"
  var m = subject.match(/received.*?from\s+(.+?)\s*$/i);
  if (m) return m[1].trim();
  m = subject.match(/Money received from\s+(.+?)\s*$/i);
  if (m) return m[1].trim();
  m = subject.match(/^(.*?)\s+sent you/i);
  if (m) return m[1].trim();
  m = (subject + '\n' + body).match(/([A-Za-z0-9][\w .,&'-]{1,40}?)\s+has sent you/i);
  if (m) return m[1].trim();
  m = subject.match(/^(.*?)\s+has authorized a payment/i);
  if (m) return m[1].trim();
  m = (subject + '\n' + body).match(/(?:payment\s+)?from\s+([A-Z0-9][\w .,&'-]{1,60})/);
  if (m) return m[1].trim();
  return '';
}

function getOrCreateSheet_() {
  var ss, props = PropertiesService.getScriptProperties(), id = props.getProperty('SHEET_ID');
  if (id) { try { ss = SpreadsheetApp.openById(id); } catch (e) { ss = null; } }
  if (!ss) { ss = SpreadsheetApp.create('Commission Receipts — Auto Log'); props.setProperty('SHEET_ID', ss.getId()); }
  var sheet = ss.getSheetByName(SHEET_NAME) || ss.getSheets()[0];
  sheet.setName(SHEET_NAME);
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(['Date','Source','Payer','Amount','Currency','Vendor','Month','Subject','MessageId']);
    sheet.setFrozenRows(1);
  }
  return sheet;
}
function getSeenMessageIds_(sheet) {
  var seen = {}, last = sheet.getLastRow();
  if (last < 2) return seen;
  sheet.getRange(2, 9, last - 1, 1).getValues().forEach(function (r) { if (r[0]) seen[r[0]] = true; });
  return seen;
}
function resetSheet() {
  var sheet = getOrCreateSheet_(), last = sheet.getLastRow();
  if (last > 1) sheet.deleteRows(2, last - 1);
  Logger.log('Sheet cleared. Now run syncReceipts().');
}

// Re-push every row already in the sheet to Supabase (one-time backfill).
function backfillToSupabase() {
  var sheet = getOrCreateSheet_(), last = sheet.getLastRow(), n = 0;
  if (last < 2) return;
  sheet.getRange(2, 1, last - 1, 7).getValues().forEach(function (r) {
    if (pushToSupabase_({ vendor: r[5], month: r[6], amount: parseFloat(r[3]) })) n++;
  });
  Logger.log('Backfilled ' + n + ' rows to tracker.');
}

function setup() {
  getOrCreateSheet_();
  var url = SpreadsheetApp.openById(PropertiesService.getScriptProperties().getProperty('SHEET_ID')).getUrl();
  setWeeklyMonday();
  syncReceipts();
  Logger.log('SHEET URL: ' + url);
}

// Run once to schedule syncReceipts for every Monday (~9am). Replaces any existing schedule.
function setWeeklyMonday() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'syncReceipts') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('syncReceipts').timeBased().onWeekDay(ScriptApp.WeekDay.MONDAY).atHour(9).create();
  Logger.log('Scheduled: every Monday ~9am.');
}
