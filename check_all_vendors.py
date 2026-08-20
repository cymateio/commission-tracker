#!/usr/bin/env python3
"""
Check all vendor dashboards and update Supabase commission tracker.
- Zapmail: reads paid payouts list only (no upcoming/scheduled payouts)
- HeyReach: reads next payout + paid history from Tolt
- Inboxkit: reads Available for Payout from /billing
- Smartlead: reads Rewardful commission balance
- Icypeas: reads FirstPromoter balance
- LeadMagic: reads Dub.co earnings
"""
import asyncio, re, sys, json
from pathlib import Path
import requests

sys.path.insert(0, str(Path.home() / 'Library/Python/3.9/lib/python3.9/site-packages'))
sys.path.insert(0, str(Path.home() / 'Library/Python/3.9/lib/python/site-packages'))
from playwright.async_api import async_playwright

JSON_MODE = '--json' in sys.argv
_results = {}  # vendor → {ok, rows, updated, error}

SUPABASE_URL = 'https://ygotkwhvydmisrkyofec.supabase.co'
SUPABASE_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inlnb3Rrd2h2eWRtaXNya3lvZmVjIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MzU0NzI0MywiZXhwIjoyMDk5MTIzMjQzfQ.VR75mGmkG5yp2INNKaeSeseg1GMPfkBh7660IJPopGg'
OWNER_ID     = '1e86ded9-ad34-431d-b60f-59ad5d80d754'
SESSIONS_DIR = Path(__file__).parent / 'scraper_sessions'

SB_HEADERS = {
    'apikey': SUPABASE_KEY,
    'Authorization': f'Bearer {SUPABASE_KEY}',
    'Content-Type': 'application/json',
    'Prefer': 'resolution=merge-duplicates,return=representation',
}

def upsert(vendor, month, expected=None, received=None):
    payload = {'vendor': vendor, 'month': month, 'user_id': OWNER_ID}
    if expected  is not None: payload['expected']  = expected
    if received  is not None: payload['received']  = received
    res = requests.post(f'{SUPABASE_URL}/rest/v1/commissions', headers=SB_HEADERS, json=payload)
    if vendor not in _results:
        _results[vendor] = {'ok': True, 'rows': [], 'updated': 0, 'error': None}
    if res.ok:
        if not JSON_MODE: print(f"  ✓ Upserted {vendor}/{month}: expected={expected}, received={received}")
        _results[vendor]['rows'].append({'month': month, 'expected': expected, 'received': received, 'action': 'upserted'})
        _results[vendor]['updated'] += 1
    else:
        if not JSON_MODE: print(f"  ✗ Upsert failed {vendor}/{month}: {res.status_code} {res.text[:100]}")
        _results[vendor]['ok'] = False
        _results[vendor]['error'] = f"Upsert failed: {res.status_code}"

def get_existing(vendor):
    res = requests.get(
        f'{SUPABASE_URL}/rest/v1/commissions?vendor=eq.{vendor}&select=month,expected,received',
        headers={'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}'},
    )
    return {r['month']: r for r in (res.json() if res.ok else [])}

def parse_amount(text):
    m = re.search(r'\$([\d,]+\.?\d*)', text.replace('\n', ' '))
    if m:
        try:
            return round(float(m.group(1).replace(',', '')), 2)
        except ValueError:
            pass
    return None

def session(vendor_id):
    return str(SESSIONS_DIR / f'{vendor_id}_session.json')

