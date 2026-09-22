"""
MY NARRATIVE — Attribution Router (Fly.io / FastAPI)
Change ID: ADD-CHK-018-260922
Risk: CRITICAL — financial backbone
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from api.models.attribution import (
    ProductRegisterRequest, ProductRegisterBatchRequest,
    ClickRecordRequest, PurchaseAttributeRequest,
    MerchantPixelEventRequest, RefundRequest,
)
from api.services.attribution import (
    register_product, get_product, find_product_by_shopify,
    record_click, get_click, attribute_purchase,
    get_commission_summary, run_reconciliation,
)

router = APIRouter()


# ── Product Registry ──────────────────────────────────────────

@router.post("/products/register")
async def api_register_product(req: ProductRegisterRequest):
    result = await register_product(
        req.brand_id, req.product_name,
        req.shopify_product_id, req.shopify_variant_ids,
        req.external_id, req.canonical_url, req.product_data,
    )
    return result


@router.post("/products/register-batch")
async def api_register_products_batch(req: ProductRegisterBatchRequest):
    results = []
    for product in req.products:
        result = await register_product(
            req.brand_id, product.product_name,
            product.shopify_product_id, product.shopify_variant_ids,
            product.external_id, product.canonical_url, product.product_data,
        )
        results.append(result)
    return {"registered": len(results), "results": results}


@router.get("/products/{mn_product_id}")
async def api_get_product(mn_product_id: str):
    product = await get_product(mn_product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


# ── Click Tracking ────────────────────────────────────────────

@router.post("/track/click")
async def api_record_click(req: ClickRecordRequest):
    result = await record_click(
        req.mn_product_id, req.host_brand_id, req.advertiser_brand_id,
        req.user_id, req.session_id, req.fingerprint, req.campaign_id,
        req.vton_session_id, req.source, req.source_detail, None, req.destination_url,
    )
    return result


@router.get("/track/c/{click_id}")
async def api_tracking_redirect(click_id: str):
    click = await get_click(click_id)
    if not click:
        raise HTTPException(status_code=404, detail="Click not found")

    destination = click.get("destination_url") or "https://mynarrative.store"
    return RedirectResponse(url=destination, status_code=302)


# ── Attribution ───────────────────────────────────────────────

@router.post("/attribute/purchase")
async def api_attribute_purchase(req: PurchaseAttributeRequest):
    items = [item.model_dump() for item in req.order_items]
    result = await attribute_purchase(req.user_id, req.order_id, items, req.source)
    return result


# ── Merchant Pixel ────────────────────────────────────────────

@router.post("/webhooks/merchant-pixel")
async def api_merchant_pixel(req: MerchantPixelEventRequest):
    import uuid as uuid_lib

    event_id = str(uuid_lib.uuid4())

    # Store pixel event
    from api.services.attribution import _sb_request
    await _sb_request("POST", "narrative_pixel_events", {
        "id": event_id,
        "merchant_brand_id": req.merchant_brand_id,
        "click_id": req.mn_click_id,
        "user_id": req.user_id,
        "session_id": req.session_id,
        "event_type": req.event_type,
        "product_data": req.product_data,
        "order_data": req.order_data,
    })

    # Process checkout_completed
    if req.event_type == "checkout_completed" and req.mn_click_id:
        click = await get_click(req.mn_click_id)
        if click:
            user_id = req.user_id or click.get("user_id")
            if user_id and req.order_data:
                order_items = []
                for item in req.order_data.get("line_items", []):
                    shopify_product_id = str(item.get("product_id", ""))
                    product = await find_product_by_shopify(shopify_product_id)
                    order_items.append({
                        "mn_product_id": product["mn_product_id"] if product else None,
                        "shopify_product_id": shopify_product_id,
                        "order_item_id": str(item.get("id", "")),
                        "price": float(item.get("price", 0)),
                        "quantity": int(item.get("quantity", 1)),
                        "advertiser_brand_id": req.merchant_brand_id,
                    })
                if order_items:
                    await attribute_purchase(user_id, str(req.order_data.get("order_id", "")), order_items, "pixel")

    return {"ok": True, "event_id": event_id}


# ── Commissions ───────────────────────────────────────────────

@router.get("/commissions/summary/{brand_id}")
async def api_commission_summary(brand_id: str):
    return await get_commission_summary(brand_id)


@router.post("/reconciliation/run")
async def api_run_reconciliation():
    return await run_reconciliation()
