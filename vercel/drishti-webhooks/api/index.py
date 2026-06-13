from __future__ import annotations

import os
import json
from http.server import BaseHTTPRequestHandler
import http.client
import ssl

BACKEND_URL = os.getenv("DRISHTI_BACKEND_URL", "https://api.mynarrative.in")


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self._proxy("POST")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Shopify-Topic, X-Shopify-Hmac-Sha256")
        self.end_headers()

    def _proxy(self, method: str):
        try:
            from urllib.parse import urlparse
            parsed = urlparse(BACKEND_URL)

            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else None

            path = self.path

            ctx = ssl.create_default_context()
            conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, context=ctx)

            headers = {}
            for key in ("Content-Type", "X-Shopify-Topic", "X-Shopify-Hmac-Sha256"):
                val = self.headers.get(key)
                if val:
                    headers[key] = val

            conn.request(method, path, body=body, headers=headers)
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
            self.wfile.write(json.dumps({"error": "Backend unavailable"}).encode())

    def log_message(self, format, *args):
        pass
