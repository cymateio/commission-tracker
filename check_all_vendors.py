#!/usr/bin/env python3
"""
Check vendor affiliate dashboards against Gmail receipts in Supabase.

Modes:
  python3 check_all_vendors.py                       → all vendors, human output
  python3 check_all_vendors.py --json                → all vendors, JSON output
  python3 check_all_vendors.py --vendor zapmail      → single vendor, human output
  python3 check_all_vendors.py --vendor zapmail --json  → single vendor, JSON
"""
import asyncio, json, re, sys
from datetime import datetime
from pathlib import Path
import requests

sys.path.insert(0, str(Path.home() / 'Library/Python/3.9/lib/python3.9/site-packages'))
sys.path.insert(0, str(Path.home() / 'Library/Python/3.9/lib/python/site-packages'))
from playwright.async_api import async_playwright

JSON_MODE  = '--json' in sys.argv
VENDOR_ARG = next(
    (sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == '--vendor' and i + 1 < len(sys.argv)),
    None,
)

_results = {}
_cache   = {}

SUPABASE_URL = 'https://ygotkwhvydmisrkyofec.supabase.co'
SUPABASE_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inlnb3Rrd2h2eWRtaXNya3lvZmVjIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MzU0NzI0MywiZXhwIjoyMDk5MTIzMjQzfQ.VR75mGmkG5yp2INNKaeSeseg1GMPfkBh7660IJPopGg'
OWNER_ID     = '1e86ded9-ad34-431d-b60f-59ad5d80d754'
SESSIONS_DIR = Path(__file__).parent / 'scraper_sessions'

SB_READ  = {'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}'}
SB_WRITE = {**SB_READ, 'Content-Type': 'application/json'}

def log(msg):
    if not JSON_MODE:
        print(msg)

def _init(vendor):
    if vendor not in _results:
        _results[vendor] = {'ok': True, 'rows': [], 'updated': 0, 'error': None, 'skipped': False}

def session_path(v):  return str(SESSIONS_DIR / f'{v}_session.json')
def session_exists(v): return (SESSIONS_DIR / f'{v}_session.json').exists()

def get_existing(vendor):
    res = requests.get(
        f'{SUPABASE_URL}/rest/v1/commissions'
        f'?vendor=eq.{vendor}&user_id=eq.{OWNER_ID}&select=id,month,expected,received',
        headers=SB_READ,
    )
    return {r['month']: r for r in (res.json() if res.ok else [])}

def _write_row(vendor, month, payload):
    row = _cache.get(vendor, {}).get(month)
    if row:
        res = requests.patch(
            f'{SUPABASE_URL}/rest/v1/commissions?id=eq.{row["id"]}',
            headers=SB_WRITE, json=payload,
        )
        return 'updated' if res.ok else None
    else:
        full = {'vendor': vendor, 'month': month, 'user_id': OWNER_ID, **payload}
        res = requests.post(f'{SUPABASE_URL}/rest/v1/commissions', headers=SB_WRITE, json=full)
        if res.ok:
            _results[vendor]['updated'] += 1
            return 'inserted'
        return None

def set_expected(vendor, month, amount):
    row = _cache.get(vendor, {}).get(month, {})
    if row.get('expected') == amount:
        return
    action = _write_row(vendor, month, {'expected': amount})
    if action:
        _results[vendor]['updated'] += 1
        log(f'  ✓ {action} {vendor}/{month}: expected=${amount:.2f}')

def _write_check_history(vendor, period, dashboard_amount, received_amount, status, reason=None):
    diff = None
    if dashboard_amount is not None and received_amount is not None:
        diff = round(float(received_amount) - float(dashboard_amount), 2)
    payload = {
        'vendor': vendor, 'period': period,
        'dashboard_amount': dashboard_amount,
        'received_amount': received_amount,
        'difference': diff,
        'status': status,
        'reason': reason,
        'user_id': OWNER_ID,
    }
    try:
        requests.post(f'{SUPABASE_URL}/rest/v1/commission_checks', headers=SB_WRITE, json=payload)
    except Exception:
        pass  # Table may not exist yet — fail silently

def _compare_and_record(vendor, period, dashboard_amount):
    """Compare dashboard amount vs Gmail-synced received amount. Write history. Return row dict."""
    existing = _cache.get(vendor, {}).get(period, {})
    received = existing.get('received')

    if received is None:
        status = 'unable_to_verify'
        reason = f'No receipt found in Gmail for {vendor} / {period}'
    elif abs(float(dashboard_amount) - float(received)) <= 0.01:
        status = 'matched'
        reason = None
    else:
        diff = round(float(received) - float(dashboard_amount), 2)
        status = 'discrepancy'
        reason = f'Dashboard: ${dashboard_amount:.2f}  |  Received: ${received:.2f}  |  Diff: ${diff:+.2f}'

    _write_check_history(vendor, period, dashboard_amount, received, status, reason)

    return {
        'period': period,
        'dashboard_amount': dashboard_amount,
        'received_amount': received,
        'status': status,
        'difference': round(float(received) - float(dashboard_amount), 2) if received is not None else None,
        'reason': reason,
    }

