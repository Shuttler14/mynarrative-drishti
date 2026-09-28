from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
import uuid
from datetime import datetime

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Header, UploadFile, File, Form, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.database import get_db
from api.models.schema import VTONJob
from api.utils.auth import verify_token

logger = logging.getLogger("drishti.vton")
settings = get_settings()
router = APIRouter()


def _humanize_vton_error(detail: str | None) -> str | None:
    """Map raw Replicate/model errors to user-safe copy."""
    if not detail:
        return None
    d = str(detail)
    low = d.lower()
    if "no detections" in low or "no person" in low:
        return "We couldn't find a person in that photo. Try a clearer full-body or upper-body shot."
    if "rate limit" in low or "429" in d:
        return "Try-on is busy right now. Please wait a moment and tap retry."
    if "402" in d or "insufficient credit" in low:
        return "Try-on is temporarily unavailable. Please try again in a moment."
    if "timed out" in low or "timeout" in low:
        return "Try-on took too long. Please retry."
    if "cannot fetch image" in low or "failed to download" in low:
        return "We couldn't load one of the garment images. Please retry."
    if "not set" in low and "token" in low:
        return "Try-on is temporarily unavailable."
    # Never surface raw model stack traces
    if len(d) > 180 or "Traceback" in d or "Error:" in d:
        return "Try-on failed. Please retry with another photo."
    return d


# ── Widget → Engine payload adapter ──

_MODE_MAP = {
    "top": ("top", None),
    "bottom": ("bottom", None),
    "dress": ("full", None),
    "saree": ("ethnic", "saree"),
    "lehenga": ("ethnic", "lehenga"),
    "kurta": ("ethnic", "kurta"),
    "sherwani": ("ethnic", "sherwani"),
    "dupatta": ("ethnic", "dupatta"),
    "anarkali": ("ethnic", "anarkali"),
    "salwar": ("ethnic", "salwar"),
}


class WidgetTryOnRequest(BaseModel):
    """Shopify widget payload format."""
    mode: str = "top"
    user_image: str  # base64 data URI or URL
    garment_image: str  # base64 data URI or URL
    category: str = "upper_body"  # upper_body | lower_body | dresses | footwear


class VTONRequest(BaseModel):
    garment_image_url: str | None = None
    garment_image_urls: list[str] | None = None  # full-look try-on (top+bottom+shoes)
    person_image_url: str  # required — missing photo must 400, not 500
    vto_engine: str = "idm-vton"
    extract_garment: bool = True  # Run garment extraction preprocessing
    seed: int | None = None  # Optional seed to re-roll artifacts on regenerate
    garment_types: list[str] | None = None  # optional per-garment type hints


class VTONJobResponse(BaseModel):
    job_id: str
    status: str
    result_url: str | None = None
    processing_time_ms: int | None = None
    error_message: str | None = None


# ── Garment Extraction Endpoint ──

class ExtractGarmentRequest(BaseModel):
    image_url: str
    upload: bool = True
    garment_type: str = "auto"  # top|bottom|shoes|full — band-crops on-model shots


@router.post("/extract-garment")
async def extract_garment_endpoint(
    req: ExtractGarmentRequest,
):
    """
    Extract a clean flat-lay garment image from a marketplace product photo.
    Uses rembg (IS-Net) background removal + connected component isolation.
    """
    from api.services.garment_extract import extract_garment

    result = await extract_garment(req.image_url, upload=req.upload, garment_type=req.garment_type)

    if "error" in result:
        raise HTTPException(500, result["error"])

    return {
        "original_url": result["original_url"],
        "garment_image": result["garment_image"],
        "processing_time_ms": result["processing_time_ms"],
        # cache hits don't know the source/crop dimensions
        "original_size": result.get("original_size"),
        "garment_size": result.get("garment_size"),
        "cached": result.get("cached", False),
    }


