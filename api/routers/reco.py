from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models.schema import User, Session, LookCard
from api.utils.auth import verify_token

logger = logging.getLogger("drishti.reco")
router = APIRouter()


class RecommendRequest(BaseModel):
    session_id: str | None = None
    occasion: str | None = None
    budget_max: float | None = None
    style: str | None = None
    body_profile: dict = {}
    exclude_ids: list[str] = []
    count: int = 6


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
    """Recommend products based on user preferences using vector search."""
    user_id = None
    if authorization:
        payload = verify_token(authorization.replace("Bearer ", ""))
        if payload:
            user_id = payload["sub"]

    user = None
    if user_id:
        user = await db.get(User, user_id)

    # Build search query from preferences
    style = req.style or "casual"
    occasion = req.occasion or ""
    budget = req.budget_max or 5000

    query_text = f"{style} clothing"
    if occasion:
        query_text += f" for {occasion}"

    # Try vector search first
    client = _get_qdrant()
    if client:
        try:
            from api.services.catalog_sync import search_similar, generate_embedding

            embedding = generate_embedding(query_text)
            if embedding:
                results = search_similar(client, embedding, limit=req.count * 2)

                # Filter by budget and format
                recommendations = []
                for r in results:
                    if r.get("price", 0) <= budget:
                        recommendations.append(
                            ProductRecommendation(
                                product_id=r.get("shopify_id", ""),
                                title=r.get("title", ""),
                                category=r.get("category", ""),
                                price=r.get("price", 0),
                                currency=r.get("currency", "INR"),
                                image_url=r.get("image_url", ""),
                                url=r.get("url", ""),
                                score=r.get("score", 0),
                                reason=f"Matches your {style} style",
                            ).model_dump()
                        )

                    if len(recommendations) >= req.count:
                        break

                if recommendations:
                    return {"recommendations": recommendations, "count": len(recommendations), "source": "vector_search"}

        except Exception as e:
            logger.error(f"Vector search failed: {e}")

    # Fallback: query from database (if products are stored there)
    try:
        from api.models.schema import Product
        stmt = select(Product).limit(req.count)
        result = await db.execute(stmt)
        products = result.scalars().all()

        if products:
            recommendations = [
                ProductRecommendation(
                    product_id=str(p.id),
                    title=p.name or "Product",
                    category=p.source or "other",
                    price=p.price or 0,
                    image_url=p.image_url if hasattr(p, 'image_url') else None,
                    score=0.5,
                    reason="From catalog",
                ).model_dump()
                for p in products
                if (p.price or 0) <= budget
            ]
            return {"recommendations": recommendations, "count": len(recommendations), "source": "database"}

    except Exception as e:
        logger.error(f"Database query failed: {e}")

    # Final fallback: empty
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
            from qdrant_client.models import Filter, FieldCondition, MatchAnything

            # Get most recently synced products
            results = client.query_points(
                collection_name="shopify_products",
                query=[0] * 512,  # dummy vector, we just want to list
                limit=limit,
            )

            trending = []
            for r in results.points:
                trending.append({
                    "product_id": r.payload.get("shopify_id", ""),
                    "title": r.payload.get("title", ""),
                    "category": r.payload.get("category", ""),
                    "price": r.payload.get("price", 0),
                    "image_url": r.payload.get("image_url", ""),
                    "url": r.payload.get("url", ""),
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
