"""
Garment extraction pipeline — extracts clean flat-lay garment images from marketplace product photos.

Pipeline:
  1. Download image from URL
  2. Background removal via Replicate's rembg model (runs on their servers, fast)
  3. Garment isolation — connected component cleanup + crop
  4. Optional: upload to R2 for persistent storage
  5. Return clean garment PNG

Models used:
  - cjwbw/rembg on Replicate (free tier, fast GPU inference)
  - PIL/Pillow for post-processing
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import time
from typing import Optional

import httpx
from PIL import Image

logger = logging.getLogger("drishti.garment")

REPLICATE_API = "https://api.replicate.com/v1"
REPLICATE_REMBG_VERSION = "fb8af171cfa1616ddcf1242c093f9c46bcada5ad4cf6f2fbe8b81b330ec5c003"


def _get_token() -> str:
    return os.getenv("REPLICATE_API_TOKEN", "")


async def _replicate_remove_bg(image_url: str) -> Optional[str]:
    """Use Replicate's rembg model to remove background. Returns output URL."""
    token = _get_token()
    if not token:
        logger.error("REPLICATE_API_TOKEN not set")
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    payload = {
        "version": REPLICATE_REMBG_VERSION,
        "input": {
            "image": image_url,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{REPLICATE_API}/predictions", headers=headers, json=payload)
            if resp.status_code != 201:
                logger.error(f"Replicate rembg create failed: {resp.status_code} {resp.text[:200]}")
                return None
            pred = resp.json()
            pred_id = pred["id"]

        # Poll until done (max 60s)
        for _ in range(30):
            await asyncio.sleep(2)
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{REPLICATE_API}/predictions/{pred_id}", headers=headers)
                if resp.status_code != 200:
                    continue
                d = json.loads(resp.text, strict=False)
                status = d.get("status")
                if status == "succeeded":
                    output = d.get("output", "")
                    if isinstance(output, list) and output:
                        return output[0]
                    elif isinstance(output, str) and output:
                        return output
                    return None
                elif status in ("failed", "canceled"):
                    logger.error(f"Replicate rembg failed: {d.get('error')}")
                    return None

        logger.error("Replicate rembg timed out")
        return None

    except Exception as e:
        logger.error(f"Replicate rembg error: {e}")
        return None


async def download_image(url: str) -> bytes:
    """Download image from URL with timeout."""
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        resp.raise_for_status()
        return resp.content


def _post_process_garment(image_bytes: bytes) -> bytes:
    """Post-process: crop to bounding box, clean edges, white background."""
    import numpy as np
    from scipy import ndimage

    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")

    # Get alpha channel
    alpha = np.array(img.split()[3], dtype=np.uint8)

    # Threshold
    binary = (alpha > 128).astype(np.uint8)

    if binary.sum() == 0:
        return image_bytes

    # Clean up with morphological operations
    binary = ndimage.binary_closing(binary, iterations=2).astype(np.uint8)
    binary = ndimage.binary_opening(binary, iterations=1).astype(np.uint8)

    # Apply clean mask
    result = img.copy()
    clean_alpha = Image.fromarray((binary * 255).astype(np.uint8), mode="L")
    result.putalpha(clean_alpha)

    # Crop to bounding box with padding
    bbox = result.getbbox()
    if bbox:
        x1, y1, x2, y2 = bbox
        w, h = x2 - x1, y2 - y1
        pad_x = max(5, int(w * 0.05))
        pad_y = max(5, int(h * 0.05))
        x1 = max(0, x1 - pad_x)
        y1 = max(0, y1 - pad_y)
        x2 = min(img.width, x2 + pad_x)
        y2 = min(img.height, y2 + pad_y)
        result = result.crop((x1, y1, x2, y2))

    # Convert to RGB with white background (VTON-optimized)
    bg = Image.new("RGBA", result.size, (255, 255, 255, 255))
    bg.paste(result, mask=result.split()[3])
    result = bg.convert("RGB")

    buf = io.BytesIO()
    result.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def upload_to_r2(image_bytes: bytes, key: str) -> Optional[str]:
    """Upload garment image to Cloudflare R2."""
    try:
        import boto3
        from botocore.config import Config

        s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{os.getenv('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
            aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
            config=Config(signature_version="s3v4"),
        )
        bucket = os.getenv("R2_BUCKET", "drishti")

        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=image_bytes,
            ContentType="image/png",
            CacheControl="public, max-age=31536000",
        )
        logger.info(f"Uploaded garment to R2: {key}")
        return f"https://{os.getenv('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com/{bucket}/{key}"
    except Exception as e:
        logger.warning(f"R2 upload failed: {e}")
        return None


async def extract_garment(
    image_url: str,
    upload: bool = True,
    output_key: Optional[str] = None,
) -> dict:
    """
    Full garment extraction pipeline.

    Returns:
        dict with original_url, garment_image URL, processing_time_ms, sizes
    """
    start = time.time()

    # Step 1: Remove background via Replicate (fast, runs on their servers)
    no_bg_url = await _replicate_remove_bg(image_url)
    if not no_bg_url:
        return {"error": "Background removal failed", "garment_image": image_url, "original_url": image_url}

    # Step 2: Download the no-bg result
    try:
        no_bg_bytes = await download_image(no_bg_url)
    except Exception as e:
        logger.error(f"Failed to download no-bg result: {e}")
        return {"error": f"Download failed: {e}", "garment_image": no_bg_url, "original_url": image_url}

    original_img = Image.open(io.BytesIO(no_bg_bytes))
    original_size = original_img.size

    # Step 3: Post-process (crop, clean, white background)
    garment_bytes = _post_process_garment(no_bg_bytes)
    garment_img = Image.open(io.BytesIO(garment_bytes))
    garment_size = garment_img.size

    # Step 4: Upload to R2
    garment_url = None
    if upload:
        if not output_key:
            import hashlib
            url_hash = hashlib.md5(image_url.encode()).hexdigest()[:12]
            output_key = f"garments/{url_hash}.png"
        garment_url = await upload_to_r2(garment_bytes, output_key)

    if not garment_url:
        import base64
        b64 = base64.b64encode(garment_bytes).decode()
        garment_url = f"data:image/png;base64,{b64}"

    elapsed = int((time.time() - start) * 1000)
    logger.info(f"Garment extracted in {elapsed}ms: {original_size} → {garment_size}")

    return {
        "original_url": image_url,
        "garment_image": garment_url,
        "garment_bytes": garment_bytes,
        "processing_time_ms": elapsed,
        "original_size": list(original_size),
        "garment_size": list(garment_size),
    }


async def extract_garment_from_bytes(
    image_bytes: bytes,
    upload: bool = True,
    output_key: Optional[str] = None,
) -> dict:
    """Extract garment from raw image bytes (for uploaded files)."""
    start = time.time()

    original_img = Image.open(io.BytesIO(image_bytes))
    original_size = original_img.size

    # For uploaded bytes, we need to upload first to get a URL for Replicate
    # Upload to R2 temporarily
    import hashlib
    temp_key = f"temp/{hashlib.md5(image_bytes).hexdigest()[:12]}.png"
    temp_url = await upload_to_r2(image_bytes, temp_key)

    if not temp_url:
        return {"error": "Failed to upload image for processing", "garment_image": None}

    # Use Replicate for background removal
    no_bg_url = await _replicate_remove_bg(temp_url)
    if not no_bg_url:
        return {"error": "Background removal failed", "garment_image": None}

    no_bg_bytes = await download_image(no_bg_url)
    garment_bytes = _post_process_garment(no_bg_bytes)

    garment_img = Image.open(io.BytesIO(garment_bytes))
    garment_size = garment_img.size

    garment_url = None
    if upload and output_key:
        garment_url = await upload_to_r2(garment_bytes, output_key)

    if not garment_url:
        import base64
        b64 = base64.b64encode(garment_bytes).decode()
        garment_url = f"data:image/png;base64,{b64}"

    elapsed = int((time.time() - start) * 1000)

    return {
        "garment_image": garment_url,
        "garment_bytes": garment_bytes,
        "processing_time_ms": elapsed,
        "original_size": list(original_size),
        "garment_size": list(garment_size),
    }


# ── Standalone test ──

async def _test():
    import sys
    logging.basicConfig(level=logging.INFO)

    url = sys.argv[1] if len(sys.argv) > 1 else "https://images.unsplash.com/photo-1521572163474-6864f9cf17ab?w=400"
    print(f"Extracting garment from: {url}")

    result = await extract_garment(url, upload=False)
    if "error" in result:
        print(f"Error: {result['error']}")
    else:
        print(f"Original: {result['original_size']}")
        print(f"Garment: {result['garment_size']}")
        print(f"Time: {result['processing_time_ms']}ms")
        with open("/tmp/garment_extracted.png", "wb") as f:
            f.write(result["garment_bytes"])
        print("Saved to /tmp/garment_extracted.png")


if __name__ == "__main__":
    asyncio.run(_test())
