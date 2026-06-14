from __future__ import annotations
import httpx
import re
import structlog
from fastapi import APIRouter, Request

from shopify_sync.config import settings
from shopify_sync.models import ShopifyProduct

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/admin/v1/shopify", tags=["shopify-admin"])


@router.post("/backfill")
async def backfill(request: Request) -> dict:
    sync = request.app.state.sync
    count = 0
    cursor = None
    async with httpx.AsyncClient(timeout=30, headers={
        "X-Shopify-Access-Token": settings.admin_api_token}) as c:
        while True:
            params = {"limit": 250, "status": "active"}
            if cursor:
                params["page_info"] = cursor
            r = await c.get(f"{settings.admin_base}/products.json", params=params)
            r.raise_for_status()
            products = r.json().get("products", [])
            for raw in products:
                await sync.upsert_product(ShopifyProduct.model_validate(raw))
                count += 1
            link = r.headers.get("link", "")
            cursor = _next_page_info(link)
            if not cursor:
                break
    logger.info("backfill.complete", count=count)
    return {"synced": count}


def _next_page_info(link_header: str) -> str | None:
    m = re.search(r'<[^>]*page_info=([^&>]+)[^>]*>;\s*rel="next"', link_header)
    return m.group(1) if m else None
