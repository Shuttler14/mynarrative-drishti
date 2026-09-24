from __future__ import annotations

import hashlib
import hmac
import secrets
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.database import get_db
from api.models.schema import User, OTPRecord, UserStylePref
from api.utils.auth import create_token, verify_token
from api.utils.identity import normalize_wear_weights, resolve_user

logger = logging.getLogger("drishti.user")
settings = get_settings()

router = APIRouter()


class SendOTPRequest(BaseModel):
    phone: str | None = None
    email: str | None = None


class VerifyOTPRequest(BaseModel):
    contact: str
    otp: str
    purpose: str = "login"


class UpdateProfileRequest(BaseModel):
    name: str | None = None
    avatar_url: str | None = None
    preferences: dict | None = None
    body_profile: dict | None = None
    style_profile: dict | None = None


class SyncProfileRequest(BaseModel):
    """Wizard profile sync payload (MN-stylist-wizard-v4 saveServerProfile)."""

    email: str | None = None
    name: str | None = None
    gender: str | None = None
    height_cm: float | None = None
    body_type: str | None = None
    shoulder_structure: str | None = None
    torso_length: str | None = None
    leg_proportion: str | None = None
    fit_preference: str | None = None
    preferred_styles: list[str] = []
    preferred_colors: list[str] = []
    style_profile: dict | None = None
    preferences: dict | None = None
    # Per-occasion contextual prefs: {occasion: {wear: [...], vibes: [...], budget_min, budget_max}}
    contexts: dict | None = None
    # Flat request-level wear/vibe for current occasion (Sprint 4)
    occasion: str | None = None
    wear: list[str] = []
    vibe: list[str] = []


class UserResponse(BaseModel):
    id: str
    phone: str | None = None
    email: str | None = None
    name: str | None = None
    avatar_url: str | None = None
    preferences: dict = {}
    body_profile: dict = {}
    style_profile: dict = {}
    wallet_balance: int = 0


async def _send_otp_email(email: str, otp: str) -> bool:
    """Send OTP via email using SMTP."""
    import aiosmtplib
    from email.mime.text import MIMEText

    if not settings.SMTP_HOST:
        logger.warning("SMTP not configured, OTP email not sent")
        return False

    msg = MIMEText(
        f"Your Drishti verification code is: {otp}\n\nThis code expires in 10 minutes.\n\nIf you didn't request this, ignore this email.",
        "plain",
    )
    msg["From"] = settings.SMTP_USER
    msg["To"] = email
    msg["Subject"] = "Your Drishti Verification Code"

    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASS,
            use_tls=True,
        )
        return True
    except Exception as e:
        logger.error(f"Failed to send OTP email: {e}")
        return False


async def _send_otp_sms(phone: str, otp: str) -> bool:
    """Send OTP via SMS. Integrate with Twilio/MSG91 in production."""
    # TODO: Integrate with SMS provider (Twilio, MSG91, etc.)
    # For now, log the OTP for development
    logger.info(f"OTP for {phone}: {otp} (SMS provider not configured)")
    return True


@router.post("/send-otp")
async def send_otp(req: SendOTPRequest, db: AsyncSession = Depends(get_db)):
    contact = req.phone or req.email
    if not contact:
        raise HTTPException(400, "Phone or email required")

    otp = f"{secrets.randbelow(900000) + 100000}"
    otp_hash = hashlib.sha256(otp.encode()).hexdigest()

    record = OTPRecord(
        contact=contact,
        otp_hash=otp_hash,
        purpose="login",
        expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10),
    )
    db.add(record)
    await db.flush()

    # Actually send the OTP
    is_phone = contact.startswith("+") or contact.isdigit()
    if is_phone:
        sent = await _send_otp_sms(contact, otp)
    else:
        sent = await _send_otp_email(contact, otp)

    if not sent:
        logger.warning(f"OTP delivery failed for {contact}, but record stored")

    return {"message": "OTP sent"}


