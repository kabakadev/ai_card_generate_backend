"""repair: align schema after supabase restore

Revision ID: e77d419730cf
Revises: c6e5600afabc
Create Date: 2025-09-27 12:39:33.217979
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e77d419730cf"
down_revision = "c6e5600afabc"
branch_labels = None
depends_on = None


def _colnames(inspector, table, schema="public"):
    return {c["name"] for c in inspector.get_columns(table, schema=schema)}


def _idxnames(inspector, table, schema="public"):
    return {i["name"] for i in inspector.get_indexes(table, schema=schema)}


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # ---------- users ----------
    users_cols = _colnames(insp, "users")

    if "email_verified" not in users_cols:
        op.add_column(
            "users",
            sa.Column(
                "email_verified",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            schema="public",
        )
    else:
        op.alter_column(
            "users",
            "email_verified",
            existing_type=sa.Boolean(),
            server_default=sa.text("false"),
            schema="public",
        )

    if "email_verified_at" not in users_cols:
        op.add_column(
            "users",
            sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
            schema="public",
        )

    if "is_demo" not in users_cols:
        op.add_column(
            "users",
            sa.Column(
                "is_demo",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            schema="public",
        )

    if "last_seen_at" not in users_cols:
        op.add_column(
            "users",
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            schema="public",
        )

    # best-effort backfill from legacy last_seen if it exists
    if "last_seen" in users_cols:
        op.execute(
            """
            UPDATE public.users
               SET last_seen_at = last_seen
             WHERE last_seen_at IS NULL
               AND last_seen IS NOT NULL;
            """
        )

    # ---------- otp_codes ----------
    otp_cols = _colnames(insp, "otp_codes")

    if "code_hash" not in otp_cols:
        op.add_column(
            "otp_codes",
            sa.Column("code_hash", sa.String(255), nullable=True),
            schema="public",
        )
    if "attempts" not in otp_cols:
        op.add_column(
            "otp_codes",
            sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
            schema="public",
        )
    if "max_attempts" not in otp_cols:
        op.add_column(
            "otp_codes",
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("5")),
            schema="public",
        )
    if "sent_to" not in otp_cols:
        op.add_column(
            "otp_codes",
            sa.Column("sent_to", sa.Text(), nullable=True),
            schema="public",
        )
    if "ip" not in otp_cols:
        op.add_column(
            "otp_codes",
            sa.Column("ip", sa.Text(), nullable=True),
            schema="public",
        )
    if "user_agent" not in otp_cols:
        op.add_column(
            "otp_codes",
            sa.Column("user_agent", sa.Text(), nullable=True),
            schema="public",
        )

    # Make legacy 'code' nullable so inserts that only set code_hash succeed
    if "code" in otp_cols:
        op.alter_column(
            "otp_codes",
            "code",
            existing_type=sa.String(length=64),
            nullable=True,
            schema="public",
        )
        # best-effort backfill: if code exists but code_hash is null, copy it over
        op.execute(
            """
            UPDATE public.otp_codes
               SET code_hash = code
             WHERE code_hash IS NULL
               AND code IS NOT NULL;
            """
        )

    # Ensure composite index exists
    otp_idx = _idxnames(insp, "otp_codes")
    if "ix_otp_user_purpose_active" not in otp_idx:
        op.create_index(
            "ix_otp_user_purpose_active",
            "otp_codes",
            ["user_id", "purpose", "consumed"],
            unique=False,
            schema="public",
        )


def downgrade():
    # no-op: we don't drop columns that production code depends on
    pass
