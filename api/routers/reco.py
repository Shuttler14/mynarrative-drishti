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
from api.services.reco_version import RECO_VERSION, version_info
from api.utils.auth import verify_token

logger = logging.getLogger("drishti.reco")
router = APIRouter()


async def _make_images_accessible(recommendations: list[dict]) -> list[dict]:
    """Make images accessible for VTON and frontend.
    
    Strategy:
    - Google Shopping thumbnails (gstatic.com) are TINY (~100px) — too small for VTON.
      We download and convert them to data URIs so the frontend can display them,
      but we also try to get the actual product image from the retailer for VTON.
    - Marketplace URLs (Amazon, Myntra, etc.) are already accessible — pass through.
    - Data URIs are already accessible — pass through.
    """
    async def _fetch_one(rec: dict) -> dict:
        url = rec.get("image_url", "")
        if not url or url.startswith("data:") or "/garments/" in url or "myshopify.com" in url or "r2.cloudflarestorage.com" in url:
            return rec
        
        # Marketplace URLs are accessible — keep as-is for VTON
        marketplace_domains = ["myntra.com", "myntassets.com", "ajio.com", "jioimages.com",
                               "amazon.in", "amazon.com", "flipkart.com", "meesho.com"]
        if any(d in url for d in marketplace_domains):
            return rec
            
        # Google Shopping CDN URLs — download for frontend display
        if "gstatic.com" in url or "google" in url:
            try:
                async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
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
    
    # Process all images with bounded concurrency (avoid overwhelming)
    sem = asyncio.Semaphore(5)

    async def _guarded(rec: dict) -> dict:
        async with sem:
            return await _fetch_one(rec)

    results = await asyncio.gather(*[_guarded(rec) for rec in recommendations], return_exceptions=True)
    
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
    style: str | None = None
    body_profile: dict = {}
    exclude_ids: list[str] = []
    count: int = 6
    # v4.0 additions
    gender: str | None = None
    brands: list[str] = []
    price_segment: str | None = None  # legacy: budget | mid | premium | luxury
    price_range: str | None = None  # legacy: under_1500 | 1500_3500 | above_3500
    # Price intelligence (new)
    budget_level: str | None = None  # value | contemporary | premium | luxury
    budget_min: float | None = None  # continuous slider
    budget_max: float | None = None
    budget_point: float | None = None  # "around my budget"
    budget_tolerance_pct: float = 20.0
    weather: dict | None = None
    city: str | None = None
    person_image_url: str | None = None
    # Fashion DNA / preference overrides (Sprint 4)
    styles: list[str] = []
    wear: list[str] = []
    vibe: list[str] = []
    look_categories: list[str] | None = None


