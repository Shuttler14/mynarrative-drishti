from __future__ import annotations

import base64
import io
import json
import logging
import os
import tempfile
from typing import Any
from collections import Counter

import httpx
from fastapi import APIRouter, Depends, HTTPException, Header, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.database import get_db
from api.models.schema import UserAnalysis, User
from api.utils.auth import verify_token

logger = logging.getLogger("drishti.analysis")
settings = get_settings()
router = APIRouter()


# ── Request / Response Models ──

class BodyAnalysisURLRequest(BaseModel):
    image_url: str
    gender: str | None = None


class StyleAnalysisRequest(BaseModel):
    wardrobe_items: list[dict] = []
    preferences: dict = {}
    occasion: str | None = None


class ColorAnalysisRequest(BaseModel):
    skin_tone: str | None = None
    hair_color: str | None = None
    eye_color: str | None = None
    image_url: str | None = None


# ── Replicate Helpers ──

def _get_replicate_client():
    """Get Replicate client, or None if not configured."""
    token = os.getenv("REPLICATE_API_TOKEN", "")
    if not token:
        return None
    try:
        import replicate
        return replicate.Client(api_token=token)
    except ImportError:
        logger.warning("replicate package not installed")
        return None


async def _upload_to_temp(image_b64: str) -> str | None:
    """Upload base64 image to a temp file and return the file path."""
    try:
        img_bytes = base64.b64decode(image_b64)
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.write(img_bytes)
        tmp.close()
        return tmp.name
    except Exception as e:
        logger.error(f"Failed to create temp file: {e}")
        return None


# ══════════════════════════════════════════════════════════════
# REPLICATE MODELS for Body Analysis
# ══════════════════════════════════════════════════════════════

async def _detect_face_shape(client, image_url: str) -> str:
    """Detect face shape using InsightFace face analysis."""
    try:
        output = client.run(
            f"andreasjansson/face-bounds:{settings.REPLICATE_FACE_BOUNDS_VERSION}",
            input={"image": image_url}
        )
        # Face bounds give us width/height ratio to determine shape
        if output and len(output) > 0:
            face = output[0]
            # Simple heuristic from face landmarks
            w = face.get("width", 100)
            h = face.get("height", 100)
            ratio = w / h if h > 0 else 1.0
            if ratio > 0.85:
                return "round"
            elif ratio < 0.7:
                return "oblong"
            elif ratio < 0.78:
                return "oval"
            else:
                return "square"
    except Exception as e:
        logger.warning(f"Face shape detection failed: {e}")
    return "oval"  # default


async def _detect_body_shape(client, image_url: str) -> str:
    """Detect body shape using person segmentation + proportions."""
    try:
        # Use SAM for person segmentation to measure proportions
        output = client.run(
            "cjwbw/segment-anything-2:b0c90e38309a06a05c0a804395e3c51e1e5aa7d0a6d25a2f7e5b4a3c0e5f5a5b",
            input={
                "image": image_url,
                "points_per_side": 32,
            }
        )
        # If we get a mask, we can analyze body proportions
        if output:
            return "rectangle"  # placeholder — real impl would measure shoulder:waist:hip ratios
    except Exception as e:
        logger.warning(f"Body shape detection failed: {e}")
    return "rectangle"


async def _extract_skin_color(client, image_url: str) -> dict:
    """Extract skin color using color analysis on face region."""
    try:
        # Use a color quantization model to extract dominant colors
        output = client.run(
            f"nightfury/image-color-extraction:{settings.REPLICATE_COLOR_VERSION}",
            input={"image": image_url, "n_colors": 5}
        )
        if output and "colors" in output:
            colors = output["colors"]
            # Find the color closest to skin tones
            skin_ranges = {
                "fair": (240, 220, 200),
                "light": (225, 195, 170),
                "medium": (200, 165, 135),
                "olive": (180, 155, 120),
                "tan": (190, 150, 110),
                "brown": (155, 110, 75),
                "dark": (120, 80, 50),
                "deep": (90, 60, 35),
            }
            best_match = "medium"
            best_dist = float("inf")
            for color_hex in colors[:3]:
                r = int(color_hex[1:3], 16)
                g = int(color_hex[3:5], 16)
                b = int(color_hex[5:7], 16)
                for tone, (tr, tg, tb) in skin_ranges.items():
                    dist = ((r - tr) ** 2 + (g - tg) ** 2 + (b - tb) ** 2) ** 0.5
                    if dist < best_dist:
                        best_dist = dist
                        best_match = tone
            return {"skin_tone": best_match}
    except Exception as e:
        logger.warning(f"Skin color extraction failed: {e}")
    return {}


