#!/usr/bin/env python3
"""
Local scraper API — http://localhost:3001

Endpoints:
  GET  /ping                    → health check
  GET  /session                 → session status for all vendors
  GET  /session/<vendor>        → session status for one vendor
  POST /run                     → run all vendors
  POST /run/<vendor>            → run specific vendor only
  POST /login/<vendor>          → open Chrome for manual login (no automation)
  POST /login/<vendor>/save     → save session from open browser and close it
"""
import contextlib, io, json, re, runpy, shutil, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime
from urllib.request import urlopen
from urllib.error import URLError

PORT = 3001
DIR  = Path(__file__).parent
SCRIPT = str(DIR / 'check_all_vendors.py')
SESSIONS_DIR = DIR / 'scraper_sessions'
SESSIONS_DIR.mkdir(exist_ok=True)

GMAIL_SYNC_URL = 'https://script.google.com/macros/s/AKfycbwF3tbu3gUTd8VMUbXo4G6ctDIuXA0fROUco4XALeaoIZwc1Eg-WceXTye6qCOfQPA/exec'

sys.path.insert(0, str(Path.home() / 'Library/Python/3.9/lib/python3.9/site-packages'))
sys.path.insert(0, str(Path.home() / 'Library/Python/3.9/lib/python/site-packages'))

VENDOR_LOGIN_URLS = {
    'zapmail':   'https://affiliates.zapmail.ai/',
    'heyreach':  'https://heyreach.tolt.io/',
    'inboxkit':  'https://studio.inboxkit.com/billing',
    'smartlead': 'https://smartproducts.getrewardful.com/login',
    'icypeas':   'https://icypeas.firstpromoter.com/login',
    'leadmagic': 'https://partners.dub.co/programs/leadmagic',
}
VALID_VENDORS = set(VENDOR_LOGIN_URLS.keys())

LOGIN_PORT  = 9222
_login_procs = {}   # vendor → {proc, tmpdir}
_login_lock  = threading.Lock()


def _chrome():
    for p in [
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        '/Applications/Chromium.app/Contents/MacOS/Chromium',
    ]:
        if Path(p).exists():
            return p
    return None


def _session_info(vendor):
    f = SESSIONS_DIR / f'{vendor}_session.json'
    with _login_lock:
        login_open = vendor in _login_procs
    if not f.exists():
        return {'exists': False, 'stale': True, 'login_open': login_open}
    age = (datetime.now().timestamp() - f.stat().st_mtime) / 86400
    return {
        'exists': True,
        'age_days': round(age, 1),
        'stale': age > 30,
        'saved_at': datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
        'login_open': login_open,
    }


def _open_login_browser(vendor):
    chrome = _chrome()
    if not chrome:
        return {'ok': False, 'error': 'Google Chrome not found. Please install it.'}

    url = VENDOR_LOGIN_URLS.get(vendor, f'https://{vendor}.com')
    tmpdir = tempfile.mkdtemp(prefix=f'session_login_{vendor}_')

    with _login_lock:
        # Close any existing login browser for this vendor first
        if vendor in _login_procs:
            try:
                _login_procs[vendor]['proc'].terminate()
                shutil.rmtree(_login_procs[vendor].get('tmpdir', ''), ignore_errors=True)
            except Exception:
                pass

        proc = subprocess.Popen([
            chrome,
            f'--remote-debugging-port={LOGIN_PORT}',
            f'--user-data-dir={tmpdir}',
            '--no-first-run',
            '--no-default-browser-check',
            '--disable-background-mode',
            url,
        ])
        _login_procs[vendor] = {'proc': proc, 'tmpdir': tmpdir, 'url': url}

    return {'ok': True, 'vendor': vendor, 'url': url, 'status': 'browser_opened'}


