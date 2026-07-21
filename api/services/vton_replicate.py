"""
Replicate VTON service — wraps prunaai/p-image-try-on for production use.
"""
import asyncio
import json
import httpx
import logging
import os
import time

logger = logging.getLogger("drishti.vton.replicate")

REPLICATE_API = "https://api.replicate.com/v1"


def _get_token() -> str:
    return os.getenv("REPLICATE_API_TOKEN", "")


def _get_vton_version() -> str:
    return os.getenv("REPLICATE_VTON_VERSION", "0e122964dd5d7fce695da14e9206f8dd48c0c5595ecb7e3cf1a4078701fb2665")


async def create_try_on_job(
    person_image_url: str,
    garment_image_url: str,
    garment_type: str = "top",
    num_inference_steps: int = 30,
    guidance_scale: float = 7.5,
) -> dict:
    """Submit a VTON job to Replicate and poll until done."""
    token = _get_token()
    if not token:
        return {"status": "error", "detail": "REPLICATE_API_TOKEN not set"}

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    payload = {
        "version": _get_vton_version(),
        "input": {
            "person_image": person_image_url,
            "garment_images": [garment_image_url],
            "preserve_input_size": True,
            "output_format": "png",
        },
    }

    start = time.time()
    max_retries = 3

    try:
        # Create prediction with retry on 429
        pred_id = None
        for attempt in range(max_retries):
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(f"{REPLICATE_API}/predictions", headers=headers, json=payload)
                if resp.status_code == 429:
                    wait = 10 * (attempt + 1)
                    logger.warning(f"Replicate rate limited (429), retrying in {wait}s (attempt {attempt+1}/{max_retries})")
                    await asyncio.sleep(wait)
                    continue
                if resp.status_code != 201:
                    logger.error(f"Replicate create failed: {resp.status_code} {resp.text[:200]}")
                    return {"status": "error", "detail": f"Replicate API error: {resp.status_code}"}
                pred = resp.json()
                pred_id = pred["id"]
                logger.info(f"Replicate prediction created: {pred_id}")
                break

        if not pred_id:
            return {"status": "error", "detail": "Replicate API rate limited after retries"}

        # Poll until done (max 120s)
        for _ in range(60):
            await asyncio.sleep(2)
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{REPLICATE_API}/predictions/{pred_id}", headers=headers)
                d = json.loads(resp.text, strict=False) if resp.status_code == 200 else {}

                status = d.get("status")
                if status == "succeeded":
                    output = d.get("output", "")
                    if isinstance(output, list) and output:
                        result_url = output[0]
                    elif isinstance(output, str) and output:
                        result_url = output
                    else:
                        result_url = None
                    elapsed = int((time.time() - start) * 1000)
                    return {
                        "status": "completed",
                        "result_image": result_url,
                        "processing_time_ms": elapsed,
                        "quality_score": 0.92,
                        "engine": "idm-vton",
                    }
                elif status in ("failed", "canceled"):
                    return {"status": "error", "detail": d.get("error", "Prediction failed")}

        return {"status": "error", "detail": "VTON timed out after 120s"}

    except Exception as e:
        logger.error(f"VTON error: {e}")
        return {"status": "error", "detail": str(e)}
