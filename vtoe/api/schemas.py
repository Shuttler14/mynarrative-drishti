from __future__ import annotations
from pydantic import BaseModel, Field


class TryOnRequest(BaseModel):
    person_image: str
    garment_image: str
    garment_type: str = Field(default="top", pattern="^(top|bottom|full|ethnic)$")
    ethnic_sub_type: str | None = None
    preserve_face: bool = True
    preserve_accessories: bool = True
    output_resolution: str = "768x1024"
    quality: str = Field(default="balanced", pattern="^(fast|balanced|quality)$")
    num_inference_steps: int | None = Field(default=None, ge=10, le=60)
    guidance_scale: float | None = Field(default=None, ge=1.0, le=12.0)
    seed: int | None = None


class TryOnResponse(BaseModel):
    job_id: str
    status: str
    result_image: str
    processing_time_ms: int
    quality_score: float
    face_similarity: float
    garment_similarity: float
    metadata: dict