def parse_payout_rows(text):
    """Extract (YYYY-MM, amount) pairs from page text."""
    rows = []
    combined = ' '.join(text.split('\n'))
    for m in re.finditer(
        r'(January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+(\d{4})\s+\$?([\d,]+\.\d{2})',
        combined, re.IGNORECASE,
    ):
        try:
            dt = datetime.strptime(f'{m.group(1)} {m.group(2)}', '%B %Y')
            rows.append((dt.strftime('%Y-%m'), round(float(m.group(3).replace(',', '')), 2)))
        except ValueError:
            pass
    for m in re.finditer(
        r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\.?\s+\d{1,2},?\s+\d{4})'
        r'.{0,100}?\$([\d,]+\.\d{2})',
        combined, re.IGNORECASE,
    ):
        date_raw = re.sub(r',', '', m.group(1).strip())
        for fmt in ('%b %d %Y', '%B %d %Y', '%b. %d %Y'):
            try:
                dt = datetime.strptime(date_raw, fmt)
                rows.append((dt.strftime('%Y-%m'), round(float(m.group(2).replace(',', '')), 2)))
                break
            except ValueError:
                pass
    return rows

def _by_month(pairs, min_month='2026-01'):
    agg = {}
    for mk, amt in pairs:
        if mk >= min_month:
            agg[mk] = round(agg.get(mk, 0.0) + amt, 2)
    return agg

def compute_discrepancies():
    issues = []
    for vendor, months in _cache.items():
        for month, row in months.items():
            if month < '2026-01':
                continue
            exp = row.get('expected')
            rec = row.get('received')
            if exp is not None and rec is not None and abs(float(exp) - float(rec)) > 0.01:
                issues.append({'vendor': vendor, 'month': month, 'expected': exp, 'received': rec,
                               'diff': round(float(rec) - float(exp), 2), 'type': 'mismatch'})
            elif exp is not None and rec is None:
                issues.append({'vendor': vendor, 'month': month, 'expected': exp, 'received': None,
                               'type': 'missing_received'})
    return issues

# ── Zapmail ────────────────────────────────────────────────────────────────────
async def check_zapmail(pw):
    log('\n=== ZAPMAIL ===')
    _init('zapmail')
    if not session_exists('zapmail'):
        _results['zapmail'].update({'skipped': True, 'error': 'Login required'}); return
    try:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=session_path('zapmail'))
        page = await ctx.new_page()
        await page.goto('https://affiliates.zapmail.ai/payouts', wait_until='domcontentloaded', timeout=20000)
        await page.wait_for_timeout(3000)
        try:
            await page.locator('text=Paid Payouts').first.click()
            await page.wait_for_timeout(2000)
        except Exception:
            pass
        body = await page.locator('body').inner_text()
        await browser.close()

        by_period = _by_month(parse_payout_rows(body))
        if not by_period:
            for ms, amt in re.findall(
                r'((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})'
                r'\s+\$([\d,]+\.?\d*)', body,
            ):
                try:
                    dt = datetime.strptime(ms.strip(), '%B %Y')
                    mk = dt.strftime('%Y-%m')
                    if mk >= '2026-01':
                        by_period[mk] = by_period.get(mk, 0.0) + round(float(amt.replace(',', '')), 2)
                except ValueError:
                    pass

        if not by_period:
            _results['zapmail']['error'] = 'Could not parse paid payouts — session may be expired'
            log('  ! Could not parse paid payouts'); return

        for period, amount in by_period.items():
            set_expected('zapmail', period, amount)
            row = _compare_and_record('zapmail', period, amount)
            _results['zapmail']['rows'].append(row)
            log(f"  {period}: dashboard=${amount:.2f} | status={row['status']}")
    except Exception as e:
        _results['zapmail'].update({'ok': False, 'error': str(e)})
        log(f'  ! Error: {e}')