@router.post("/verify-otp")
async def verify_otp(req: VerifyOTPRequest, db: AsyncSession = Depends(get_db)):
    stmt = (
        select(OTPRecord)
        .where(OTPRecord.contact == req.contact)
        .where(OTPRecord.purpose == req.purpose)
        .where(OTPRecord.is_used == False)
        .order_by(OTPRecord.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    record = result.scalar_one_or_none()

    if not record:
        raise HTTPException(400, "No OTP found")

    if record.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(400, "OTP expired")

    if record.attempts >= 5:
        raise HTTPException(429, "Too many attempts")

    input_hash = hashlib.sha256(req.otp.encode()).hexdigest()
    if not hmac.compare_digest(input_hash, record.otp_hash):
        record.attempts += 1
        await db.flush()
        raise HTTPException(400, "Invalid OTP")

    record.is_used = True

    user_stmt = select(User).where(or_(User.phone == req.contact, User.email == req.contact))
    user_result = await db.execute(user_stmt)
    user = user_result.scalar_one_or_none()

    if not user:
        is_phone = req.contact.startswith("+") or req.contact.isdigit()
        user = User(phone=req.contact if is_phone else None, email=req.contact if not is_phone else None)
        db.add(user)
        await db.flush()

    token = create_token({"sub": str(user.id), "role": user.role or "user", "exp": datetime.now(timezone.utc) + timedelta(days=7)})

    return {
        "token": token,
        "user": UserResponse(
            id=str(user.id),
            phone=user.phone,
            email=user.email,
            name=user.name,
            avatar_url=user.avatar_url,
            preferences=user.preferences or {},
            body_profile=user.body_profile or {},
            style_profile=user.style_profile or {},
            wallet_balance=user.wallet_balance or 0,
        ).model_dump(),
    }


async def _upsert_style_pref(
    db: AsyncSession,
    user_id,
    occasion: str,
    *,
    wear_weights: dict | None = None,
    vibes: list | None = None,
    brands: list | None = None,
    budget_min: float | None = None,
    budget_max: float | None = None,
    adventure_level: str | None = None,
    source: str = "wizard",
) -> None:
    from api.services.fashion_rules import canonicalize_occasion

    occ = canonicalize_occasion(occasion or "casual")
    result = await db.execute(
        select(UserStylePref)
        .where(UserStylePref.user_id == user_id)
        .where(UserStylePref.occasion == occ)
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = UserStylePref(user_id=user_id, occasion=occ)
        db.add(row)
    if wear_weights is not None:
        row.wear_weights = normalize_wear_weights(wear_weights)
    if vibes is not None:
        row.vibes = list(vibes)
    if brands is not None:
        row.brands = list(brands)
    if budget_min is not None:
        row.budget_min = budget_min
    if budget_max is not None:
        row.budget_max = budget_max
    if adventure_level:
        row.adventure_level = adventure_level
    row.source = source
    await db.flush()


@router.post("/sync")
async def sync_profile(
    req: SyncProfileRequest,
    authorization: str = Header(None),
    x_shopify_customer_id: str | None = Header(None, alias="X-Shopify-Customer-Id"),
    db: AsyncSession = Depends(get_db),
):
    """Persist wizard Style DNA + per-occasion wear preferences.

    Accepts JWT (Authorization) and/or X-Shopify-Customer-Id (wizard default).
    Creates a user shell from Shopify customer id when no JWT is present.
    """
    user = await resolve_user(
        db,
        authorization=authorization,
        shopify_customer_id=x_shopify_customer_id,
        create_if_missing=True,
        email=req.email,
        name=req.name,
    )
    if user is None:
        raise HTTPException(401, "Identity required (JWT or X-Shopify-Customer-Id)")

    if req.name:
        user.name = req.name
    if req.email and not user.email:
        user.email = req.email

    # Fold flat body fields into body_profile JSON
    body = dict(user.body_profile or {})
    if req.height_cm is not None:
        body["height_cm"] = req.height_cm
    if req.body_type:
        body["body_type"] = req.body_type
    if req.shoulder_structure:
        body["shoulder_structure"] = req.shoulder_structure
    if req.torso_length:
        body["torso_length"] = req.torso_length
    if req.leg_proportion:
        body["leg_proportion"] = req.leg_proportion
    if req.fit_preference:
        body["fit_preference"] = req.fit_preference
    if req.gender:
        body["gender"] = req.gender
    user.body_profile = body

    # style_profile JSON (styles/occasions/brands/places/budget)
    style = dict(user.style_profile or {})
    if req.style_profile:
        for k, v in req.style_profile.items():
            if v is not None:
                style[k] = v
    if req.preferred_styles:
        style["styles"] = req.preferred_styles
    if req.preferred_colors:
        style["colors"] = req.preferred_colors
    if req.gender:
        style["gender"] = req.gender
    user.style_profile = style

    prefs = dict(user.preferences or {})
    if req.preferences:
        prefs.update(req.preferences)
    user.preferences = prefs

    # Per-occasion contexts (primary path for wear chips)
    if req.contexts:
        for occ, ctx in req.contexts.items():
            if not isinstance(ctx, dict):
                continue
            wear = ctx.get("wear")
            vibes = ctx.get("vibes") or ctx.get("vibe")
            await _upsert_style_pref(
                db,
                user.id,
                occ,
                wear_weights=wear if wear is not None else None,
                vibes=vibes if isinstance(vibes, list) else None,
                brands=ctx.get("brands") if isinstance(ctx.get("brands"), list) else None,
                budget_min=ctx.get("budget_min"),
                budget_max=ctx.get("budget_max"),
                adventure_level=ctx.get("adventure_level"),
                source=ctx.get("source") or "wizard",
            )

    # Flat current-turn wear/vibe (occurrence-scoped)
    if req.occasion and (req.wear or req.vibe):
        await _upsert_style_pref(
            db,
            user.id,
            req.occasion,
            wear_weights=req.wear or None,
            vibes=req.vibe or None,
            source="wizard",
        )

    await db.flush()
    return {
        "ok": True,
        "user_id": str(user.id),
        "style_profile": user.style_profile or {},
        "body_profile": user.body_profile or {},
    }


@router.get("/me")
async def get_me(
    authorization: str = Header(None),
    x_shopify_customer_id: str | None = Header(None, alias="X-Shopify-Customer-Id"),
    db: AsyncSession = Depends(get_db),
):
    user = await resolve_user(
        db,
        authorization=authorization,
        shopify_customer_id=x_shopify_customer_id,
        create_if_missing=False,
    )
    if not user:
        raise HTTPException(401, "Missing or invalid identity")

    return UserResponse(
        id=str(user.id),
        phone=user.phone,
        email=user.email,
        name=user.name,
        avatar_url=user.avatar_url,
        preferences=user.preferences or {},
        body_profile=user.body_profile or {},
        style_profile=user.style_profile or {},
        wallet_balance=user.wallet_balance or 0,
    ).model_dump()


@router.patch("/me")
async def update_me(
    req: UpdateProfileRequest,
    authorization: str = Header(None),
    x_shopify_customer_id: str | None = Header(None, alias="X-Shopify-Customer-Id"),
    db: AsyncSession = Depends(get_db),
):
    user = await resolve_user(
        db,
        authorization=authorization,
        shopify_customer_id=x_shopify_customer_id,
        create_if_missing=False,
    )
    if not user:
        raise HTTPException(401, "Missing or invalid identity")

    if req.name is not None:
        user.name = req.name
    if req.avatar_url is not None:
        user.avatar_url = req.avatar_url
    if req.preferences is not None:
        user.preferences = req.preferences
    if req.body_profile is not None:
        user.body_profile = req.body_profile
    if req.style_profile is not None:
        user.style_profile = req.style_profile

    await db.flush()

    return {"message": "Updated", "user_id": str(user.id)}


@router.get("/{user_id}")
async def get_user(user_id: str, authorization: str = Header(None), db: AsyncSession = Depends(get_db)):
    if not authorization:
        raise HTTPException(401, "Missing token")

    payload = verify_token(authorization.replace("Bearer ", ""))
    if not payload:
        raise HTTPException(401, "Invalid token")

    requesting_user_id = payload.get("sub")
    role = payload.get("role", "user")

    if requesting_user_id != user_id and role != "admin":
        raise HTTPException(403, "Not authorized to view this user")

    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")

    return UserResponse(
        id=str(user.id),
        phone=user.phone,
        email=user.email,
        name=user.name,
        avatar_url=user.avatar_url,
        preferences=user.preferences or {},
        body_profile=user.body_profile or {},
        style_profile=user.style_profile or {},
        wallet_balance=user.wallet_balance or 0,
    ).model_dump()
