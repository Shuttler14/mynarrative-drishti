from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models.schema import User, Session, LookCard
from api.utils.auth import verify_token

logger = logging.getLogger("drishti.reco")
router = APIRouter()


async def _make_images_accessible(recommendations: list[dict]) -> list[dict]:
    """Download inaccessible image URLs (Google Shopping thumbnails) and convert to base64 data URIs.
    
    Google Shopping thumbnails from SerpApi use encrypted-tbn3.gstatic.com which returns
    404 when accessed from servers. This function downloads them locally and converts to
    data URIs so VTON and frontend can use them.
    """
    async def _fetch_one(rec: dict) -> dict:
        url = rec.get("image_url", "")
        if not url or url.startswith("data:") or "/garments/" in url or "myshopify.com" in url or "r2.cloudflarestorage.com" in url:
            return rec
        
        # Only fix Google Shopping CDN URLs (they're inaccessible externally)
        if "gstatic.com" not in url and "google" not in url:
            return rec
            
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    "Referer": "https://www.google.com/",
                })
                if resp.status_code == 200 and len(resp.content) > 100:
                    img_bytes = resp.content
                    ct = "image/jpeg"
                    if img_bytes[:8] == b'\x89PNG\r\n\x1a\n':
                        ct = "image/png"
                    elif img_bytes[:4] == b'RIFF':
                        ct = "image/webp"
                    b64 = base64.b64encode(img_bytes).decode()
                    rec["image_url"] = f"data:{ct};base64,{b64}"
                    logger.info(f"Proxied Google Shopping image ({len(img_bytes)} bytes)")
                else:
                    logger.warning(f"Failed to proxy image: HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"Failed to proxy image {url[:60]}: {e}")
        
        return rec
    
    # Process up to 4 images in parallel (avoid overwhelming)
    tasks = [_fetch_one(rec) for rec in recommendations[:4]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    out = []
    for i, rec in enumerate(recommendations):
        if i < len(results) and isinstance(results[i], dict):
            out.append(results[i])
        else:
            out.append(rec)
    return out


class RecommendRequest(BaseModel):
    session_id: str | None = None
    occasion: str | None = None
    budget_max: float | None = None
    style: str | None = None
    body_profile: dict = {}
    exclude_ids: list[str] = []
    count: int = 6
    # v4.0 additions
    gender: str | None = None
    brands: list[str] = []
    price_segment: str | None = None  # budget | mid | premium | luxury
    weather: dict | None = None
    city: str | None = None
    person_image_url: str | None = None


class ProductRecommendation(BaseModel):
    product_id: str
    title: str
    category: str
    price: float
    currency: str = "INR"
    image_url: str | None = None
    url: str | None = None
    score: float = 0.0
    reason: str = ""


def _get_qdrant():
    """Lazy import Qdrant client."""
    try:
        from api.services.catalog_sync import get_qdrant_client, COLLECTION_NAME
        client = get_qdrant_client()
        # Check collection exists
        collections = client.get_collections().collections
        if COLLECTION_NAME not in [c.name for c in collections]:
            return None
        return client
    except Exception as e:
        logger.warning(f"Qdrant unavailable: {e}")
        return None


def _get_clip_embedding(text: str) -> list[float] | None:
    """Generate CLIP embedding for query text."""
    try:
        from api.services.catalog_sync import generate_embedding
        return generate_embedding(text)
    except Exception as e:
        logger.warning(f"CLIP unavailable: {e}")
        return None


@router.post("/outfits")
async def recommend_outfits(
    req: RecommendRequest,
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Recommend products from ALL marketplaces (Amazon, Myntra, Flipkart) based on user preferences."""
    user_id = None
    if authorization:
        payload = verify_token(authorization.replace("Bearer ", ""))
        if payload:
            user_id = payload["sub"]

    # ── PRIMARY: Search marketplaces directly ──
    try:
        from api.services.marketplace_reco import get_marketplace_recommendations
        
        result = await get_marketplace_recommendations(
            occasion=req.occasion or "",
            style=req.style or "",
            gender=req.gender or "",
            body_profile=req.body_profile or {},
            weather=req.weather or {},
            price_segment=req.price_segment or "",
            city=req.city or "",
            count=req.count or 12,
        )
        
        if result.get("recommendations"):
            # Post-process: download inaccessible image URLs (Google Shopping thumbnails)
            # and convert to base64 data URIs so VTON can use them
            recs = result["recommendations"]
            recs = await _make_images_accessible(recs)
            # Add aliases for frontend compatibility
            result["outfits"] = recs
            result["products"] = recs
            return result
            
    except Exception as e:
        logger.error(f"Marketplace search failed, falling back to Qdrant: {e}")

    # ── FALLBACK: Search from Shopify catalog via Qdrant ──
    try:
        from api.services.catalog_sync import search_similar, generate_embedding

        style = req.style or "casual"
        occasion = req.occasion or ""
        gender_label = ""
        if req.gender:
            gender_term = "women" if req.gender == "female" else "men" if req.gender == "male" else ""
            if gender_term:
                gender_label = f" for {gender_term}"

        query_text = f"{style} clothing{gender_label}"
        if occasion:
            query_text += f" for {occasion}"

        client = None
        try:
            from api.services.catalog_sync import get_qdrant_client, COLLECTION_NAME
            client = get_qdrant_client()
            collections = client.get_collections().collections
            if COLLECTION_NAME not in [c.name for c in collections]:
                client = None
        except Exception:
            client = None

        if client:
            embedding = generate_embedding(query_text)
            if embedding:
                results = search_similar(client, embedding, limit=req.count * 2)
                
                # Configurable price tiers (INR)
                price_ranges = {
                    "budget": int(os.getenv("PRICE_TIER_BUDGET", "1500")),
                    "mid": int(os.getenv("PRICE_TIER_MID", "5000")),
                    "premium": int(os.getenv("PRICE_TIER_PREMIUM", "15000")),
                    "luxury": int(os.getenv("PRICE_TIER_LUXURY", "999999")),
                }
                budget = price_ranges.get(req.price_segment, 5000) if req.price_segment else 5000
                brand_filter = [b.lower() for b in req.brands] if req.brands else []

                recommendations = []
                for r in results:
                    price = r.get("price", 0)
                    if price > budget:
                        continue
                    if brand_filter:
                        title_lower = (r.get("title", "") or "").lower()
                        brand_vendor = (r.get("vendor", "") or "").lower()
                        if not any(b in title_lower or b in brand_vendor for b in brand_filter):
                            continue
                    recommendations.append({
                        "product_id": r.get("shopify_id", ""),
                        "title": r.get("title", ""),
                        "category": r.get("category", ""),
                        "price": price,
                        "currency": r.get("currency", "INR"),
                        "image_url": r.get("image_url", ""),
                        "url": r.get("url", ""),
                        "score": r.get("score", 0),
                        "source": "shopify",
                        "reason": f"Matches your {style} style",
                    })
                    if len(recommendations) >= req.count:
                        break

                if recommendations:
                    return {"recommendations": recommendations, "count": len(recommendations), "source": "vector_search"}

    except Exception as e:
        logger.error(f"Qdrant fallback failed: {e}")

    # ── FINAL FALLBACK: Empty ──
    return {"recommendations": [], "count": 0, "source": "none"}


@router.get("/trending")
async def trending_items(
    category: str | None = None,
    limit: int = Query(10, ge=1, le=50),
):
    """Get trending products from Qdrant (sorted by recency)."""
    client = _get_qdrant()
    if client:
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue

            # Use scroll to list all products by recency (no vector search needed)
            results = client.scroll(
                collection_name="shopify_products",
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )[0]

            trending = []
            for r in results:
                payload = r.payload or {}
                trending.append({
                    "product_id": payload.get("shopify_id", ""),
                    "title": payload.get("title", ""),
                    "category": payload.get("category", ""),
                    "price": payload.get("price", 0),
                    "image_url": payload.get("image_url", ""),
                    "url": payload.get("url", ""),
                })

            if trending:
                return {"trending": trending, "source": "vector_store"}

        except Exception as e:
            logger.error(f"Trending query failed: {e}")

    return {"trending": [], "source": "none"}


@router.get("/similar/{product_id}")
async def similar_products(
    product_id: str,
    limit: int = Query(6, ge=1, le=20),
):
    """Find similar products using vector similarity."""
    client = _get_qdrant()
    if not client:
        return {"similar_products": [], "count": 0, "source": "none"}

    try:
        from qdrant_client.models import PointIdsList
        import hashlib

        # Get the product's embedding
        point_id = hashlib.md5(product_id.encode()).hexdigest()

        # Retrieve the point to get its vector
        points = client.retrieve(
            collection_name="shopify_products",
            ids=[point_id],
        )

        if not points:
            return {"similar_products": [], "count": 0, "source": "none"}

        product = points[0]

        # Search for similar products using the vector
        from api.services.catalog_sync import search_similar

        results = search_similar(
            client,
            product.vector,
            limit=limit + 1,  # +1 to exclude self
        )

        # Filter out the original product
        similar = [
            {
                "product_id": r.get("shopify_id", ""),
                "title": r.get("title", ""),
                "category": r.get("category", ""),
                "price": r.get("price", 0),
                "image_url": r.get("image_url", ""),
                "url": r.get("url", ""),
                "score": r.get("score", 0),
            }
            for r in results
            if r.get("shopify_id") != product_id
        ][:limit]

        return {"similar_products": similar, "count": len(similar), "source": "vector_search"}

    except Exception as e:
        logger.error(f"Similar products query failed: {e}")
        return {"similar_products": [], "count": 0, "source": "error"}