# ── HeyReach (Tolt) ───────────────────────────────────────────────────────────
async def check_heyreach(pw):
    log('\n=== HEYREACH ===')
    _init('heyreach')
    if not session_exists('heyreach'):
        _results['heyreach'].update({'skipped': True, 'error': 'Login required'}); return
    try:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=session_path('heyreach'))
        page = await ctx.new_page()
        await page.goto('https://heyreach.tolt.io/payouts', wait_until='networkidle', timeout=20000)
        await page.wait_for_timeout(3000)
        body = await page.locator('body').inner_text()
        await browser.close()

        log('  Payouts page (relevant lines):')
        for line in body.split('\n'):
            s = line.strip()
            if s and any(kw in s.lower() for kw in ['paid', 'payout', '$', '2026', '2025']):
                log(f'    {s}')

        by_period = _by_month(parse_payout_rows(body))
        if not by_period:
            _results['heyreach']['error'] = 'Could not parse payout history — session may be expired'
            log('  ! Could not parse payout rows')
        else:
            for period, amount in by_period.items():
                set_expected('heyreach', period, amount)
                row = _compare_and_record('heyreach', period, amount)
                _results['heyreach']['rows'].append(row)
                log(f"  {period}: dashboard=${amount:.2f} | status={row['status']}")
    except Exception as e:
        _results['heyreach'].update({'ok': False, 'error': str(e)})
        log(f'  ! Error: {e}')

# ── Inboxkit ──────────────────────────────────────────────────────────────────
async def check_inboxkit(pw):
    log('\n=== INBOXKIT ===')
    _init('inboxkit')
    if not session_exists('inboxkit'):
        _results['inboxkit'].update({'skipped': True, 'error': 'Login required'}); return
    try:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=session_path('inboxkit'))
        page = await ctx.new_page()

        # Try billing page first, then partner/payouts page
        found = False
        for url in ['https://studio.inboxkit.com/billing', 'https://studio.inboxkit.com/partner']:
            await page.goto(url, wait_until='domcontentloaded', timeout=20000)
            try:
                await page.wait_for_load_state('networkidle', timeout=8000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)
            body = await page.locator('body').inner_text()

            log(f'  Page: {url}')
            log('  Relevant lines:')
            for line in body.split('\n'):
                s = line.strip()
                if s and any(kw in s.lower() for kw in ['payout', 'available', 'earning', '$', 'balance', 'commission', 'withdraw']):
                    log(f'    {s}')

            # Try all known label patterns
            PATTERNS = [
                r'Available\s+for\s+Payout\s*:?\s*\$?([\d,]+\.?\d*)',
                r'Available\s+Balance\s*:?\s*\$?([\d,]+\.?\d*)',
                r'Pending\s+Payout\s*:?\s*\$?([\d,]+\.?\d*)',
                r'Pending\s+Earnings?\s*:?\s*\$?([\d,]+\.?\d*)',
                r'Unpaid\s+Earnings?\s*:?\s*\$?([\d,]+\.?\d*)',
                r'Earnings?\s+Balance\s*:?\s*\$?([\d,]+\.?\d*)',
                r'Commission\s+Balance\s*:?\s*\$?([\d,]+\.?\d*)',
                r'Withdraw\s+Balance\s*:?\s*\$?([\d,]+\.?\d*)',
                r'\$?([\d,]+\.\d{2})\s*(?:\n[^\n]{0,40})?Available for Payout',
                r'\$?([\d,]+\.\d{2})\s*(?:\n[^\n]{0,40})?Available Balance',
                r'\$?([\d,]+\.\d{2})\s*(?:\n[^\n]{0,40})?Pending Payout',
            ]
            m = None
            for pat in PATTERNS:
                m = re.search(pat, body, re.IGNORECASE)
                if m:
                    log(f'  Matched pattern: {pat}')
                    break

            if m:
                amount = round(float(m.group(1).replace(',', '')), 2)
                period = datetime.now().strftime('%Y-%m')
                log(f'  Found balance: ${amount:.2f}')
                set_expected('inboxkit', period, amount)
                row = _compare_and_record('inboxkit', period, amount)
                _results['inboxkit']['rows'].append(row)
                log(f"  {period}: dashboard=${amount:.2f} | status={row['status']}")
                found = True
                break

        await browser.close()

        if not found:
            # Log first 60 non-empty lines of last page for debugging
            preview = '\n'.join(l.strip() for l in body.split('\n') if l.strip())[:800]
            _results['inboxkit']['error'] = (
                'Could not find payout balance on billing or partner pages. '
                'The page may have changed. Page preview:\n' + preview[:300]
            )
            log('  ! Could not parse any payout balance. Full relevant content above.')
    except Exception as e:
        _results['inboxkit'].update({'ok': False, 'error': str(e)})
        log(f'  ! Error: {e}')

