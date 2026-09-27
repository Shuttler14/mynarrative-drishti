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
import threading
import time
from typing import Optional

import httpx
import numpy as np
from PIL import Image

from api.services.replicate_limits import create_prediction

logger = logging.getLogger("drishti.garment")

REPLICATE_API = "https://api.replicate.com/v1"


def _get_token() -> str:
    return os.getenv("REPLICATE_API_TOKEN", "")


def _get_rembg_version() -> str:
    return os.getenv("REPLICATE_REMBG_VERSION", "fb8af171cfa1616ddcf1242c093f9c46bcada5ad4cf6f2fbe8b81b330ec5c003")


# Pooled client: a fresh TLS handshake per create/poll costs 300-500ms on the
# SIN → Replicate (US) path, and extraction makes up to ~15 calls per job.
_rembg_client: Optional[httpx.AsyncClient] = None


def _rc() -> httpx.AsyncClient:
    global _rembg_client
    if _rembg_client is None or _rembg_client.is_closed:
        _rembg_client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_connections=6, max_keepalive_connections=4),
        )
    return _rembg_client


_rembg_session = None
_rembg_infer_lock: Optional["asyncio.Lock"] = None
_session_load_lock = threading.Lock()  # single-flight: a timed-out waiter must never start a 2nd load
_local_infer_ok = False  # set by warmup_rembg(): True only if this host can infer fast


def _local_enabled() -> bool:
    """Should we attempt local u2net at all?

    On this shared vCPU a single forward pass takes 60-90s — trying it per
    request spawns a leaked CPU-burning thread every time (our wait_for gives
    up at 60s but the thread keeps running) and starves the whole machine.
    warmup_rembg() probes real speed once at boot; REMBG_LOCAL=on/off forces it.
    """
    mode = os.getenv("REMBG_LOCAL", "auto").lower()
    if mode in ("on", "1", "true", "yes"):
        return True
    if mode in ("off", "0", "false", "no"):
        return False
    return _local_infer_ok


def warmup_rembg() -> None:
    """Load the ONNX session and time a tiny run; enable local only if fast.

    Runs in a background thread at startup (session load alone is ~30s here).
    Skipped entirely when REMBG_LOCAL=off — the session costs ~400MB RSS, which
    matters on the small Fly machine when local inference is disabled anyway.
    """
    global _local_infer_ok
    mode = os.getenv("REMBG_LOCAL", "auto").lower()
    if mode in ("off", "0", "false", "no"):
        _local_infer_ok = False
        logger.info("rembg warmup skipped (REMBG_LOCAL=off)")
        return
    try:
        import numpy as np

        t0 = time.time()
        session = _get_rembg_session()
        load_s = time.time() - t0
        t1 = time.time()
        inp = session.inner_session.get_inputs()[0]
        session.inner_session.run(None, {inp.name: np.zeros((1, 3, 320, 320), dtype=np.float32)})
        run_s = time.time() - t1
        _local_infer_ok = run_s < 8.0
        logger.info(
            f"rembg warmup: load={load_s:.1f}s run={run_s:.1f}s "
            f"local={'enabled' if _local_infer_ok else 'disabled (slow host)'}"
        )
    except Exception as e:
        _local_infer_ok = False
        logger.warning(f"rembg warmup failed: {e}")


def _get_rembg_session():
    """Lazily load the local rembg ONNX session (model baked into the image).

    ORT defaults to one intra-op thread per visible core — on a shared host
    that spawns huge memory arenas and OOM-kills the 1-2GB machine. Pin to a
    single thread: u2net infers at 320x320 anyway, so this costs little time
    and bounds RSS. Graph optimization runs on a shared vCPU where the full
    pass on u2net can take minutes — BASIC is plenty for segmentation.
    """
    global _rembg_session
    if _rembg_session is not None:
        return _rembg_session
    with _session_load_lock:
        if _rembg_session is None:
            import onnxruntime as ort
            from rembg import new_session

            t0 = time.time()
            so = ort.SessionOptions()
            so.intra_op_num_threads = 1
            so.inter_op_num_threads = 1
            so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
            _rembg_session = new_session(
                os.getenv("REMBG_SESSION", "u2net"), sess_opts=so
            )
            logger.info(f"rembg session loaded in {time.time() - t0:.1f}s")
        return _rembg_session


