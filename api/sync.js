/**
 * Commission Tracker — /api/sync
 * Vercel serverless function. Calls each affiliate platform's API,
 * upserts results into Supabase, and returns fresh rows to the client.
 *
 * Required environment variables (set in Vercel project → Settings → Environment Variables):
 *   SUPABASE_SERVICE_ROLE_KEY  — service_role key for ygotkwhvydmisrkyofec
 *   REWARDFUL_API_KEY          — Smartlead / Rewardful campaign API secret
 *   TOLT_API_KEY               — HeyReach / Tolt partner API token
 *   FIRSTPROMOTER_API_KEY      — Icypeas / FirstPromoter API key
 *   DUBCO_API_KEY              — LeadMagic / Dub.co API key
 *
 * Zapmail and Inboxkit have no public affiliate API and are skipped with a portal link.
 */

const SB_URL   = 'https://ygotkwhvydmisrkyofec.supabase.co';
const OWNER_ID = '1e86ded9-ad34-431d-b60f-59ad5d80d754'; // admin@cymate.io

function sbHeaders(key) {
  return {
    'Content-Type': 'application/json',
    apikey: key,
    Authorization: `Bearer ${key}`,
  };
}

// Safe upsert: query by vendor+month, PATCH if row exists, POST if not.
// Avoids duplicates since the table has no unique constraint on (vendor, month).
async function sbUpsertRow(sbKey, vendor, month, fields) {
  const qs  = `vendor=eq.${encodeURIComponent(vendor)}&month=eq.${encodeURIComponent(month)}&select=id`;
  const res = await fetch(`${SB_URL}/rest/v1/commissions?${qs}`, { headers: sbHeaders(sbKey) });
  if (!res.ok) throw new Error(`Supabase query failed (${res.status})`);
  const existing = await res.json();

  const payload = { vendor, month, user_id: OWNER_ID, updated_at: new Date().toISOString(), ...fields };

  if (Array.isArray(existing) && existing.length > 0) {
    // Remove any duplicate rows beyond the first
    if (existing.length > 1) {
      const extras = existing.slice(1).map(r => `id.eq.${r.id}`).join(',');
      await fetch(`${SB_URL}/rest/v1/commissions?or=(${extras})`, {
        method: 'DELETE', headers: sbHeaders(sbKey),
      });
    }
    const r = await fetch(`${SB_URL}/rest/v1/commissions?id=eq.${existing[0].id}`, {
      method: 'PATCH',
      headers: { ...sbHeaders(sbKey), Prefer: 'return=minimal' },
      body: JSON.stringify(payload),
    });
    if (!r.ok) throw new Error(`Supabase patch failed (${r.status})`);
    return 'updated';
  }

  const r = await fetch(`${SB_URL}/rest/v1/commissions`, {
    method: 'POST',
    headers: { ...sbHeaders(sbKey), Prefer: 'return=minimal' },
    body: JSON.stringify(payload),
  });
  if (!r.ok) throw new Error(`Supabase insert failed (${r.status})`);
  return 'inserted';
}