# ── Zapmail ────────────────────────────────────────────────────────────────────
async def check_zapmail(pw):
    if not JSON_MODE: print("\n=== ZAPMAIL ===")
    if 'zapmail' not in _results:
        _results['zapmail'] = {'ok': True, 'rows': [], 'updated': 0, 'error': None}
    existing = get_existing('zapmail')

    try:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=session('zapmail'))
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

        rows = re.findall(r'((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})\s+\$([\d,]+\.?\d*)', body)
        if not rows:
            if not JSON_MODE: print("  ! Could not parse paid payouts")
            _results['zapmail']['error'] = 'Could not parse paid payouts from portal'
            return

        from datetime import datetime
        updates = 0
        for month_str, amount_str in rows:
            dt = datetime.strptime(month_str.strip(), '%B %Y')
            mk = dt.strftime('%Y-%m')
            if dt.year < 2026:
                continue
            amount = round(float(amount_str.replace(',', '')), 2)
            row = existing.get(mk, {})
            if row.get('expected') == amount:
                if not JSON_MODE: print(f"  – {mk}: ${amount:.2f} already correct")
                _results['zapmail']['rows'].append({'month': mk, 'expected': amount, 'received': row.get('received'), 'action': 'verified'})
            else:
                upsert('zapmail', mk, expected=amount, received=amount)
                updates += 1

        if not JSON_MODE: print(f"  → {updates} zapmail rows updated")
    except Exception as e:
        _results['zapmail']['ok'] = False
        _results['zapmail']['error'] = str(e)
        if not JSON_MODE: print(f"  ! Error: {e}")

# ── HeyReach (Tolt) ───────────────────────────────────────────────────────────
async def check_heyreach(pw):
    print("\n=== HEYREACH ===")
    existing = get_existing('heyreach')

    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(storage_state=session('heyreach'))
    page = await ctx.new_page()

    await page.goto('https://heyreach.tolt.io/', wait_until='networkidle', timeout=20000)
    await page.wait_for_timeout(3000)
    body = await page.locator('body').inner_text()

    # Try payouts/history page
    await page.goto('https://heyreach.tolt.io/payouts', wait_until='domcontentloaded', timeout=15000)
    await page.wait_for_timeout(3000)
    body2 = await page.locator('body').inner_text()

    await browser.close()

    print("  Dashboard text:")
    for line in body.split('\n'):
        line = line.strip()
        if line and any(kw in line.lower() for kw in ['payout', 'paid', 'earning', '$', 'next', 'total']):
            print(f"    {line}")

    print("  Payouts page text:")
    for line in body2.split('\n'):
        line = line.strip()
        if line and any(kw in line.lower() for kw in ['payout', 'paid', 'earning', '$', 'next', 'total', '2026', '2025']):
            print(f"    {line}")

# ── Inboxkit ──────────────────────────────────────────────────────────────────
async def check_inboxkit(pw):
    if not JSON_MODE: print("\n=== INBOXKIT ===")
    if 'inboxkit' not in _results:
        _results['inboxkit'] = {'ok': True, 'rows': [], 'updated': 0, 'error': None}
    existing = get_existing('inboxkit')

    try:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=session('inboxkit'))
        page = await ctx.new_page()

        await page.goto('https://studio.inboxkit.com/billing', wait_until='domcontentloaded', timeout=20000)
        try:
            await page.wait_for_load_state('networkidle', timeout=8000)
        except Exception:
            pass
        await page.wait_for_timeout(3000)
        body = await page.locator('body').inner_text()
        await browser.close()

        if not JSON_MODE:
            print("  Billing page text:")
            for line in body.split('\n'):
                line = line.strip()
                if line and any(kw in line.lower() for kw in ['payout', 'available', 'earning', '$', 'balance', 'total']):
                    print(f"    {line}")

        m = re.search(r'Available for Payout\s*\$?([\d,]+\.?\d*)', body, re.IGNORECASE)
        if not m:
            m = re.search(r'\$([\d,]+\.?\d*)\s*(?:\n[^\n]*)?Available for Payout', body, re.IGNORECASE)
        if m:
            amount = round(float(m.group(1).replace(',', '')), 2)
            if not JSON_MODE: print(f"  Found Available for Payout: ${amount:.2f}")
            from datetime import datetime
            current_month = datetime.now().strftime('%Y-%m')
            row = existing.get(current_month, {})
            if row.get('expected') == amount:
                if not JSON_MODE: print(f"  – {current_month}: ${amount:.2f} already correct")
                _results['inboxkit']['rows'].append({'month': current_month, 'expected': amount, 'received': row.get('received'), 'action': 'verified'})
            else:
                upsert('inboxkit', current_month, expected=amount)
        else:
            _results['inboxkit']['error'] = 'Could not parse Available for Payout'
            if not JSON_MODE: print("  ! Could not parse Available for Payout")

        # Report existing confirmed rows
        for month, row in existing.items():
            if row.get('received') is not None:
                already = any(r['month'] == month for r in _results['inboxkit']['rows'])
                if not already:
                    _results['inboxkit']['rows'].append({'month': month, 'expected': row.get('expected'), 'received': row.get('received'), 'action': 'verified'})

    except Exception as e:
        _results['inboxkit']['ok'] = False
        _results['inboxkit']['error'] = str(e)
        if not JSON_MODE: print(f"  ! Error: {e}")

