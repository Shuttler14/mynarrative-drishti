"""
MY NARRATIVE — Attribution Service (Fly.io / FastAPI)
Adapted from Vercel attribution.py to use SQLAlchemy async.

Change ID: ADD-CHK-016-260922
Risk: CRITICAL — financial backbone
"""

import uuid
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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


async def register_product(
    db: AsyncSession,
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

    await db.execute(text("""
        INSERT INTO narrative_product_registry 
        (mn_product_id, brand_id, product_name, shopify_product_id, shopify_variant_ids, 
         external_id, canonical_url, product_data, status, created_at, updated_at)
        VALUES (:mn_product_id, :brand_id, :product_name, :shopify_product_id, :shopify_variant_ids,
                :external_id, :canonical_url, :product_data, 'active', now(), now())
        ON CONFLICT (mn_product_id) DO UPDATE SET
            canonical_url = EXCLUDED.canonical_url,
            product_data = EXCLUDED.product_data,
            updated_at = now()
    """), {
        "mn_product_id": mn_product_id,
        "brand_id": brand_id,
        "product_name": product_name,
        "shopify_product_id": shopify_product_id,
        "shopify_variant_ids": shopify_variant_ids or [],
        "external_id": external_id,
        "canonical_url": canonical_url,
        "product_data": product_data or {},
    })

    return {"mn_product_id": mn_product_id, "status": "registered"}


async def get_product(db: AsyncSession, mn_product_id: str) -> Optional[dict]:
    result = await db.execute(text("""
        SELECT * FROM narrative_product_registry WHERE mn_product_id = :id
    """), {"id": mn_product_id})
    row = result.mappings().first()
    return dict(row) if row else None


async def find_product_by_shopify(db: AsyncSession, shopify_product_id: str) -> Optional[dict]:
    result = await db.execute(text("""
        SELECT * FROM narrative_product_registry WHERE shopify_product_id = :id
    """), {"id": shopify_product_id})
    row = result.mappings().first()
    return dict(row) if row else None


async def find_product_by_variant(db: AsyncSession, shopify_variant_id: str) -> Optional[dict]:
    result = await db.execute(text("""
        SELECT * FROM narrative_product_registry 
        WHERE :variant_id = ANY(shopify_variant_ids)
    """), {"variant_id": shopify_variant_id})
    row = result.mappings().first()
    return dict(row) if row else None


async def record_click(
    db: AsyncSession,
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

    await db.execute(text("""
        INSERT INTO narrative_clicks
        (click_id, mn_product_id, host_brand_id, advertiser_brand_id, user_id, session_id,
         fingerprint, campaign_id, vton_session_id, source, source_detail, referrer_url,
         destination_url, attribution_window_days, attributed, created_at, expires_at)
        VALUES (:click_id, :mn_product_id, :host_brand_id, :advertiser_brand_id, :user_id, :session_id,
                :fingerprint, :campaign_id, :vton_session_id, :source, :source_detail, :referrer_url,
                :destination_url, :attribution_window_days, false, :created_at, :expires_at)
    """), {
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
        "created_at": now,
        "expires_at": expires_at,
    })

    return {
        "click_id": click_id,
        "tracking_url": f"https://go.mynarrative.store/c/{click_id}",
        "expires_at": expires_at.isoformat(),
    }


async def get_click(db: AsyncSession, click_id: str) -> Optional[dict]:
    result = await db.execute(text("""
        SELECT * FROM narrative_clicks WHERE click_id = :id
    """), {"id": click_id})
    row = result.mappings().first()
    return dict(row) if row else None


async def find_last_eligible_click(
    db: AsyncSession,
    user_id: str,
    mn_product_id: str,
    advertiser_brand_id: str,
) -> Optional[dict]:
    result = await db.execute(text("""
        SELECT * FROM narrative_clicks
        WHERE user_id = :user_id
          AND mn_product_id = :mn_product_id
          AND advertiser_brand_id = :advertiser_brand_id
          AND attributed = false
          AND expires_at > now()
        ORDER BY created_at DESC
        LIMIT 1
    """), {
        "user_id": user_id,
        "mn_product_id": mn_product_id,
        "advertiser_brand_id": advertiser_brand_id,
    })
    row = result.mappings().first()
    return dict(row) if row else None


async def record_attribution_event(
    db: AsyncSession,
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

    await db.execute(text("""
        INSERT INTO narrative_attribution_events
        (event_id, click_id, mn_product_id, user_id, session_id, host_brand_id,
         advertiser_brand_id, campaign_id, event_type, event_data, created_at)
        VALUES (:event_id, :click_id, :mn_product_id, :user_id, :session_id, :host_brand_id,
                :advertiser_brand_id, :campaign_id, :event_type, :event_data, now())
    """), {
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
    })

    return {"event_id": event_id, "event_type": event_type}


async def attribute_purchase(
    db: AsyncSession,
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
            mn_product_id = await _resolve_product_id(db, item)
            if not mn_product_id:
                results.append({"mn_product_id": "unknown", "status": "no_product_match"})
                continue

        click = await find_last_eligible_click(db, user_id, mn_product_id, item.get("advertiser_brand_id", ""))

        if not click:
            commission_event = await _create_commission_event(
                db, order_id, item, None, "UNVERIFIED", source, now
            )
            results.append(commission_event)
            continue

        # Mark click as attributed
        await db.execute(text("""
            UPDATE narrative_clicks 
            SET attributed = true, attributed_at = now(), attributed_order_id = :order_id
            WHERE click_id = :click_id
        """), {"click_id": click["click_id"], "order_id": order_id})

        # Record attribution event
        await record_attribution_event(
            db, click["click_id"], mn_product_id, user_id, click.get("session_id"),
            click["host_brand_id"], click["advertiser_brand_id"], click.get("campaign_id"),
            "purchase", {"order_id": order_id, "source": source}
        )

        confidence = "DIRECT" if source == "checkout" else "VERIFIED"
        commission_event = await _create_commission_event(
            db, order_id, item, click, confidence, source, now
        )
        results.append(commission_event)

    return {"attributed": True, "commissions": results}


async def _resolve_product_id(db: AsyncSession, item: dict) -> Optional[str]:
    if item.get("shopify_product_id"):
        product = await find_product_by_shopify(db, item["shopify_product_id"])
        if product:
            return product["mn_product_id"]

    if item.get("shopify_variant_id"):
        product = await find_product_by_variant(db, item["shopify_variant_id"])
        if product:
            return product["mn_product_id"]

    if item.get("sku") or item.get("external_id"):
        sku = item.get("sku") or item.get("external_id")
        brand_id = item.get("advertiser_brand_id", "")
        if brand_id:
            mn_id = _generate_product_id(brand_id, sku)
            existing = await get_product(db, mn_id)
            if existing:
                return mn_id

    return None


async def _create_commission_event(
    db: AsyncSession,
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

    await db.execute(text("""
        INSERT INTO narrative_commission_ledger
        (event_id, event_type, idempotency_key, order_id, order_item_id, click_id,
         mn_product_id, host_brand_id, advertiser_brand_id, campaign_id,
         gross_item_value, discount, tax, shipping, net_commissionable_value,
         commission_rate, commission_amount, host_affiliate_rate, host_affiliate_amount,
         platform_fee, brand_payout, attribution_confidence, attribution_window_days,
         status, created_at, updated_at)
        VALUES (:event_id, 'COMMISSION_CREATED', :idempotency_key, :order_id, :order_item_id, :click_id,
                :mn_product_id, :host_brand_id, :advertiser_brand_id, :campaign_id,
                :gross_item_value, :discount, :tax, :shipping, :net_commissionable_value,
                :commission_rate, :commission_amount, :host_affiliate_rate, :host_affiliate_amount,
                :platform_fee, :brand_payout, :attribution_confidence, :attribution_window_days,
                'PENDING', :created_at, :updated_at)
    """), {
        "event_id": event_id,
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
        "created_at": now,
        "updated_at": now,
    })

    return {
        "event_id": event_id,
        "order_id": order_id,
        "mn_product_id": item.get("mn_product_id"),
        "commission_amount": commission_amount,
        "host_affiliate_amount": host_affiliate_amount,
        "status": "PENDING",
        "confidence": confidence,
    }


async def get_commission_summary(db: AsyncSession, brand_id: str) -> dict:
    result = await db.execute(text("""
        SELECT 
            COUNT(*) as total_events,
            COALESCE(SUM(gross_item_value), 0) as total_gross,
            COALESCE(SUM(commission_amount), 0) as total_commission,
            COALESCE(SUM(host_affiliate_amount), 0) as total_host_affiliate,
            COALESCE(SUM(platform_fee), 0) as total_platform_fee,
            COALESCE(SUM(brand_payout), 0) as total_brand_payout
        FROM narrative_commission_ledger
        WHERE (host_brand_id = :brand_id OR advertiser_brand_id = :brand_id)
          AND event_type = 'COMMISSION_CREATED'
          AND status NOT IN ('VOIDED', 'REFUNDED')
    """), {"brand_id": brand_id})
    row = result.mappings().first()
    return dict(row) if row else {}


async def run_reconciliation(db: AsyncSession) -> dict:
    run_id = str(uuid.uuid4())
    now = datetime.utcnow()

    # Log start
    await db.execute(text("""
        INSERT INTO narrative_reconciliation_log (id, run_type, started_at, status)
        VALUES (:id, 'scheduled', :started_at, 'running')
    """), {"id": run_id, "started_at": now})

    # Advance PENDING → CONFIRMED
    cutoff = now - timedelta(hours=CONFIRMATION_DELAY_HOURS)
    pending = await db.execute(text("""
        SELECT event_id FROM narrative_commission_ledger
        WHERE status = 'PENDING' AND event_type = 'COMMISSION_CREATED' AND created_at < :cutoff
    """), {"cutoff": cutoff})
    pending_ids = [row["event_id"] for row in pending.mappings()]

    advanced = 0
    for event_id in pending_ids:
        new_event_id = _generate_event_id()
        await db.execute(text("""
            INSERT INTO narrative_commission_ledger
            SELECT :new_event_id, 'COMMISSION_CONFIRMED', :idempotency_key,
                   order_id, order_item_id, click_id, mn_product_id, host_brand_id,
                   advertiser_brand_id, campaign_id, gross_item_value, discount, tax, shipping,
                   net_commissionable_value, commission_rate, commission_amount, host_affiliate_rate,
                   host_affiliate_amount, platform_fee, brand_payout, attribution_confidence,
                   attribution_window_days, 'CONFIRMED', :original_event_id, 0, now(), now()
            FROM narrative_commission_ledger WHERE event_id = :original_event_id
        """), {
            "new_event_id": new_event_id,
            "idempotency_key": _generate_idempotency_key("reconciliation", event_id, "CONFIRMED"),
            "original_event_id": event_id,
        })
        advanced += 1

    # Update log
    await db.execute(text("""
        UPDATE narrative_reconciliation_log
        SET completed_at = :completed_at, status = 'completed', orders_checked = :checked
        WHERE id = :id
    """), {"completed_at": datetime.utcnow(), "checked": len(pending_ids), "id": run_id})

    return {"run_id": run_id, "advanced_pending_to_confirmed": advanced}
