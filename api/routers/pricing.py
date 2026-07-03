from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models.schema import Product, PriceAlert
from api.services.price_scraper import compare_prices as scrape_compare
from api.services.card_offers import apply_card_offers, get_best_card_offer
from api.utils.auth import verify_token

logger = logging.getLogger("drishti.pricing")

router = APIRouter()


class CreateAlertRequest(BaseModel):
    source: str
    source_id: str
    product_name: str | None = None
    target_price: float


@router.get("/compare/{source}/{source_id}")
async def compare_prices(
    source: str,
    source_id: str,
    product_name: str = Query(None),
    brand: str = Query(None),
    category: str = Query(None),
    card_bank: str = Query(None, description="User's bank name for card offers (e.g., HDFC, ICICI, SBI)"),
    card_type: str = Query("credit", description="Card type: credit or debit"),
    db: AsyncSession = Depends(get_db),
):
    """Compare prices across Myntra, AJIO, and Amazon.
    Uses live scraping with anti-blocking measures."""

    # Get product info from local catalog if available
    stmt = select(Product).where(Product.source_id == source_id)
    result = await db.execute(stmt)
    product = result.scalar_one_or_none()

    if product:
        product_name = product_name or product.title or ""
        brand = brand or product.brand or ""
        category = category or product.category or ""

    if not product_name:
        raise HTTPException(400, "product_name query param required when product not in local catalog")

    # Scrape live prices from all platforms
    try:
        comparison = await scrape_compare(
            product_name=product_name,
            brand=brand,
            category=category,
            sources=["amazon", "myntra", "flipkart"],
        )
    except Exception as e:
        logger.error(f"Scraping failed: {e}")
        comparison = {"query": product_name, "results": [], "total_found": 0, "best_price": None, "savings": 0}

    # Add local Shopify data if available
    if product and product.price:
        comparison["results"].insert(0, {
            "source": "shopify",
            "product_id": source_id,
            "title": product.title or "",
            "brand": product.brand or "",
            "price": product.price or 0,
            "mrp": product.original_price or product.price or 0,
            "discount_pct": product.discount_pct or 0,
            "rating": 0,
            "rating_count": 0,
            "image_url": product.image_url or "",
            "url": product.source_url or "",
            "color": "",
            "category": product.category or "",
        })

    # Recalculate best after adding Shopify
    available = [p for p in comparison["results"] if p.get("price", 0) > 0]
    available.sort(key=lambda x: x["price"])
    comparison["best_price"] = available[0] if available else None
    if len(available) > 1:
        comparison["savings"] = available[-1]["price"] - available[0]["price"]
    else:
        comparison["savings"] = 0

    # Apply card offers if user specified their bank
    if card_bank:
        comparison["results"] = apply_card_offers(
            comparison["results"], card_bank, card_type
        )
        # Find best deal after card discount
        with_card = [p for p in comparison["results"] if p.get("card_offer")]
        if with_card:
            best_with_card = min(with_card, key=lambda x: x["card_offer"]["final_price"])
            comparison["best_card_deal"] = {
                "platform": best_with_card["source"],
                "original_price": best_with_card["price"],
                "card_discount": best_with_card["card_offer"]["actual_discount"],
                "final_price": best_with_card["card_offer"]["final_price"],
                "bank": best_with_card["card_offer"]["bank"],
                "card_type": best_with_card["card_offer"]["card_type"],
                "code": best_with_card["card_offer"]["code"],
                "terms": best_with_card["card_offer"]["terms"],
            }

    return {
        "source": source,
        "source_id": source_id,
        "comparisons": comparison["results"],
        "best_price": comparison["best_price"],
        "best_card_deal": comparison.get("best_card_deal"),
        "savings": comparison.get("savings", 0),
        "compared_at": comparison.get("compared_at"),
        "card_offers_applied": bool(card_bank),
    }


@router.get("/compare-by-url")
async def compare_by_url(
    url: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """URL-based price comparison. Extracts product info and scrapes live prices."""
    
    # Try to find by URL pattern in local catalog
    stmt = select(Product).where(Product.source_url.ilike(f"%{url}%"))
    result = await db.execute(stmt)
    product = result.scalar_one_or_none()

    if product and product.source and product.source_id:
        return await compare_prices(
            source=product.source,
            source_id=product.source_id,
            product_name=product.title,
            brand=product.brand,
            db=db,
        )

    # If not found locally, try to extract product name from URL and search
    # e.g., Myntra URLs contain product slugs
    return {
        "url": url,
        "comparisons": [],
        "message": "Product not found in local catalog. Use /compare endpoint with product details.",
    }


@router.post("/alerts")
async def create_price_alert(
    req: CreateAlertRequest,
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    if not authorization:
        raise HTTPException(401, "Missing token")

    payload = verify_token(authorization.replace("Bearer ", ""))
    if not payload:
        raise HTTPException(401, "Invalid token")

    alert = PriceAlert(
        user_id=payload["sub"],
        source=req.source,
        source_id=req.source_id,
        product_name=req.product_name,
        target_price=req.target_price,
    )
    db.add(alert)
    await db.flush()

    return {"alert_id": str(alert.id), "message": "Price alert created"}


@router.get("/alerts")
async def list_price_alerts(
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    if not authorization:
        raise HTTPException(401, "Missing token")

    payload = verify_token(authorization.replace("Bearer ", ""))
    if not payload:
        raise HTTPException(401, "Invalid token")

    stmt = (
        select(PriceAlert)
        .where(PriceAlert.user_id == payload["sub"])
        .where(PriceAlert.is_active == True)
        .order_by(PriceAlert.created_at.desc())
    )
    result = await db.execute(stmt)
    alerts = result.scalars().all()

    return {
        "alerts": [
            {
                "id": str(a.id),
                "source": a.source,
                "source_id": a.source_id,
                "product_name": a.product_name,
                "target_price": a.target_price,
                "current_price": a.current_price,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in alerts
        ]
    }


@router.delete("/alerts/{alert_id}")
async def delete_price_alert(
    alert_id: str,
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    if not authorization:
        raise HTTPException(401, "Missing token")

    payload = verify_token(authorization.replace("Bearer ", ""))
    if not payload:
        raise HTTPException(401, "Invalid token")

    alert = await db.get(PriceAlert, alert_id)
    if not alert:
        raise HTTPException(404, "Alert not found")

    if str(alert.user_id) != payload["sub"]:
        raise HTTPException(403, "Not authorized to delete this alert")

    alert.is_active = False
    return {"message": "Alert deleted"}
