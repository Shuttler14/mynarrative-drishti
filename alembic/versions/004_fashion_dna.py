"""fashion DNA: user_style_prefs, user_events, users.shopify_customer_id

Revision ID: 004_fashion_dna
Revises: 003_add_source_url
Create Date: 2026-09-24

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "004_fashion_dna"
down_revision = "003_add_source_url"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("shopify_customer_id", sa.String(64), nullable=True))
    op.create_index("ix_users_shopify_customer_id", "users", ["shopify_customer_id"], unique=True)

    op.create_table(
        "user_style_prefs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("occasion", sa.String(100), nullable=False),
        sa.Column("wear_weights", sa.JSON, default=dict),
        sa.Column("vibes", sa.JSON, default=list),
        sa.Column("brands", sa.JSON, default=list),
        sa.Column("budget_min", sa.Float, nullable=True),
        sa.Column("budget_max", sa.Float, nullable=True),
        sa.Column("adventure_level", sa.String(20), default="balanced"),
        sa.Column("confidence", sa.Float, default=0.5),
        sa.Column("source", sa.String(50), default="wizard"),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("user_id", "occasion", name="uq_user_style_pref_occasion"),
    )
    op.create_index("ix_user_style_prefs_user_id", "user_style_prefs", ["user_id"])
    op.create_index("ix_user_style_prefs_occasion", "user_style_prefs", ["occasion"])

    op.create_table(
        "user_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("occasion", sa.String(100), nullable=True),
        sa.Column("session_id", sa.String(100), nullable=True),
        sa.Column("payload", sa.JSON, default=dict),
        sa.Column("weight", sa.Float, default=1.0),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_user_events_user_id", "user_events", ["user_id"])
    op.create_index("ix_user_events_event_type", "user_events", ["event_type"])
    op.create_index("ix_user_events_created_at", "user_events", ["created_at"])
    op.create_index("ix_user_events_user_type", "user_events", ["user_id", "event_type"])


def downgrade() -> None:
    op.drop_index("ix_user_events_user_type", table_name="user_events")
    op.drop_index("ix_user_events_created_at", table_name="user_events")
    op.drop_index("ix_user_events_event_type", table_name="user_events")
    op.drop_index("ix_user_events_user_id", table_name="user_events")
    op.drop_table("user_events")

    op.drop_index("ix_user_style_prefs_occasion", table_name="user_style_prefs")
    op.drop_index("ix_user_style_prefs_user_id", table_name="user_style_prefs")
    op.drop_table("user_style_prefs")

    op.drop_index("ix_users_shopify_customer_id", table_name="users")
    op.drop_column("users", "shopify_customer_id")
