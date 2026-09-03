#!/usr/bin/env python3
"""
Local scraper API — http://localhost:3001
Used by the commission dashboard's 'Run Now' button for Playwright-based vendors
that have no public affiliate API.

Start: python3 local_api.py
The LaunchAgent plist starts this automatically at login.
"""
import io, json, runpy, sys
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PORT = 3001
DIR  = Path(__file__).parent
SCRIPT = str(DIR / 'check_all_vendors.py')

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
        if self.path == '/ping':
            self._send({'ok': True, 'service': 'commission-tracker-scraper'})
        else:
            self._send({'error': 'Not found'}, 404)

    def do_POST(self):
        if self.path != '/run':
            self._send({'error': 'Not found'}, 404)
            return

        # Run check_all_vendors.py in-process to avoid macOS Full Disk Access
        # restrictions that block the CLI python3 subprocess from ~/Documents.
        old_argv = sys.argv[:]
        sys.argv = [SCRIPT, '--json']
        buf = io.StringIO()
        result = None
        try:
            with redirect_stdout(buf):
                runpy.run_path(SCRIPT, run_name='__main__')
            output = buf.getvalue().strip()
            result = json.loads(output)
        except json.JSONDecodeError:
            result = {'ok': False, 'error': f'Script produced invalid JSON: {buf.getvalue()[:300]}'}
        except Exception as exc:
            result = {'ok': False, 'error': str(exc)}
        finally:
            sys.argv = old_argv

        self._send(result)

    def log_message(self, *a):
        pass  # suppress access logs

if __name__ == '__main__':
    server = HTTPServer(('localhost', PORT), Handler)
    print(f'Commission Tracker local API on http://localhost:{PORT}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
