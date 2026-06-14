from __future__ import annotations
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.responses import Response

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests",
                        ["service", "method", "path", "status"])
HTTP_LATENCY = Histogram("http_request_duration_seconds", "HTTP latency",
                         ["service", "method", "path"],
                         buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5))


def metrics_endpoint() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
