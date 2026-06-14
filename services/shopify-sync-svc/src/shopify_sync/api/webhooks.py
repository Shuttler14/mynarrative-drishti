from __future__ import annotations
import json

from fastapi import APIRouter, Depends, Request

from shopify_sync.models import ShopifyProduct
from shopify_sync.shopify_auth import verify_webhook_hmac

router = APIRouter(prefix="/webhooks/shopify", tags=["shopify-webhooks"])


def _svc(request: Request):
    return request.app.state.sync


@router.post("/products/create")
@router.post("/products/update")
async def product_upsert(request: Request, body: bytes = Depends(verify_webhook_hmac)) -> dict:
    sp = ShopifyProduct.model_validate(json.loads(body))
    await _svc(request).upsert_product(sp)
    return {"ok": True}


@router.post("/products/delete")
async def product_delete(request: Request, body: bytes = Depends(verify_webhook_hmac)) -> dict:
    payload = json.loads(body)
    await _svc(request).delete_product(int(payload["id"]))
    return {"ok": True}
