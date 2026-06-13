from __future__ import annotations

import os
import json
from http.server import BaseHTTPRequestHandler
import http.client
import ssl

BACKEND_URL = os.getenv("DRISHTI_BACKEND_URL", "https://api.mynarrative.in")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_dashboard()
        else:
            self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def _serve_dashboard(self):
        html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Drishti Admin</title>
<style>body{font-family:system-ui;margin:2rem;background:#0a0a0a;color:#fff}
.card{background:#1a1a2e;padding:1.5rem;border-radius:12px;margin:1rem 0}
h1{color:#a855f7}h2{color:#818cf8}
.metric{display:inline-block;margin:0.5rem;padding:1rem;background:#16213e;border-radius:8px;min-width:150px}
.metric .value{font-size:2rem;font-weight:bold;color:#a855f7}
.metric .label{font-size:0.875rem;color:#94a3b8}
button{background:#a855f7;color:#fff;border:none;padding:0.75rem 1.5rem;border-radius:8px;cursor:pointer;font-size:1rem}
button:hover{background:#9333ea}
#results{margin-top:1rem;white-space:pre-wrap;font-family:monospace;background:#1a1a2e;padding:1rem;border-radius:8px}
</style></head><body>
<h1>Drishti Admin Dashboard</h1>
<div class="card"><h2>System Status</h2>
<div class="metric"><div class="value" id="users">-</div><div class="label">Users</div></div>
<div class="metric"><div class="value" id="products">-</div><div class="label">Products</div></div>
<div class="metric"><div class="value" id="sessions">-</div><div class="label">Sessions</div></div>
<div class="metric"><div class="value" id="looks">-</div><div class="label">Looks</div></div>
<div class="metric"><div class="value" id="vton">-</div><div class="label">VTON Jobs</div></div>
<div class="metric"><div class="value" id="offers">-</div><div class="label">Offers</div></div>
</div>
<div class="card"><h2>Actions</h2>
<button onclick="loadDashboard()">Refresh Dashboard</button>
<button onclick="runScrape()">Trigger Scrape</button>
<button onclick="checkHealth()">Health Check</button>
</div>
<div id="results"></div>
<script>
const BACKEND = window.location.origin.replace('admin.','api.');
async function api(path) {
  const r = await fetch(BACKEND + path, {headers:{'Authorization':'Bearer '+(localStorage.getItem('token')||'')}});
  return r.json();
}
async function loadDashboard() {
  try {
    const d = await api('/api/admin/dashboard');
    document.getElementById('users').textContent = d.total_users||0;
    document.getElementById('products').textContent = d.total_products||0;
    document.getElementById('sessions').textContent = d.total_sessions||0;
    document.getElementById('looks').textContent = d.total_looks||0;
    document.getElementById('vton').textContent = d.total_vton_jobs||0;
    document.getElementById('offers').textContent = d.total_offers||0;
    document.getElementById('results').textContent = JSON.stringify(d,null,2);
  } catch(e) { document.getElementById('results').textContent = 'Error: '+e.message; }
}
async function checkHealth() {
  const r = await fetch(BACKEND.replace('https://','https://')+'/health');
  const d = await r.json();
  document.getElementById('results').textContent = JSON.stringify(d,null,2);
}
async function runScrape() {
  document.getElementById('results').textContent = 'Scrape trigger sent...';
}
loadDashboard();
</script></body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def _proxy(self, method: str):
        try:
            from urllib.parse import urlparse
            parsed = urlparse(BACKEND_URL)

            body = None
            if method in ("POST", "PATCH", "PUT"):
                content_length = int(self.headers.get("Content-Length", 0))
                if content_length > 0:
                    body = self.rfile.read(content_length)

            ctx = ssl.create_default_context()
            conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, context=ctx)

            headers = {"Content-Type": self.headers.get("Content-Type", "application/json")}
            auth = self.headers.get("Authorization")
            if auth:
                headers["Authorization"] = auth

            conn.request(method, self.path, body=body, headers=headers)
            resp = conn.getresponse()

            self.send_response(resp.status)
            self.send_header("Access-Control-Allow-Origin", "*")
            for key, value in resp.getheaders():
                if key.lower() not in ("transfer-encoding", "connection"):
                    self.send_header(key, value)
            self.end_headers()

            self.wfile.write(resp.read())
            conn.close()

        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        pass
