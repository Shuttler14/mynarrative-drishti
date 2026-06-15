"""add missing columns

Revision ID: 002_add_missing
Revises: 001_initial
Create Date: 2026-06-15

"""
from alembic import op
import sqlalchemy as sa

revision = "002_add_missing"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Users table: add is_active
    op.add_column("users", sa.Column("is_active", sa.Boolean, default=True))

    # Products table: add missing columns
    op.add_column("products", sa.Column("subcategory", sa.String(255), nullable=True))
    op.add_column("products", sa.Column("gender", sa.String(50), nullable=True))
    op.add_column("products", sa.Column("original_price", sa.Float, nullable=True))
    op.add_column("products", sa.Column("discount_pct", sa.Float, nullable=True))
    op.add_column("products", sa.Column("color", sa.String(100), nullable=True))
    op.add_column("products", sa.Column("color_family", sa.String(50), nullable=True))
    op.add_column("products", sa.Column("sizes", sa.JSON, default=list))
    op.add_column("products", sa.Column("images", sa.JSON, default=list))
    op.add_column("products", sa.Column("thumbnail", sa.Text, nullable=True))
    op.add_column("products", sa.Column("rating", sa.Float, nullable=True))
    op.add_column("products", sa.Column("review_count", sa.Integer, default=0))
    op.add_column("products", sa.Column("embedding", sa.Text, nullable=True))
    op.add_column("products", sa.Column("last_scraped", sa.DateTime, nullable=True))

    # Create missing tables
    op.create_table(
        "brands",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), unique=True, nullable=False),
        sa.Column("slug", sa.String(255), unique=True, nullable=False),
        sa.Column("logo_url", sa.Text, nullable=True),
        sa.Column("tier", sa.String(50), default="mid"),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("is_indian", sa.Boolean, default=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("brands")
    op.drop_column("products", "last_scraped")
    op.drop_column("products", "embedding")
    op.drop_column("products", "review_count")
    op.drop_column("products", "rating")
    op.drop_column("products", "thumbnail")
    op.drop_column("products", "images")
    op.drop_column("products", "sizes")
    op.drop_column("products", "color_family")
    op.drop_column("products", "color")
    op.drop_column("products", "discount_pct")
    op.drop_column("products", "original_price")
    op.drop_column("products", "gender")
    op.drop_column("products", "subcategory")
    op.drop_column("users", "is_active")
