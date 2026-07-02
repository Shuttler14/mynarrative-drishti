from __future__ import annotations

import asyncio
import base64
import logging
import os
import uuid
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Header, UploadFile, File, Form, Query
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


class VTONRequest(BaseModel):
    garment_image_url: str
    person_image_url: str | None = None
    vto_engine: str = "idm-vton"
    extract_garment: bool = True  # Run garment extraction preprocessing


class VTONJobResponse(BaseModel):
    job_id: str
    status: str
    result_url: str | None = None
    processing_time_ms: int | None = None
    error_message: str | None = None


# ── Garment Extraction Endpoint ──

@router.post("/extract-garment")
async def extract_garment_endpoint(
    image_url: str = Query(..., description="Marketplace product image URL"),
    upload: bool = Query(True, description="Upload extracted garment to R2"),
):
    """
    Extract a clean flat-lay garment image from a marketplace product photo.
    Uses rembg (IS-Net) background removal + connected component isolation.
    """
    from api.services.garment_extract import extract_garment

    result = await extract_garment(image_url, upload=upload)

    if "error" in result:
        raise HTTPException(500, result["error"])

    return {
        "original_url": result["original_url"],
        "garment_image": result["garment_image"],
        "processing_time_ms": result["processing_time_ms"],
        "original_size": result["original_size"],
        "garment_size": result["garment_size"],
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

async def _preprocess_garment_for_vton(garment_url: str, should_extract: bool) -> str:
    """Optionally run garment extraction on marketplace images before VTON."""
    if not should_extract:
        return garment_url

    # Skip extraction if already a data URI or R2 garment URL
    if garment_url.startswith("data:"):
        return garment_url
    if "/garments/" in garment_url:
        return garment_url

    # Check if it looks like a marketplace URL (not already a clean garment)
    marketplace_domains = ["myntra.com", "ajio.com", "amazon.in", "amazon.com", "flipkart.com"]
    is_marketplace = any(d in garment_url for d in marketplace_domains)

    if is_marketplace:
        logger.info(f"Extracting garment from marketplace URL: {garment_url[:80]}")
        from api.services.garment_extract import extract_garment
        result = await extract_garment(garment_url, upload=True)
        if "error" not in result:
            return result["garment_image"]
        logger.warning(f"Garment extraction failed, using original: {result.get('error')}")

    return garment_url


# ── VTON Endpoints ──

@router.post("/try-on")
async def create_vton_job(
    req: VTONRequest,
    authorization: str = Header(None),
):
    user_id = None
    if authorization:
        payload = verify_token(authorization.replace("Bearer ", ""))
        if payload:
            user_id = payload["sub"]

    # Preprocess garment image (extract from marketplace if needed)
    garment_url = await _preprocess_garment_for_vton(
        req.garment_image_url, req.extract_garment
    )

    from api.services.vton_replicate import create_try_on_job

    result = await create_try_on_job(
        person_image_url=req.person_image_url,
        garment_image_url=garment_url,
        garment_type="top",
    )

    return {
        "status": result.get("status", "error"),
        "result_image": result.get("result_image"),
        "processing_time_ms": result.get("processing_time_ms"),
        "quality_score": result.get("quality_score"),
        "engine": result.get("engine", "idm-vton"),
        "garment_extracted": garment_url != req.garment_image_url,
        "message": result.get("detail", "VTON completed"),
    }


@router.post("/widget/try-on")
async def widget_try_on(
    req: WidgetTryOnRequest,
    authorization: str = Header(None),
):
    """Shopify widget endpoint — normalizes payload format before forwarding to Replicate."""
    user_id = None
    if authorization:
        payload = verify_token(authorization.replace("Bearer ", ""))
        if payload:
            user_id = payload["sub"]

    # Preprocess garment image (extract from marketplace if needed)
    garment_url = await _preprocess_garment_for_vton(req.garment_image, should_extract=True)

    engine_payload = _MODE_MAP.get(req.mode, ("top", None))

    from api.services.vton_replicate import create_try_on_job

    result = await create_try_on_job(
        person_image_url=req.user_image,
        garment_image_url=garment_url,
        garment_type=engine_payload[0],
    )

    return {
        "status": result.get("status", "error"),
        "result_image": result.get("result_image"),
        "processing_time_ms": result.get("processing_time_ms"),
        "quality_score": result.get("quality_score"),
        "engine": result.get("engine", "idm-vton"),
        "garment_extracted": garment_url != req.garment_image,
    }


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

    # Upload to Cloudflare R2 (S3-compatible)
    try:
        import boto3
        from botocore.exceptions import ClientError

        account_id = os.getenv("R2_ACCOUNT_ID", "")
        endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com" if account_id else None

        s3_client = boto3.client(
            "s3",
            aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID", "") or os.getenv("AWS_ACCESS_KEY_ID", ""),
            aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY", "") or os.getenv("AWS_SECRET_ACCESS_KEY", ""),
            region_name="auto",
            endpoint_url=endpoint_url,
        )

        bucket = os.getenv("R2_BUCKET", "") or os.getenv("S3_BUCKET", "drishti")

        s3_client.put_object(
            Bucket=bucket,
            Key=filename,
            Body=contents,
            ContentType=file.content_type,
        )

        # Generate presigned URL
        presigned_url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": filename},
            ExpiresIn=3600,
        )

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
