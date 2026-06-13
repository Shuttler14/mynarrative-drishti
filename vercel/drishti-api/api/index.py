from __future__ import annotations

import os
import json
from http.server import BaseHTTPRequestHandler
import http.client
import ssl

BACKEND_URL = os.getenv("DRISHTI_BACKEND_URL", "https://api.mynarrative.in")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def do_PATCH(self):
        self._proxy("PATCH")

    def do_DELETE(self):
        self._proxy("DELETE")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def _proxy(self, method: str):
        try:
            from urllib.parse import urlparse
            parsed = urlparse(BACKEND_URL)

            body = None
            if method in ("POST", "PATCH", "PUT"):
                content_length = int(self.headers.get("Content-Length", 0))
                if content_length > 0:
                    body = self.rfile.read(content_length)

            path = self.path
            if path.startswith("/api/"):
                backend_path = "/" + path[5:]
            else:
                backend_path = path

            ctx = ssl.create_default_context()
            conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, context=ctx)

            headers = {"Content-Type": self.headers.get("Content-Type", "application/json")}
            auth = self.headers.get("Authorization")
            if auth:
                headers["Authorization"] = auth

            conn.request(method, backend_path, body=body, headers=headers)
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
            self.wfile.write(json.dumps({"error": "Backend unavailable", "detail": str(e)}).encode())

    def log_message(self, format, *args):
        pass
