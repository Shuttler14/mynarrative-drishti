"""
Replicate VTON service — wraps prunaai/p-image-try-on for production use.
"""
import asyncio
import base64
import json
import httpx
import logging
import os
import time

from api.services.replicate_limits import create_prediction

logger = logging.getLogger("drishti.vton.replicate")

REPLICATE_API = "https://api.replicate.com/v1"

# Pooled client: a fresh TLS handshake per create/poll costs 300-500ms on the
# SIN → Replicate (US) path, and we make ~10-30 calls per prediction.
_replicate_client: httpx.AsyncClient | None = None


def _rc() -> httpx.AsyncClient:
    global _replicate_client
    if _replicate_client is None or _replicate_client.is_closed:
        _replicate_client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _replicate_client


def _get_token() -> str:
    return os.getenv("REPLICATE_API_TOKEN", "")


def _get_vton_version() -> str:
    """Pinned model version — quality-verified. The 'latest' version of this
    model produces worse artifacts (e.g. extra legs), so we keep this pinned."""
    return os.getenv("REPLICATE_VTON_VERSION", "0e122964dd5d7fce695da14e9206f8dd48c0c5595ecb7e3cf1a4078701fb2665")


def _max_dim() -> int:
    """Cap on input image longest side before VTON.

    preserve_input_size=True makes the model infer at the input resolution, so
    full-camera photos (4032px+) multiply inference time. 1024 is far above the
    widget display size (~640px). VTON_MAX_DIM=0 disables the cap.
    """
    try:
        return int(os.getenv("VTON_MAX_DIM", "1024"))
    except ValueError:
        return 1024


def _downscale_bytes(img_bytes: bytes, ct: str) -> tuple[bytes, str]:
    """Returns (bytes, content_type) — re-encodes non-PNG as JPEG, so the
    caller must use the returned type for the data URI header."""
    cap = _max_dim()
    if cap <= 0:
        return img_bytes, ct
    try:
        from io import BytesIO
        from PIL import Image

        im = Image.open(BytesIO(img_bytes))
        w, h = im.size
        if max(w, h) <= cap:
            return img_bytes, ct
        scale = cap / float(max(w, h))
        im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        out = BytesIO()
        if ct == "image/png":
            im.save(out, format="PNG", optimize=True)
            new_ct = "image/png"
        else:
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(out, format="JPEG", quality=90)
            new_ct = "image/jpeg"
        resized = out.getvalue()
        logger.info(f"Downscaled {w}x{h} → {im.size[0]}x{im.size[1]} ({len(img_bytes)}→{len(resized)} bytes)")
        return resized, new_ct
    except Exception as e:
        logger.warning(f"Downscale skipped: {e}")
        return img_bytes, ct


async def _download_as_data_uri(url: str) -> str:
    """Download an image URL and convert to base64 data URI.

    Replicate cannot fetch many URLs (Google Shopping thumbnails return 404,
    marketplace CDNs block non-browser requests, R2 presigned URLs may expire).
    Downloading locally and sending as data URI avoids all these issues.
    Inputs are downscaled to VTON_MAX_DIM so inference resolution stays bounded.
    """
    if url is None:
        raise ValueError("Image URL is missing")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Image URL is empty")
    if url.startswith("data:"):
        # Already a data URI (e.g. pre-extracted garment) — still bound its size
        try:
            header, b64 = url.split(",", 1)
            img_bytes = base64.b64decode(b64)
            ct = header[5:].split(";")[0] or "image/png"
            new_bytes, new_ct = await asyncio.to_thread(_downscale_bytes, img_bytes, ct)
            if new_bytes is not img_bytes:
                return f"data:{new_ct};base64,{base64.b64encode(new_bytes).decode()}"
        except Exception:
            pass
        return url

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*",
                "Referer": "https://www.google.com/",
            })
            resp.raise_for_status()
            img_bytes = resp.content

            if img_bytes[:8] == b'\x89PNG\r\n\x1a\n':
                ct = "image/png"
            elif img_bytes[:3] == b'\xff\xd8\xff':
                ct = "image/jpeg"
            elif img_bytes[:4] == b'RIFF' and img_bytes[8:12] == b'WEBP':
                ct = "image/webp"
            else:
                ct = "image/jpeg"

            img_bytes, ct = await asyncio.to_thread(_downscale_bytes, img_bytes, ct)
            b64 = base64.b64encode(img_bytes).decode()
            logger.info(f"Downloaded image ({len(img_bytes)} bytes) → data URI")
            return f"data:{ct};base64,{b64}"
    except Exception as e:
        logger.error(f"Failed to download image for VTON ({str(url)[:80]}): {e}")
        raise ValueError(f"Cannot fetch image: {str(url)[:80]}... ({e})")


