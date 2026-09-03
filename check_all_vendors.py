#!/usr/bin/env python3
"""
Check all vendor dashboards and update Supabase commission tracker.
- Zapmail: reads paid payouts list from affiliates.zapmail.ai
- HeyReach: reads payout history from Tolt
- Inboxkit: reads Available for Payout from /billing
- Smartlead: reads Rewardful payout history
- Icypeas: reads FirstPromoter balance
- LeadMagic: reads Dub.co earnings

Modes:
  python3 check_all_vendors.py          → human-readable output
  python3 check_all_vendors.py --json   → JSON for local_api.py Run Now trigger
"""
import asyncio, re, sys, json
from datetime import datetime
from pathlib import Path
import requests

sys.path.insert(0, str(Path.home() / 'Library/Python/3.9/lib/python3.9/site-packages'))
sys.path.insert(0, str(Path.home() / 'Library/Python/3.9/lib/python/site-packages'))
from playwright.async_api import async_playwright

JSON_MODE = '--json' in sys.argv
_results = {}

SUPABASE_URL = 'https://ygotkwhvydmisrkyofec.supabase.co'
SUPABASE_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inlnb3Rrd2h2eWRtaXNya3lvZmVjIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MzU0NzI0MywiZXhwIjoyMDk5MTIzMjQzfQ.VR75mGmkG5yp2INNKaeSeseg1GMPfkBh7660IJPopGg'
OWNER_ID     = '1e86ded9-ad34-431d-b60f-59ad5d80d754'
SESSIONS_DIR = Path(__file__).parent / 'scraper_sessions'

SB_READ  = {'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}'}
SB_WRITE = {**SB_READ, 'Content-Type': 'application/json'}

# Pre-loaded existing Supabase rows: vendor → {month → {id, expected, received}}
_cache = {}

def log(msg):
    if not JSON_MODE:
        print(msg)

def _init(vendor):
    if vendor not in _results:
        _results[vendor] = {'ok': True, 'rows': [], 'updated': 0, 'error': None, 'skipped': False}

def session_path(v):
    return str(SESSIONS_DIR / f'{v}_session.json')

def session_exists(v):
    return (SESSIONS_DIR / f'{v}_session.json').exists()

def get_existing(vendor):
    res = requests.get(
        f'{SUPABASE_URL}/rest/v1/commissions'
        f'?vendor=eq.{vendor}&user_id=eq.{OWNER_ID}&select=id,month,expected,received',
        headers=SB_READ,
    )
    return {r['month']: r for r in (res.json() if res.ok else [])}

def _write_row(vendor, month, payload):
    """Patch existing row or insert new one. Never blindly POST (avoids duplicates)."""
    row = _cache.get(vendor, {}).get(month)
    if row:
        res = requests.patch(
            f'{SUPABASE_URL}/rest/v1/commissions?id=eq.{row["id"]}',
            headers=SB_WRITE, json=payload,
        )
        action = 'updated'
    else:
        full = {'vendor': vendor, 'month': month, 'user_id': OWNER_ID, **payload}
        res = requests.post(f'{SUPABASE_URL}/rest/v1/commissions', headers=SB_WRITE, json=full)
        action = 'inserted'

    if res.ok:
        log(f"  ✓ {action} {vendor}/{month}: {payload}")
        existing_received = row.get('received') if row else None
        _results[vendor]['rows'].append({
            'month': month,
            'expected': payload.get('expected'),
            'received': payload.get('received', existing_received),
            'action': action,
        })
        _results[vendor]['updated'] += 1
    else:
        log(f"  ✗ {action} failed {vendor}/{month}: {res.status_code} {res.text[:120]}")
        _results[vendor]['ok'] = False
        _results[vendor]['error'] = f'{action} failed: {res.status_code}'

