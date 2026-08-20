#!/usr/bin/env python3
"""
Local scraper API — http://localhost:3001
Used by the commission dashboard's 'Run Now' button for Playwright-based vendors
(Zapmail, Inboxkit) that have no public affiliate API.

Start: python3 local_api.py
The LaunchAgent plist starts this automatically at login.
"""
import json, subprocess, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PORT = 3001
DIR = Path(__file__).parent

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
        try:
            proc = subprocess.run(
                [sys.executable, str(DIR / 'check_all_vendors.py'), '--json'],
                capture_output=True, text=True, timeout=180, cwd=str(DIR),
            )
            try:
                result = json.loads(proc.stdout)
            except json.JSONDecodeError:
                result = {
                    'ok': False,
                    'error': (proc.stderr or 'Script produced no JSON output')[:500],
                }
        except subprocess.TimeoutExpired:
            result = {'ok': False, 'error': 'Scraper timed out after 3 minutes'}
        except Exception as exc:
            result = {'ok': False, 'error': str(exc)}
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
