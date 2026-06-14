from __future__ import annotations
import asyncio

from fastapi import FastAPI, HTTPException

from vtoe.api.schemas import TryOnRequest, TryOnResponse
from vtoe.config import settings
from vtoe.pipeline.engine import TryOnEngine
from vtoe.utils.imaging import decode_image, encode_image
from vtoe.utils.memory import vram_gb

app = FastAPI(title="MyNarrative VTON", version="1.0.0")
engine = TryOnEngine()

_gpu_lock = asyncio.Lock()
_queue_depth = 0


@app.post("/v1/try-on", response_model=TryOnResponse)
async def try_on(req: TryOnRequest) -> TryOnResponse:
    global _queue_depth
    _queue_depth += 1
    try:
        person = decode_image(req.person_image)
        garment = decode_image(req.garment_image)
        async with _gpu_lock:
            result = await asyncio.to_thread(
                engine.run,
                person_img=person, garment_img=garment,
                garment_type=req.garment_type, sub_type=req.ethnic_sub_type,
                quality=req.quality, preserve_face=req.preserve_face,
                steps=req.num_inference_steps, guidance=req.guidance_scale, seed=req.seed)
    except Exception as e:
        raise HTTPException(500, detail={"code": "TRYON_FAILED", "message": str(e)})
    finally:
        _queue_depth -= 1

    return TryOnResponse(
        job_id=result["job_id"], status=result["status"],
        result_image=encode_image(result["result"]),
        processing_time_ms=result["processing_time_ms"],
        quality_score=result["quality_score"], face_similarity=result["face_similarity"],
        garment_similarity=result["garment_similarity"], metadata=result["metadata"])


@app.post("/v1/try-on-batch")
async def try_on_batch(person_image: str, garments: list[dict]) -> dict:
    results = []
    for g in garments:
        req = TryOnRequest(person_image=person_image, garment_image=g["garment_image"],
                           garment_type=g.get("garment_type", "ethnic"),
                           ethnic_sub_type=g.get("ethnic_sub_type"))
        results.append((await try_on(req)).model_dump())
    return {"results": results}


@app.get("/v1/engines")
async def engines() -> dict:
    return {"engines": [
        {"name": "idm-vton", "best_for": ["detail", "western", "ethnic"], "speed": "balanced"},
        {"name": "catvton", "best_for": ["complex_ethnic", "fast_fallback"], "speed": "fast"},
    ], "ethnic_subtypes": ["saree", "lehenga", "kurta", "sherwani", "dupatta", "anarkali"]}


@app.get("/v1/health")
async def health() -> dict:
    used, total = vram_gb()
    return {"status": "ok", "gpu_used_gb": round(used, 2), "gpu_total_gb": round(total, 2),
            "queue_depth": _queue_depth}