// Earned month rules:
//   zapmail   → payment month − 3 (3-month processing lag)
//   all others → same month as payment
function earnedMonth(vendor, yyyyMM) {
  const [y, m] = yyyyMM.split('-').map(Number);
  if (vendor === 'zapmail') {
    const d = new Date(Date.UTC(y, m - 4, 1)); // 0-indexed month, minus 3 more
    return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`;
  }
  return yyyyMM;
}

// ── Smartlead / Rewardful ────────────────────────────────────────────────────
// API key: Rewardful → campaign settings → API secret
// Amounts returned in cents (integer). Payout = commission for Smartlead.
async function syncSmartlead(sbKey) {
  const apiKey = process.env.REWARDFUL_API_KEY;
  if (!apiKey) return { ok: false, skipped: true, reason: 'REWARDFUL_API_KEY not set' };

  const auth = Buffer.from(`${apiKey}:`).toString('base64');
  const r = await fetch('https://api.rewardful.com/v1/commissions?limit=100&expand[]=sale', {
    headers: { Authorization: `Basic ${auth}` },
  });
  if (!r.ok) {
    const body = await r.text().catch(() => '');
    return { ok: false, error: `Rewardful ${r.status}: ${body.slice(0, 200)}` };
  }

  const { data = [] } = await r.json();
  const byMonth = {};
  for (const c of data) {
    if (!['paid', 'approved'].includes(c.state)) continue;
    const month = (c.paid_at || c.created_at || '').slice(0, 7);
    if (!month) continue;
    byMonth[month] = (byMonth[month] || 0) + ((c.amount || 0) / 100);
  }

  const actions = [];
  for (const [month, amt] of Object.entries(byMonth)) {
    const rounded = +amt.toFixed(2);
    const action  = await sbUpsertRow(sbKey, 'smartlead', month, { expected: rounded, received: rounded });
    actions.push({ month, amount: rounded, action });
  }
  return { ok: true, upserted: actions.length, rows: actions };
}

// ── HeyReach / Tolt ─────────────────────────────────────────────────────────
// API key: Tolt partner dashboard → Settings → API
// Amounts assumed cents. Same month rule (not Net-15).
async function syncHeyreach(sbKey) {
  const apiKey = process.env.TOLT_API_KEY;
  if (!apiKey) return { ok: false, skipped: true, reason: 'TOLT_API_KEY not set' };

  const r = await fetch('https://api.tolt.io/v1/payouts?status=paid&limit=100', {
    headers: { Authorization: `Bearer ${apiKey}` },
  });
  if (!r.ok) {
    const body = await r.text().catch(() => '');
    return { ok: false, error: `Tolt ${r.status}: ${body.slice(0, 200)}` };
  }

  const body    = await r.json();
  const payouts = body.data || (Array.isArray(body) ? body : []);
  const actions = [];

  for (const p of payouts) {
    const payDate = (p.paid_at || p.created_at || '').slice(0, 7);
    if (!payDate) continue;
    const month   = earnedMonth('heyreach', payDate);
    const gross   = +((p.amount || 0) / 100).toFixed(2);
    const action  = await sbUpsertRow(sbKey, 'heyreach', month, {
      expected: gross,
      payment_date: (p.paid_at || '').slice(0, 10) || null,
    });
    actions.push({ month, amount: gross, action });
  }
  return { ok: true, upserted: actions.length, rows: actions };
}

// ── Icypeas / FirstPromoter ──────────────────────────────────────────────────
// API key: FirstPromoter → Settings → Integrations → API key
// Amounts in dollars (float).
async function syncIcypeas(sbKey) {
  const apiKey = process.env.FIRSTPROMOTER_API_KEY;
  if (!apiKey) return { ok: false, skipped: true, reason: 'FIRSTPROMOTER_API_KEY not set' };

  const r = await fetch('https://firstpromoter.com/api/v1/rewards?state=paid&limit=100', {
    headers: { 'x-api-key': apiKey },
  });
  if (!r.ok) {
    const body = await r.text().catch(() => '');
    return { ok: false, error: `FirstPromoter ${r.status}: ${body.slice(0, 200)}` };
  }

  const raw  = await r.json();
  const list = Array.isArray(raw) ? raw : (raw.data || []);
  const byMonth = {};
  for (const rw of list) {
    const date = (rw.paid_at || rw.created_at || '').slice(0, 7);
    if (!date) continue;
    byMonth[date] = (byMonth[date] || 0) + (parseFloat(rw.amount) || 0);
  }

  const actions = [];
  for (const [month, amt] of Object.entries(byMonth)) {
    const rounded = +amt.toFixed(2);
    const action  = await sbUpsertRow(sbKey, 'icypeas', month, { expected: rounded });
    actions.push({ month, amount: rounded, action });
  }
  return { ok: true, upserted: actions.length, rows: actions };
}

// ── LeadMagic / Dub.co Partners ─────────────────────────────────────────────
// API key: Dub.co → Settings → API keys
// Amounts assumed cents.
async function syncLeadmagic(sbKey) {
  const apiKey = process.env.DUBCO_API_KEY;
  if (!apiKey) return { ok: false, skipped: true, reason: 'DUBCO_API_KEY not set' };

  const r = await fetch('https://api.dub.co/partners/payouts?status=completed&pageSize=100', {
    headers: { Authorization: `Bearer ${apiKey}` },
  });
  if (!r.ok) {
    const body = await r.text().catch(() => '');
    return { ok: false, error: `Dub.co ${r.status}: ${body.slice(0, 200)}` };
  }

  const body    = await r.json();
  const payouts = body.result || body.data || (Array.isArray(body) ? body : []);
  const actions = [];

  for (const p of payouts) {
    const date = (p.periodStart || p.createdAt || '').slice(0, 7);
    if (!date) continue;
    const amt    = +((p.amount || 0) / 100).toFixed(2);
    const action = await sbUpsertRow(sbKey, 'leadmagic', date, { expected: amt });
    actions.push({ month: date, amount: amt, action });
  }
  return { ok: true, upserted: actions.length, rows: actions };
}

// ── No-API vendors ───────────────────────────────────────────────────────────
async function syncZapmail()  { return { ok: true, skipped: true, reason: 'No public API — update via affiliate portal' }; }
async function syncInboxkit() { return { ok: true, skipped: true, reason: 'No public API — update via Partner Studio' }; }

// ── Handler ──────────────────────────────────────────────────────────────────
const VENDOR_FNS = {
  zapmail:   syncZapmail,
  smartlead: syncSmartlead,
  heyreach:  syncHeyreach,
  icypeas:   syncIcypeas,
  inboxkit:  syncInboxkit,
  leadmagic: syncLeadmagic,
};

module.exports = async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  if (req.method === 'OPTIONS') return res.status(200).end();
  if (req.method !== 'POST')   return res.status(405).json({ error: 'Method not allowed' });

  const sbKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!sbKey) {
    return res.status(500).json({
      error: 'SUPABASE_SERVICE_ROLE_KEY not configured. Add it in Vercel → project → Settings → Environment Variables.',
    });
  }

  const vendors = (req.body && Array.isArray(req.body.vendors))
    ? req.body.vendors
    : Object.keys(VENDOR_FNS);

  const results = {};
  const TIMEOUT = 20_000;

  await Promise.allSettled(
    vendors.map(async (v) => {
      const fn = VENDOR_FNS[v];
      if (!fn) { results[v] = { ok: false, error: 'Unknown vendor' }; return; }
      try {
        const t0 = Date.now();
        results[v] = await Promise.race([
          fn(sbKey),
          new Promise((_, rej) => setTimeout(() => rej(new Error('Timed out after 20s')), TIMEOUT)),
        ]);
        results[v].ms = Date.now() - t0;
      } catch (e) {
        results[v] = { ok: false, error: e.message };
      }
    })
  );

  // Return fresh rows so the client only needs one round-trip
  let rows = [];
  try {
    const r = await fetch(`${SB_URL}/rest/v1/commissions?select=*&order=month.desc`, { headers: sbHeaders(sbKey) });
    if (r.ok) rows = await r.json();
  } catch (_) {}

  // Build discrepancy report
  const discrepancies = [];
  for (const row of rows) {
    if (!row.month || row.month < '2026-01') continue;
    if (row.expected !== null && row.received !== null && Math.abs(row.expected - row.received) > 0.01) {
      discrepancies.push({
        vendor: row.vendor, month: row.month,
        expected: row.expected, received: row.received,
        diff: +((row.received - row.expected).toFixed(2)),
        type: 'mismatch',
      });
    } else if (row.expected !== null && row.received === null) {
      discrepancies.push({
        vendor: row.vendor, month: row.month,
        expected: row.expected, received: null,
        type: 'missing_received',
      });
    }
  }

  return res.status(200).json({ ok: true, results, rows, discrepancies, synced_at: new Date().toISOString() });
};
