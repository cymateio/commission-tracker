#!/usr/bin/env python3
"""
Deploy commission-tracker to Vercel.
Usage: python3 deploy.py <VERCEL_TOKEN>
Get token at: https://vercel.com/account/tokens
"""
import json, sys, base64
from pathlib import Path
sys.path.insert(0, str(Path.home() / 'Library/Python/3.9/lib/python3.9/site-packages'))
import requests

if len(sys.argv) < 2:
    print("Usage: python3 deploy.py <VERCEL_TOKEN>")
    print("Get token at: https://vercel.com/account/tokens")
    sys.exit(1)

TOKEN     = sys.argv[1]
HTML_FILE = Path(__file__).parent / 'index.html'
HEADERS   = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'}

print("Deploying to Vercel...")

# Step 1: upload the file to get its SHA
content = HTML_FILE.read_bytes()
upload_res = requests.post(
    'https://api.vercel.com/v2/files',
    headers={**HEADERS, 'x-vercel-digest': __import__('hashlib').sha1(content).hexdigest()},
    data=content,
)
if upload_res.status_code not in (200, 409):
    print(f"Upload failed: {upload_res.status_code} {upload_res.text}")
    sys.exit(1)

sha = __import__('hashlib').sha1(content).hexdigest()

# Step 2: create deployment
deploy_res = requests.post(
    'https://api.vercel.com/v13/deployments',
    headers=HEADERS,
    json={
        'name': 'commission-tracker',
        'files': [{'file': 'index.html', 'sha': sha, 'size': len(content)}],
        'projectSettings': {'framework': None, 'outputDirectory': None},
        'target': 'production',
    },
)

if deploy_res.status_code not in (200, 201):
    print(f"Deploy failed: {deploy_res.status_code}")
    print(deploy_res.text[:500])
    sys.exit(1)

data = deploy_res.json()
url  = data.get('url') or data.get('alias', [''])[0]
print(f"\n✓ Deployed!")
print(f"  URL: https://{url}")
print(f"  Dashboard: https://vercel.com/dashboard")