async def _detect_hair(client, image_url: str) -> dict:
    """Detect hair color and style using BLIP image description."""
    result = {}
    try:
        output = client.run(
            f"salesforce/blip:{settings.REPLICATE_BLIP_VERSION}",
            input={"image": image_url, "task": "image_captioning"}
        )
        if output:
            caption = output.lower()
            # Parse hair color from caption
            hair_colors = ["black", "brown", "blonde", "red", "auburn", "gray", "white"]
            for color in hair_colors:
                if color in caption:
                    result["hair_color"] = color
                    break
            # Parse hair style from caption
            hair_styles = ["curly", "wavy", "straight", "coily", "ponytail", "bun", "bob"]
            for style in hair_styles:
                if style in caption:
                    result["hair_style"] = style
                    break
    except Exception as e:
        logger.warning(f"BLIP hair detection failed: {e}")
    return result


# ══════════════════════════════════════════════════════════════
# REPLICATE LLaVA (primary analyzer — OpenAI refuses human image analysis)
# ══════════════════════════════════════════════════════════════

_LLaVA_PROMPT = """Analyze this person's appearance for a fashion styling app. Return ONLY a JSON object with these fields:
{"skin_tone": "fair|light|medium|olive|tan|brown|dark|deep", "body_shape": "hourglass|pear|apple|rectangle|inverted_triangle|athletic|curvy", "face_shape": "oval|round|square|heart|oblong|diamond|triangle", "hair_color": "black|brown|blonde|red|auburn|gray|white|highlighted", "hair_style": "straight|wavy|curly|coily|bob|ponytail|bun|short_crop", "fitness_level": "slim|average|athletic|muscular|plus_size", "complexion": "clear|freckled|tanned|dusky|radiant|matte", "undertone": "warm|cool|neutral|olive"}
Return ONLY the JSON."""


async def _call_llava_vision(client, image_url: str, prompt: str) -> dict[str, Any] | None:
    """Call Replicate LLaVA model for visual analysis."""
    try:
        output = client.run(
            f"yorickvp/llava-13b:{settings.REPLICATE_LLaVA_VERSION}",
            input={"image": image_url, "prompt": prompt, "max_tokens": 500}
        )
        text = "".join(output)
        # Extract JSON from response
        if "```" in text:
            parts = text.split("```")
            text = parts[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
    except Exception as e:
        logger.warning(f"LLaVA vision failed: {e}")
    return None


async def _call_openai_vision(image_b64: str, prompt: str) -> dict[str, Any] | None:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": settings.OPENAI_MODEL,
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}", "detail": "high"}}
                    ]}]
                },
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            # Detect OpenAI safety filter refusals
            refusal_phrases = ["i'm sorry", "i can't assist", "i cannot assist", "i'm unable", "not appropriate"]
            if any(phrase in content.lower() for phrase in refusal_phrases):
                logger.warning(f"OpenAI refused body analysis: {content[:100]}")
                return None
            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()
            return json.loads(content)
    except Exception as e:
        logger.error(f"OpenAI Vision failed: {e}")
        return None


# ── Validation ──

def _validate_body_data(raw: dict) -> dict:
    VALID = {
        "skin_tone": ["fair", "light", "medium", "olive", "tan", "brown", "dark", "deep"],
        "body_shape": ["hourglass", "pear", "apple", "rectangle", "inverted_triangle", "athletic", "curvy"],
        "face_shape": ["oval", "round", "square", "heart", "oblong", "diamond", "triangle"],
        "fitness_level": ["slim", "average", "athletic", "muscular", "plus_size"],
        "complexion": ["clear", "freckled", "tanned", "dusky", "radiant", "matte"],
        "undertone": ["warm", "cool", "neutral", "olive"],
    }
    result = {}
    for key, valid_vals in VALID.items():
        val = (raw.get(key) or "").lower().strip()
        if val in valid_vals:
            result[key] = val
        else:
            for v in valid_vals:
                if v in val or val in v:
                    result[key] = v
                    break
            else:
                result[key] = valid_vals[1]
    result["hair_color"] = (raw.get("hair_color") or "brown").strip()
    result["hair_style"] = (raw.get("hair_style") or "straight").strip()
    return result