def _get_infer_lock() -> "asyncio.Lock":
    global _rembg_infer_lock
    if _rembg_infer_lock is None:
        _rembg_infer_lock = asyncio.Lock()
    return _rembg_infer_lock


async def _local_remove_bg(image_bytes: bytes) -> Optional[bytes]:
    """Local background removal (onnxruntime, runs in this container).

    Primary path — Replicate's hosted rembg has been stuck in "starting"
    for minutes, which silently disabled extraction entirely.
    Single-flight: concurrent extract requests serialize so ONNX working
    memory never doubles on the small Fly machine, and session creation
    never races.
    Returns RGBA PNG bytes, or None on failure.
    """
    if not _local_enabled():
        return None

    def _run() -> bytes:
        from rembg import remove

        t0 = time.time()
        session = _get_rembg_session()
        logger.info(f"rembg session acquired in {time.time() - t0:.2f}s")
        img = Image.open(io.BytesIO(image_bytes))
        # Cap working size — keeps peak RSS sane on the small machine
        if max(img.size) > 768:
            img.thumbnail((768, 768))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        result = remove(buf.getvalue(), session=session)
        logger.info(f"rembg remove done in {time.time() - t0:.2f}s")
        return result

    # Bounded waits: a leaked/stuck inference thread must not wedge every
    # later request behind the lock forever — time out and fall back.
    lock = _get_infer_lock()
    t_lock = time.time()
    try:
        await asyncio.wait_for(lock.acquire(), timeout=30)
    except asyncio.TimeoutError:
        logger.error("local rembg: infer lock busy >30s — falling back")
        return None
    try:
        lock_waited = time.time() - t_lock
        if lock_waited > 2:
            logger.warning(f"local rembg: waited {lock_waited:.1f}s for infer lock")
        try:
            result = await asyncio.wait_for(asyncio.to_thread(_run), timeout=60)
        except asyncio.TimeoutError:
            logger.error("local rembg: inference timed out after 60s — falling back")
            return None
        logger.info(f"local rembg: inference {time.time() - t_lock - lock_waited:.2f}s")
        return result
    except Exception as e:
        logger.error(f"Local rembg failed: {e}")
        return None
    finally:
        lock.release()


def _rembg_max_dim() -> int:
    """Cap for rembg input resolution. Masks don't need full camera pixels
    (4032px inputs cost seconds of inference), and the final garment output
    is thumbnailed to 1024 anyway. REMBG_MAX_DIM=0 disables."""
    try:
        return int(os.getenv("REMBG_MAX_DIM", "1280"))
    except ValueError:
        return 1280


def _prep_rembg_input(img_bytes: bytes) -> str:
    """Downscale (if needed) and encode image bytes as a data URI for rembg."""
    import base64 as _b64

    cap = _rembg_max_dim()
    if cap > 0:
        try:
            im = Image.open(io.BytesIO(img_bytes))
            w, h = im.size
            if max(w, h) > cap:
                scale = cap / float(max(w, h))
                im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
                buf = io.BytesIO()
                is_png = img_bytes[:8] == b'\x89PNG\r\n\x1a\n'
                if is_png:
                    im.save(buf, format="PNG", optimize=True)
                else:
                    if im.mode not in ("RGB", "L"):
                        im = im.convert("RGB")
                    im.save(buf, format="JPEG", quality=90)
                img_bytes = buf.getvalue()
                logger.info(f"rembg input downscaled {w}x{h} → {im.size[0]}x{im.size[1]}")
        except Exception as e:
            logger.warning(f"rembg downscale skipped: {e}")

    if img_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        ct = "image/png"
    elif img_bytes[:3] == b'\xff\xd8\xff':
        ct = "image/jpeg"
    elif img_bytes[:4] == b'RIFF' and img_bytes[8:12] == b'WEBP':
        ct = "image/webp"
    else:
        ct = "image/jpeg"
    return f"data:{ct};base64,{_b64.b64encode(img_bytes).decode()}"


