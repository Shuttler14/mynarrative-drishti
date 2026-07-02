"""
Garment extraction pipeline — extracts clean flat-lay garment images from marketplace product photos.

Pipeline:
  1. Download image from URL
  2. Background removal via rembg (IS-Net model, best quality for CPU)
  3. Garment isolation — largest connected component, exclude model/mannequin
  4. Crop to bounding box + padding
  5. Optional: upload to R2 for persistent storage
  6. Return clean garment PNG

Models used:
  - rembg with isnet-general-use (best free CPU model, MIT license)
  - PIL/Pillow for connected component analysis and morphological ops
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import httpx
from PIL import Image, ImageFilter

logger = logging.getLogger("drishti.garment")

# ── Thread pool for CPU-bound rembg work ──
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="garment")

# ── Lazy-loaded rembg session (model loaded once) ──
_remgb_session = None
_remgb_lock = asyncio.Lock()


def _get_remgb_session():
    """Get or create rembg session with silueta model (fast, good for product photos)."""
    global _remgb_session
    if _remgb_session is None:
        from rembg import new_session
        # silueta: small (14MB), fast on CPU, good edge quality for product photos
        _remgb_session = new_session("silueta")
        logger.info("rembg session created with silueta model")
    return _remgb_session


def _remove_background_sync(image_bytes: bytes) -> bytes:
    """Synchronous background removal via rembg. Runs in thread pool."""
    from rembg import remove

    session = _get_remgb_session()
    input_image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")

    # Remove background — alpha_matting for better edge quality on clothing
    output_image = remove(
        input_image,
        session=session,
        alpha_matting=True,
        alpha_matting_foreground_threshold=240,
        alpha_matting_background_threshold=10,
        alpha_matting_erode_size=10,
    )

    buf = io.BytesIO()
    output_image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _isolate_garment_sync(image_bytes: bytes) -> bytes:
    """
    Isolate garment from person/mannequin after background removal.
    
    Strategy:
      1. rembg already removed the background → transparent pixels
      2. The remaining foreground is person + garment
      3. We crop to the foreground bounding box with smart padding
      4. We apply morphological cleaning to remove small noise
    """
    import numpy as np
    from scipy import ndimage

    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")

    # Get alpha channel as mask
    alpha = img.split()[3]
    mask_np = np.array(alpha, dtype=np.uint8)

    # Threshold to binary
    binary = (mask_np > 128).astype(np.uint8)

    # Label connected components
    labeled, num_features = ndimage.label(binary)

    if num_features == 0:
        return image_bytes

    # Get component sizes
    component_sizes = ndimage.sum(binary, labeled, range(1, num_features + 1))

    # Keep components that are >5% of the largest (handles garment pieces)
    largest_size = max(component_sizes)
    threshold = largest_size * 0.05

    # Create clean mask with significant components only
    clean_mask = np.zeros_like(binary)
    for i, size in enumerate(component_sizes):
        if size >= threshold:
            clean_mask |= (labeled == (i + 1)).astype(np.uint8)

    # Morphological operations to clean edges
    # Close small gaps in the garment
    clean_mask = ndimage.binary_closing(clean_mask, iterations=3).astype(np.uint8)
    # Remove small isolated noise
    clean_mask = ndimage.binary_opening(clean_mask, iterations=2).astype(np.uint8)

    # Apply clean mask to original image
    result = img.copy()
    clean_alpha = Image.fromarray((clean_mask * 255).astype(np.uint8), mode="L")
    result.putalpha(clean_alpha)

    # Crop to bounding box with smart padding
    bbox = result.getbbox()
    if bbox:
        x1, y1, x2, y2 = bbox
        w, h = x2 - x1, y2 - y1

        # Add padding proportional to image size (5-10%)
        pad_x = max(10, int(w * 0.08))
        pad_y = max(10, int(h * 0.08))

        x1 = max(0, x1 - pad_x)
        y1 = max(0, y1 - pad_y)
        x2 = min(img.width, x2 + pad_x)
        y2 = min(img.height, y2 + pad_y)
        result = result.crop((x1, y1, x2, y2))

    # Convert to RGB with white background (better for VTON models)
    # VTON models typically expect garment on white/plain background
    bg = Image.new("RGBA", result.size, (255, 255, 255, 255))
    bg.paste(result, mask=result.split()[3])
    result = bg.convert("RGB")

    buf = io.BytesIO()
    result.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def download_image(url: str) -> bytes:
    """Download image from URL with timeout."""
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        resp.raise_for_status()
        return resp.content


async def upload_to_r2(image_bytes: bytes, key: str) -> Optional[str]:
    """Upload garment image to Cloudflare R2 for persistent storage."""
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

        # Return public URL (if bucket has public access) or signed URL
        account_id = os.getenv("R2_ACCOUNT_ID")
        url = f"https://{account_id}.r2.cloudflarestorage.com/{bucket}/{key}"
        logger.info(f"Uploaded garment to R2: {key}")
        return url
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

    Args:
        image_url: URL of marketplace product image
        upload: Whether to upload result to R2
        output_key: R2 key for upload (auto-generated if None)

    Returns:
        dict with:
          - original_url: input URL
          - garment_image: URL of extracted garment (R2 or data URI)
          - garment_bytes: raw PNG bytes (for immediate use)
          - processing_time_ms: total processing time
          - original_size: (width, height) of original
          - garment_size: (width, height) of extracted garment
    """
    start = time.time()

    # Step 1: Download
    try:
        original_bytes = await download_image(image_url)
    except Exception as e:
        logger.error(f"Failed to download image: {e}")
        return {"error": f"Download failed: {e}", "garment_image": image_url}

    original_img = Image.open(io.BytesIO(original_bytes))
    original_size = original_img.size

    # Step 2: Background removal (CPU-bound, run in thread pool)
    loop = asyncio.get_event_loop()
    no_bg_bytes = await loop.run_in_executor(_executor, _remove_background_sync, original_bytes)

    # Step 3: Garment isolation (CPU-bound, run in thread pool)
    garment_bytes = await loop.run_in_executor(_executor, _isolate_garment_sync, no_bg_bytes)

    garment_img = Image.open(io.BytesIO(garment_bytes))
    garment_size = garment_img.size

    # Step 4: Upload to R2 (optional)
    garment_url = None
    if upload:
        if not output_key:
            import hashlib
            url_hash = hashlib.md5(image_url.encode()).hexdigest()[:12]
            output_key = f"garments/{url_hash}.png"
        garment_url = await upload_to_r2(garment_bytes, output_key)

    # Fallback: data URI if R2 upload fails
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

    loop = asyncio.get_event_loop()
    no_bg_bytes = await loop.run_in_executor(_executor, _remove_background_sync, image_bytes)
    garment_bytes = await loop.run_in_executor(_executor, _isolate_garment_sync, no_bg_bytes)

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

        # Save for inspection
        with open("/tmp/garment_extracted.png", "wb") as f:
            f.write(result["garment_bytes"])
        print("Saved to /tmp/garment_extracted.png")


if __name__ == "__main__":
    asyncio.run(_test())
