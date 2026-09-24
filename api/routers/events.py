from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models.schema import UserEvent, UserStylePref
from api.utils.identity import normalize_wear_weights, resolve_user

logger = logging.getLogger("drishti.events")
router = APIRouter()

# Signal strength table (soft preference updater)
_EVENT_WEIGHTS = {
    "impression": 0.1,
    "look_click": 0.5,
    "look_save": 1.5,
    "vton_completed": 2.0,
    "purchase": 3.0,
    "look_feedback": 1.0,
    "dismiss": -1.0,
}

_DECAY = 0.9
_LEARNING_RATE = 0.1
_MIN_WEIGHT = 0.05
_MAX_WEIGHT = 1.0


class EventRequest(BaseModel):
    event_type: str
    occasion: str | None = None
    session_id: str | None = None
    payload: dict = Field(default_factory=dict)


def _extract_cultures(payload: dict) -> list[str]:
    """Pull culture labels from look/event payload if present."""
    cultures: list[str] = []
    # Direct field
    if payload.get("culture"):
        cultures.append(str(payload["culture"]).lower())
    # items / garments with culture
    for key in ("items", "garments", "looks"):
        arr = payload.get(key) or []
        if isinstance(arr, list):
            for it in arr:
                if isinstance(it, dict) and it.get("culture"):
                    cultures.append(str(it["culture"]).lower())
                # Infer from title
                title = str(it.get("title", "")).lower() if isinstance(it, dict) else ""
                if any(h in title for h in ("kurta", "kurti", "saree", "lehenga", "sherwani", "churidar")):
                    cultures.append("indian")
                elif any(h in title for h in ("blazer", "trouser", "jean", "shirt", "dress", "trench")):
                    cultures.append("western")
    # wear preference directly (from wear chips save path)
    if payload.get("wear"):
        wear = payload["wear"]
        if isinstance(wear, list):
            for w in wear:
                key = normalize_wear_weights({w: 1.0})
                cultures.extend(key.keys())
    out = []
    for c in cultures:
        c = c.replace("-", "_")
        if c in ("western", "indian", "indo_western") and c not in out:
            out.append(c)
    return out


async def _update_wear_prefs(
    db: AsyncSession,
    user_id,
    occasion: str,
    cultures: list[str],
    signal: float,
    *,
    source: str = "behavior",
) -> None:
    """Closed-loop soft preference update with recency decay. Never touches hard rules."""
    if not cultures or signal == 0:
        return
    from api.services.fashion_rules import canonicalize_occasion

    occ = canonicalize_occasion(occasion or "casual")
    result = await db.execute(
        select(UserStylePref)
        .where(UserStylePref.user_id == user_id)
        .where(UserStylePref.occasion == occ)
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = UserStylePref(
            user_id=user_id,
            occasion=occ,
            wear_weights={c: 0.5 for c in ("western", "indian", "indo_western")},
            confidence=0.3,
            source=source,
        )
        db.add(row)

    weights = normalize_wear_weights(row.wear_weights or {}) or {
        "western": 0.5, "indian": 0.5, "indo_western": 0.5,
    }
    # Ensure all keys exist
    for k in ("western", "indian", "indo_western"):
        weights.setdefault(k, 0.5)

    for c in cultures:
        if c not in weights:
            continue
        old = float(weights[c])
        # Recency decay + learning rate: blend old toward target by signal strength
        target = 1.0 if signal > 0 else 0.0
        lr = _LEARNING_RATE * min(abs(signal), 2.0) / 2.0
        new = old * (1 - lr) + target * lr
        weights[c] = max(_MIN_WEIGHT, min(_MAX_WEIGHT, new))

    # Renormalize so weights sum roughly to 1.2 (soft, not strict simplex)
    total = sum(weights.values()) or 1.0
    if total > 0:
        scale = 1.2 / total
        if abs(scale - 1.0) > 0.05:
            for k in weights:
                weights[k] = max(_MIN_WEIGHT, min(_MAX_WEIGHT, weights[k] * scale))

    row.wear_weights = weights
    row.source = source
    row.confidence = min(1.0, float(row.confidence or 0.5) + 0.03 if signal > 0 else float(row.confidence or 0.5))
    await db.flush()


@router.post("")
async def record_event(
    req: EventRequest,
    authorization: str = Header(None),
    x_shopify_customer_id: str | None = Header(None, alias="X-Shopify-Customer-Id"),
    db: AsyncSession = Depends(get_db),
):
    """Ingest a behavior event (save/click/VTON/feedback/dismiss) and optionally
    update soft Fashion DNA wear weights (closed loop, never hard rules)."""
    user = await resolve_user(
        db,
        authorization=authorization,
        shopify_customer_id=x_shopify_customer_id,
        create_if_missing=False,
    )
    # Anonymous events are dropped (no durable identity) — still accept for logging
    if user is None:
        return {"ok": True, "recorded": False, "reason": "anonymous"}

    et = (req.event_type or "").strip().lower()
    weight = _EVENT_WEIGHTS.get(et, 0.5)
    if et == "look_feedback":
        fb = str(req.payload.get("feedback", "")).lower()
        if fb in ("negative", "dislike", "hate", "no", "bad"):
            weight = -1.0
        elif fb in ("positive", "like", "love", "yes", "good"):
            weight = 1.5

    event = UserEvent(
        user_id=user.id,
        event_type=et,
        occasion=req.occasion,
        session_id=req.session_id,
        payload=req.payload or {},
        weight=weight,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(event)

    # Preference updater for high-signal events only
    updated_prefs = False
    if weight != 0 and et in ("look_save", "vton_completed", "purchase", "look_feedback", "dismiss", "look_click"):
        cultures = _extract_cultures(req.payload or {})
        # Also accept explicit wear from payload
        if not cultures and (req.payload or {}).get("wear"):
            cultures = list(normalize_wear_weights(req.payload["wear"]).keys())
        if cultures:
            await _update_wear_prefs(
                db,
                user.id,
                req.occasion or (req.payload or {}).get("occasion") or "casual",
                cultures,
                weight,
            )
            updated_prefs = True

    await db.flush()
    return {"ok": True, "recorded": True, "event_id": str(event.id), "prefs_updated": updated_prefs}