async def _run_rembg(image_input: str) -> Optional[str]:
    """Run the hosted rembg prediction. Returns output URL or None.

    ``image_input`` should be a data URI (or a URL as last resort).
    """
    token = _get_token()
    if not token:
        logger.error("REPLICATE_API_TOKEN not set")
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    payload = {
        "version": _get_rembg_version(),
        "input": {
            "image": image_input,
        },
    }

    try:
        # Shared create helper: spacing + 429 body logging + Retry-After backoff
        resp = await create_prediction(payload, headers, tag="rembg")
        if resp is None:
            logger.error("Replicate rembg rate limited after retries")
            return None
        if resp.status_code != 201:
            logger.error(f"Replicate rembg create failed: {resp.status_code} {resp.text[:200]}")
            return None
        pred = resp.json()
        pred_id = pred["id"]

        # Poll until done (max 30s). 429s on GET used to be swallowed
        # (status=None → keeps polling) and showed up as dead seconds.
        poll_429 = 0
        for _ in range(30):
            await asyncio.sleep(1)
            resp = await _rc().get(f"{REPLICATE_API}/predictions/{pred_id}", headers=headers)
            if resp.status_code == 429:
                poll_429 += 1
                ra = resp.headers.get("Retry-After", "")
                try:
                    await asyncio.sleep(min(float(ra), 3.0) if ra else 1.5)
                except ValueError:
                    await asyncio.sleep(1.5)
                continue
            if resp.status_code != 200:
                continue
            d = json.loads(resp.text, strict=False)
            status = d.get("status")
            if status == "succeeded":
                if poll_429:
                    logger.warning(f"rembg polls hit {poll_429}x429 before success")
                output = d.get("output", "")
                if isinstance(output, list) and output:
                    return output[0]
                elif isinstance(output, str) and output:
                    return output
                return None
            elif status in ("failed", "canceled"):
                logger.error(f"Replicate rembg failed: {d.get('error')}")
                return None

        logger.error(f"Replicate rembg timed out (poll_429={poll_429})")
        return None

    except Exception as e:
        logger.error(f"Replicate rembg error: {e}")
        return None


async def _replicate_remove_bg(image_url: str) -> Optional[str]:
    """Use Replicate's rembg model to remove background. Returns output URL.

    Downloads the image first and sends a downscaled base64 data URI to avoid
    DNS issues with marketplace CDNs (e.g. Flipkart's rukminim2.flikt.com).
    """
    try:
        img_bytes = await download_image(image_url)
        logger.info(f"Downloaded image ({len(img_bytes)} bytes) for rembg")
        image_input = await asyncio.to_thread(_prep_rembg_input, img_bytes)
    except Exception as e:
        # Last resort: hand Replicate the raw URL directly
        logger.warning(f"Failed to prepare image for rembg, passing URL directly: {e}")
        image_input = image_url
    return await _run_rembg(image_input)


async def _replicate_remove_bg_bytes(img_bytes: bytes) -> Optional[str]:
    """Same as _replicate_remove_bg but from in-memory bytes — skips the
    temp-R2-upload + re-download round-trip entirely."""
    image_input = await asyncio.to_thread(_prep_rembg_input, img_bytes)
    return await _run_rembg(image_input)


async def download_image(url: str) -> bytes:
    """Download image from URL with timeout."""
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        resp.raise_for_status()
        return resp.content


