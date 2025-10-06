"""add demo flags

Revision ID: 25b293890c40
Revises: 05db5c7286ea
Create Date: 2025-09-20 01:00:11.752806
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "25b293890c40"
down_revision = "05db5c7286ea"
branch_labels = None
depends_on = None


def upgrade():
    # Add with a server_default to backfill existing rows, then drop the default.
    with op.batch_alter_table("users", schema=None) as b:
        b.add_column(sa.Column("is_demo", sa.Boolean(), nullable=False, server_default=sa.text("false")))
        b.add_column(sa.Column("demo_expires_at", sa.DateTime(), nullable=True))

    # Drop the default so the DB doesn't enforce it on future inserts
    op.alter_column("users", "is_demo", server_default=None, existing_type=sa.Boolean())


def downgrade():
    with op.batch_alter_table("users", schema=None) as b:
        b.drop_column("demo_expires_at")
        b.drop_column("is_demo")