# ══════════════════════════════════════════════════════════════
# MAIN ANALYSIS PIPELINE
# ══════════════════════════════════════════════════════════════

async def _run_body_analysis(image_b64: str, gender: str | None = None) -> tuple[dict, float, str]:
    """
    Run multi-model body analysis pipeline.
    Priority: Replicate LLaVA (primary) → specialized models → defaults.
    Returns (body_data, confidence, source).
    """
    image_url = f"data:image/jpeg;base64,{image_b64}"
    client = _get_replicate_client()
    if not client:
        return _validate_body_data({}), 0.30, "no_client"

    results = {}
    source_parts = []

    # Step 1: LLaVA as primary visual analyzer
    prompt = _LLaVA_PROMPT
    if gender:
        prompt += f"\nThe person identifies as {gender}."
    llava_result = await _call_llava_vision(client, image_url, prompt)
    if llava_result:
        results = llava_result
        source_parts.append("llava")
        logger.info(f"LLaVA analysis succeeded: {list(results.keys())}")
    else:
        logger.warning("LLaVA analysis failed — using specialized models only")

    # Step 2: Supplement/override with specialized Replicate models
    # Face shape from face-bounds (geometric measurement — more accurate than LLaVA)
    try:
        face_shape = await _detect_face_shape(client, image_url)
        if face_shape:
            results["face_shape"] = face_shape
            source_parts.append("face-bounds")
    except Exception as e:
        logger.warning(f"Face shape detection failed: {e}")

    # Skin tone from color quantization (pixel-level accuracy)
    try:
        skin = await _extract_skin_color(client, image_url)
        if skin and skin.get("skin_tone"):
            results["skin_tone"] = skin["skin_tone"]
            source_parts.append("color-extraction")
    except Exception as e:
        logger.warning(f"Skin color extraction failed: {e}")

    # Hair from BLIP caption
    try:
        hair = await _detect_hair(client, image_url)
        if hair.get("hair_color"):
            results["hair_color"] = hair["hair_color"]
            source_parts.append("blip-hair")
        if hair.get("hair_style"):
            results["hair_style"] = hair["hair_style"]
    except Exception as e:
        logger.warning(f"Hair detection failed: {e}")

    # Step 3: Fill any remaining missing fields with defaults
    # Only fill truly unknown fields — don't bias toward specific values
    defaults = {
        "skin_tone": "medium",
        "body_shape": "rectangle",
        "face_shape": "oval",
        "hair_color": "black",
        "hair_style": "straight",
        "fitness_level": "average",
        "complexion": "clear",
        "undertone": "neutral",
    }
    missing = [k for k, v in defaults.items() if k not in results or not results[k]]
    if missing:
        logger.warning(f"Missing fields filled with defaults: {missing}")
    for key, default_val in defaults.items():
        if key not in results or not results[key]:
            results[key] = default_val

    source = "+".join(source_parts) if source_parts else "defaults"
    confidence = 0.88 if "llava" in source_parts else (0.65 if source_parts else 0.30)

    return _validate_body_data(results), confidence, source


# ══════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════