# ── Icypeas (FirstPromoter) ───────────────────────────────────────────────────
async def check_icypeas(pw):
    log('\n=== ICYPEAS ===')
    _init('icypeas')
    if not session_exists('icypeas'):
        _results['icypeas'].update({'skipped': True, 'error': 'Login required'}); return
    try:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=session_path('icypeas'))
        page = await ctx.new_page()
        await page.goto('https://icypeas.firstpromoter.com', wait_until='networkidle', timeout=20000)
        await page.wait_for_timeout(3000)
        body = await page.locator('body').inner_text()
        await browser.close()

        log('  FirstPromoter (relevant lines):')
        for line in body.split('\n'):
            s = line.strip()
            if s and any(kw in s.lower() for kw in ['commission', 'payout', 'paid', 'earning', '$', 'balance']):
                log(f'    {s}')

        m = (re.search(r'(?:unpaid|available|balance)\s*(?:commissions?)?\s*:?\s*\$?([\d,]+\.?\d*)', body, re.IGNORECASE)
             or re.search(r'\$([\d,]+\.?\d*)\s*\n?(?:unpaid|available|balance)', body, re.IGNORECASE))
        if m:
            amount = round(float(m.group(1).replace(',', '')), 2)
            period = datetime.now().strftime('%Y-%m')
            log(f'  Found balance: ${amount:.2f}')
            set_expected('icypeas', period, amount)
            row = _compare_and_record('icypeas', period, amount)
            _results['icypeas']['rows'].append(row)
            log(f"  {period}: dashboard=${amount:.2f} | status={row['status']}")
        else:
            _results['icypeas']['error'] = 'Could not parse balance — session may be expired'
            log('  ! Could not parse balance')
    except Exception as e:
        _results['icypeas'].update({'ok': False, 'error': str(e)})
        log(f'  ! Error: {e}')

# ── LeadMagic (Dub.co) ───────────────────────────────────────────────────────
async def check_leadmagic(pw):
    log('\n=== LEADMAGIC ===')
    _init('leadmagic')
    if not session_exists('leadmagic'):
        _results['leadmagic'].update({'skipped': True, 'error': 'Login required'}); return
    try:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=session_path('leadmagic'))
        page = await ctx.new_page()
        await page.goto('https://partners.dub.co/programs/leadmagic', wait_until='networkidle', timeout=20000)
        await page.wait_for_timeout(4000)
        body = await page.locator('body').inner_text()
        await browser.close()

        log('  Dub.co (relevant lines):')
        for line in body.split('\n'):
            s = line.strip()
            if s and any(kw in s.lower() for kw in ['earning', 'payout', 'paid', '$', 'balance', 'commission', '2026']):
                log(f'    {s}')

        if re.search(r'no earnings|no payouts|no commissions', body, re.IGNORECASE):
            log('  → No earnings on LeadMagic yet')
            period = datetime.now().strftime('%Y-%m')
            _results['leadmagic']['rows'].append({
                'period': period, 'dashboard_amount': 0.0, 'received_amount': None,
                'status': 'unable_to_verify', 'difference': None,
                'reason': 'No earnings found on affiliate dashboard',
            })
        else:
            m = (re.search(r'(?:pending\s+payout|earnings?|balance|available)\s*:?\s*\$?([\d,]+\.?\d*)', body, re.IGNORECASE)
                 or re.search(r'\$([\d,]+\.?\d*)\s*\n?(?:pending|earning|available)', body, re.IGNORECASE))
            if m:
                amount = round(float(m.group(1).replace(',', '')), 2)
                period = datetime.now().strftime('%Y-%m')
                log(f'  Found balance: ${amount:.2f}')
                set_expected('leadmagic', period, amount)
                row = _compare_and_record('leadmagic', period, amount)
                _results['leadmagic']['rows'].append(row)
                log(f"  {period}: dashboard=${amount:.2f} | status={row['status']}")
            else:
                _results['leadmagic']['error'] = 'Could not parse balance'
                log('  ! Could not parse balance')
    except Exception as e:
        _results['leadmagic'].update({'ok': False, 'error': str(e)})
        log(f'  ! Error: {e}')

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    global _cache
    # Smartlead is fully managed by the Gmail Apps Script (EXPECTED_EQUALS_RECEIVED=true).
    # When the PayPal receipt from "521 code" arrives, it sets both expected and received.
    all_vendors = ['zapmail', 'heyreach', 'inboxkit', 'icypeas', 'leadmagic']
    target = [VENDOR_ARG] if VENDOR_ARG and VENDOR_ARG in all_vendors else all_vendors

    for v in target:
        _cache[v] = get_existing(v)
        _init(v)

    vendor_funcs = {
        'zapmail': check_zapmail, 'heyreach': check_heyreach,
        'inboxkit': check_inboxkit, 'icypeas': check_icypeas,
        'leadmagic': check_leadmagic,
    }

    async with async_playwright() as pw:
        for v in target:
            if v in vendor_funcs:
                await vendor_funcs[v](pw)

    for v in target:
        _cache[v] = get_existing(v)

    if JSON_MODE:
        print(json.dumps({'ok': True, 'results': _results, 'discrepancies': compute_discrepancies()}))
    else:
        print('\n=== Done ===')

if __name__ == '__main__':
    asyncio.run(main())
