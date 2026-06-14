"""
Drishti VTOE GPU Worker - Google Colab T4 Edition
==================================================
Runs on Colab free tier (NVIDIA T4 16GB VRAM)
Exposes FastAPI server via Cloudflare Tunnel
"""
# !pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
# !pip install diffusers transformers accelerate safetensors
# !pip install insightface onnxruntime-gpu
# !pip install opencv-python-headless rembg
# !pip install fastapi uvicorn pydantic httpx
# !pip install cloudflared

import subprocess
import time
import os
import sys
import json
import logging
import asyncio
from pathlib import Path
from typing import Optional

import torch
import numpy as np
import cv2
from PIL import Image

# --- Observability: structured logging + Sentry (fail-safe) ---
try:
    from drishti_observability.config import ObsSettings
    from drishti_observability.logging import configure as configure_logging
    from drishti_observability import sentry
    _obs = ObsSettings.load()
    configure_logging("vtoe-worker", level=_obs.log_level, json_logs=_obs.log_json, version=_obs.service_version)
    sentry.init("vtoe-worker", _obs)
except ImportError:
    logging.basicConfig(level=logging.INFO)

logger = logging.getLogger("vtoe-worker")

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
DTYPE = torch.float16 if DEVICE == "cuda" else torch.float32
VRAM_GB = torch.cuda.get_device_properties(0).total_mem / (1024**3) if DEVICE == "cuda" else 0

logger.info(f"Device: {DEVICE}, VRAM: {VRAM_GB:.1f}GB, Dtype: {DTYPE}")

# ============================================================
# Model Registry
# ============================================================

MODELS = {}


def load_idm_vton():
    """Load IDM-VTON model for garment try-on"""
    logger.info("Loading IDM-VTON model...")
    from diffusers import AutoPipelineInpainting

    pipe = AutoPipelineInpainting.from_pretrained(
        "yisol/IDM-VTON",
        torch_dtype=DTYPE,
        variant="fp16" if DTYPE == torch.float16 else None,
    )
    pipe.to(DEVICE)

    if DEVICE == "cuda":
        pipe.enable_attention_slicing()
        try:
            pipe.enable_xformers_memory_efficient_attention()
            logger.info("xformers enabled")
        except Exception:
            logger.warning("xformers not available")

    MODELS["idm-vton"] = pipe
    logger.info("IDM-VTON loaded")


def load_face_preserver():
    """Load InsightFace for face preservation"""
    logger.info("Loading face preservation model...")
    import insightface
    from insightface.app import FaceAnalysis

    app = FaceAnalysis(
        name="buffalo_l",
        providers=["CUDAExecutionProvider"],
    )
    app.prepare(ctx_id=0, det_size=(640, 640))
    MODELS["face_analysis"] = app
    logger.info("Face analysis loaded")


def load_controlnet():
    """Load ControlNet for pose-guided try-on"""
    logger.info("Loading ControlNet pipeline...")
    from diffusers import StableDiffusionControlNetPipeline, ControlNetModel

    controlnet = ControlNetModel.from_pretrained(
        "lllyasviel/control_v11p_sd15_openpose",
        torch_dtype=DTYPE,
    )
    pipe = StableDiffusionControlNetPipeline.from_pretrained(
        "runwayml/stable-diffusion-v1-5",
        controlnet=controlnet,
        torch_dtype=DTYPE,
    )
    pipe.to(DEVICE)
    MODELS["controlnet"] = pipe
    logger.info("ControlNet loaded")


# ============================================================
# Processing Pipeline
# ============================================================

def extract_person_from_image(image: Image.Image) -> tuple[Image.Image, np.ndarray]:
    """Extract person and generate mask"""
    try:
        from rembg import remove
        result = remove(image)
        mask = np.array(result)[:, :, 3]
        return result, mask
    except Exception as e:
        logger.error(f"Person extraction failed: {e}")
        arr = np.array(image)
        mask = np.ones((arr.shape[0], arr.shape[1]), dtype=np.uint8) * 255
        return image, mask


def extract_garment_segment(image: Image.Image) -> Image.Image:
    """Extract garment from image"""
    try:
        from rembg import remove
        result = remove(image)
        bbox = result.getbbox()
        if bbox:
            result = result.crop(bbox)
        return result
    except Exception as e:
        logger.error(f"Garment extraction failed: {e}")
        return image


def preserve_face(person_img: Image.Image, result_img: Image.Image) -> Image.Image:
    """Preserve face from original person in result"""
    if "face_analysis" not in MODELS:
        return result_img

    try:
        person_arr = np.array(person_img.convert("RGB"))
        result_arr = np.array(result_img.convert("RGB"))

        faces = MODELS["face_analysis"].get(person_arr)
        if not faces:
            return result_img

        face = max(faces, key=lambda f: f.bbox[2] * f.bbox[3])
        x1, y1, x2, y2 = face.bbox.astype(int)

        h, w = result_arr.shape[:2]
        x1 = max(0, min(x1, w - 1))
        y2 = max(0, min(y2, h - 1))
        y1 = max(0, min(y1, h - 1))
        x2 = max(0, min(x2, w - 1))

        if x2 <= x1 or y2 <= y1:
            return result_img

        face_region = person_arr[y1:y2, x1:x2]
        face_h, face_w = face_region.shape[:2]

        result_face_h = int(face_h * 0.9)
        result_face_w = int(face_w * 0.9)
        result_face_x = x1 + (face_w - result_face_w) // 2
        result_face_y = y1 + (face_h - result_face_h) // 2

        result_face_x = max(0, min(result_face_x, w - result_face_w))
        result_face_y = max(0, min(result_face_y, h - result_face_h))

        face_pil = Image.fromarray(face_region)
        face_pil = face_pil.resize((result_face_w, result_face_h), Image.LANCZOS)

        result_pil = Image.fromarray(result_arr)
        result_pil.paste(face_pil, (result_face_x, result_face_y))

        return result_pil

    except Exception as e:
        logger.error(f"Face preservation failed: {e}")
        return result_img