@router.post("/extract-garment-upload")
async def extract_garment_upload(
    file: UploadFile = File(...),
):
    """Extract garment from an uploaded image file."""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "File must be an image")

    contents = await file.read()
    if len(contents) > 10 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 10MB)")

    from api.services.garment_extract import extract_garment_from_bytes

    ext = file.filename.rsplit(".", 1)[-1] if file.filename and "." in file.filename else "png"
    output_key = f"garments/upload-{uuid.uuid4().hex}.{ext}"

    result = await extract_garment_from_bytes(contents, upload=True, output_key=output_key)

    return {
        "garment_image": result["garment_image"],
        "processing_time_ms": result["processing_time_ms"],
        "original_size": result["original_size"],
        "garment_size": result["garment_size"],
    }


# ── Helper: preprocess garment image for VTON ──

async def _preprocess_garment_for_vton(garment_url: str, should_extract: bool, garment_type: str = "auto") -> str:
    """Run garment extraction on marketplace images before VTON.
    
    Only extracts from HTTP marketplace URLs (Amazon, Myntra, etc.) that show
    models wearing garments. Skips:
    - data: URIs (already processed or small thumbnails)
    - /garments/ URLs (already extracted)
    - Google Shopping thumbnails (already flat-lay, just tiny)

    garment_type (top|bottom|shoes|full|auto): limits the kept silhouette to
    that garment's vertical band — a shirt photo's visible pants must not be
    handed to VTON or they get painted onto the user too.
    """
    if not should_extract or not garment_url:
        return garment_url

    # Skip extraction for data URIs, R2 garment URLs
    if garment_url.startswith("data:") or "/garments/" in garment_url:
        return garment_url

    # Only extract from marketplace URLs with model shots
    marketplace_domains = ["myntra.com", "myntassets.com", "ajio.com", "jioimages.com",
                           "amazon.in", "amazon.com", "flipkart.com", "meesho.com"]
    is_marketplace = any(d in garment_url for d in marketplace_domains)

    if not is_marketplace:
        # Not a marketplace URL (e.g. Unsplash, Google Shopping thumbnail)
        # Send directly to VTON — it's likely already a flat-lay
        return garment_url

    from api.services.garment_extract import _norm_garment_type
    gtype = _norm_garment_type(garment_type)

    logger.info(f"Extracting garment from marketplace URL (type={gtype}): {garment_url[:80]}")
    from api.services.garment_extract import extract_garment
    result = await extract_garment(garment_url, upload=False, garment_type=gtype)
    if "error" not in result:
        logger.info(f"Garment extracted successfully: {result.get('garment_size')}")
        return result["garment_image"]
    logger.warning(f"Garment extraction failed, using original: {result.get('error')}")

    return garment_url


async def _persist_vton_job_async(
    job_id: uuid.UUID,
    uid: uuid.UUID | None,
    garment_image: str | None,
    person_image: str | None,
    person_url: str | None,
    result: dict,
    event_payload: dict | None = None,
):
    """Persist the VTON job AFTER the response is sent.

    Neon (us-east-1) sits ~200ms/RTT from Fly SIN; the inline insert+flush was
    adding 1.1-5.8s to every try-on response for data nothing user-facing needs.
    """
    try:
        from api.database import async_session
        from api.models.schema import UserEvent

        async with async_session() as session:
            session.add(VTONJob(
                id=job_id,
                user_id=uid,
                garment_image=(garment_image or "")[:2000],
                person_image=(person_image or "")[:2000] or None,
                person_url=(person_url or "")[:2000] or None,
                result_image=result.get("result_image"),
                status=result.get("status", "error"),
                vto_engine=result.get("engine", "idm-vton"),
                error_message=result.get("detail") if result.get("status") == "error" else None,
                processing_time_ms=result.get("processing_time_ms"),
            ))
            if uid and result.get("status") == "completed":
                session.add(UserEvent(
                    user_id=uid,
                    event_type="vton_completed",
                    payload=event_payload or {},
                    weight=2.0,
                ))
            await session.commit()
        logger.info(f"vton_job persisted in background: {job_id}")
    except Exception:
        logger.warning("Background VTON persist failed", exc_info=True)


# ── VTON Endpoints ──

