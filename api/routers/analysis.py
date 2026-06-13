from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models.schema import UserAnalysis, User
from api.utils.auth import verify_token

router = APIRouter()


class BodyAnalysisRequest(BaseModel):
    image_url: str | None = None
    height_cm: float | None = None
    weight_kg: float | None = None
    skin_tone: str | None = None
    body_shape: str | None = None


class StyleAnalysisRequest(BaseModel):
    wardrobe_items: list[dict] = []
    preferences: dict = {}
    occasion: str | None = None


class ColorAnalysisRequest(BaseModel):
    skin_tone: str | None = None
    hair_color: str | None = None
    eye_color: str | None = None
    image_url: str | None = None


@router.post("/body")
async def analyze_body(
    req: BodyAnalysisRequest,
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    user_id = None
    if authorization:
        payload = verify_token(authorization.replace("Bearer ", ""))
        if payload:
            user_id = payload["sub"]

    results = {
        "body_shape": req.body_shape or "rectangle",
        "recommended_silhouettes": ["A-line", "straight-cut", "empire-waist"],
        "fit_recommendations": {
            "tops": "regular fit",
            "bottoms": "straight or slim",
            "dresses": "A-line or wrap",
        },
        "color_harmony": "warm" if req.skin_tone in ["warm", "medium", "dark"] else "cool",
    }

    if user_id:
        analysis = UserAnalysis(
            user_id=user_id,
            analysis_type="body",
            input_data=req.model_dump(),
            results=results,
            confidence=0.85,
        )
        db.add(analysis)

    return {"results": results, "confidence": 0.85}


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
    occasions = set()

    for item in req.wardrobe_items:
        cat = item.get("category", "").lower()
        color = item.get("color", "").lower()
        if "jeans" in cat or "tshirt" in cat:
            style_keywords.append("casual")
        if "blazer" in cat or "shirt" in cat:
            style_keywords.append("formal")
        if "ethnic" in cat or "kurta" in cat:
            style_keywords.append("ethnic")
        if "sport" in cat:
            style_keywords.append("athleisure")

    dominant_style = max(set(style_keywords), key=style_keywords.count) if style_keywords else "casual"

    results = {
        "dominant_style": dominant_style,
        "style_personality": {
            "classic": 0.3,
            "bohemian": 0.2,
            "minimalist": 0.3,
            "trendy": 0.2,
        },
        "wardrobe_gaps": ["ethnic-formal", "workwear", "party-wear"],
        "color_palette": ["navy", "white", "beige", "olive"],
        "recommended_brands": ["Allen Solly", "FabIndia", "W", "Suta"],
    }

    if user_id:
        analysis = UserAnalysis(
            user_id=user_id,
            analysis_type="style",
            input_data=req.model_dump(),
            results=results,
            confidence=0.78,
        )
        db.add(analysis)

    return {"results": results, "confidence": 0.78}


@router.post("/color")
async def analyze_color(req: ColorAnalysisRequest):
    skin = req.skin_tone or "medium"

    seasonal = {
        "light": "spring",
        "fair": "spring",
        "medium": "autumn",
        "olive": "autumn",
        "dark": "winter",
        "deep": "winter",
    }

    season = seasonal.get(skin, "autumn")

    palettes = {
        "spring": {
            "best_colors": ["coral", "peach", "warm yellow", "aqua", "cream"],
            "avoid": ["black", "dark purple", "navy"],
            "metals": "gold",
        },
        "summer": {
            "best_colors": ["lavender", "rose", "soft blue", "mint", "mauve"],
            "avoid": ["orange", "bright yellow", "neon"],
            "metals": "silver",
        },
        "autumn": {
            "best_colors": ["rust", "olive", "mustard", "terracotta", "burgundy"],
            "avoid": ["hot pink", "electric blue", "neon"],
            "metals": "gold",
        },
        "winter": {
            "best_colors": ["emerald", "ruby", "cobalt", "black", "white"],
            "avoid": ["pastels", "beige", "orange"],
            "metals": "silver",
        },
    }

    return {
        "season": season,
        "palette": palettes[season],
        "confidence": 0.75,
    }