async def process_vton(
    garment_image: Image.Image,
    person_image: Image.Image,
    engine: str = "idm-vton",
    preserve_face_enabled: bool = True,
) -> Image.Image:
    """Process virtual try-on"""
    start_time = time.time()

    if engine not in MODELS:
        if engine == "idm-vton":
            load_idm_vton()
        elif engine == "controlnet":
            load_controlnet()
        else:
            raise ValueError(f"Unknown engine: {engine}")

    person_extracted, mask = extract_person_from_image(person_image)
    garment_segmented = extract_garment_segment(garment_image)

    if engine == "idm-vton":
        pipe = MODELS["idm-vton"]
        result = pipe(
            prompt="fashion photo, high quality, detailed clothing",
            negative_prompt="blurry, low quality, distorted",
            image=person_extracted.convert("RGB"),
            mask_image=Image.fromarray(mask).convert("L"),
            width=768,
            height=1024,
            num_inference_steps=30,
            guidance_scale=7.5,
        ).images[0]
    elif engine == "controlnet":
        pipe = MODELS["controlnet"]
        result = pipe(
            prompt="fashion photo, wearing the garment, high quality",
            negative_prompt="blurry, low quality",
            image=person_extracted.convert("RGB"),
            num_inference_steps=30,
            guidance_scale=7.5,
        ).images[0]
    else:
        raise ValueError(f"Engine {engine} not loaded")

    if preserve_face_enabled:
        result = preserve_face(person_image, result)

    elapsed = time.time() - start_time
    logger.info(f"VTON completed in {elapsed:.2f}s")

    return result


# ============================================================
# FastAPI Server
# ============================================================

def create_server():
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import StreamingResponse
    from pydantic import BaseModel
    import io
    import httpx

    app = FastAPI(title="Drishti VTOE Worker")

    class TryOnRequest(BaseModel):
        job_id: str
        garment_image: str
        person_image: Optional[str] = None
        engine: str = "idm-vton"

    class HealthResponse(BaseModel):
        status: str
        device: str
        vram_gb: float
        models_loaded: list[str]

    @app.get("/health")
    async def health():
        return HealthResponse(
            status="ok",
            device=DEVICE,
            vram_gb=round(VRAM_GB, 1),
            models_loaded=list(MODELS.keys()),
        )

    @app.post("/v1/try-on")
    async def try_on(req: TryOnRequest):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                garment_resp = await client.get(req.garment_image)
                garment_bytes = garment_resp.content
                garment_img = Image.open(io.BytesIO(garment_bytes)).convert("RGB")

                person_img = None
                if req.person_image:
                    person_resp = await client.get(req.person_image)
                    person_img = Image.open(io.BytesIO(person_resp.content)).convert("RGB")
                else:
                    person_img = Image.new("RGB", (768, 1024), (200, 200, 200))

            result = await process_vton(
                garment_img, person_img, req.engine, preserve_face_enabled=True
            )

            buffer = io.BytesIO()
            result.save(buffer, format="PNG", quality=95)
            buffer.seek(0)

            return StreamingResponse(buffer, media_type="image/png")

        except Exception as e:
            logger.error(f"VTON failed: {e}", exc_info=True)
            raise HTTPException(500, detail=str(e))

    @app.post("/v1/load-model/{engine}")
    async def load_model(engine: str):
        if engine == "idm-vton":
            load_idm_vton()
        elif engine == "face":
            load_face_preserver()
        elif engine == "controlnet":
            load_controlnet()
        else:
            raise HTTPException(400, f"Unknown engine: {engine}")
        return {"status": "loaded", "engine": engine}

    @app.get("/v1/gpu-status")
    async def gpu_status():
        if DEVICE == "cuda":
            mem = torch.cuda.mem_get_info()
            return {
                "device": "cuda",
                "name": torch.cuda.get_device_name(0),
                "vram_total_gb": round(mem[1] / (1024**3), 1),
                "vram_used_gb": round((mem[1] - mem[0]) / (1024**3), 1),
                "vram_free_gb": round(mem[0] / (1024**3), 1),
                "models_loaded": list(MODELS.keys()),
            }
        return {"device": "cpu", "models_loaded": list(MODELS.keys())}

    return app


# ============================================================
# Colab Entry Point
# ============================================================

def main():
    logger.info("=== Drishti VTOE Worker Starting ===")
    logger.info(f"GPU: {torch.cuda.get_device_name(0) if DEVICE == 'cuda' else 'None'}")
    logger.info(f"VRAM: {VRAM_GB:.1f}GB")

    # Load face preservation on startup (small, fast)
    try:
        load_face_preserver()
    except Exception as e:
        logger.warning(f"Face model load failed: {e}")

    # Start FastAPI server
    app = create_server()

    import uvicorn
    config = uvicorn.Config(app, host="0.0.0.0", port=8001, log_level="info")
    server = uvicorn.Server(config)

    # Start Cloudflare Tunnel in background
    def start_tunnel():
        subprocess.Popen(
            ["cloudflared", "tunnel", "--url", "http://localhost:8001"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    tunnel_thread = threading.Thread(target=start_tunnel, daemon=True)
    tunnel_thread.start()

    logger.info("Starting server on port 8001...")
    server.run()


if __name__ == "__main__":
    import threading
    main()