@router.post("/try-on")
async def create_vton_job(
    req: VTONRequest,
    authorization: str = Header(None),
    x_shopify_customer_id: str | None = Header(None, alias="X-Shopify-Customer-Id"),
    background_tasks: BackgroundTasks = None,
    db: AsyncSession = Depends(get_db),
):
    from api.utils.identity import resolve_user

    t_entry = time.time()
    user = await resolve_user(
        db,
        authorization=authorization,
        shopify_customer_id=x_shopify_customer_id,
        create_if_missing=False,
    )
    user_id = str(user.id) if user else None

    if not req.person_image_url or not str(req.person_image_url).strip():
        raise HTTPException(400, "person_image_url is required — upload a photo to try on garments")

    # Full-look try-on: top + bottom + shoes in one generation (max 4 garments)
    garment_urls = [u for u in (req.garment_image_urls or []) if u][:4]
    if not garment_urls and req.garment_image_url:
        garment_urls = [req.garment_image_url]

    if not garment_urls:
        raise HTTPException(400, "garment_image_url or garment_image_urls required")

    # Preprocess garment images (extract from marketplace if needed).
    # Per-garment type hints from the wizard (slot names) drive the band crop
    # so only the target garment's silhouette region is handed to VTON.
    types = list(req.garment_types or [])
    t_pre = time.time()
    # Parallel: sequential awaits made a 3-look full-try-on pay extract 3×
    processed = list(await asyncio.gather(*[
        _preprocess_garment_for_vton(
            u,
            req.extract_garment,
            garment_type=(types[i] if i < len(types) else "auto"),
        )
        for i, u in enumerate(garment_urls)
    ]))
    preprocess_ms = int((time.time() - t_pre) * 1000)

    from api.services.vton_replicate import create_try_on_job

    # Prefer explicit per-garment types; fall back to "top" for multi-look
    garment_type = "top"
    if req.garment_types:
        garment_type = req.garment_types[0] or "top"
    elif len(processed) > 1:
        garment_type = "full"

    t_create_call = time.time()
    result = await create_try_on_job(
        person_image_url=req.person_image_url,
        garment_image_url=processed[0],
        garment_image_urls=processed,
        garment_type=garment_type,
        seed=req.seed,
    )
    t_post = time.time()
    if result.get("stages") is not None:
        result["stages"]["preprocess_ms"] = preprocess_ms

    # Persist in the background — the response must not wait on Neon roundtrips
    try:
        uid = uuid.UUID(user_id) if user_id else None
    except (TypeError, ValueError):
        uid = None
    job_id = str(uuid.uuid4())
    background_tasks.add_task(
        _persist_vton_job_async,
        uuid.UUID(job_id),
        uid,
        processed[0],
        req.person_image_url,
        req.person_image_url,
        result,
        {
            "job_id": job_id,
            "result_image": bool(result.get("result_image")),
            "engine": result.get("engine", "idm-vton"),
        },
    )

    status = result.get("status", "error")
    result_image = result.get("result_image")
    message = _humanize_vton_error(result.get("detail") if status != "completed" else None) or \
              "VTON completed"
    resp = {
        "status": status,
        "result_image": result_image,
        "processing_time_ms": result.get("processing_time_ms"),
        "quality_score": result.get("quality_score"),
        "engine": result.get("engine", "idm-vton"),
        "garment_extracted": any(p != u for p, u in zip(processed, garment_urls)),
        "message": message,
    }
    if result.get("stages") is not None:
        resp["stages"] = result["stages"]
    resp["job_id"] = job_id
    t_end = time.time()
    logger.info(
        "vton_handler pre_ms=%d create_wall_ms=%d persist_ms=%d total_ms=%d",
        int((t_create_call - t_entry) * 1000),
        int((t_post - t_create_call) * 1000),
        int((t_end - t_post) * 1000),
        int((t_end - t_entry) * 1000),
    )
    return resp


