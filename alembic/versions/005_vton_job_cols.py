"""add missing vton_jobs columns (session_id, person_image)

Revision ID: 005_vton_job_cols
Revises: 004_fashion_dna
Create Date: 2026-09-24

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "005_vton_job_cols"
down_revision = "004_fashion_dna"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "vton_jobs",
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("sessions.id"), nullable=True),
    )
    op.add_column("vton_jobs", sa.Column("person_image", sa.Text, nullable=True))
    op.create_index("idx_vton_status", "vton_jobs", ["status"])


def downgrade() -> None:
    op.drop_index("idx_vton_status", table_name="vton_jobs")
    op.drop_column("vton_jobs", "person_image")
    op.drop_column("vton_jobs", "session_id")