def set_expected(vendor, month, amount):
    """Update expected for an existing row or insert. Never touches received."""
    row = _cache.get(vendor, {}).get(month, {})
    if row.get('expected') == amount:
        log(f"  – {month}: ${amount:.2f} already correct")
        _results[vendor]['rows'].append({
            'month': month, 'expected': amount,
            'received': row.get('received'), 'action': 'verified',
        })
        return
    _write_row(vendor, month, {'expected': amount})

def set_expected_received(vendor, month, amount):
    """Set both expected and received (Smartlead: payout = commission)."""
    row = _cache.get(vendor, {}).get(month, {})
    if row.get('expected') == amount and row.get('received') == amount:
        log(f"  – {month}: ${amount:.2f} already correct")
        _results[vendor]['rows'].append({
            'month': month, 'expected': amount, 'received': amount, 'action': 'verified',
        })
        return
    _write_row(vendor, month, {'expected': amount, 'received': amount})

def parse_payout_rows(text):
    """Extract (YYYY-MM, amount) pairs from page text. Returns list, may have duplicates."""
    rows = []
    combined = ' '.join(text.split('\n'))

    # "August 2026  $15.48" or "August 2026 15.48"
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

    # "Jul 14, 2026" ... "$15.48" (within 100 chars)
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
    """Sum amounts per month, filter to >= min_month."""
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
        _results['zapmail']['skipped'] = True
        _results['zapmail']['error'] = 'No saved session'
        log('  ! No saved session'); return

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

        rows = _by_month(parse_payout_rows(body))
        if not rows:
            # Fallback: "January 2026  $X" pattern (no dollar sign prefix)
            for ms, amt in re.findall(
                r'((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})\s+\$([\d,]+\.?\d*)',
                body,
            ):
                try:
                    dt = datetime.strptime(ms.strip(), '%B %Y')
                    mk = dt.strftime('%Y-%m')
                    if mk >= '2026-01':
                        rows[mk] = rows.get(mk, 0.0) + round(float(amt.replace(',', '')), 2)
                except ValueError:
                    pass

        if not rows:
            log('  ! Could not parse paid payouts')
            _results['zapmail']['error'] = 'Could not parse paid payouts from portal'; return

        for mk, total in rows.items():
            # Scraper sets expected; Gmail script sets received (Wise transfer from Rapidify)
            set_expected('zapmail', mk, total)

        log(f"  → {_results['zapmail']['updated']} rows updated")
    except Exception as e:
        _results['zapmail']['ok'] = False
        _results['zapmail']['error'] = str(e)
        log(f'  ! Error: {e}')

# ── HeyReach (Tolt) ───────────────────────────────────────────────────────────
async def check_heyreach(pw):
    log('\n=== HEYREACH ===')
    _init('heyreach')
    if not session_exists('heyreach'):
        _results['heyreach']['skipped'] = True
        _results['heyreach']['error'] = 'No saved session'
        log('  ! No saved session'); return

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

        rows = _by_month(parse_payout_rows(body))
        if not rows:
            _results['heyreach']['error'] = 'Could not parse payout history — session may be expired'
            log('  ! Could not parse payout rows')
        else:
            for mk, total in rows.items():
                # Expected from Tolt dashboard; received comes from Gmail (PayPal from Tolt, Inc.)
                set_expected('heyreach', mk, total)

        log(f"  → {_results['heyreach']['updated']} rows updated")
    except Exception as e:
        _results['heyreach']['ok'] = False
        _results['heyreach']['error'] = str(e)
        log(f'  ! Error: {e}')