class BrandsRequest(BaseModel):
    occasion: str | None = None
    style: str | None = None
    gender: str | None = None
    brands: list[str] = []
    price_segment: str | None = None
    price_range: str | None = None
    budget_level: str | None = None
    budget_min: float | None = None
    budget_max: float | None = None
    budget_point: float | None = None
    budget_tolerance_pct: float = 20.0
    city: str | None = None


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
    x_shopify_customer_id: str | None = Header(None, alias="X-Shopify-Customer-Id"),
    db: AsyncSession = Depends(get_db),
):
    """Recommend products from ALL marketplaces (Amazon, Myntra, Flipkart) based on user preferences."""
    from api.utils.identity import load_style_prefs, normalize_wear_weights, resolve_user

    user = await resolve_user(
        db,
        authorization=authorization,
        shopify_customer_id=x_shopify_customer_id,
        create_if_missing=False,
    )
    user_id = str(user.id) if user else None

    # Load contextual Fashion DNA prefs (soft preferences — hard gates untouched)
    prefs: dict | None = None
    if user_id:
        prefs = await load_style_prefs(db, user_id, req.occasion or "")
    # Request-level wear/vibe override DB for this turn (wizard chips)
    if req.wear:
        prefs = dict(prefs or {})
        prefs["wear_weights"] = normalize_wear_weights(req.wear)
    if req.vibe:
        prefs = dict(prefs or {})
        prefs["vibes"] = list(req.vibe)
    if req.styles:
        prefs = dict(prefs or {})
        prefs["styles"] = list(req.styles)

    style = req.style or ""
    if not style and req.styles:
        style = req.styles[0] or ""

    # Merge DB style_profile brands if request didn't send any
    brands = list(req.brands or [])
    if not brands and user and isinstance(user.style_profile, dict):
        brands = list(user.style_profile.get("brands") or [])

    # ── PRIMARY: Search marketplaces directly ──
    try:
        from api.services.marketplace_reco import get_marketplace_recommendations

        result = await get_marketplace_recommendations(
            occasion=req.occasion or "",
            style=style,
            gender=req.gender or "",
            body_profile=req.body_profile or {},
            weather=req.weather or {},
            price_segment=req.price_segment or "",
            price_range=req.price_range or "",
            city=req.city or "",
            brands=brands or [],
            count=req.count or 12,
            budget_level=req.budget_level or "",
            budget_min=req.budget_min,
            budget_max=req.budget_max,
            budget_point=req.budget_point,
            budget_tolerance_pct=req.budget_tolerance_pct,
            prefs=prefs,
            user_id=user_id,
        )
        
        if result.get("recommendations"):
            # Post-process: download inaccessible image URLs (Google Shopping thumbnails)
            # and convert to base64 data URIs so VTON can use them
            recs = result["recommendations"]
            recs = await _make_images_accessible(recs)
            # Add aliases for frontend compatibility
            result["outfits"] = recs
            result["products"] = recs
            result["reco_version"] = RECO_VERSION

            # Fire-and-forget impression event (Fashion DNA loop)
            if user_id:
                try:
                    from api.models.schema import UserEvent
                    from uuid import UUID
                    try:
                        uid = UUID(str(user_id))
                    except (TypeError, ValueError):
                        uid = None
                    if uid is not None:
                        looks_meta = [
                            {"id": lk.get("id"), "archetype": lk.get("archetype"), "why": lk.get("why")}
                            for lk in (result.get("looks") or [])[:3]
                        ]
                        db.add(UserEvent(
                            user_id=uid,
                            event_type="impression",
                            occasion=req.occasion,
                            payload={
                                "count": len(recs),
                                "looks": looks_meta,
                                "dress_code": (result.get("outfit_validation") or {}).get("dress_code"),
                            },
                            weight=0.1,
                        ))
                        await db.flush()
                except Exception:
                    logger.debug("impression event failed", exc_info=True)

            return result
            
    except Exception as e:
        logger.error(f"Marketplace search failed, falling back to Qdrant: {e}")

    # ── FALLBACK: Search from Shopify catalog via Qdrant ──
    # Must run through the SAME fashion gates as marketplace results —
    # otherwise office/western gets a random hoodie wall (no occasion,
    # no formal signals, no slot tags).
    try:
        from api.services.catalog_sync import search_similar, generate_embedding
        from api.services.fashion_engine import build_intent, enrich_product, hard_filter

        style = req.style or ""
        if not style and req.styles:
            style = req.styles[0] or ""
        occasion = req.occasion or ""
        # wear is a chip list ["western","formal"] — normalise to weights
        wear: dict = {}
        if req.wear:
            try:
                from api.utils.identity import normalize_wear_weights
                wear = normalize_wear_weights(req.wear) or {}
            except Exception:
                wear = {str(w).lower(): 1.0 for w in req.wear if w}

        gender_term = ""
        if req.gender in ("female", "woman", "girl", "f"):
            gender_term = "women"
        elif req.gender in ("male", "man", "m"):
            gender_term = "men"

        # Query text must match what we actually want to wear — formal
        # signals for office western, ethnic words only if user asked.
        query_bits = [style or "clothing"]
        if occasion:
            query_bits.append(occasion)
        if gender_term:
            query_bits.append(gender_term)
        if occasion in ("office", "work", "interview"):
            western = float(wear.get("western") or 0) >= 0.75 and float(wear.get("indian") or 0) < 0.75
            if western:
                query_bits.extend(["formal", "shirt", "trousers", "blazer", "office wear"])
            else:
                query_bits.extend(["office wear", "formal"])
        query_text = " ".join(str(q) for q in query_bits if q)

        # Same intent object the marketplace path uses → same hard gates
        intent = build_intent(
            occasion=occasion,
            style=style,
            gender=req.gender or "",
            price_segment=req.price_segment or "",
            price_range=req.price_range or "",
            weather=req.weather or {},
            city=req.city or "",
            brands=req.brands or [],
            budget_level=req.budget_level or "",
            budget_min=req.budget_min,
            budget_max=req.budget_max,
            budget_point=req.budget_point,
            budget_tolerance_pct=req.budget_tolerance_pct or 20.0,
            user_prefs={"wear_weights": wear} if wear else None,
        )
        band = intent.get("band")
        if not band and (req.budget_min is not None or req.budget_max is not None):
            band = (req.budget_min or 0, req.budget_max or 10**9)

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
                results = search_similar(client, embedding, limit=max(req.count * 4, 24))

                candidates = []
                for r in results:
                    p = {
                        "title": r.get("title", ""),
                        "price": r.get("price", 0),
                        "image_url": r.get("image_url", ""),
                        "url": r.get("url", ""),
                        "category": r.get("category", ""),
                        "vendor": r.get("vendor", ""),
                        "shopify_id": r.get("shopify_id", ""),
                        "score": r.get("score", 0),
                        "source": "shopify",
                    }
                    enriched = enrich_product(p)
                    if enriched.get("price") and not band:
                        pass
                    candidates.append(enriched)

                # Apply the same occasion/gender/formal/budget gates
                filtered = hard_filter(candidates, intent, band)
                if not filtered and band:
                    filtered = hard_filter(candidates, intent, None)

                recommendations = []
                for p in filtered:
                    recommendations.append({
                        "product_id": p.get("shopify_id", ""),
                        "title": p.get("title", ""),
                        "category": (p.get("fashion") or {}).get("category") or p.get("category", ""),
                        "slot": (p.get("fashion") or {}).get("slot", ""),
                        "price": p.get("price", 0),
                        "currency": "INR",
                        "image_url": p.get("image_url", ""),
                        "url": p.get("url", ""),
                        "score": round(float(p.get("_final") or p.get("score") or 0), 3),
                        "source": "vector_search",
                        "reason": f"Matches your {style or occasion or 'style'} style",
                    })
                    if len(recommendations) >= req.count:
                        break

                if recommendations:
                    return {"recommendations": recommendations, "count": len(recommendations), "source": "vector_search", "reco_version": RECO_VERSION}

    except Exception as e:
        logger.error(f"Qdrant fallback failed: {e}")

    # ── FINAL FALLBACK: Empty ──
    return {"recommendations": [], "count": 0, "source": "none", "reco_version": RECO_VERSION}


@router.get("/version")
async def reco_version():
    """Deployed recommender version — used by bench/ for scorecard traceability."""
    return version_info()


@router.post("/brands")
async def recommend_brands(req: BrandsRequest):
    """Algorithmically ranked brands for the current context + budget intelligence.

    Powers the live "Brands you'll probably like" panel in the wizard.
    """
    try:
        from api.services.marketplace_reco import get_marketplace_recommendations

        result = await get_marketplace_recommendations(
            occasion=req.occasion or "",
            style=req.style or "",
            gender=req.gender or "",
            price_segment=req.price_segment or "",
            price_range=req.price_range or "",
            city=req.city or "",
            brands=req.brands or [],
            count=12,
            budget_level=req.budget_level or "",
            budget_min=req.budget_min,
            budget_max=req.budget_max,
            budget_point=req.budget_point,
            budget_tolerance_pct=req.budget_tolerance_pct,
        )
        return {
            "brands": result.get("brands", []),
            "budget_intelligence": result.get("budget_intelligence", {}),
            "source": result.get("source", "none"),
        }
    except Exception as e:
        logger.error(f"Brand ranking failed: {e}")
        return {"brands": [], "budget_intelligence": {}, "source": "error"}


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
