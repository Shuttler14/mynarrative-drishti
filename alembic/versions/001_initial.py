"""initial schema

Revision ID: 001_initial
Revises: 
Create Date: 2026-06-14

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("phone", sa.String(15), unique=True, nullable=True),
        sa.Column("email", sa.String(255), unique=True, nullable=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("avatar_url", sa.Text, nullable=True),
        sa.Column("role", sa.String(20), default="user"),
        sa.Column("is_verified", sa.Boolean, default=False),
        sa.Column("preferences", sa.JSON, default=dict),
        sa.Column("body_profile", sa.JSON, default=dict),
        sa.Column("style_profile", sa.JSON, default=dict),
        sa.Column("wallet_balance", sa.Integer, default=0),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "otp_records",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("contact", sa.String(255), nullable=False),
        sa.Column("otp_hash", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(50), nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("attempts", sa.Integer, default=0),
        sa.Column("is_used", sa.Boolean, default=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "products",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("brand", sa.String(255), nullable=True),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("price", sa.Float, nullable=True),
        sa.Column("currency", sa.String(3), default="INR"),
        sa.Column("image_url", sa.Text, nullable=True),
        sa.Column("product_url", sa.Text, nullable=True),
        sa.Column("availability", sa.Boolean, default=True),
        sa.Column("attributes", sa.JSON, default=dict),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("session_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(50), default="active"),
        sa.Column("body_profile", sa.JSON, default=dict),
        sa.Column("style_preferences", sa.JSON, default=dict),
        sa.Column("look_cards", sa.JSON, default=list),
        sa.Column("recommendations", sa.JSON, default=list),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "look_cards",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("sessions.id"), nullable=True),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("garments", sa.JSON, default=list),
        sa.Column("total_price", sa.Float, default=0),
        sa.Column("occasion", sa.String(100), nullable=True),
        sa.Column("confidence", sa.Float, default=0),
        sa.Column("vto_result_url", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "vton_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("garment_image", sa.Text, nullable=True),
        sa.Column("person_url", sa.Text, nullable=True),
        sa.Column("result_image", sa.Text, nullable=True),
        sa.Column("status", sa.String(50), default="queued"),
        sa.Column("vto_engine", sa.String(50), default="idm-vton"),
        sa.Column("quality_score", sa.Float, nullable=True),
        sa.Column("processing_time_ms", sa.Integer, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "scrape_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("status", sa.String(50), default="pending"),
        sa.Column("items_found", sa.Integer, default=0),
        sa.Column("items_stored", sa.Integer, default=0),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "offers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("discount_type", sa.String(20), nullable=True),
        sa.Column("discount_value", sa.Float, nullable=True),
        sa.Column("max_discount", sa.Float, nullable=True),
        sa.Column("min_order", sa.Float, default=0),
        sa.Column("coupon_code", sa.String(100), nullable=True),
        sa.Column("valid_from", sa.DateTime, nullable=True),
        sa.Column("valid_until", sa.DateTime, nullable=True),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("source", sa.String(50), default="manual"),
        sa.Column("meta", sa.JSON, default=dict),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "price_alerts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("product_id", UUID(as_uuid=True), sa.ForeignKey("products.id"), nullable=True),
        sa.Column("target_price", sa.Float, nullable=False),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "user_analyses",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("analysis_type", sa.String(50), nullable=False),
        sa.Column("input_data", sa.JSON, default=dict),
        sa.Column("results", sa.JSON, default=dict),
        sa.Column("confidence", sa.Float, default=0),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "garment_assets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("source_image", sa.Text, nullable=False),
        sa.Column("segmented_image", sa.Text, nullable=True),
        sa.Column("mask_image", sa.Text, nullable=True),
        sa.Column("meta", sa.JSON, default=dict),
        sa.Column("quality_score", sa.Float, default=0),
        sa.Column("status", sa.String(50), default="pending"),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("garment_assets")
    op.drop_table("user_analyses")
    op.drop_table("price_alerts")
    op.drop_table("offers")
    op.drop_table("scrape_jobs")
    op.drop_table("vton_jobs")
    op.drop_table("look_cards")
    op.drop_table("sessions")
    op.drop_table("products")
    op.drop_table("otp_records")
    op.drop_table("users")
