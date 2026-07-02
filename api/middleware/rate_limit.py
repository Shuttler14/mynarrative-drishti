from __future__ import annotations

import time
import logging
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from api.config import get_settings

logger = logging.getLogger("drishti.middleware")
settings = get_settings()


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_requests: int = 100, window_seconds: int = 60):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._redis = None
        self._fallback: dict[str, list[float]] = {}

    async def _get_redis(self):
        """Lazy-load Redis connection."""
        if self._redis is None:
            try:
                import redis.asyncio as aioredis
                self._redis = aioredis.from_url(
                    settings.REDIS_URL,
                    decode_responses=True,
                    socket_connect_timeout=2,
                )
                await self._redis.ping()
                logger.info("Rate limiter connected to Redis")
            except Exception as e:
                logger.warning(f"Redis unavailable, using in-memory fallback: {e}")
                self._redis = False
        return self._redis if self._redis is not False else None

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        client_ip = request.client.host if request.client else "unknown"
        key = f"rate_limit:{client_ip}"
        now = time.time()

        redis = await self._get_redis()

        if redis:
            try:
                pipe = redis.pipeline()
                pipe.zremrangebyscore(key, 0, now - self.window_seconds)
                pipe.zadd(key, {str(now): now})
                pipe.zcard(key)
                pipe.expire(key, self.window_seconds)
                results = await pipe.execute()
                request_count = results[2]

                if request_count > self.max_requests:
                    return Response(
                        content='{"error": "Rate limit exceeded"}',
                        status_code=429,
                        media_type="application/json",
                    )
            except Exception as e:
                logger.error(f"Redis error, falling back to in-memory: {e}")
                if client_ip not in self._fallback:
                    self._fallback[client_ip] = []
                self._fallback[client_ip] = [t for t in self._fallback[client_ip] if now - t < self.window_seconds]
                if len(self._fallback[client_ip]) >= self.max_requests:
                    return Response(
                        content='{"error": "Rate limit exceeded"}',
                        status_code=429,
                        media_type="application/json",
                    )
                self._fallback[client_ip].append(now)
        else:
            if client_ip not in self._fallback:
                self._fallback[client_ip] = []
            self._fallback[client_ip] = [t for t in self._fallback[client_ip] if now - t < self.window_seconds]
            if len(self._fallback[client_ip]) >= self.max_requests:
                return Response(
                    content='{"error": "Rate limit exceeded"}',
                    status_code=429,
                    media_type="application/json",
                )
            self._fallback[client_ip].append(now)

        return await call_next(request)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        import uuid
        start = time.time()
        request_id = str(uuid.uuid4())
        response = await call_next(request)
        duration = time.time() - start

        logger.info(
            f"[{request_id}] {request.method} {request.url.path} -> {response.status_code} ({duration:.3f}s)"
        )

        response.headers["X-Process-Time"] = f"{duration:.4f}"
        response.headers["X-Request-ID"] = request_id
        return response