# ── Inboxkit ──────────────────────────────────────────────────────────────────
async def check_inboxkit(pw):
    log('\n=== INBOXKIT ===')
    _init('inboxkit')
    if not session_exists('inboxkit'):
        _results['inboxkit']['skipped'] = True
        _results['inboxkit']['error'] = 'No saved session'
        log('  ! No saved session'); return

    try:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=session_path('inboxkit'))
        page = await ctx.new_page()
        await page.goto('https://studio.inboxkit.com/billing', wait_until='domcontentloaded', timeout=20000)
        try:
            await page.wait_for_load_state('networkidle', timeout=8000)
        except Exception:
            pass
        await page.wait_for_timeout(3000)
        body = await page.locator('body').inner_text()
        await browser.close()

        log('  Billing page (relevant lines):')
        for line in body.split('\n'):
            s = line.strip()
            if s and any(kw in s.lower() for kw in ['payout', 'available', 'earning', '$', 'balance', 'total']):
                log(f'    {s}')

        m = (re.search(r'Available for Payout\s*\$?([\d,]+\.?\d*)', body, re.IGNORECASE)
             or re.search(r'\$([\d,]+\.?\d*)\s*(?:\n[^\n]*)?Available for Payout', body, re.IGNORECASE))
        if m:
            amount = round(float(m.group(1).replace(',', '')), 2)
            log(f'  Found Available for Payout: ${amount:.2f}')
            set_expected('inboxkit', datetime.now().strftime('%Y-%m'), amount)
        else:
            _results['inboxkit']['error'] = 'Could not parse Available for Payout'
            log('  ! Could not parse Available for Payout')

        # Surface existing confirmed rows
        for month, row in _cache.get('inboxkit', {}).items():
            if row.get('received') is not None and not any(r['month'] == month for r in _results['inboxkit']['rows']):
                _results['inboxkit']['rows'].append({'month': month, 'expected': row.get('expected'),
                                                     'received': row.get('received'), 'action': 'verified'})
        log(f"  → {_results['inboxkit']['updated']} rows updated")
    except Exception as e:
        _results['inboxkit']['ok'] = False
        _results['inboxkit']['error'] = str(e)
        log(f'  ! Error: {e}')

# ── Smartlead (Rewardful) ─────────────────────────────────────────────────────
async def check_smartlead(pw):
    log('\n=== SMARTLEAD ===')
    _init('smartlead')
    if not session_exists('smartlead'):
        _results['smartlead']['skipped'] = True
        _results['smartlead']['error'] = (
            'No saved session — open a browser, log into '
            'https://smartproducts.getrewardful.com, then run: '
            'python3 save_session.py smartlead'
        )
        log('  ! No saved session — skipping (run save_session.py smartlead first)')
        return

    try:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=session_path('smartlead'))
        page = await ctx.new_page()

        body_parts = []
        for url in [
            'https://smartproducts.getrewardful.com/payouts',
            'https://smartproducts.getrewardful.com/commissions',
        ]:
            try:
                await page.goto(url, wait_until='networkidle', timeout=15000)
                await page.wait_for_timeout(2000)
                body_parts.append(await page.locator('body').inner_text())
            except Exception:
                pass
        await browser.close()

        body = '\n'.join(body_parts)
        log('  Rewardful (relevant lines):')
        for line in body.split('\n'):
            s = line.strip()
            if s and any(kw in s.lower() for kw in ['commission', 'payout', 'paid', '$', '2026', 'august', 'july', 'balance']):
                log(f'    {s}')

        rows = _by_month(parse_payout_rows(body))
        if not rows:
            all_amts = re.findall(r'\$([\d,]+\.\d{2})', body)
            log(f'  All dollar amounts found: {all_amts[:10]}')
            _results['smartlead']['error'] = 'Could not parse payout history'
            log('  ! Could not parse payout rows')
        else:
            for mk, total in rows.items():
                # Smartlead: payout = commission (expected = received)
                set_expected_received('smartlead', mk, total)

        log(f"  → {_results['smartlead']['updated']} rows updated")
    except Exception as e:
        _results['smartlead']['ok'] = False
        _results['smartlead']['error'] = str(e)
        log(f'  ! Error: {e}')

