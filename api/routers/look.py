from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models.schema import Session, LookCard, User
from api.utils.auth import verify_token

router = APIRouter()


class CreateSessionRequest(BaseModel):
    session_type: str = "discovery"
    occasion: str | None = None
    budget_min: float | None = None
    budget_max: float | None = None
    style_preferences: list[str] = []
    context: dict = {}


class AddLookCardRequest(BaseModel):
    title: str | None = None
    occasion: str | None = None
    garments: list[dict] = []
    total_price: float | None = None


class FeedbackRequest(BaseModel):
    feedback: str


def _get_user_id(authorization: str | None) -> str | None:
    """Extract user_id from authorization header. Returns None if not authenticated."""
    if not authorization:
        return None
    payload = verify_token(authorization.replace("Bearer ", ""))
    return payload["sub"] if payload else None


def _require_user(authorization: str | None) -> str:
    """Extract and verify user from authorization header. Raises 401 if not authenticated."""
    if not authorization:
        raise HTTPException(401, "Missing token")
    payload = verify_token(authorization.replace("Bearer ", ""))
    if not payload:
        raise HTTPException(401, "Invalid token")
    return payload["sub"]


@router.post("/sessions")
async def create_session(
    req: CreateSessionRequest,
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    user_id = _get_user_id(authorization)

    session = Session(
        user_id=user_id,
        session_type=req.session_type,
        occasion=req.occasion,
        budget_min=req.budget_min,
        budget_max=req.budget_max,
        style_preferences=req.style_preferences,
        context=req.context,
    )
    db.add(session)
    await db.flush()

    return {
        "session_id": str(session.id),
        "session_type": session.session_type,
        "status": session.status,
    }


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    user_id = _require_user(authorization)

    session = await db.get(Session, session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    # Ownership check: users can only access their own sessions
    if session.user_id and str(session.user_id) != user_id:
        raise HTTPException(403, "Not authorized to access this session")

    look_cards_stmt = select(LookCard).where(LookCard.session_id == session.id)
    result = await db.execute(look_cards_stmt)
    look_cards = result.scalars().all()

    return {
        "session_id": str(session.id),
        "session_type": session.session_type,
        "status": session.status,
        "occasion": session.occasion,
        "context": session.context,
        "look_cards": [
            {
                "id": str(lc.id),
                "title": lc.title,
                "occasion": lc.occasion,
                "garments": lc.garments or [],
                "total_price": lc.total_price,
                "vto_images": lc.vto_images or [],
                "score": lc.score,
                "feedback": lc.feedback,
                "is_saved": lc.is_saved,
            }
            for lc in look_cards
        ],
    }


@router.post("/sessions/{session_id}/looks")
async def add_look_card(
    session_id: str,
    req: AddLookCardRequest,
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    user_id = _require_user(authorization)

    session = await db.get(Session, session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    # Ownership check
    if session.user_id and str(session.user_id) != user_id:
        raise HTTPException(403, "Not authorized to modify this session")

    look_card = LookCard(
        session_id=session.id,
        user_id=session.user_id,
        title=req.title,
        occasion=req.occasion,
        garments=req.garments,
        total_price=req.total_price,
    )
    db.add(look_card)
    await db.flush()

    return {"look_id": str(look_card.id), "message": "Look card created"}


@router.post("/looks/{look_id}/feedback")
async def add_feedback(
    look_id: str,
    req: FeedbackRequest,
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    user_id = _require_user(authorization)

    look_card = await db.get(LookCard, look_id)
    if not look_card:
        raise HTTPException(404, "Look card not found")

    # Ownership check
    if look_card.user_id and str(look_card.user_id) != user_id:
        raise HTTPException(403, "Not authorized to modify this look card")

    look_card.feedback = req.feedback
    return {"message": "Feedback recorded"}


@router.post("/looks/{look_id}/save")
async def save_look(
    look_id: str,
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    user_id = _require_user(authorization)

    look_card = await db.get(LookCard, look_id)
    if not look_card:
        raise HTTPException(404, "Look card not found")

    # Ownership check
    if look_card.user_id and str(look_card.user_id) != user_id:
        raise HTTPException(403, "Not authorized to modify this look card")

    look_card.is_saved = True
    return {"message": "Look saved"}


@router.delete("/looks/{look_id}")
async def delete_look(
    look_id: str,
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    user_id = _require_user(authorization)

    look_card = await db.get(LookCard, look_id)
    if not look_card:
        raise HTTPException(404, "Look card not found")

    # Ownership check
    if look_card.user_id and str(look_card.user_id) != user_id:
        raise HTTPException(403, "Not authorized to delete this look card")

    await db.delete(look_card)
    return {"message": "Look deleted"}


@router.get("/sessions")
async def list_sessions(
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    user_id = _require_user(authorization)

    stmt = select(Session).where(Session.user_id == user_id).order_by(Session.created_at.desc()).limit(50)
    result = await db.execute(stmt)
    sessions = result.scalars().all()

    return {
        "sessions": [
            {
                "id": str(s.id),
                "session_type": s.session_type,
                "status": s.status,
                "occasion": s.occasion,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in sessions
        ]
    }