async def create_try_on_job(
    person_image_url: str,
    garment_image_url: str | None = None,
    garment_type: str = "top",
    num_inference_steps: int = 30,
    guidance_scale: float = 7.5,
    seed: int | None = None,
    garment_image_urls: list[str] | None = None,
    prompt: str | None = None,
) -> dict:
    """Submit a VTON job to Replicate and poll until done.

    Supports the full-look try-on: pass ``garment_image_urls`` (up to 4) to
    dress the person in top + bottom + shoes in one coherent generation.
    The pinned prunaai/p-image-try-on version renders all provided garments,
    including footwear, when they are provided together.

    Single ``garment_image_url`` stays supported for backward compatibility.
    Images are downloaded and converted to base64 data URIs to avoid DNS/access
    issues with Google Shopping thumbnails, marketplace CDNs, and expiring R2
    presigned URLs.
    """
    token = _get_token()
    if not token:
        return {"status": "error", "detail": "REPLICATE_API_TOKEN not set"}

    garment_urls = [u for u in (garment_image_urls or []) if u] or \
                   ([garment_image_url] if garment_image_url else [])
    if not garment_urls:
        return {"status": "error", "detail": "No garment image provided"}

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Download all images as data URIs to avoid access issues
    if not person_image_url:
        return {"status": "error", "detail": "person_image_url is required"}
    t0 = time.time()
    try:
        downloaded = await asyncio.gather(
            _download_as_data_uri(person_image_url),
            *[_download_as_data_uri(u) for u in garment_urls],
        )
    except Exception as e:
        logger.error(f"Image download failed: {e}")
        return {"status": "error", "detail": str(e)}

    person_data_uri, garment_data_uris = downloaded[0], list(downloaded[1:])
    download_ms = int((time.time() - t0) * 1000)

    vton_input = {
        "person_image": person_data_uri,
        "garment_images": garment_data_uris,
        "preserve_input_size": True,
        "output_format": "png",
    }
    if seed is not None:
        vton_input["seed"] = int(seed)
    if prompt:
        vton_input["prompt"] = prompt

    payload = {
        "version": _get_vton_version(),
        "input": vton_input,
    }
    payload_kb = (len(json.dumps(payload)) + 1023) // 1024

    start = time.time()
    created_at = None

    try:
        # Shared create helper: spacing + 429 body logging + Retry-After backoff
        t_create = time.time()
        resp = await create_prediction(payload, headers, tag="vton")
        pred_id = None
        if resp is None:
            return {"status": "error", "detail": "Replicate API rate limited after retries"}
        if resp.status_code != 201:
            logger.error(f"Replicate create failed: {resp.status_code} {resp.text[:500]}")
            return {"status": "error", "detail": f"Replicate API error: {resp.status_code}: {resp.text[:200]}"}
        pred = resp.json()
        pred_id = pred["id"]
        created_at = pred.get("created_at")
        create_ms = int((time.time() - t_create) * 1000)
        logger.info(f"Replicate prediction created: {pred_id}")

        # Poll until done (max 120s). 0.5s cadence: at 2s completion was
        # observed up to 2s late; at 1s we still lose ~0.5s on average.
        # 429s on GET used to be swallowed ({} → keeps polling) and showed up
        # as ~12s "poll lag" — back off explicitly and log them.
        started_at = None
        predict_ms = None
        poll_429 = 0
        poll_err = 0
        last_status = None
        transitions: list[str] = []
        for _ in range(240):
            await asyncio.sleep(0.5)
            resp = await _rc().get(f"{REPLICATE_API}/predictions/{pred_id}", headers=headers)
            if resp.status_code == 429:
                poll_429 += 1
                ra = resp.headers.get("Retry-After", "")
                try:
                    await asyncio.sleep(min(float(ra), 5.0) if ra else 2.0)
                except ValueError:
                    await asyncio.sleep(2.0)
                continue
            if resp.status_code != 200:
                poll_err += 1
                continue
            d = json.loads(resp.text, strict=False)

            status = d.get("status")
            if status != last_status:
                transitions.append(f"{status}@{int((time.time() - start) * 1000)}ms")
                last_status = status
            if started_at is None:
                started_at = d.get("started_at")
            if status == "succeeded":
                output = d.get("output", "")
                if isinstance(output, list) and output:
                    result_url = output[0]
                elif isinstance(output, str) and output:
                    result_url = output
                else:
                    result_url = None
                elapsed = int((time.time() - start) * 1000)
                # Stage breakdown: Replicate reports queue + inference
                # timestamps, so we can separate model time from our overhead.
                stages = _build_stages(
                    d, created_at, started_at, download_ms, create_ms, elapsed,
                    len(garment_data_uris), payload_kb,
                )
                stages["poll_429"] = poll_429
                stages["poll_err"] = poll_err
                stages["transitions"] = transitions
                logger.info("vton_stages %s", json.dumps(stages))
                return {
                    "status": "completed",
                    "result_image": result_url,
                    "processing_time_ms": elapsed,
                    "quality_score": 0.92,
                    "engine": "idm-vton",
                    "stages": stages,
                }
            elif status in ("failed", "canceled"):
                error_detail = d.get("error", "Prediction failed")
                logger.error(f"Replicate prediction {status}: {error_detail}")
                logger.error(f"Full response: {json.dumps(d, indent=2)[:500]}")
                return {"status": "error", "detail": f"Prediction {status}: {error_detail}"}

        return {"status": "error", "detail": "VTON timed out after 120s"}

    except Exception as e:
        logger.error(f"VTON error: {e}")
        return {"status": "error", "detail": str(e)}


