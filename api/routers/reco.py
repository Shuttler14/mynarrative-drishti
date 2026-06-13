from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models.schema import User, Session, LookCard
from api.utils.auth import verify_token

router = APIRouter()


class RecommendRequest(BaseModel):
    session_id: str | None = None
    occasion: str | None = None
    budget_max: float | None = None
    style: str | None = None
    body_profile: dict = {}
    exclude_ids: list[str] = []
    count: int = 6


class OutfitRecommendation(BaseModel):
    look_id: str
    title: str
    garments: list[dict]
    total_price: float
    occasion: str | None = None
    confidence: float = 0.0
    vto_available: bool = False
    styling_tips: list[str] = []


@router.post("/outfits")
async def recommend_outfits(
    req: RecommendRequest,
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    user_id = None
    if authorization:
        payload = verify_token(authorization.replace("Bearer ", ""))
        if payload:
            user_id = payload["sub"]

    user = None
    if user_id:
        user = await db.get(User, user_id)

    body_shape = "rectangle"
    style = req.style or "casual"
    occasion = req.occasion or "casual"
    budget = req.budget_max or 5000

    outfit_templates = {
        "casual": [
            {"title": "Weekend Casual", "garments": [{"type": "tshirt", "brand": "H&M"}, {"type": "jeans", "brand": "Levi's"}], "price_range": (800, 2000)},
            {"title": "Street Style", "garments": [{"type": "oversized-tee", "brand": "Zara"}, {"type": "cargo-pants", "brand": "Roadster"}], "price_range": (1200, 3000)},
        ],
        "formal": [
            {"title": "Office Ready", "garments": [{"type": "shirt", "brand": "Allen Solly"}, {"type": "trousers", "brand": "Van Heusen"}], "price_range": (2000, 5000)},
            {"title": "Boardroom", "garments": [{"type": "blazer", "brand": "Park Avenue"}, {"type": "shirt", "brand": "Louis Philippe"}], "price_range": (4000, 8000)},
        ],
        "ethnic": [
            {"title": "Festive Elegance", "garments": [{"type": "kurta", "brand": "FabIndia"}, {"type": "churidar", "brand": "W"}], "price_range": (1500, 4000)},
            {"title": "Wedding Guest", "garments": [{"type": "sherwani", "brand": "Manyavar"}, {"type": "dupatta", "brand": "Biba"}], "price_range": (3000, 8000)},
        ],
        "party": [
            {"title": "Night Out", "garments": [{"type": "polo", "brand": "Tommy Hilfiger"}, {"type": "chinos", "brand": "US Polo"}], "price_range": (2000, 5000)},
        ],
    }

    templates = outfit_templates.get(style, outfit_templates["casual"])
    recommendations = []

    for i, template in enumerate(templates):
        avg_price = sum(template["price_range"]) / 2
        if avg_price <= budget:
            recommendations.append(
                OutfitRecommendation(
                    look_id=f"rec-{i}",
                    title=template["title"],
                    garments=template["garments"],
                    total_price=avg_price,
                    occasion=occasion,
                    confidence=0.85 - (i * 0.05),
                    vto_available=True,
                    styling_tips=[
                        f"Pair with {template['garments'][0]['brand']} accessories",
                        "Add a watch for a polished look",
                    ],
                ).model_dump()
            )

    if user_id and req.session_id:
        session = await db.get(Session, req.session_id)
        if session:
            session.recommendations = recommendations

    return {"recommendations": recommendations, "count": len(recommendations)}


@router.get("/trending")
async def trending_items(
    category: str | None = None,
    limit: int = Query(10, ge=1, le=50),
):
    trending = [
        {"name": "Oversized Tees", "category": "tops", "trend_score": 0.95, "brands": ["H&M", "Zara", "Bershka"]},
        {"name": "Wide-Leg Pants", "category": "bottoms", "trend_score": 0.92, "brands": ["Mango", "Forever 21"]},
        {"name": "Ethnic Fusion", "category": "ethnic", "trend_score": 0.90, "brands": ["FabIndia", "W", "Anita Dongre"]},
        {"name": "Chunky Sneakers", "category": "footwear", "trend_score": 0.88, "brands": ["Nike", "New Balance"]},
        {"name": "Minimal Watches", "category": "accessories", "trend_score": 0.85, "brands": ["Fossil", "Daniel Wellington"]},
    ]

    if category:
        trending = [t for t in trending if t["category"] == category]

    return {"trending": trending[:limit]}


@router.get("/similar/{product_id}")
async def similar_products(
    product_id: str,
    limit: int = Query(6, ge=1, le=20),
):
    return {"similar_products": [], "count": 0}
