from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models.schema import (
    User, Product, Session, LookCard, VTONJob, ScrapeJob, Offer, PriceAlert,
)
from api.utils.auth import verify_token

router = APIRouter()


class AdminDashboard(BaseModel):
    total_users: int = 0
    total_products: int = 0
    total_sessions: int = 0
    total_looks: int = 0
    total_vton_jobs: int = 0
    active_scrape_jobs: int = 0
    total_offers: int = 0
    total_alerts: int = 0


@router.get("/dashboard")
async def get_dashboard(
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    if not authorization:
        raise HTTPException(401, "Missing token")

    payload = verify_token(authorization.replace("Bearer ", ""))
    if not payload:
        raise HTTPException(401, "Invalid token")

    users = (await db.execute(select(func.count(User.id)))).scalar() or 0
    products = (await db.execute(select(func.count(Product.id)))).scalar() or 0
    sessions = (await db.execute(select(func.count(Session.id)))).scalar() or 0
    looks = (await db.execute(select(func.count(LookCard.id)))).scalar() or 0
    vton = (await db.execute(select(func.count(VTONJob.id)))).scalar() or 0
    active_scrapes = (await db.execute(
        select(func.count(ScrapeJob.id)).where(ScrapeJob.status == "running")
    )).scalar() or 0
    offers = (await db.execute(select(func.count(Offer.id)))).scalar() or 0
    alerts = (await db.execute(
        select(func.count(PriceAlert.id)).where(PriceAlert.is_active == True)
    )).scalar() or 0

    return AdminDashboard(
        total_users=users,
        total_products=products,
        total_sessions=sessions,
        total_looks=looks,
        total_vton_jobs=vton,
        active_scrape_jobs=active_scrapes,
        total_offers=offers,
        total_alerts=alerts,
    ).model_dump()


@router.get("/users")
async def list_users(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    if not authorization:
        raise HTTPException(401, "Missing token")

    payload = verify_token(authorization.replace("Bearer ", ""))
    if not payload:
        raise HTTPException(401, "Invalid token")

    stmt = select(User).order_by(User.created_at.desc()).offset((page - 1) * limit).limit(limit)
    result = await db.execute(stmt)
    users = result.scalars().all()

    return {
        "users": [
            {
                "id": str(u.id),
                "phone": u.phone,
                "email": u.email,
                "name": u.name,
                "is_verified": u.is_verified,
                "wallet_balance": u.wallet_balance,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in users
        ]
    }


@router.get("/products")
async def list_all_products(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    if not authorization:
        raise HTTPException(401, "Missing token")

    payload = verify_token(authorization.replace("Bearer ", ""))
    if not payload:
        raise HTTPException(401, "Invalid token")

    stmt = select(Product).order_by(Product.created_at.desc()).offset((page - 1) * limit).limit(limit)
    result = await db.execute(stmt)
    products = result.scalars().all()

    return {
        "products": [
            {
                "id": str(p.id),
                "source": p.source,
                "name": p.name,
                "brand": p.brand,
                "price": p.price,
                "availability": p.availability,
            }
            for p in products
        ]
    }


@router.get("/scrape-jobs")
async def list_scrape_jobs(
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    if not authorization:
        raise HTTPException(401, "Missing token")

    payload = verify_token(authorization.replace("Bearer ", ""))
    if not payload:
        raise HTTPException(401, "Invalid token")

    stmt = select(ScrapeJob).order_by(ScrapeJob.created_at.desc()).limit(50)
    result = await db.execute(stmt)
    jobs = result.scalars().all()

    return {
        "jobs": [
            {
                "id": str(j.id),
                "source": j.source,
                "category": j.category,
                "status": j.status,
                "items_found": j.items_found,
                "items_stored": j.items_stored,
                "created_at": j.created_at.isoformat() if j.created_at else None,
            }
            for j in jobs
        ]
    }


@router.get("/analytics")
async def get_analytics(
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    if not authorization:
        raise HTTPException(401, "Missing token")

    payload = verify_token(authorization.replace("Bearer ", ""))
    if not payload:
        raise HTTPException(401, "Invalid token")

    return {
        "daily_active_users": 0,
        "weekly_active_users": 0,
        "monthly_active_users": 0,
        "avg_session_duration_minutes": 0,
        "avg_looks_per_session": 0,
        "vton_conversion_rate": 0,
        "top_categories": [],
        "top_brands": [],
    }
