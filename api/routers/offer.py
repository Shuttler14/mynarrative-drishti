from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models.schema import Offer
from api.utils.auth import verify_token

router = APIRouter()


class OfferResponse(BaseModel):
    id: str
    bank: str
    card_network: str | None = None
    card_type: str | None = None
    title: str
    description: str | None = None
    discount_type: str | None = None
    discount_value: float | None = None
    max_discount: float | None = None
    min_order: float = 0
    coupon_code: str | None = None
    valid_until: str | None = None


@router.get("/list")
async def list_offers(
    bank: str | None = None,
    card_network: str | None = None,
    min_amount: float | None = None,
    category: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Offer).where(Offer.is_active == True)

    if bank:
        stmt = stmt.where(Offer.bank.ilike(f"%{bank}%"))
    if card_network:
        stmt = stmt.where(Offer.card_network == card_network)
    if min_amount is not None:
        stmt = stmt.where(Offer.min_order <= min_amount)

    stmt = stmt.order_by(Offer.discount_value.desc().nullslast()).limit(limit)
    result = await db.execute(stmt)
    offers = result.scalars().all()

    return {
        "offers": [
            OfferResponse(
                id=str(o.id),
                bank=o.bank,
                card_network=o.card_network,
                card_type=o.card_type,
                title=o.title,
                description=o.description,
                discount_type=o.discount_type,
                discount_value=o.discount_value,
                max_discount=o.max_discount,
                min_order=o.min_order,
                coupon_code=o.coupon_code,
                valid_until=o.valid_until.isoformat() if o.valid_until else None,
            ).model_dump()
            for o in offers
        ],
        "count": len(offers),
    }


@router.get("/best-for/{amount}")
async def best_offers_for_amount(
    amount: float,
    bank: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Offer)
        .where(Offer.is_active == True)
        .where(Offer.min_order <= amount)
    )
    if bank:
        stmt = stmt.where(Offer.bank.ilike(f"%{bank}%"))

    result = await db.execute(stmt)
    offers = result.scalars().all()

    scored = []
    for o in offers:
        if o.discount_type == "percentage" and o.discount_value:
            discount = min(amount * o.discount_value / 100, o.max_discount or float("inf"))
        elif o.discount_type == "flat" and o.discount_value:
            discount = o.discount_value
        else:
            discount = 0
        scored.append((discount, o))

    scored.sort(key=lambda x: x[0], reverse=True)

    return {
        "amount": amount,
        "best_offers": [
            {
                "offer_id": str(o.id),
                "bank": o.bank,
                "title": o.title,
                "coupon_code": o.coupon_code,
                "estimated_discount": round(d, 2),
                "final_price": round(amount - d, 2),
            }
            for d, o in scored[:5]
        ],
    }


@router.post("/track-usage")
async def track_offer_usage(
    offer_id: str,
    authorization: str = Header(None),
):
    return {"message": "Usage tracked", "offer_id": offer_id}
