"""Add ON DELETE CASCADE to all user foreign keys

Revision ID: c6e5600afabc
Revises: 312c65738a5d
Create Date: 2025-09-22 20:53:04.653681
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "c6e5600afabc"
down_revision = "312c65738a5d"
branch_labels = None
depends_on = None

SCHEMA = "public"


def _exists_regclass(qname: str) -> bool:
    bind = op.get_bind()
    return bind.execute(
        sa.text("SELECT to_regclass(:qname) IS NOT NULL").bindparams(sa.bindparam("qname", qname))
    ).scalar()


def _table_exists(table: str) -> bool:
    return _exists_regclass(f"{SCHEMA}.{table}")


def _index_exists(index: str) -> bool:
    return _exists_regclass(f"{SCHEMA}.{index}")


def _drop_index(index: str):
    # index is unqualified name; we qualify to schema explicitly
    op.execute(sa.text(f'DROP INDEX IF EXISTS "{SCHEMA}"."{index}"'))


def _drop_fk(table: str, constraint: str):
    op.execute(sa.text(f'ALTER TABLE "{SCHEMA}"."{table}" DROP CONSTRAINT IF EXISTS "{constraint}"'))


def _add_fk(
    table: str,
    constraint: str,
    cols: list[str],
    ref_table: str,
    ref_cols: list[str],
    ondelete: str | None = None,
):
    cols_sql = ", ".join([f'"{c}"' for c in cols])
    ref_cols_sql = ", ".join([f'"{c}"' for c in ref_cols])
    ondel = f" ON DELETE {ondelete}" if ondelete else ""
    op.execute(
        sa.text(
            f'ALTER TABLE "{SCHEMA}"."{table}" '
            f'ADD CONSTRAINT "{constraint}" FOREIGN KEY ({cols_sql}) '
            f'REFERENCES "{SCHEMA}"."{ref_table}" ({ref_cols_sql}){ondel}'
        )
    )


def upgrade():
    # DECKS: user_id -> users.id
    if _table_exists("decks"):
        if _index_exists("idx_decks_user_id"):
            _drop_index("idx_decks_user_id")
        _drop_fk("decks", "decks_user_id_fkey")
        _add_fk("decks", "decks_user_id_fkey", ["user_id"], "users", ["id"], ondelete="CASCADE")

    # FLASHCARDS: deck_id -> decks.id
    if _table_exists("flashcards"):
        if _index_exists("idx_flashcards_deck_id"):
            _drop_index("idx_flashcards_deck_id")
        _drop_fk("flashcards", "flashcards_deck_id_fkey")
        _add_fk("flashcards", "flashcards_deck_id_fkey", ["deck_id"], "decks", ["id"], ondelete="CASCADE")

    # OTP_CODES: drop aux indexes only (no FK here in your file)
    if _table_exists("otp_codes"):
        for idx in ("idx_otp_codes_created_at", "idx_otp_codes_expires_at", "idx_otp_codes_user_id"):
            if _index_exists(idx):
                _drop_index(idx)

    # PAYMENT_TRANSACTIONS: drop aux indexes only
    if _table_exists("payment_transactions"):
        for idx in ("idx_payment_transactions_created_at", "idx_payment_transactions_user_id"):
            if _index_exists(idx):
                _drop_index(idx)

    # PAYMENTS: user_id -> users.id
    if _table_exists("payments"):
        _drop_fk("payments", "payments_user_id_fkey")
        _add_fk("payments", "payments_user_id_fkey", ["user_id"], "users", ["id"], ondelete="CASCADE")

    # PROGRESS: user_id -> users.id, deck_id -> decks.id (+ drop aux indexes)
    if _table_exists("progress"):
        for idx in (
            "idx_progress_deck_id",
            "idx_progress_flashcard_id",
            "idx_progress_user_deck",
            "idx_progress_user_flashcard",
            "idx_progress_user_id",
        ):
            if _index_exists(idx):
                _drop_index(idx)
        _drop_fk("progress", "progress_user_id_fkey")
        _drop_fk("progress", "progress_deck_id_fkey")
        _add_fk("progress", "progress_user_id_fkey", ["user_id"], "users", ["id"], ondelete="CASCADE")
        _add_fk("progress", "progress_deck_id_fkey", ["deck_id"], "decks", ["id"], ondelete="CASCADE")

    # SUBSCRIPTIONS: drop aux indexes only
    if _table_exists("subscriptions"):
        for idx in ("idx_subscriptions_end_date", "idx_subscriptions_status", "idx_subscriptions_user_id"):
            if _index_exists(idx):
                _drop_index(idx)

    # TRUSTED_DEVICES: drop aux index only
    if _table_exists("trusted_devices"):
        if _index_exists("idx_trusted_devices_user_id"):
            _drop_index("idx_trusted_devices_user_id")

    # USAGE_LIMITS: drop aux index only
    if _table_exists("usage_limits"):
        if _index_exists("idx_usage_limits_user_id"):
            _drop_index("idx_usage_limits_user_id")

    # USER_CREDITS: user_id -> users.id
    if _table_exists("user_credits"):
        _drop_fk("user_credits", "user_credits_user_id_fkey")
        _add_fk("user_credits", "user_credits_user_id_fkey", ["user_id"], "users", ["id"], ondelete="CASCADE")

    # USER_STATS: user_id -> users.id
    if _table_exists("user_stats"):
        _drop_fk("user_stats", "user_stats_user_id_fkey")
        _add_fk("user_stats", "user_stats_user_id_fkey", ["user_id"], "users", ["id"], ondelete="CASCADE")


def downgrade():
    # Reverse: re-add dropped indexes (IF NOT EXISTS) and remove CASCADE from FKs

    def _create_index(table: str, index: str, cols: list[str]):
        cols_sql = ", ".join([f'"{c}"' for c in cols])
        op.execute(
            sa.text(
                f'CREATE INDEX IF NOT EXISTS "{index}" ON "{SCHEMA}"."{table}" ({cols_sql})'
            )
        )

    # USER_STATS
    if _table_exists("user_stats"):
        _drop_fk("user_stats", "user_stats_user_id_fkey")
        _add_fk("user_stats", "user_stats_user_id_fkey", ["user_id"], "users", ["id"])

    # USER_CREDITS
    if _table_exists("user_credits"):
        _drop_fk("user_credits", "user_credits_user_id_fkey")
        _add_fk("user_credits", "user_credits_user_id_fkey", ["user_id"], "users", ["id"])

    # USAGE_LIMITS (indexes only)
    if _table_exists("usage_limits"):
        _create_index("usage_limits", "idx_usage_limits_user_id", ["user_id"])

    # TRUSTED_DEVICES (indexes only)
    if _table_exists("trusted_devices"):
        _create_index("trusted_devices", "idx_trusted_devices_user_id", ["user_id"])

    # SUBSCRIPTIONS (indexes only)
    if _table_exists("subscriptions"):
        _create_index("subscriptions", "idx_subscriptions_user_id", ["user_id"])
        _create_index("subscriptions", "idx_subscriptions_status", ["status"])
        _create_index("subscriptions", "idx_subscriptions_end_date", ["end_date"])

    # PROGRESS
    if _table_exists("progress"):
        _drop_fk("progress", "progress_user_id_fkey")
        _drop_fk("progress", "progress_deck_id_fkey")
        _add_fk("progress", "progress_user_id_fkey", ["user_id"], "users", ["id"])
        _add_fk("progress", "progress_deck_id_fkey", ["deck_id"], "decks", ["id"])
        _create_index("progress", "idx_progress_user_id", ["user_id"])
        _create_index("progress", "idx_progress_user_flashcard", ["user_id", "flashcard_id"])
        _create_index("progress", "idx_progress_user_deck", ["user_id", "deck_id"])
        _create_index("progress", "idx_progress_flashcard_id", ["flashcard_id"])
        _create_index("progress", "idx_progress_deck_id", ["deck_id"])

    # PAYMENTS
    if _table_exists("payments"):
        _drop_fk("payments", "payments_user_id_fkey")
        _add_fk("payments", "payments_user_id_fkey", ["user_id"], "users", ["id"])

    # PAYMENT_TRANSACTIONS (indexes only)
    if _table_exists("payment_transactions"):
        _create_index("payment_transactions", "idx_payment_transactions_user_id", ["user_id"])
        _create_index("payment_transactions", "idx_payment_transactions_created_at", ["created_at"])

    # OTP_CODES (indexes only)
    if _table_exists("otp_codes"):
        _create_index("otp_codes", "idx_otp_codes_user_id", ["user_id"])
        _create_index("otp_codes", "idx_otp_codes_expires_at", ["expires_at"])
        _create_index("otp_codes", "idx_otp_codes_created_at", ["created_at"])

    # FLASHCARDS
    if _table_exists("flashcards"):
        _drop_fk("flashcards", "flashcards_deck_id_fkey")
        _add_fk("flashcards", "flashcards_deck_id_fkey", ["deck_id"], "decks", ["id"])
        _create_index("flashcards", "idx_flashcards_deck_id", ["deck_id"])

    # DECKS
    if _table_exists("decks"):
        _drop_fk("decks", "decks_user_id_fkey")
        _add_fk("decks", "decks_user_id_fkey", ["user_id"], "users", ["id"])
        _create_index("decks", "idx_decks_user_id", ["user_id"])
