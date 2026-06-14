from __future__ import annotations

import base64
import logging
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Header, UploadFile, File, Form
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


def _widget_to_engine(req: WidgetTryOnRequest) -> dict:
    """Translate widget payload to VTOE engine format."""
    garment_type, ethnic_sub_type = _MODE_MAP.get(req.mode, ("top", None))
    return {
        "person_image": req.user_image,
        "garment_image": req.garment_image,
        "garment_type": garment_type,
        "ethnic_sub_type": ethnic_sub_type,
        "preserve_face": True,
        "quality": "balanced",
    }


class VTONRequest(BaseModel):
    garment_image_url: str
    person_image_url: str | None = None
    vto_engine: str = "idm-vton"


class VTONJobResponse(BaseModel):
    job_id: str
    status: str
    result_url: str | None = None
    processing_time_ms: int | None = None
    error_message: str | None = None


@router.post("/try-on")
async def create_vton_job(
    req: VTONRequest,
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    user_id = None
    if authorization:
        payload = verify_token(authorization.replace("Bearer ", ""))
        if payload:
            user_id = payload["sub"]

    job = VTONJob(
        user_id=user_id,
        garment_image=req.garment_image_url,
        person_url=req.person_image_url,
        status="queued",
        vto_engine=req.vto_engine,
    )
    db.add(job)
    await db.flush()

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{settings.VTOE_GPU_URL}/v1/try-on",
                json={
                    "job_id": str(job.id),
                    "garment_image": req.garment_image_url,
                    "person_image": req.person_image_url,
                    "engine": req.vto_engine,
                },
            )
            if resp.status_code == 200:
                job.status = "processing"
            else:
                job.status = "queued"
                logger.warning(f"GPU worker returned {resp.status_code}, queued for retry")
    except Exception as e:
        job.status = "queued"
        logger.warning(f"GPU worker unavailable: {e}, queued for retry")

    return {
        "job_id": str(job.id),
        "status": job.status,
        "message": "VTON job created",
    }


@router.post("/widget/try-on")
async def widget_try_on(
    req: WidgetTryOnRequest,
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Shopify widget endpoint — normalizes payload format before forwarding to GPU."""
    user_id = None
    if authorization:
        payload = verify_token(authorization.replace("Bearer ", ""))
        if payload:
            user_id = payload["sub"]

    engine_payload = _widget_to_engine(req)

    job = VTONJob(
        user_id=user_id,
        garment_image=req.garment_image[:200] if len(req.garment_image) > 200 else req.garment_image,
        person_url=req.user_image[:200] if len(req.user_image) > 200 else req.user_image,
        status="queued",
        vto_engine="idm-vton",
    )
    db.add(job)
    await db.flush()

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{settings.VTOE_GPU_URL}/v1/try-on",
                json={**engine_payload, "job_id": str(job.id)},
            )
            if resp.status_code == 200:
                result = resp.json()
                job.status = "completed"
                job.result_image = result.get("result_image")
                job.processing_time_ms = result.get("processing_time_ms")
                job.quality_score = result.get("quality_score")
                return {
                    "status": "completed",
                    "result_image": result.get("result_image"),
                    "processing_time_ms": result.get("processing_time_ms"),
                    "quality_score": result.get("quality_score"),
                    "face_similarity": result.get("face_similarity"),
                    "garment_similarity": result.get("garment_similarity"),
                }
            else:
                job.status = "queued"
                logger.warning(f"GPU worker returned {resp.status_code}")
    except Exception as e:
        job.status = "queued"
        logger.warning(f"GPU worker unavailable: {e}")

    return {
        "status": job.status,
        "job_id": str(job.id),
        "message": "Processing",
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

    return {
        "message": "Upload received",
        "filename": file.filename,
        "content_type": file.content_type,
        "size": len(contents),
    }


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
