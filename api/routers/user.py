from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models.schema import User, OTPRecord
from api.utils.auth import create_token, verify_token

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
        expires_at=datetime.utcnow() + timedelta(minutes=10),
    )
    db.add(record)
    await db.flush()

    return {"message": "OTP sent", "otp_dev": otp}


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

    if record.expires_at < datetime.utcnow():
        raise HTTPException(400, "OTP expired")

    if record.attempts >= 5:
        raise HTTPException(429, "Too many attempts")

    input_hash = hashlib.sha256(req.otp.encode()).hexdigest()
    if not hmac.compare_digest(input_hash, record.otp_hash):
        record.attempts += 1
        await db.flush()
        raise HTTPException(400, "Invalid OTP")

    record.is_used = True

    user_stmt = select(User).where(User.phone == req.contact) | select(User).where(User.email == req.contact)
    user_result = await db.execute(user_stmt)
    user = user_result.scalar_one_or_none()

    if not user:
        user = User(phone=req.contact if req.phone else None, email=req.contact if req.email else None)
        db.add(user)
        await db.flush()

    token = create_token({"sub": str(user.id), "exp": datetime.utcnow() + timedelta(days=7)})

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
async def get_user(user_id: str, db: AsyncSession = Depends(get_db)):
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
