"""Shared Replicate prediction-create helper.

POST /v1/predictions is rate limited per account. Both the VTON path and the
garment-extraction path create predictions; sharing one client + a minimum
create gap + explicit 429-body logging keeps bursts under the account's
burst limit and makes throttling visible (silent 429s used to surface as
mysterious multi-second stalls).
"""
import asyncio
import logging
import os
import time

import httpx

logger = logging.getLogger("drishti.replicate")

REPLICATE_API = "https://api.replicate.com/v1"

_client: httpx.AsyncClient | None = None
_last_create = 0.0


def get_client() -> httpx.AsyncClient:
    """One pooled client — a fresh TLS handshake per call costs 300-500ms on
    the SIN → Replicate (US) path."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _client


def _min_gap() -> float:
    # Account (credit < $5) is limited to 6 creates/min with burst of 1
    # → token ≈ every 10s. Env-overridable in case credit is topped up.
    try:
        return float(os.getenv("REPLICATE_MIN_CREATE_GAP", "10"))
    except ValueError:
        return 10.0


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    """Prefer the JSON body's retry_after (authoritative), then the header."""
    wait = None
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("retry_after") is not None:
            wait = float(body["retry_after"])
    except Exception:
        pass
    if wait is None:
        ra = resp.headers.get("Retry-After", "")
        try:
            wait = float(ra) if ra else None
        except ValueError:
            wait = None
    if wait is None:
        wait = 10 * (attempt + 1)
    return min(max(wait, 3.0), 30.0)


async def create_prediction(payload: dict, headers: dict, tag: str = "create",
                            max_attempts: int = 3) -> httpx.Response | None:
    """POST a prediction with shared create-spacing + 429 handling.

    Returns the last non-429 response, or None if all attempts were 429s.
    Logs the 429 body so the exact limit (and Retry-After) is visible.
    """
    global _last_create
    for attempt in range(max_attempts):
        gap = _min_gap() - (time.time() - _last_create)
        if gap > 0:
            await asyncio.sleep(gap)
        resp = await get_client().post(f"{REPLICATE_API}/predictions",
                                       headers=headers, json=payload)
        _last_create = time.time()
        if resp.status_code == 429:
            wait = _retry_after(resp, attempt)
            logger.warning(
                f"[{tag}] Replicate 429 attempt {attempt + 1}/{max_attempts} "
                f"wait={wait}s body={resp.text[:250]}"
            )
            await asyncio.sleep(wait)
            continue
        return resp
    logger.error(f"[{tag}] Replicate create rate limited after {max_attempts} attempts")
    return None