@router.post("/widget/try-on")
async def widget_try_on(
    req: WidgetTryOnRequest,
    authorization: str = Header(None),
    x_shopify_customer_id: str | None = Header(None, alias="X-Shopify-Customer-Id"),
    background_tasks: BackgroundTasks = None,
    db: AsyncSession = Depends(get_db),
):
    """Shopify widget endpoint — normalizes payload format before forwarding to Replicate."""
    from api.utils.identity import resolve_user

    user = await resolve_user(
        db,
        authorization=authorization,
        shopify_customer_id=x_shopify_customer_id,
        create_if_missing=False,
    )
    user_id = str(user.id) if user else None

    # Honor category hints from Liquid/iframe (upper_body|lower_body|dresses|footwear)
    cat = (req.category or "").lower()
    if cat in ("lower_body", "bottom", "pants", "trousers"):
        engine_type = "bottom"
    elif cat in ("dresses", "dress", "full", "ethnic"):
        engine_type = "full"
    elif cat in ("footwear", "shoes"):
        engine_type = "shoes"
    else:
        engine_type = _MODE_MAP.get(req.mode, ("top", None))[0]
        if req.mode in ("vton", "", None) and engine_type == "top":
            engine_type = "top"

    # Preprocess garment image with the category so the band crop keeps only
    # the target garment (model shots include pants/shoes VTON would copy).
    t_pre = time.time()
    garment_url = await _preprocess_garment_for_vton(
        req.garment_image, should_extract=True, garment_type=engine_type
    )
    preprocess_ms = int((time.time() - t_pre) * 1000)

    from api.services.vton_replicate import create_try_on_job

    result = await create_try_on_job(
        person_image_url=req.user_image,
        garment_image_url=garment_url,
        garment_type=engine_type,
    )
    if result.get("stages") is not None:
        result["stages"]["preprocess_ms"] = preprocess_ms

    # Persist in the background — the response must not wait on Neon roundtrips
    try:
        uid = uuid.UUID(user_id) if user_id else None
    except (TypeError, ValueError):
        uid = None
    job_id = str(uuid.uuid4())
    background_tasks.add_task(
        _persist_vton_job_async,
        uuid.UUID(job_id),
        uid,
        garment_url,
        req.user_image,
        None,
        result,
        {"job_id": job_id, "mode": req.mode},
    )

    status = result.get("status", "error")
    result_image = result.get("result_image")
    message = _humanize_vton_error(result.get("detail") if status != "completed" else None) or \
              result.get("message") or "VTON completed"
    resp = {
        "status": status,
        "result_image": result_image,
        "processing_time_ms": result.get("processing_time_ms"),
        "quality_score": result.get("quality_score"),
        "engine": result.get("engine", "idm-vton"),
        "garment_extracted": garment_url != req.garment_image,
        # Contract aliases for Shopify modal / embed widgets
        "success": status == "completed" and bool(result_image),
        "image": result_image,
        "error": None if status == "completed" and result_image else message,
        "message": message,
    }
    if result.get("stages") is not None:
        resp["stages"] = result["stages"]
    resp["job_id"] = job_id
    return resp


