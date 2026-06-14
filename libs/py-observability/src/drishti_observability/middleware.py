from __future__ import annotations
import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from drishti_observability.logging import request_id_var, user_id_var
from drishti_observability.metrics import HTTP_LATENCY, HTTP_REQUESTS

logger = structlog.get_logger("http")
_SKIP = {"/healthz", "/readyz", "/metrics"}


class ObservabilityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, service: str) -> None:
        super().__init__(app)
        self.service = service

    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        rtok = request_id_var.set(rid)
        utok = user_id_var.set(request.headers.get("x-user-id", ""))
        structlog.contextvars.bind_contextvars(request_id=rid)

        path = request.url.path
        skip = path in _SKIP
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["x-request-id"] = rid
            return response
        finally:
            dur = time.perf_counter() - start
            if not skip:
                route = request.scope.get("route")
                label = getattr(route, "path", path) if route else path
                HTTP_REQUESTS.labels(self.service, request.method, label, str(status)).inc()
                HTTP_LATENCY.labels(self.service, request.method, label).observe(dur)
                logger.info("http.access", method=request.method, path=path,
                            status=status, duration_ms=round(dur * 1000, 1))
            request_id_var.reset(rtok)
            user_id_var.reset(utok)
            structlog.contextvars.clear_contextvars()


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Injects standard security headers on every response.

    CSP is intentionally lenient (no script-src) since this is an API,
    not a rendered HTML app. The headers prevent common misconfigurations:
    clickjacking, MIME sniffing, open redirects via referrer, and cached
    sensitive data.
    """

    _HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "X-XSS-Protection": "0",              # modern browsers; 1 mode is exploitable
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    }

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for k, v in self._HEADERS.items():
            response.headers[k] = v
        # HSTS only over HTTPS (skip localhost / dev)
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains; preload"
            )
        return response
