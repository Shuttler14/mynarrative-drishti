from __future__ import annotations
import base64
import hashlib
import hmac

from fastapi import HTTPException, Request

from shopify_sync.config import settings


async def verify_webhook_hmac(request: Request) -> bytes:
    body = await request.body()
    received = request.headers.get("X-Shopify-Hmac-SHA256", "")
    if not settings.webhook_secret:
        return body
    computed = base64.b64encode(
        hmac.new(settings.webhook_secret.encode(), body, hashlib.sha256).digest()
    ).decode()
    if not hmac.compare_digest(computed, received):
        raise HTTPException(401, detail="Invalid webhook signature")
    return body
