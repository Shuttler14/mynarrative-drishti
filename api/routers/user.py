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
from api.models.schema import User, OTPRecord
from api.utils.auth import create_token, verify_token

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
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
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


@router.get("/me")
async def get_me(authorization: str = Header(None), db: AsyncSession = Depends(get_db)):
    if not authorization:
        raise HTTPException(401, "Missing token")

    payload = verify_token(authorization.replace("Bearer ", ""))
    if not payload:
        raise HTTPException(401, "Invalid token")

    user = await db.get(User, payload["sub"])
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


@router.patch("/me")
async def update_me(
    req: UpdateProfileRequest,
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    if not authorization:
        raise HTTPException(401, "Missing token")

    payload = verify_token(authorization.replace("Bearer ", ""))
    if not payload:
        raise HTTPException(401, "Invalid token")

    user = await db.get(User, payload["sub"])
    if not user:
        raise HTTPException(404, "User not found")

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