@router.get("/job/{job_id}")
async def get_vton_job(job_id: str, db: AsyncSession = Depends(get_db)):
    job = await db.get(VTONJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    return VTONJobResponse(
        job_id=str(job.id),
        status=job.status,
        result_url=job.result_image,
        processing_time_ms=job.processing_time_ms,
        error_message=job.error_message,
    ).model_dump()


@router.post("/upload-person")
async def upload_person_image(
    file: UploadFile = File(...),
    authorization: str = Header(None),
):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "File must be an image")

    contents = await file.read()
    if len(contents) > 10 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 10MB)")

    # Extract user_id from token if provided
    user_id = None
    if authorization:
        payload = verify_token(authorization.replace("Bearer ", ""))
        if payload:
            user_id = payload["sub"]

    # Generate unique filename
    ext = file.filename.rsplit(".", 1)[-1] if file.filename and "." in file.filename else "jpg"
    filename = f"person-images/{user_id or 'anonymous'}-{uuid.uuid4().hex}.{ext}"

    from botocore.config import Config
    from botocore.exceptions import ClientError

    # Upload to Cloudflare R2 (S3-compatible) — boto3 is sync: run it off the
    # event loop so a slow R2 hop can't stall health checks and other requests
    try:
        def _do_upload() -> str:
            import boto3

            account_id = os.getenv("R2_ACCOUNT_ID", "")
            endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com" if account_id else None

            s3_client = boto3.client(
                "s3",
                aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID", "") or os.getenv("AWS_ACCESS_KEY_ID", ""),
                aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY", "") or os.getenv("AWS_SECRET_ACCESS_KEY", ""),
                region_name="auto",
                endpoint_url=endpoint_url,
                config=Config(
                    signature_version="s3v4",
                    connect_timeout=5,
                    read_timeout=30,
                    retries={"max_attempts": 2, "mode": "standard"},
                ),
            )

            bucket = os.getenv("R2_BUCKET", "") or os.getenv("S3_BUCKET", "drishti")

            s3_client.put_object(
                Bucket=bucket,
                Key=filename,
                Body=contents,
                ContentType=file.content_type,
            )

            # Generate presigned URL
            return s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": filename},
                ExpiresIn=3600,
            )

        presigned_url = await asyncio.to_thread(_do_upload)

        return {
            "message": "Upload successful",
            "filename": filename,
            "url": presigned_url,
            "content_type": file.content_type,
            "size": len(contents),
        }

    except ClientError as e:
        logger.error(f"R2 upload failed: {e}")
        raise HTTPException(500, "Failed to upload image to storage")
    except Exception as e:
        logger.error(f"Upload error: {e}")
        raise HTTPException(500, "Upload failed")


@router.get("/engines")
async def list_vton_engines():
    return {
        "engines": [
            {
                "id": "idm-vton",
                "name": "IDM-VTON",
                "description": "State-of-the-art virtual try-on with garment detail preservation",
                "quality": "high",
                "speed": "medium",
                "gpu_required": True,
            },
            {
                "id": "catvton",
                "name": "CatVTON",
                "description": "Category-aware virtual try-on for diverse garment types",
                "quality": "medium",
                "speed": "fast",
                "gpu_required": True,
            },
            {
                "id": "controlnet-vton",
                "name": "ControlNet VTON",
                "description": "Pose-guided virtual try-on with ControlNet",
                "quality": "high",
                "speed": "slow",
                "gpu_required": True,
            },
        ],
        "default": "idm-vton",
    }


class BatchVTONRequest(BaseModel):
    person_image_url: str
    garment_image_urls: list[str]
    session_id: str | None = None


@router.post("/batch-try-on")
async def batch_try_on(
    req: BatchVTONRequest,
    authorization: str = Header(None),
):
    """Batch VTON — try on multiple garments against one person image.
    Each garment URL is preprocessed with extraction if it's a marketplace URL."""
    user_id = None
    if authorization:
        payload = verify_token(authorization.replace("Bearer ", ""))
        if payload:
            user_id = payload["sub"]

    from api.services.vton_replicate import create_try_on_job

    async def _run_vton(garment_url):
        clean_url = await _preprocess_garment_for_vton(garment_url, should_extract=True)
        return await create_try_on_job(
            person_image_url=req.person_image_url,
            garment_image_url=clean_url,
        )

    results = await asyncio.gather(*[_run_vton(url) for url in req.garment_image_urls[:6]])

    return {
        "results": results,
        "count": len(results),
    }


@router.get("/history")
async def vton_history(
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    if not authorization:
        raise HTTPException(401, "Missing token")

    payload = verify_token(authorization.replace("Bearer ", ""))
    if not payload:
        raise HTTPException(401, "Invalid token")

    stmt = (
        select(VTONJob)
        .where(VTONJob.user_id == payload["sub"])
        .order_by(VTONJob.created_at.desc())
        .limit(50)
    )
    result = await db.execute(stmt)
    jobs = result.scalars().all()

    return {
        "jobs": [
            {
                "id": str(j.id),
                "status": j.status,
                "vto_engine": j.vto_engine,
                "result_image": j.result_image,
                "processing_time_ms": j.processing_time_ms,
                "created_at": j.created_at.isoformat() if j.created_at else None,
            }
            for j in jobs
        ]
    }
