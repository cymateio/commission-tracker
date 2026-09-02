#!/usr/bin/env python3
"""
Save a browser session for a vendor so check_all_vendors.py can log in headlessly.

Usage:
  python3 save_session.py smartlead
  python3 save_session.py heyreach
  python3 save_session.py icypeas
  ... (any vendor name)

A visible browser window opens. Log in manually, then press Enter here.
The session cookies are saved to scraper_sessions/<vendor>_session.json.
"""
import asyncio, sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / 'Library/Python/3.9/lib/python3.9/site-packages'))
sys.path.insert(0, str(Path.home() / 'Library/Python/3.9/lib/python/site-packages'))
from playwright.async_api import async_playwright

SESSIONS_DIR = Path(__file__).parent / 'scraper_sessions'

START_URLS = {
    'smartlead':  'https://smartproducts.getrewardful.com/login',
    'heyreach':   'https://heyreach.tolt.io/',
    'zapmail':    'https://affiliates.zapmail.ai/',
    'inboxkit':   'https://studio.inboxkit.com/billing',
    'icypeas':    'https://icypeas.firstpromoter.com',
    'leadmagic':  'https://partners.dub.co/programs/leadmagic',
}

async def save_session(vendor):
    SESSIONS_DIR.mkdir(exist_ok=True)
    url = START_URLS.get(vendor, f'https://{vendor}.com')
    out = SESSIONS_DIR / f'{vendor}_session.json'

    print(f'\nOpening browser for: {vendor}')
    print(f'Start URL: {url}')
    print('Log in manually, then press Enter here to save the session.\n')

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(url)
        input('>>> Press Enter after you have logged in...')
        await ctx.storage_state(path=str(out))
        await browser.close()

    print(f'\nSession saved to: {out}')
    print('You can now run check_all_vendors.py normally.\n')

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python3 save_session.py <vendor>')
        print(f'Vendors: {", ".join(START_URLS)}')
        sys.exit(1)
    asyncio.run(save_session(sys.argv[1]))