def _iso_ms(a: str | None, b: str | None) -> int | None:
    """Delta between two ISO timestamps in ms."""
    if not a or not b:
        return None
    try:
        from datetime import datetime
        ta = datetime.fromisoformat(a.replace("Z", "+00:00"))
        tb = datetime.fromisoformat(b.replace("Z", "+00:00"))
        return int((tb - ta).total_seconds() * 1000)
    except Exception:
        return None


def _build_stages(d: dict, created_at, started_at, download_ms: int, create_ms: int,
                  total_ms: int, garments: int, payload_kb: int) -> dict:
    """Split total latency into download / create / queue / load / inference / poll lag.

    On Replicate serverless a cold container shows run_ms >> predict_ms:
    the extra is container + model load inside started→completed.
    """
    metrics = d.get("metrics") or {}
    completed_at = d.get("completed_at")
    started_at = started_at or d.get("started_at")
    predict_ms = None
    if isinstance(metrics.get("predict_time"), (int, float)):
        predict_ms = int(metrics["predict_time"] * 1000)
    else:
        predict_ms = _iso_ms(started_at, completed_at)

    queue_ms = _iso_ms(created_at, started_at)
    run_ms = _iso_ms(started_at, completed_at)
    load_ms = (run_ms - predict_ms) if (run_ms is not None and predict_ms is not None) else None
    # Everything we own after the create call returns: queue already counted,
    # so this is container run + our 1s poll lag + clock skew.
    wait_ms = total_ms - create_ms
    poll_lag_ms = wait_ms - (queue_ms or 0) - (run_ms or 0)
    return {
        "download_ms": download_ms,
        "create_ms": create_ms,
        "queue_ms": queue_ms,
        "load_ms": load_ms,
        "predict_ms": predict_ms,
        "poll_lag_ms": poll_lag_ms,
        "total_ms": total_ms,
        "garments": garments,
        "payload_kb": payload_kb,
    }
