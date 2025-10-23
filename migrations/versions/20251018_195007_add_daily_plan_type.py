"""Add daily plan type to subscription enum

Revision ID: add_daily_plan_type
Revises: 83c858c3c113
Create Date: 2025-10-18 19:50:07.000000
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "add_daily_plan_type"
down_revision = "83c858c3c113"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TYPE plan_types_v1 ADD VALUE IF NOT EXISTS 'daily'")


def downgrade():
    # PostgreSQL enum values cannot be removed safely without recreating the type.
    # Leaving as no-op to avoid accidental data loss.
    pass