def _norm_garment_type(garment_type: str | None) -> str:
    """Normalise type hints from reco slots / widget categories."""
    t = (garment_type or "").strip().lower()
    aliases = {
        "top": "top", "upper": "top", "upper_body": "top",
        "bottom": "bottom", "lower": "bottom", "lower_body": "bottom",
        "shoes": "shoes", "footwear": "shoes",
        "full": "full", "dress": "full", "dresses": "full",
    }
    return aliases.get(t, "auto")


# Fallback bands when no garment boundary can be found. Framing:
#   full  = head + feet visible
#   upper = head visible, feet cut off (waist-up / knee-up)
#   lower = head cut off, feet visible (waist-down)
#   mid   = torso closeup (no head, no feet)
# On-model shots show other garments too, and VTON copies everything we hand
# it (shirt photo with pants visible → pants get painted onto the user along
# with the real bottom). Fixed fractions are unreliable — a knee-up shot ends
# at the knees, so its hem sits at ~0.90, not the full-body ~0.55 — the
# primary path finds the actual garment→pants color boundary per image.
_BAND_VERSION = "b4"
_FULL_FALLBACK: dict[str, tuple[float, float]] = {
    "top": (0.0, 0.58),
    "bottom": (0.42, 1.0),
    "shoes": (0.84, 1.0),
}
_LOWER_SHOES_FALLBACK = (0.86, 1.0)
# Boundary detection: min sustained luminance change (0..1) garment↔other
_BOUNDARY_THRESH = 0.16
# Never shrink the silhouette below this fraction of its original area
# (protects against a bad band nearly emptying the mask)
_BAND_MIN_AREA = 0.06


def _row_lum(binary, rgb: np.ndarray, bbox: tuple[int, int, int, int]) -> np.ndarray:
    """Median luminance of masked pixels per bbox row (nan where too few).

    Median, not mean: arms/hands crossing the garment boundary are a minority
    of the row and would otherwise dampen the shirt→pants shift below the
    detection threshold — the garment's own color wins the row.
    """
    x1, y1, x2, y2 = bbox
    reg = binary[y1:y2, x1:x2].astype(bool)
    sub = rgb[y1:y2, x1:x2].astype(np.float64) / 255.0
    lum = 0.299 * sub[:, :, 0] + 0.587 * sub[:, :, 1] + 0.114 * sub[:, :, 2]
    h, w = reg.shape
    min_px = max(3, int(0.05 * w))
    L = np.full(h, np.nan)
    for y in range(h):
        m = reg[y]
        if int(m.sum()) >= min_px:
            L[y] = float(np.median(lum[y][m]))
    return L


