"""
MY NARRATIVE — Attribution Service (Fly.io / FastAPI)
Uses Supabase REST API for attribution tables (shared with Vercel backend).

Change ID: ADD-CHK-016-260922
Risk: CRITICAL — financial backbone
"""

import uuid
import hashlib
import os
import httpx
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

# Supabase credentials (same as Vercel backend)
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://fmganuxtqbquubtvvqdo.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

# Constants
PLATFORM_COMMISSION_RATE = 10.00
HOST_AFFILIATE_RATE = 7.00
ATTRIBUTION_WINDOW_DAYS = 30
CLICK_ID_PREFIX = "MN-CLK"
PRODUCT_ID_PREFIX = "MN-P"
EVENT_ID_PREFIX = "MN-EVT"
CONFIRMATION_DELAY_HOURS = 72
PAYOUT_DELAY_DAYS = 14


def _generate_click_id() -> str:
    random_part = hashlib.md5(str(uuid.uuid4()).encode()).hexdigest()[:12].upper()
    return f"{CLICK_ID_PREFIX}-{random_part}"


def _generate_event_id() -> str:
    random_part = hashlib.md5(str(uuid.uuid4()).encode()).hexdigest()[:12].upper()
    return f"{EVENT_ID_PREFIX}-{random_part}"


def _generate_product_id(brand_id: str, sku: str) -> str:
    raw = f"{brand_id}:{sku}"
    hash_part = hashlib.md5(raw.encode()).hexdigest()[:10].upper()
    return f"{PRODUCT_ID_PREFIX}-{hash_part}"


def _generate_idempotency_key(order_id: str, item_id: str, event_type: str) -> str:
    raw = f"{order_id}:{item_id}:{event_type}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


async def _sb_request(method: str, table: str, data: dict = None, params: dict = None) -> Any:
    """Supabase REST API request."""
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url += f"?{query}"

    async with httpx.AsyncClient() as client:
        if method == "GET":
            resp = await client.get(url, headers=headers)
        elif method == "POST":
            resp = await client.post(url, headers=headers, json=data)
        elif method == "PATCH":
            resp = await client.patch(url, headers=headers, json=data)
        else:
            raise ValueError(f"Unsupported method: {method}")

        if resp.status_code >= 400:
            raise Exception(f"Supabase error: {resp.status_code} - {resp.text}")
        return resp.json()


async def register_product(
    brand_id: str,
    product_name: str,
    shopify_product_id: str = None,
    shopify_variant_ids: list = None,
    external_id: str = None,
    canonical_url: str = None,
    product_data: dict = None,
) -> dict:
    sku = external_id or shopify_product_id or str(uuid.uuid4())[:8]
    mn_product_id = _generate_product_id(brand_id, sku)

    row = {
        "mn_product_id": mn_product_id,
        "brand_id": brand_id,
        "product_name": product_name,
        "shopify_product_id": shopify_product_id,
        "shopify_variant_ids": shopify_variant_ids or [],
        "external_id": external_id,
        "canonical_url": canonical_url,
        "product_data": product_data or {},
        "status": "active",
    }

    await _sb_request("POST", "narrative_product_registry", row)
    return {"mn_product_id": mn_product_id, "status": "registered"}


async def get_product(mn_product_id: str) -> Optional[dict]:
    result = await _sb_request("GET", "narrative_product_registry",
                              params={"mn_product_id": f"eq.{mn_product_id}", "select": "*"})
    if result and len(result) > 0:
        return result[0]
    return None


async def find_product_by_shopify(shopify_product_id: str) -> Optional[dict]:
    result = await _sb_request("GET", "narrative_product_registry",
                              params={"shopify_product_id": f"eq.{shopify_product_id}", "select": "*"})
    if result and len(result) > 0:
        return result[0]
    return None


async def find_product_by_variant(shopify_variant_id: str) -> Optional[dict]:
    result = await _sb_request("GET", "narrative_product_registry",
                              params={"select": "*", "status": "eq.active"})
    if not result:
        return None
    for product in result:
        variants = product.get("shopify_variant_ids", [])
        if shopify_variant_id in variants:
            return product
    return None


async def record_click(
    mn_product_id: str,
    host_brand_id: str,
    advertiser_brand_id: str,
    user_id: str = None,
    session_id: str = None,
    fingerprint: str = None,
    campaign_id: str = None,
    vton_session_id: str = None,
    source: str = "widget",
    source_detail: str = None,
    referrer_url: str = None,
    destination_url: str = None,
) -> dict:
    click_id = _generate_click_id()
    now = datetime.utcnow()
    expires_at = now + timedelta(days=ATTRIBUTION_WINDOW_DAYS)

    row = {
        "click_id": click_id,
        "mn_product_id": mn_product_id,
        "host_brand_id": host_brand_id,
        "advertiser_brand_id": advertiser_brand_id,
        "user_id": user_id,
        "session_id": session_id,
        "fingerprint": fingerprint,
        "campaign_id": campaign_id,
        "vton_session_id": vton_session_id,
        "source": source,
        "source_detail": source_detail,
        "referrer_url": referrer_url,
        "destination_url": destination_url,
        "attribution_window_days": ATTRIBUTION_WINDOW_DAYS,
        "attributed": False,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }

    await _sb_request("POST", "narrative_clicks", row)

    return {
        "click_id": click_id,
        "tracking_url": f"https://go.mynarrative.store/c/{click_id}",
        "expires_at": expires_at.isoformat(),
    }


