from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.schema import User, UserStylePref

logger = logging.getLogger("drishti.identity")

VALID_WEAR = ("western", "indian", "indo_western", "indo-western")
WEAR_ALIASES = {
    "western": "western",
    "indian": "indian",
    "indo_western": "indo_western",
    "indo-western": "indo_western",
    "fusion": "indo_western",
    "ethnic": "indian",
    "none": "",
    "no preference": "",
    "no_preference": "",
}


def normalize_wear_weights(raw: dict | list | str | None) -> dict[str, float]:
    """Normalize wear preference input to {western, indian, indo_western} floats in [0,1]."""
    weights: dict[str, float] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            key = WEAR_ALIASES.get(str(k).strip().lower())
            if not key:
                continue
            try:
                weights[key] = max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                continue
    elif isinstance(raw, (list, tuple, set)):
        items = [x for x in raw if isinstance(x, str) and x.strip()]
        if not items:
            return {}
        for culture in ("western", "indian", "indo_western"):
            if any(WEAR_ALIASES.get(i.strip().lower()) == culture for i in items):
                weights[culture] = 0.9
            else:
                weights[culture] = 0.15
    elif isinstance(raw, str):
        key = WEAR_ALIASES.get(raw.strip().lower())
        if key:
            for culture in ("western", "indian", "indo_western"):
                weights[culture] = 0.9 if culture == key else 0.15
    return weights


async def resolve_user(
    db: AsyncSession,
    authorization: str | None = None,
    shopify_customer_id: str | None = None,
    *,
    create_if_missing: bool = False,
    email: str | None = None,
    name: str | None = None,
) -> User | None:
    """Resolve user from JWT (preferred) or X-Shopify-Customer-Id header."""
    if authorization:
        from api.utils.auth import verify_token  # lazy: pulls passlib

        token = authorization.replace("Bearer ", "").strip()
        if token:
            payload = verify_token(token)
            if payload and payload.get("sub"):
                user = await db.get(User, payload["sub"])
                if user is not None:
                    if shopify_customer_id and not user.shopify_customer_id:
                        try:
                            existing = await db.execute(
                                select(User).where(User.shopify_customer_id == str(shopify_customer_id))
                            )
                            if existing.scalar_one_or_none() is None:
                                user.shopify_customer_id = str(shopify_customer_id)
                                await db.flush()
                        except Exception:
                            logger.debug("Failed to attach shopify_customer_id", exc_info=True)
                    return user

    sid = (str(shopify_customer_id).strip() if shopify_customer_id else "")
    if not sid:
        return None

    result = await db.execute(select(User).where(User.shopify_customer_id == sid))
    user = result.scalar_one_or_none()
    if user is not None:
        if email and not user.email:
            user.email = email
        if name and not user.name:
            user.name = name
        return user

    if email:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if user is not None:
            user.shopify_customer_id = sid
            await db.flush()
            return user

    if not create_if_missing:
        return None

    user = User(
        email=email or None,
        name=name or None,
        shopify_customer_id=sid,
        is_verified=False,
        is_active=True,
    )
    db.add(user)
    await db.flush()
    logger.info(f"Created user from shopify customer {sid}")
    return user


async def load_style_prefs(
    db: AsyncSession,
    user_id: str,
    occasion: str = "",
) -> dict | None:
    """Load contextual style prefs for occasion, with global fallback."""
    from uuid import UUID

    from api.services.fashion_rules import canonicalize_occasion

    try:
        try:
            uid = UUID(str(user_id))
        except (TypeError, ValueError):
            logger.debug(f"Invalid user_id for load_style_prefs: {user_id!r}")
            return None

        canonical = canonicalize_occasion(occasion) if occasion else ""

        stmt = select(UserStylePref).where(UserStylePref.user_id == uid)
        if canonical:
            stmt = stmt.where(UserStylePref.occasion == canonical)
        stmt = stmt.order_by(UserStylePref.updated_at.desc())
        result = await db.execute(stmt)
        rows = list(result.scalars().all())

        if not rows and canonical and canonical != occasion:
            stmt = (
                select(UserStylePref)
                .where(UserStylePref.user_id == uid)
                .where(UserStylePref.occasion == occasion)
                .order_by(UserStylePref.updated_at.desc())
            )
            result = await db.execute(stmt)
            rows = list(result.scalars().all())

        if not rows:
            stmt = (
                select(UserStylePref)
                .where(UserStylePref.user_id == uid)
                .order_by(UserStylePref.updated_at.desc())
                .limit(1)
            )
            result = await db.execute(stmt)
            rows = list(result.scalars().all())

        if not rows:
            return None

        row = rows[0]
        return {
            "occasion": row.occasion,
            "wear_weights": normalize_wear_weights(row.wear_weights or {}),
            "vibes": list(row.vibes or []),
            "brands": list(row.brands or []),
            "budget_min": row.budget_min,
            "budget_max": row.budget_max,
            "adventure_level": row.adventure_level or "balanced",
            "confidence": float(row.confidence or 0.5),
            "source": row.source or "wizard",
        }
    except Exception:
        logger.debug("load_style_prefs failed", exc_info=True)
        return None