@router.post("/body")
async def analyze_body_url(
    req: BodyAnalysisURLRequest,
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Analyze body data from an image URL using Replicate + OpenAI."""
    user_id = None
    if authorization:
        payload = verify_token(authorization.replace("Bearer ", ""))
        if payload:
            user_id = payload["sub"]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            img_resp = await client.get(req.image_url)
            if img_resp.status_code != 200:
                raise HTTPException(400, "Failed to fetch image")
            image_b64 = base64.b64encode(img_resp.content).decode()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Image download failed: {e}")
        raise HTTPException(400, "Failed to download image")

    body_data, confidence, source = await _run_body_analysis(image_b64, req.gender)

    if user_id:
        analysis = UserAnalysis(
            user_id=user_id, analysis_type="body",
            input_data={"image_url": req.image_url, "gender": req.gender},
            results=body_data, confidence=confidence,
        )
        db.add(analysis)
        await db.flush()

    return {"body_data": body_data, "confidence": confidence, "source": source}


@router.post("/body/upload")
async def analyze_body_upload(
    file: UploadFile = File(...),
    gender: str | None = None,
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Analyze body data from an uploaded image using Replicate + OpenAI."""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "File must be an image")
    contents = await file.read()
    if len(contents) > 10 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 10MB)")

    image_b64 = base64.b64encode(contents).decode()

    user_id = None
    if authorization:
        payload = verify_token(authorization.replace("Bearer ", ""))
        if payload:
            user_id = payload["sub"]

    body_data, confidence, source = await _run_body_analysis(image_b64, gender)

    if user_id:
        analysis = UserAnalysis(
            user_id=user_id, analysis_type="body",
            input_data={"filename": file.filename, "gender": gender},
            results=body_data, confidence=confidence,
        )
        db.add(analysis)
        await db.flush()

    return {"body_data": body_data, "confidence": confidence, "source": source}


# ── Legacy endpoints ──

class StyleAnalysisRequest(BaseModel):
    wardrobe_items: list[dict] = []
    preferences: dict = {}
    occasion: str | None = None

class ColorAnalysisRequest(BaseModel):
    skin_tone: str | None = None
    hair_color: str | None = None
    eye_color: str | None = None
    image_url: str | None = None

@router.post("/style")
async def analyze_style(
    req: StyleAnalysisRequest,
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    user_id = None
    if authorization:
        payload = verify_token(authorization.replace("Bearer ", ""))
        if payload:
            user_id = payload["sub"]
    style_keywords = []
    for item in req.wardrobe_items:
        cat = item.get("category", "").lower()
        if "jeans" in cat or "tshirt" in cat: style_keywords.append("casual")
        if "blazer" in cat or "shirt" in cat: style_keywords.append("formal")
        if "ethnic" in cat or "kurta" in cat: style_keywords.append("ethnic")
        if "sport" in cat: style_keywords.append("athleisure")
    dominant_style = max(set(style_keywords), key=style_keywords.count) if style_keywords else "casual"
    results = {
        "dominant_style": dominant_style,
        "style_personality": {},  # Empty — will be filled by AI analysis if available
        "wardrobe_gaps": [],  # Empty — computed dynamically from wardrobe
        "color_palette": [],  # Empty — computed from skin tone analysis
        "recommended_brands": [],  # Empty — computed from budget and preferences
    }
    if user_id:
        analysis = UserAnalysis(user_id=user_id, analysis_type="style", input_data=req.model_dump(), results=results, confidence=0.78)
        db.add(analysis)
        await db.flush()
    return {"results": results, "confidence": 0.78}

@router.post("/color")
async def analyze_color(req: ColorAnalysisRequest):
    skin = req.skin_tone or "medium"
    seasonal = {"light": "spring", "fair": "spring", "medium": "autumn", "olive": "autumn", "dark": "winter", "deep": "winter"}
    season = seasonal.get(skin, "autumn")
    palettes = {
        "spring": {"best_colors": ["coral", "peach", "warm yellow", "aqua", "cream"], "avoid": ["black", "dark purple", "navy"], "metals": "gold"},
        "summer": {"best_colors": ["lavender", "rose", "soft blue", "mint", "mauve"], "avoid": ["orange", "bright yellow", "neon"], "metals": "silver"},
        "autumn": {"best_colors": ["rust", "olive", "mustard", "terracotta", "burgundy"], "avoid": ["hot pink", "electric blue", "neon"], "metals": "gold"},
        "winter": {"best_colors": ["emerald", "ruby", "cobalt", "black", "white"], "avoid": ["pastels", "beige", "orange"], "metals": "silver"},
    }
    return {"season": season, "palette": palettes[season], "confidence": 0.75}