def _save_login_session(vendor):
    with _login_lock:
        if vendor not in _login_procs:
            return {'ok': False, 'error': 'No login browser open. Click Login first.'}
        info = dict(_login_procs[vendor])

    import asyncio
    from playwright.async_api import async_playwright

    session_file = str(SESSIONS_DIR / f'{vendor}_session.json')

    async def do_save():
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.connect_over_cdp(f'http://localhost:{LOGIN_PORT}')
                ctxs = browser.contexts
                if not ctxs:
                    return {'ok': False, 'error': 'No browser context found. Make sure you are logged in.'}
                await ctxs[0].storage_state(path=session_file)
                return {'ok': True, 'saved': True, 'vendor': vendor}
            except Exception as e:
                return {'ok': False, 'error': f'Cannot connect to login browser: {e}'}

    try:
        result = asyncio.run(do_save())
    except Exception as e:
        result = {'ok': False, 'error': str(e)}

    # Always close the browser after save attempt
    with _login_lock:
        try:
            info['proc'].terminate()
        except Exception:
            pass
        shutil.rmtree(info.get('tmpdir', ''), ignore_errors=True)
        _login_procs.pop(vendor, None)

    return result


def _sync_gmail():
    """Trigger the Gmail Apps Script to sync receipts from admin@cymate.io. Returns {'ok', 'added'}."""
    try:
        with urlopen(GMAIL_SYNC_URL, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except URLError as e:
        return {'ok': False, 'error': f'Gmail sync failed: {e}'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def _run_scraper(vendor=None):
    """Sync Gmail receipts then run check_all_vendors.py in-process."""
    # Pull fresh receipts from admin@cymate.io before comparing
    gmail = _sync_gmail()

    old_argv = sys.argv[:]
    args = [SCRIPT, '--json']
    if vendor:
        args += ['--vendor', vendor]
    sys.argv = args
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            runpy.run_path(SCRIPT, run_name='__main__')
        output = buf.getvalue().strip()
        result = json.loads(output)
        result['gmail_sync'] = gmail
        return result
    except json.JSONDecodeError:
        return {'ok': False, 'error': f'Scraper produced invalid JSON: {buf.getvalue()[:300]}', 'gmail_sync': gmail}
    except Exception as e:
        return {'ok': False, 'error': str(e), 'gmail_sync': gmail}
    finally:
        sys.argv = old_argv


class Handler(BaseHTTPRequestHandler):
    def _send(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        path = self.path.rstrip('/')

        if path == '/ping':
            self._send({'ok': True, 'service': 'commission-tracker-scraper', 'version': '2.0'})
            return

        if path == '/sync-gmail':
            self._send(_sync_gmail())
            return

        if path == '/session':
            self._send({v: _session_info(v) for v in VALID_VENDORS})
            return

        m = re.match(r'^/session/(\w+)$', path)
        if m:
            vendor = m.group(1)
            if vendor not in VALID_VENDORS:
                self._send({'error': f'Unknown vendor: {vendor}'}, 404); return
            self._send(_session_info(vendor))
            return

        self._send({'error': 'Not found'}, 404)

    def do_POST(self):
        path = self.path.rstrip('/')

        if path == '/run':
            self._send(_run_scraper()); return

        m = re.match(r'^/run/(\w+)$', path)
        if m:
            vendor = m.group(1)
            if vendor not in VALID_VENDORS:
                self._send({'error': f'Unknown vendor: {vendor}'}, 404); return
            self._send(_run_scraper(vendor=vendor)); return

        m = re.match(r'^/login/(\w+)/save$', path)
        if m:
            vendor = m.group(1)
            if vendor not in VALID_VENDORS:
                self._send({'error': f'Unknown vendor: {vendor}'}, 404); return
            self._send(_save_login_session(vendor)); return

        m = re.match(r'^/login/(\w+)$', path)
        if m:
            vendor = m.group(1)
            if vendor not in VALID_VENDORS:
                self._send({'error': f'Unknown vendor: {vendor}'}, 404); return
            self._send(_open_login_browser(vendor)); return

        self._send({'error': 'Not found'}, 404)

    def log_message(self, *a):
        pass  # suppress access logs


if __name__ == '__main__':
    server = ThreadingHTTPServer(('localhost', PORT), Handler)
    print(f'Commission Tracker local API v2.0 on http://localhost:{PORT}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