def _find_boundary(L: np.ndarray, lo: float, hi: float, smooth: int) -> int | None:
    """First sustained color change in the L segment → the garment edge.

    Sustained = BOTH halves of the remainder after the edge still differ from
    the region before it (kills one-off prints/shadows; pants run to frame end).
    Returns the row index RELATIVE to the bbox start (0..len(L)-1), or None.
    """
    h = len(L)
    i0, i1 = int(h * lo), int(h * hi)
    if i1 - i0 < smooth * 3:
        return None
    valid = ~np.isnan(L)
    if valid.sum() < smooth:
        return None
    L = np.interp(np.arange(h), np.where(valid)[0], L[valid])
    d = L[i0 + smooth: i1] - L[i0: i1 - smooth]  # change over window, len n
    for k in range(len(d)):
        if abs(d[k]) < _BOUNDARY_THRESH:
            continue
        i = i0 + k
        pre = L[max(0, i - smooth): i]
        post = L[i + smooth: i1]
        if len(post) < smooth:
            return None
        pre_m = float(np.mean(pre)) if len(pre) else None
        if pre_m is None:
            continue
        m1 = float(np.mean(post[: len(post) // 2]))
        m2 = float(np.mean(post[len(post) // 2:]))
        t = _BOUNDARY_THRESH * 0.85
        if abs(m1 - pre_m) >= t and abs(m2 - pre_m) >= t and (m1 - pre_m) * (m2 - pre_m) > 0:
            # window [i, i+smooth] straddles the step → step is at i+smooth
            return min(i + smooth, h - 1)
    return None


def _find_all_boundaries(L: np.ndarray, smooth: int, lo: float = 0.0,
                         hi: float = 1.0, max_cuts: int = 8) -> list[int]:
    """Every sustained color change in [lo, hi), top-down, bbox-relative."""
    h = len(L)
    valid = ~np.isnan(L)
    if valid.sum() < smooth:
        return []
    Li = np.interp(np.arange(h), np.where(valid)[0], L[valid])
    cuts: list[int] = []
    start = float(lo)
    while start < hi:
        c = _find_boundary(Li, start, hi, smooth)
        if c is None:
            break
        cuts.append(int(c))
        start = (c + smooth + 1) / h
    return cuts


def _pick_bottom_band(L: np.ndarray, framing: str) -> tuple[float, float] | None:
    """Vertical band containing the bottom garment (bbox-relative fractions).

    One boundary was inverted on mid-framed shots: the first luminance change
    INSIDE the pants is the hem (pants→skin), and (hem, 1.0) kept only
    legs/shoes while the trousers never reached VTON — the user's outfit
    simply didn't appear in the try-on. Instead: split the silhouette at
    every sustained boundary and take the longest segment that starts in the
    upper half (bottom garments hang from the waist; legs/shoes below the
    hem must not qualify). Ties go to the lower segment.
    """
    h = len(L)
    smooth = max(15, int(h * 0.04))
    cuts = _find_all_boundaries(L, smooth)
    edges = [0] + cuts + [h]
    min_len = max(16, int(0.10 * h))
    best: tuple[int, int] | None = None
    for a, b in zip(edges[:-1], edges[1:]):
        if b - a < min_len or a >= 0.5 * h:
            continue
        if best is None or (b - a) >= (best[1] - best[0]):
            best = (a, b)
    if best is None:
        if framing == "full":
            return _FULL_FALLBACK["bottom"]
        return None  # frame already garment-focused (or ambiguous) — keep all
    return (best[0] / h, best[1] / h)


def _framing(binary, rgb: np.ndarray, bbox: tuple[int, int, int, int]) -> str:
    """How the on-model photo is framed: "", "full", "upper", "lower", "mid".

    "" means not a person (flat-lay / product-only shot) — no band crop.
    """
    x1, y1, x2, y2 = bbox
    region = binary[y1:y2, x1:x2].astype("float64")
    h, w = region.shape
    if h < 60 or w < 30 or h / float(w) < 1.05:
        return ""
    row_w = region.sum(axis=1)
    peak = float(row_w.max())
    if peak <= 0:
        return ""

    # Head → shoulder jump: narrow head rows then a much wider shoulder span
    head_end = max(1, int(h * 0.10))
    head_max = float(row_w[:head_end].max())
    shoulder = float(row_w[head_end: int(h * 0.25)].max()) if h > int(h * 0.25) else peak
    head_like = head_max < 0.50 * peak and shoulder > 1.40 * max(head_max, 1.0)

    # Skin pixels — face in the top quarter signals head presence; any skin
    # (face/hands) signals a person. Flat-lays on white have none.
    sub = rgb[y1:y2, x1:x2]
    r = sub[:, :, 0].astype(int)
    g = sub[:, :, 1].astype(int)
    b = sub[:, :, 2].astype(int)
    skin = (r > 95) & (g > 40) & (b > 20) & (r > g) & (r > b) & ((r - g) > 15) & (region > 0)
    mask_area = max(int((region > 0).sum()), 1)
    face_skin = int(skin[: max(1, int(h * 0.25))].sum()) > 0.002 * mask_area
    any_skin = int(skin.sum()) > 0.004 * mask_area
    head_present = head_like or face_skin

    # Standing feet: bottom rows must be MUCH narrower than mid-body AND the
    # overall peak — knee-up shots end at the thighs/knees with legs still
    # ~0.7 of peak width, which must NOT count as feet (hem-cut false full).
    mid = float(row_w[int(h * 0.45): int(h * 0.55)].mean())
    feet = float(row_w[int(h * 0.92):].mean())
    feet_present = mid > 0 and feet < 0.60 * mid and feet < 0.65 * peak

    if not (head_like or any_skin or feet_present):
        return ""
    if head_present and feet_present:
        return "full"
    if head_present:
        return "upper"
    if feet_present:
        return "lower"
    return "mid"  # skin-only (torso closeup with hands) — ambiguous framing


def _apply_garment_band(binary, bbox: tuple[int, int, int, int], band: tuple[float, float]):
    """Zero out silhouette rows outside the target garment's vertical band."""
    x1, y1, x2, y2 = bbox
    h = y2 - y1
    lo, hi = band
    cut1 = y1 + int(h * lo)
    cut2 = min(y2, y1 + int(h * hi))
    if cut2 - cut1 < 20:
        return binary
    area_before = int(binary.sum())
    band_arr = np.zeros_like(binary)
    band_arr[cut1:cut2, :] = 1
    cropped = binary * band_arr
    # Guard: a bad band must not nearly empty the silhouette
    if int(cropped.sum()) == 0 or int(cropped.sum()) < _BAND_MIN_AREA * area_before:
        return binary
    return cropped


def _post_process_garment(image_bytes: bytes, garment_type: str = "auto") -> bytes:
    """Post-process: crop to bounding box, clean edges, white background.

    When ``garment_type`` names a slot (top/bottom/shoes) and the mask looks
    like an on-model person, first zero every silhouette row outside that
    garment's vertical band — so a shirt photo's visible pants never reach VTON.
    """
    import numpy as np
    from scipy import ndimage

    gtype = _norm_garment_type(garment_type)
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    # Band math is relative to the silhouette — cap working size so the
    # Replicate fallback's full-res output (1600x2000) can't thrash memory
    # or block the event loop for a minute
    if max(img.size) > 1024:
        img.thumbnail((1024, 1024))

    # Get alpha channel
    alpha = np.array(img.split()[3], dtype=np.uint8)

    # Threshold
    binary = (alpha > 128).astype(np.uint8)

    if binary.sum() == 0:
        return image_bytes

    # Clean up with morphological operations
    binary = ndimage.binary_closing(binary, iterations=2).astype(np.uint8)
    binary = ndimage.binary_opening(binary, iterations=1).astype(np.uint8)

    # Keep only the target garment on person silhouettes. VTON copies
    # everything we hand it, so sibling garments (pants under a shirt) must
    # not leak into the extracted image. Primary path: find the actual
    # garment→other color boundary in the photo; fixed fractions only as a
    # full-body fallback.
    if gtype in ("top", "bottom", "shoes"):
        ys, xs = np.where(binary)
        if len(ys):
            bbox = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
            rgb = np.array(img)[:, :, :3]
            framing = _framing(binary, rgb, bbox)
            if framing:
                y1b, y2b = bbox[1], bbox[3]
                h = y2b - y1b
                smooth = max(15, int(h * 0.04))
                L = _row_lum(binary, rgb, bbox)
                band = None
                if gtype == "top" and framing != "lower":
                    # scan starts below any hair/chin (would false-trigger)
                    lo = 0.35 if framing == "full" else 0.40
                    hi = 0.82 if framing == "full" else 1.0
                    cut = _find_boundary(L, lo, hi, smooth)
                    if cut is not None:
                        band = (0.0, cut / h)
                    elif framing == "full":
                        band = _FULL_FALLBACK["top"]
                elif gtype == "bottom":
                    # segment-based pick — see _pick_bottom_band for why a
                    # single boundary shipped shoes instead of trousers
                    band = _pick_bottom_band(L, framing)
                else:  # shoes — feet must be in frame; scan starts below any
                    # shirt→pants boundary (that would catch trousers, not shoes)
                    if framing in ("full", "lower"):
                        cut = _find_boundary(L, 0.72, 1.0, smooth)
                        if cut is not None:
                            band = (cut / h, 1.0)
                        elif framing == "full":
                            band = _FULL_FALLBACK["shoes"]
                        else:
                            band = _LOWER_SHOES_FALLBACK
                if band:
                    binary = _apply_garment_band(binary, bbox, band)
                    logger.info(
                        f"Garment band crop applied: type={gtype} framing={framing} "
                        f"band=({band[0]:.3f},{band[1]:.3f}) bbox={bbox}"
                    )

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


def _r2_public_url(key: str) -> str:
    return f"https://{os.getenv('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com/{os.getenv('R2_BUCKET', 'drishti')}/{key}"


# Boto3 defaults are 60s connect / 60s read × 3 retries — a slow R2 hop then
# blocks the event loop for minutes (health checks fail, every request queues).
_R2_TIMEOUTS: dict = {"connect_timeout": 5, "read_timeout": 15, "retries": {"max_attempts": 2, "mode": "standard"}}


def _r2_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.getenv('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
        aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
        config=Config(signature_version="s3v4", **_R2_TIMEOUTS),
    )


def _r2_presign_url(key: str) -> str:
    """A GET URL that works without SigV4.

    The bare ``{account}.r2.cloudflarestorage.com/{bucket}/{key}`` form is the
    S3 API endpoint — fetching it unsigned returns 400, so VTON/JS can't load
    it. Presigned for 1h (sessions are shorter). Sync boto3 — run via to_thread.
    """
    try:
        return _r2_client().generate_presigned_url(
            "get_object",
            Params={"Bucket": os.getenv("R2_BUCKET", "drishti"), "Key": key},
            ExpiresIn=3600,
        )
    except Exception as e:
        logger.warning(f"R2 presign failed: {e}; returning unsigned URL")
        return _r2_public_url(key)


def _r2_exists(key: str) -> bool:
    """True if the garment already lives in R2 (skip slow Replicate rembg).

    Sync boto3 — callers must run this via asyncio.to_thread so a slow R2
    response never stalls the event loop.
    """
    try:
        _r2_client().head_object(Bucket=os.getenv("R2_BUCKET", "drishti"), Key=key)
        return True
    except Exception:
        return False


async def upload_to_r2(image_bytes: bytes, key: str) -> Optional[str]:
    """Upload garment image to Cloudflare R2 (off the event loop).

    Returns a presigned GET URL (the bare endpoint URL isn't fetchable).
    """
    def _put() -> Optional[str]:
        s3 = _r2_client()
        bucket = os.getenv("R2_BUCKET", "drishti")

        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=image_bytes,
            ContentType="image/png",
            CacheControl="public, max-age=31536000",
        )
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=3600,
        )

    try:
        url = await asyncio.to_thread(_put)
        logger.info(f"Uploaded garment to R2: {key}")
        return url
    except Exception as e:
        logger.warning(f"R2 upload failed: {e}")
        return None


async def extract_garment(
    image_url: str,
    upload: bool = True,
    output_key: Optional[str] = None,
    garment_type: str = "auto",
) -> dict:
    """
    Full garment extraction pipeline.

    ``garment_type`` (top|bottom|shoes|full|auto) limits the kept silhouette
    to that garment's vertical band on on-model photos.

    Returns:
        dict with original_url, garment_image URL, processing_time_ms, sizes
    """
    start = time.time()
    gtype = _norm_garment_type(garment_type)
    logger.info(f"extract_garment start: type={gtype} url={image_url[:96]}")

    # Step 0: R2 cache hit → skip slow Replicate rembg entirely
    if upload and not output_key:
        import hashlib
        url_hash = hashlib.md5(image_url.encode()).hexdigest()[:12]
        output_key = f"garments/{url_hash}-{gtype}-{_BAND_VERSION}.png"
    if upload and output_key and await asyncio.to_thread(_r2_exists, output_key):
        cached_url = await asyncio.to_thread(_r2_presign_url, output_key)
        logger.info(f"Garment cache hit: {output_key}")
        return {
            "original_url": image_url,
            "garment_image": cached_url,
            "processing_time_ms": int((time.time() - start) * 1000),
            "cached": True,
        }

    # Step 1: Download source once (shared by local + Replicate paths)
    try:
        original_bytes = await download_image(image_url)
        original_size = Image.open(io.BytesIO(original_bytes)).size
        logger.info(f"source downloaded in {time.time() - start:.1f}s ({original_size[0]}x{original_size[1]})")
    except Exception as e:
        logger.error(f"Failed to download source image: {e}")
        return {"error": f"Download failed: {e}", "garment_image": image_url, "original_url": image_url}

    # Step 2: Background removal — local onnxruntime first, Replicate fallback.
    # Bytes variant reuses the download from Step 1 (no second fetch, no
    # temp-R2 round-trip) and sends a downscaled data URI.
    no_bg_bytes = await _local_remove_bg(original_bytes)
    if no_bg_bytes:
        logger.info(f"Background removal: local rembg ({time.time() - start:.1f}s)")
    else:
        no_bg_url = await _replicate_remove_bg_bytes(original_bytes)
        if not no_bg_url:
            return {"error": "Background removal failed", "garment_image": image_url, "original_url": image_url}
        try:
            no_bg_bytes = await download_image(no_bg_url)
        except Exception as e:
            logger.error(f"Failed to download no-bg result: {e}")
            return {"error": f"Download failed: {e}", "garment_image": no_bg_url, "original_url": image_url}

    # Step 3: Post-process (band crop by garment type, clean, white background)
    garment_bytes = await asyncio.to_thread(_post_process_garment, no_bg_bytes, gtype)
    garment_img = Image.open(io.BytesIO(garment_bytes))
    garment_size = garment_img.size

    # Step 4: Upload to R2 (type in key — a shirt crop and a pant crop of the
    # same source URL must not collide in cache)
    garment_url = None
    if upload:
        if not output_key:
            import hashlib
            url_hash = hashlib.md5(image_url.encode()).hexdigest()[:12]
            output_key = f"garments/{url_hash}-{gtype}-{_BAND_VERSION}.png"
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
    garment_type: str = "auto",
) -> dict:
    """Extract garment from raw image bytes (for uploaded files)."""
    start = time.time()
    gtype = _norm_garment_type(garment_type)

    original_img = Image.open(io.BytesIO(image_bytes))
    original_size = original_img.size

    # Background removal — bytes path: no temp R2 upload, downscaled data URI
    no_bg_bytes = await _local_remove_bg(image_bytes)
    if no_bg_bytes:
        logger.info("Background removal: local rembg (bytes)")
    else:
        no_bg_url = await _replicate_remove_bg_bytes(image_bytes)
        if not no_bg_url:
            return {"error": "Background removal failed", "garment_image": None}
        no_bg_bytes = await download_image(no_bg_url)
    t_rembg = time.time()

    garment_bytes = await asyncio.to_thread(_post_process_garment, no_bg_bytes, gtype)
    t_post = time.time()

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
    logger.info(
        "extract_bytes stages: rembg_ms=%d post_ms=%d upload_ms=%d total_ms=%d in=%sx%s out=%sx%s",
        int((t_rembg - start) * 1000), int((t_post - t_rembg) * 1000),
        int((time.time() - t_post) * 1000), elapsed,
        original_size[0], original_size[1], garment_size[0], garment_size[1],
    )

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
