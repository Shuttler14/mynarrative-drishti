from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models.schema import Product, Brand

router = APIRouter()


class ProductResponse(BaseModel):
    id: str
    source: str
    source_id: str
    source_url: str | None = None
    name: str
    brand: str | None = None
    category: str | None = None
    subcategory: str | None = None
    gender: str | None = None
    price: float | None = None
    original_price: float | None = None
    discount_pct: float | None = None
    color: str | None = None
    sizes: list = []
    images: list = []
    thumbnail: str | None = None
    rating: float | None = None
    review_count: int = 0


@router.get("/products")
async def list_products(
    q: str | None = None,
    category: str | None = None,
    brand: str | None = None,
    gender: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    color: str | None = None,
    sort_by: str = "relevance",
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Product).where(Product.availability == True)

    if q:
        search = f"%{q}%"
        stmt = stmt.where(
            or_(
                Product.name.ilike(search),
                Product.brand.ilike(search),
                Product.category.ilike(search),
            )
        )
    if category:
        stmt = stmt.where(Product.category == category)
    if brand:
        stmt = stmt.where(Product.brand == brand)
    if gender:
        stmt = stmt.where(Product.gender == gender)
    if min_price is not None:
        stmt = stmt.where(Product.price >= min_price)
    if max_price is not None:
        stmt = stmt.where(Product.price <= max_price)
    if color:
        stmt = stmt.where(Product.color_family == color)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar()

    if sort_by == "price_low":
        stmt = stmt.order_by(Product.price.asc())
    elif sort_by == "price_high":
        stmt = stmt.order_by(Product.price.desc())
    elif sort_by == "rating":
        stmt = stmt.order_by(Product.rating.desc())
    elif sort_by == "discount":
        stmt = stmt.order_by(Product.discount_pct.desc())
    else:
        stmt = stmt.order_by(Product.rating.desc().nullslast())

    stmt = stmt.offset((page - 1) * limit).limit(limit)
    result = await db.execute(stmt)
    products = result.scalars().all()

    return {
        "total": total,
        "page": page,
        "limit": limit,
        "products": [
            ProductResponse(
                id=str(p.id),
                source=p.source,
                source_id=p.source_id,
                source_url=p.source_url,
                name=p.name,
                brand=p.brand,
                category=p.category,
                subcategory=p.subcategory,
                gender=p.gender,
                price=p.price,
                original_price=p.original_price,
                discount_pct=p.discount_pct,
                color=p.color,
                sizes=p.sizes or [],
                images=p.images or [],
                thumbnail=p.thumbnail,
                rating=p.rating,
                review_count=p.review_count or 0,
            ).model_dump()
            for p in products
        ],
    }


@router.get("/products/{product_id}")
async def get_product(product_id: str, db: AsyncSession = Depends(get_db)):
    product = await db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Product not found")

    return ProductResponse(
        id=str(product.id),
        source=product.source,
        source_id=product.source_id,
        source_url=product.source_url,
        name=product.name,
        brand=product.brand,
        category=product.category,
        subcategory=product.subcategory,
        gender=product.gender,
        price=product.price,
        original_price=product.original_price,
        discount_pct=product.discount_pct,
        color=product.color,
        sizes=product.sizes or [],
        images=product.images or [],
        thumbnail=product.thumbnail,
        rating=product.rating,
        review_count=product.review_count or 0,
    ).model_dump()


@router.get("/categories")
async def list_categories(db: AsyncSession = Depends(get_db)):
    stmt = select(Product.category, func.count(Product.id)).where(
        Product.availability == True
    ).group_by(Product.category).order_by(func.count(Product.id).desc())
    result = await db.execute(stmt)
    return [{"name": row[0], "count": row[1]} for row in result.all() if row[0]]


@router.get("/brands")
async def list_brands(db: AsyncSession = Depends(get_db)):
    stmt = select(Brand).order_by(Brand.name)
    result = await db.execute(stmt)
    brands = result.scalars().all()
    return [{"id": str(b.id), "name": b.name, "slug": b.slug, "tier": b.tier, "is_indian": b.is_indian} for b in brands]