# ── Smartlead (Rewardful) ─────────────────────────────────────────────────────
async def check_smartlead(pw):
    print("\n=== SMARTLEAD ===")
    existing = get_existing('smartlead')

    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(storage_state=session('smartlead'))
    page = await ctx.new_page()

    await page.goto('https://smartproducts.getrewardful.com', wait_until='networkidle', timeout=20000)
    await page.wait_for_timeout(3000)
    body = await page.locator('body').inner_text()

    # Try commissions/payouts page
    for url in ['https://smartproducts.getrewardful.com/commissions', 'https://smartproducts.getrewardful.com/payouts']:
        try:
            await page.goto(url, wait_until='networkidle', timeout=15000)
            await page.wait_for_timeout(2000)
            extra = await page.locator('body').inner_text()
            body += '\n' + extra
        except Exception:
            pass

    await browser.close()

    print("  Rewardful text (commission/payout lines):")
    for line in body.split('\n'):
        line = line.strip()
        if line and any(kw in line.lower() for kw in ['commission', 'payout', 'paid', 'earning', '$', 'balance', 'total', '2026', 'august', 'july']):
            print(f"    {line}")

# ── Icypeas (FirstPromoter) ───────────────────────────────────────────────────
async def check_icypeas(pw):
    print("\n=== ICYPEAS ===")
    existing = get_existing('icypeas')

    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(storage_state=session('icypeas'))
    page = await ctx.new_page()

    await page.goto('https://icypeas.firstpromoter.com', wait_until='networkidle', timeout=20000)
    await page.wait_for_timeout(3000)
    body = await page.locator('body').inner_text()
    await browser.close()

    print("  FirstPromoter text:")
    for line in body.split('\n'):
        line = line.strip()
        if line and any(kw in line.lower() for kw in ['commission', 'payout', 'paid', 'earning', '$', 'balance', 'total', '2026']):
            print(f"    {line}")

# ── LeadMagic (Dub.co) ───────────────────────────────────────────────────────
async def check_leadmagic(pw):
    print("\n=== LEADMAGIC ===")
    existing = get_existing('leadmagic')

    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(storage_state=session('leadmagic'))
    page = await ctx.new_page()

    await page.goto('https://partners.dub.co/programs/leadmagic', wait_until='networkidle', timeout=20000)
    await page.wait_for_timeout(4000)
    body = await page.locator('body').inner_text()
    await browser.close()

    print("  Dub.co text:")
    for line in body.split('\n'):
        line = line.strip()
        if line and any(kw in line.lower() for kw in ['earning', 'payout', 'paid', '$', 'balance', 'total', 'commission', '2026']):
            print(f"    {line}")

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    async with async_playwright() as pw:
        await check_zapmail(pw)
        await check_inboxkit(pw)
        if not JSON_MODE:
            await check_heyreach(pw)
            await check_smartlead(pw)
            await check_icypeas(pw)
            await check_leadmagic(pw)

    if JSON_MODE:
        print(json.dumps({'ok': True, 'results': _results}))
    else:
        print("\n=== Done. Check above for any amounts needing manual review. ===")

asyncio.run(main())