async def get_click(click_id: str) -> Optional[dict]:
    result = await _sb_request("GET", "narrative_clicks",
                              params={"click_id": f"eq.{click_id}", "select": "*"})
    if result and len(result) > 0:
        return result[0]
    return None


async def find_last_eligible_click(
    user_id: str,
    mn_product_id: str,
    advertiser_brand_id: str,
) -> Optional[dict]:
    result = await _sb_request("GET", "narrative_clicks", params={
        "user_id": f"eq.{user_id}",
        "mn_product_id": f"eq.{mn_product_id}",
        "advertiser_brand_id": f"eq.{advertiser_brand_id}",
        "attributed": "eq.false",
        "expires_at": f"gt.{datetime.utcnow().isoformat()}",
        "select": "*",
        "order": "created_at.desc",
        "limit": "1",
    })
    if result and len(result) > 0:
        return result[0]
    return None


async def record_attribution_event(
    click_id: str,
    mn_product_id: str,
    user_id: str,
    session_id: str,
    host_brand_id: str,
    advertiser_brand_id: str,
    campaign_id: str,
    event_type: str,
    event_data: dict = None,
) -> dict:
    event_id = _generate_event_id()

    row = {
        "event_id": event_id,
        "click_id": click_id,
        "mn_product_id": mn_product_id,
        "user_id": user_id,
        "session_id": session_id,
        "host_brand_id": host_brand_id,
        "advertiser_brand_id": advertiser_brand_id,
        "campaign_id": campaign_id,
        "event_type": event_type,
        "event_data": event_data or {},
        "created_at": datetime.utcnow().isoformat(),
    }

    await _sb_request("POST", "narrative_attribution_events", row)
    return {"event_id": event_id, "event_type": event_type}


async def attribute_purchase(
    user_id: str,
    order_id: str,
    order_items: list,
    source: str = "checkout",
) -> dict:
    results = []
    now = datetime.utcnow()

    for item in order_items:
        mn_product_id = item.get("mn_product_id")
        if not mn_product_id:
            mn_product_id = await _resolve_product_id(item)
            if not mn_product_id:
                results.append({"mn_product_id": "unknown", "status": "no_product_match"})
                continue

        click = await find_last_eligible_click(user_id, mn_product_id, item.get("advertiser_brand_id", ""))

        if not click:
            commission_event = await _create_commission_event(order_id, item, None, "UNVERIFIED", source, now)
            results.append(commission_event)
            continue

        # Mark click as attributed
        await _sb_request("PATCH", "narrative_clicks",
                         {"attributed": True, "attributed_at": now.isoformat(), "attributed_order_id": order_id},
                         params={"click_id": f"eq.{click['click_id']}"})

        # Record attribution event
        await record_attribution_event(
            click["click_id"], mn_product_id, user_id, click.get("session_id"),
            click["host_brand_id"], click["advertiser_brand_id"], click.get("campaign_id"),
            "purchase", {"order_id": order_id, "source": source}
        )

        confidence = "DIRECT" if source == "checkout" else "VERIFIED"
        commission_event = await _create_commission_event(order_id, item, click, confidence, source, now)
        results.append(commission_event)

    return {"attributed": True, "commissions": results}


async def _resolve_product_id(item: dict) -> Optional[str]:
    if item.get("shopify_product_id"):
        product = await find_product_by_shopify(item["shopify_product_id"])
        if product:
            return product["mn_product_id"]

    if item.get("shopify_variant_id"):
        product = await find_product_by_variant(item["shopify_variant_id"])
        if product:
            return product["mn_product_id"]

    if item.get("sku") or item.get("external_id"):
        sku = item.get("sku") or item.get("external_id")
        brand_id = item.get("advertiser_brand_id", "")
        if brand_id:
            mn_id = _generate_product_id(brand_id, sku)
            existing = await get_product(mn_id)
            if existing:
                return mn_id

    return None


