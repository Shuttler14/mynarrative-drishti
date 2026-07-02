from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


def require_admin(authorization: str = Header(None)) -> dict:
    """Extract and verify JWT, enforce admin role. Returns payload."""
    if not authorization:
        raise HTTPException(401, "Missing token")
    payload = verify_token(authorization.replace("Bearer ", ""))
    if not payload:
        raise HTTPException(401, "Invalid token")
    if payload.get("role") != "admin":
        raise HTTPException(403, "Admin access required")
    return payload


def _require_admin_secret(authorization: str = Header(None)) -> bool:
    """Simple shared-secret admin check for internal endpoints."""
    import os
    secret = os.getenv("ADMIN_SECRET", "")
    if not secret:
        raise HTTPException(500, "ADMIN_SECRET not configured")
    token = authorization.replace("Bearer ", "") if authorization else ""
    if token != secret:
        raise HTTPException(403, "Invalid admin secret")
    return True


@router.post("/catalog/sync")
async def trigger_catalog_sync(
    authorization: str = Header(None),
    max_products: int = Query(1000, ge=1, le=5000),
):
    """Trigger full catalog sync from Shopify to Qdrant. No JWT needed — uses shared secret."""
    import logging
    logger = logging.getLogger("drishti.admin")

    _require_admin_secret(authorization)

    try:
        from api.services.catalog_sync import full_sync
        result = await full_sync(max_products=max_products)
        return {"status": "ok", "sync": result}
    except Exception as e:
        logger.error(f"Catalog sync failed: {e}", exc_info=True)
        raise HTTPException(500, f"Sync failed: {str(e)}")


@router.get("/catalog/status")
async def catalog_status(authorization: str = Header(None)):
    """Check Qdrant catalog status."""
    _require_admin_secret(authorization)

    try:
        from api.services.catalog_sync import get_qdrant_client, COLLECTION_NAME
        client = get_qdrant_client()
        collections = client.get_collections().collections
        names = [c.name for c in collections]

        if COLLECTION_NAME not in names:
            return {"status": "empty", "collection_exists": False, "count": 0}

        info = collection_info = client.get_collection(COLLECTION_NAME)
        return {
            "status": "ok",
            "collection_exists": True,
            "count": collection_info.points_count or 0,
            "vectors_size": collection_info.config.params.vectors.size if collection_info.config.params.vectors else 0,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


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
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):

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
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):

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
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):

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
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):

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
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())
    month_start = today_start.replace(day=1)

    # DAU: users with sessions today
    dau_stmt = (
        select(func.count(func.distinct(Session.user_id)))
        .where(Session.created_at >= today_start)
        .where(Session.user_id.isnot(None))
    )
    dau = (await db.execute(dau_stmt)).scalar() or 0

    # WAU: users with sessions this week
    wau_stmt = (
        select(func.count(func.distinct(Session.user_id)))
        .where(Session.created_at >= week_start)
        .where(Session.user_id.isnot(None))
    )
    wau = (await db.execute(wau_stmt)).scalar() or 0

    # MAU: users with sessions this month
    mau_stmt = (
        select(func.count(func.distinct(Session.user_id)))
        .where(Session.created_at >= month_start)
        .where(Session.user_id.isnot(None))
    )
    mau = (await db.execute(mau_stmt)).scalar() or 0

    # Average looks per session
    avg_looks_stmt = (
        select(func.avg(func.count(LookCard.id)))
        .join(Session, LookCard.session_id == Session.id)
        .group_by(Session.id)
    )
    avg_looks_result = (await db.execute(avg_looks_stmt)).scalar()
    avg_looks_per_session = round(float(avg_looks_result or 0), 1)

    # VTON conversion rate: completed / total
    total_vton = (await db.execute(select(func.count(VTONJob.id)))).scalar() or 0
    completed_vton = (
        await db.execute(
            select(func.count(VTONJob.id)).where(VTONJob.status == "completed")
        )
    ).scalar() or 0
    vton_conversion_rate = round(completed_vton / total_vton * 100, 1) if total_vton > 0 else 0

    # Top categories (from products table)
    top_categories_stmt = (
        select(Product.category, func.count(Product.id).label("count"))
        .where(Product.category.isnot(None))
        .group_by(Product.category)
        .order_by(func.count(Product.id).desc())
        .limit(5)
    )
    top_categories_result = await db.execute(top_categories_stmt)
    top_categories = [{"category": r[0], "count": r[1]} for r in top_categories_result.all()]

    # Top brands
    top_brands_stmt = (
        select(Product.brand, func.count(Product.id).label("count"))
        .where(Product.brand.isnot(None))
        .group_by(Product.brand)
        .order_by(func.count(Product.id).desc())
        .limit(5)
    )
    top_brands_result = await db.execute(top_brands_stmt)
    top_brands = [{"brand": r[0], "count": r[1]} for r in top_brands_result.all()]

    # Session duration: approximate from session analytics JSON if available
    avg_session_duration = 0  # TODO: implement if session duration tracking is added

    return {
        "daily_active_users": dau,
        "weekly_active_users": wau,
        "monthly_active_users": mau,
        "avg_session_duration_minutes": avg_session_duration,
        "avg_looks_per_session": avg_looks_per_session,
        "vton_conversion_rate": vton_conversion_rate,
        "top_categories": top_categories,
        "top_brands": top_brands,
    }
