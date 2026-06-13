from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models.schema import PriceAlert
from api.utils.auth import verify_token

router = APIRouter()


class CreateAlertRequest(BaseModel):
    source: str
    source_id: str
    product_name: str | None = None
    target_price: float


class PriceComparison(BaseModel):
    source: str
    source_id: str
    source_url: str | None = None
    price: float
    original_price: float | None = None
    discount_pct: float | None = None
    availability: bool = True


@router.get("/compare/{source}/{source_id}")
async def compare_prices(source: str, source_id: str):
    comparisons = [
        PriceComparison(
            source=src,
            source_id=source_id,
            source_url=f"https://{src}.in/product/{source_id}",
            price=price,
            original_price=orig,
            discount_pct=round((1 - price / orig) * 100) if orig else None,
        ).model_dump()
        for src, price, orig in [
            ("myntra", 1299, 1999),
            ("ajio", 1199, 1899),
            ("amazon", 1399, 1999),
            ("flipkart", 1349, 1999),
        ]
    ]

    best = min(comparisons, key=lambda x: x["price"])
    return {
        "source": source,
        "source_id": source_id,
        "comparisons": sorted(comparisons, key=lambda x: x["price"]),
        "best_price": best,
        "savings": max(c["price"] for c in comparisons) - best["price"],
    }


@router.get("/compare-by-url")
async def compare_by_url(url: str = Query(...)):
    return {
        "url": url,
        "comparisons": [],
        "message": "URL-based comparison requires scraper integration",
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

    alert.is_active = False
    return {"message": "Alert deleted"}
