"""add source_url to products

Revision ID: 003_add_source_url
Revises: 002_add_missing
Create Date: 2026-06-15

"""
from alembic import op
import sqlalchemy as sa

revision = "003_add_source_url"
down_revision = "002_add_missing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("source_url", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("products", "source_url")