async def _create_commission_event(
    order_id: str,
    item: dict,
    click: Optional[dict],
    confidence: str,
    source: str,
    now: datetime,
) -> dict:
    event_id = _generate_event_id()
    idempotency_key = _generate_idempotency_key(
        order_id, item.get("order_item_id", item.get("mn_product_id", "unknown")), "COMMISSION_CREATED"
    )

    gross = float(item.get("price", 0)) * int(item.get("quantity", 1))
    discount = float(item.get("discount", 0))
    tax = float(item.get("tax", 0))
    shipping = float(item.get("shipping", 0))
    net = max(gross - discount, 0)

    commission_amount = round(net * PLATFORM_COMMISSION_RATE / 100, 2)
    host_affiliate_amount = 0.0
    if click and click.get("host_brand_id") != item.get("advertiser_brand_id"):
        host_affiliate_amount = round(net * HOST_AFFILIATE_RATE / 100, 2)

    platform_fee = commission_amount
    brand_payout = gross - discount - commission_amount - host_affiliate_amount

    row = {
        "event_id": event_id,
        "event_type": "COMMISSION_CREATED",
        "idempotency_key": idempotency_key,
        "order_id": order_id,
        "order_item_id": item.get("order_item_id", item.get("mn_product_id", "unknown")),
        "click_id": click.get("click_id") if click else None,
        "mn_product_id": item.get("mn_product_id"),
        "host_brand_id": click.get("host_brand_id") if click else item.get("host_brand_id", ""),
        "advertiser_brand_id": item.get("advertiser_brand_id", ""),
        "campaign_id": click.get("campaign_id") if click else None,
        "gross_item_value": gross,
        "discount": discount,
        "tax": tax,
        "shipping": shipping,
        "net_commissionable_value": net,
        "commission_rate": PLATFORM_COMMISSION_RATE,
        "commission_amount": commission_amount,
        "host_affiliate_rate": HOST_AFFILIATE_RATE if host_affiliate_amount > 0 else 0,
        "host_affiliate_amount": host_affiliate_amount,
        "platform_fee": platform_fee,
        "brand_payout": brand_payout,
        "attribution_confidence": confidence,
        "attribution_window_days": ATTRIBUTION_WINDOW_DAYS,
        "status": "PENDING",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }

    await _sb_request("POST", "narrative_commission_ledger", row)

    return {
        "event_id": event_id,
        "order_id": order_id,
        "mn_product_id": item.get("mn_product_id"),
        "commission_amount": commission_amount,
        "host_affiliate_amount": host_affiliate_amount,
        "status": "PENDING",
        "confidence": confidence,
    }


async def get_commission_summary(brand_id: str) -> dict:
    result = await _sb_request("GET", "narrative_commission_ledger", params={
        "or": f"(host_brand_id.eq.{brand_id},advertiser_brand_id.eq.{brand_id})",
        "event_type": "eq.COMMISSION_CREATED",
        "status": "not.in.(VOIDED,REFUNDED)",
        "select": "commission_amount,host_affiliate_amount,platform_fee,brand_payout,gross_item_value",
    })

    if not result:
        return {"total_commission_events": 0, "total_gross": 0, "total_commission": 0}

    total_gross = sum(float(r.get("gross_item_value", 0)) for r in result)
    total_commission = sum(float(r.get("commission_amount", 0)) for r in result)
    total_host = sum(float(r.get("host_affiliate_amount", 0)) for r in result)
    total_platform = sum(float(r.get("platform_fee", 0)) for r in result)
    total_payout = sum(float(r.get("brand_payout", 0)) for r in result)

    return {
        "total_commission_events": len(result),
        "total_gross": round(total_gross, 2),
        "total_commission": round(total_commission, 2),
        "total_host_affiliate": round(total_host, 2),
        "total_platform_fee": round(total_platform, 2),
        "total_brand_payout": round(total_payout, 2),
    }


async def run_reconciliation() -> dict:
    run_id = str(uuid.uuid4())
    now = datetime.utcnow()

    # Log start
    await _sb_request("POST", "narrative_reconciliation_log", {
        "id": run_id,
        "run_type": "scheduled",
        "started_at": now.isoformat(),
        "status": "running",
    })

    # Advance PENDING → CONFIRMED
    cutoff = (now - timedelta(hours=CONFIRMATION_DELAY_HOURS)).isoformat()
    pending = await _sb_request("GET", "narrative_commission_ledger", params={
        "status": "eq.PENDING",
        "event_type": "eq.COMMISSION_CREATED",
        "created_at": f"lt.{cutoff}",
        "select": "event_id",
    })

    advanced = 0
    for event in (pending or []):
        new_event_id = _generate_event_id()
        # Copy the original record with new status
        original = await _sb_request("GET", "narrative_commission_ledger", params={
            "event_id": f"eq.{event['event_id']}", "select": "*"
        })
        if original and len(original) > 0:
            orig = original[0]
            orig["event_id"] = new_event_id
            orig["event_type"] = "COMMISSION_CONFIRMED"
            orig["idempotency_key"] = _generate_idempotency_key(orig["order_id"], orig.get("order_item_id", ""), "CONFIRMED")
            orig["status"] = "CONFIRMED"
            orig["original_event_id"] = event["event_id"]
            orig["created_at"] = now.isoformat()
            orig["updated_at"] = now.isoformat()
            await _sb_request("POST", "narrative_commission_ledger", orig)
            advanced += 1

    # Update log
    await _sb_request("PATCH", "narrative_reconciliation_log", {
        "completed_at": now.isoformat(),
        "status": "completed",
        "orders_checked": len(pending or []),
    }, params={"id": f"eq.{run_id}"})

    return {"run_id": run_id, "advanced_pending_to_confirmed": advanced}
