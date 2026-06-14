from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.database import get_db, async_session
from api.models.schema import User

logger = logging.getLogger("drishti.webhooks")
settings = get_settings()
router = APIRouter()


def verify_shopify_hmac(body: bytes, hmac_header: str) -> bool:
    if not settings.SHOPIFY_WEBHOOK_SECRET:
        return True
    computed = hmac.new(
        settings.SHOPIFY_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256,
    ).digest()
    import base64
    computed_b64 = base64.b64encode(computed).decode()
    return hmac.compare_digest(computed_b64, hmac_header)


@router.post("/shopify")
async def shopify_webhook(request: Request):
    body = await request.body()
    topic = request.headers.get("X-Shopify-Topic", "unknown")
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")

    if settings.SHOPIFY_WEBHOOK_SECRET:
        if not verify_shopify_hmac(body, hmac_header):
            raise HTTPException(401, "Invalid HMAC")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")

    logger.info(f"Shopify webhook: {topic}")

    if topic == "orders/create":
        await handle_order_created(payload)
    elif topic == "orders/paid":
        await handle_order_paid(payload)
    elif topic == "orders/fulfilled":
        await handle_order_fulfilled(payload)
    elif topic == "customers/create":
        await handle_customer_created(payload)
    elif topic == "products/update":
        await handle_product_update(payload)

    return {"status": "ok", "topic": topic}


async def handle_order_created(order: dict):
    logger.info(f"Order created: {order.get('order_number')}")
    async with async_session() as db:
        email = order.get("email")
        if email:
            stmt = select(User).where(User.email == email)
            result = await db.execute(stmt)
            user = result.scalar_one_or_none()
            if user:
                logger.info(f"Order linked to user {user.id}")

            customer = order.get("customer", {})
            if not user and customer.get("email"):
                new_user = User(
                    email=customer["email"],
                    name=f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip(),
                    phone=customer.get("phone"),
                )
                db.add(new_user)
                await db.commit()


async def handle_order_paid(order: dict):
    logger.info(f"Order paid: {order.get('order_number')} - total: {order.get('total_price')}")


async def handle_order_fulfilled(order: dict):
    logger.info(f"Order fulfilled: {order.get('order_number')}")


async def handle_customer_created(customer: dict):
    logger.info(f"Customer created: {customer.get('email')}")


async def handle_product_update(product: dict):
    """Sync product to Qdrant on Shopify product create/update."""
    logger.info(f"Product updated: {product.get('title')}")

    try:
        from api.services.catalog_sync import sync_single_product, delete_single_product

        shopify_id = str(product.get("id", ""))
        if not shopify_id:
            return

        # Check if product is being deleted (published = false)
        published = product.get("published_at")
        if not published:
            delete_single_product(shopify_id)
            return

        # Parse and sync
        parsed = {
            "shopify_id": shopify_id,
            "title": product.get("title", ""),
            "description": product.get("body_html", "")[:500],
            "product_type": product.get("product_type", ""),
            "category": product.get("product_type", "other").lower(),
            "vendor": product.get("vendor", ""),
            "tags": [t.lower() for t in (product.get("tags") or "").split(", ") if t],
            "price": float(product.get("variants", [{}])[0].get("price", 0)) if product.get("variants") else 0,
            "currency": "INR",
            "image_url": product.get("image", {}).get("src") if product.get("image") else None,
            "url": f"{settings.SHOPIFY_STORE_URL}/products/{product.get('handle', '')}",
            "variants": [
                {
                    "id": str(v.get("id", "")),
                    "title": v.get("title", ""),
                    "price": float(v.get("price", 0)),
                    "available": v.get("available", False),
                }
                for v in product.get("variants", [])
            ],
            "collections": [],
        }

        sync_single_product(parsed)

    except Exception as e:
        logger.error(f"Failed to sync product: {e}")


@router.post("/dtf-completion")
async def dtf_completion_webhook(request: Request):
    body = await request.body()
    payload = json.loads(body)

    job_id = payload.get("job_id")
    status = payload.get("status")
    logger.info(f"DTF job {job_id} status: {status}")

    return {"status": "ok"}


@router.post("/vton-completion")
async def vton_completion_webhook(request: Request):
    body = await request.body()
    payload = json.loads(body)

    job_id = payload.get("job_id")
    status = payload.get("status")
    logger.info(f"VTON job {job_id} status: {status}")

    return {"status": "ok"}