# ── Icypeas (FirstPromoter) ───────────────────────────────────────────────────
async def check_icypeas(pw):
    log('\n=== ICYPEAS ===')
    _init('icypeas')
    if not session_exists('icypeas'):
        _results['icypeas']['skipped'] = True
        _results['icypeas']['error'] = 'No saved session'
        log('  ! No saved session'); return

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
            if s and any(kw in s.lower() for kw in ['commission', 'payout', 'paid', 'earning', '$', 'balance', 'total', '2026']):
                log(f'    {s}')

        m = (re.search(r'(?:unpaid|available|balance)\s*(?:commissions?)?\s*:?\s*\$?([\d,]+\.?\d*)', body, re.IGNORECASE)
             or re.search(r'\$([\d,]+\.?\d*)\s*\n?(?:unpaid|available|balance)', body, re.IGNORECASE))
        if m:
            amount = round(float(m.group(1).replace(',', '')), 2)
            log(f'  Found balance: ${amount:.2f}')
            set_expected('icypeas', datetime.now().strftime('%Y-%m'), amount)
        else:
            _results['icypeas']['error'] = 'Could not parse balance'
            log('  ! Could not parse balance')

        log(f"  → {_results['icypeas']['updated']} rows updated")
    except Exception as e:
        _results['icypeas']['ok'] = False
        _results['icypeas']['error'] = str(e)
        log(f'  ! Error: {e}')

# ── LeadMagic (Dub.co) ───────────────────────────────────────────────────────
async def check_leadmagic(pw):
    log('\n=== LEADMAGIC ===')
    _init('leadmagic')
    if not session_exists('leadmagic'):
        _results['leadmagic']['skipped'] = True
        _results['leadmagic']['error'] = 'No saved session'
        log('  ! No saved session'); return

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
            if s and any(kw in s.lower() for kw in ['earning', 'payout', 'paid', '$', 'balance', 'total', 'commission', '2026']):
                log(f'    {s}')

        if re.search(r'no earnings|no payouts|no commissions', body, re.IGNORECASE):
            log('  → No earnings on LeadMagic yet (expected)')
            _results['leadmagic']['rows'].append({'month': datetime.now().strftime('%Y-%m'), 'expected': 0, 'received': None, 'action': 'verified'})
        else:
            m = (re.search(r'(?:pending\s+payout|earnings?|balance|available)\s*:?\s*\$?([\d,]+\.?\d*)', body, re.IGNORECASE)
                 or re.search(r'\$([\d,]+\.?\d*)\s*\n?(?:pending|earning|available)', body, re.IGNORECASE))
            if m:
                amount = round(float(m.group(1).replace(',', '')), 2)
                log(f'  Found balance: ${amount:.2f}')
                set_expected('leadmagic', datetime.now().strftime('%Y-%m'), amount)
            else:
                _results['leadmagic']['error'] = 'Could not parse balance'
                log('  ! Could not parse balance')

        log(f"  → {_results['leadmagic']['updated']} rows updated")
    except Exception as e:
        _results['leadmagic']['ok'] = False
        _results['leadmagic']['error'] = str(e)
        log(f'  ! Error: {e}')

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    global _cache
    all_vendors = ['zapmail', 'heyreach', 'inboxkit', 'smartlead', 'icypeas', 'leadmagic']

    # Pre-load existing Supabase rows before scraping
    for v in all_vendors:
        _cache[v] = get_existing(v)
        _init(v)

    async with async_playwright() as pw:
        # Run sequentially to avoid Playwright resource contention
        await check_zapmail(pw)
        await check_heyreach(pw)
        await check_inboxkit(pw)
        await check_smartlead(pw)
        await check_icypeas(pw)
        await check_leadmagic(pw)

    # Reload cache after updates for accurate discrepancy calculation
    for v in all_vendors:
        _cache[v] = get_existing(v)

    if JSON_MODE:
        print(json.dumps({'ok': True, 'results': _results, 'discrepancies': compute_discrepancies()}))
    else:
        print('\n=== Done. Check above for any amounts needing manual review. ===')

if __name__ == '__main__':
    asyncio.run(main())
