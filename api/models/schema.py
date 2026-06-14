from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean, Column, DateTime, Enum, Float, ForeignKey,
    Index, Integer, JSON, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    phone = Column(String(15), unique=True, nullable=True)
    email = Column(String(255), unique=True, nullable=True)
    name = Column(String(255), nullable=True)
    avatar_url = Column(Text, nullable=True)
    is_verified = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    role = Column(String(20), default="user")
    preferences = Column(JSON, default=dict)
    body_profile = Column(JSON, default=dict)
    style_profile = Column(JSON, default=dict)
    wallet_balance = Column(Integer, default=0)

    sessions = relationship("Session", back_populates="user")
    analyses = relationship("UserAnalysis", back_populates="user")


class OTPRecord(Base, TimestampMixin):
    __tablename__ = "otp_records"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    contact = Column(String(255), nullable=False)
    otp_hash = Column(String(255), nullable=False)
    purpose = Column(String(50), default="login")
    is_used = Column(Boolean, default=False)
    expires_at = Column(DateTime, nullable=False)
    attempts = Column(Integer, default=0)

    __table_args__ = (
        Index("idx_otp_contact_purpose", "contact", "purpose"),
    )


class Product(Base, TimestampMixin):
    __tablename__ = "products"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    source = Column(String(50), nullable=False)
    source_id = Column(String(255), nullable=False)
    source_url = Column(Text, nullable=True)
    name = Column(Text, nullable=False)
    brand = Column(String(255), nullable=True)
    category = Column(String(255), nullable=True)
    subcategory = Column(String(255), nullable=True)
    gender = Column(String(50), nullable=True)
    price = Column(Float, nullable=True)
    original_price = Column(Float, nullable=True)
    discount_pct = Column(Float, nullable=True)
    currency = Column(String(10), default="INR")
    color = Column(String(100), nullable=True)
    color_family = Column(String(50), nullable=True)
    sizes = Column(JSON, default=list)
    images = Column(JSON, default=list)
    thumbnail = Column(Text, nullable=True)
    attributes = Column(JSON, default=dict)
    rating = Column(Float, nullable=True)
    review_count = Column(Integer, default=0)
    availability = Column(Boolean, default=True)
    embedding = Column(Text, nullable=True)
    last_scraped = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_product_source"),
        Index("idx_product_category", "category"),
        Index("idx_product_brand", "brand"),
        Index("idx_product_gender", "gender"),
    )


class Brand(Base, TimestampMixin):
    __tablename__ = "brands"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String(255), unique=True, nullable=False)
    slug = Column(String(255), unique=True, nullable=False)
    logo_url = Column(Text, nullable=True)
    tier = Column(String(50), default="mid")
    description = Column(Text, nullable=True)
    is_indian = Column(Boolean, default=False)


class Session(Base, TimestampMixin):
    __tablename__ = "sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    session_type = Column(String(50), nullable=False)
    status = Column(String(50), default="active")
    context = Column(JSON, default=dict)
    occasion = Column(String(100), nullable=True)
    budget_min = Column(Float, nullable=True)
    budget_max = Column(Float, nullable=True)
    style_preferences = Column(JSON, default=list)
    recommendations = Column(JSON, default=list)
    vto_results = Column(JSON, default=list)
    analytics = Column(JSON, default=dict)

    user = relationship("User", back_populates="sessions")
    look_cards = relationship("LookCard", back_populates="session")


class LookCard(Base, TimestampMixin):
    __tablename__ = "look_cards"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    session_id = Column(UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    title = Column(String(255), nullable=True)
    occasion = Column(String(100), nullable=True)
    garments = Column(JSON, default=list)
    total_price = Column(Float, nullable=True)
    vto_images = Column(JSON, default=list)
    score = Column(Float, default=0.0)
    feedback = Column(String(20), nullable=True)
    is_saved = Column(Boolean, default=False)

    session = relationship("Session", back_populates="look_cards")

    __table_args__ = (
        Index("idx_lookcard_session", "session_id"),
    )


class PriceAlert(Base, TimestampMixin):
    __tablename__ = "price_alerts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=True)
    source = Column(String(50), nullable=False)
    source_id = Column(String(255), nullable=False)
    product_name = Column(Text, nullable=True)
    target_price = Column(Float, nullable=False)
    current_price = Column(Float, nullable=True)
    is_active = Column(Boolean, default=True)
    last_checked = Column(DateTime, nullable=True)
    triggered_at = Column(DateTime, nullable=True)


class Offer(Base, TimestampMixin):
    __tablename__ = "offers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    bank = Column(String(255), nullable=False)
    card_network = Column(String(50), nullable=True)
    card_type = Column(String(50), nullable=True)
    title = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    discount_type = Column(String(50), nullable=True)
    discount_value = Column(Float, nullable=True)
    max_discount = Column(Float, nullable=True)
    min_order = Column(Float, default=0)
    coupon_code = Column(String(100), nullable=True)
    valid_from = Column(DateTime, nullable=True)
    valid_until = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)
    source = Column(String(50), default="manual")
    meta = Column(JSON, default=dict)


class UserAnalysis(Base, TimestampMixin):
    __tablename__ = "user_analyses"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    analysis_type = Column(String(50), nullable=False)
    input_data = Column(JSON, default=dict)
    results = Column(JSON, default=dict)
    confidence = Column(Float, default=0.0)

    user = relationship("User", back_populates="analyses")

    __table_args__ = (
        Index("idx_analysis_user_type", "user_id", "analysis_type"),
    )


class GarmentAsset(Base, TimestampMixin):
    __tablename__ = "garment_assets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    source_image = Column(Text, nullable=False)
    segmented_image = Column(Text, nullable=True)
    mask_image = Column(Text, nullable=True)
    meta = Column(JSON, default=dict)
    quality_score = Column(Float, default=0.0)
    status = Column(String(50), default="pending")


class VTONJob(Base, TimestampMixin):
    __tablename__ = "vton_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    session_id = Column(UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True)
    garment_image = Column(Text, nullable=False)
    person_image = Column(Text, nullable=True)
    person_url = Column(Text, nullable=True)
    result_image = Column(Text, nullable=True)
    status = Column(String(50), default="pending")
    vto_engine = Column(String(50), default="idm-vton")
    error_message = Column(Text, nullable=True)
    processing_time_ms = Column(Integer, nullable=True)

    __table_args__ = (
        Index("idx_vton_status", "status"),
    )


class ScrapeJob(Base, TimestampMixin):
    __tablename__ = "scrape_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    source = Column(String(50), nullable=False)
    category = Column(String(255), nullable=True)
    status = Column(String(50), default="pending")
    items_found = Column(Integer, default=0)
    items_stored = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_scrape_source_status", "source", "status"),
    )
