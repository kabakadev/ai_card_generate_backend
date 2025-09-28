"""add bcrypt check on users.password_hash

Revision ID: 5ec7bc0c5467
Revises: 4e1f245aa8c2
Create Date: 2025-09-28 17:23:08.678589
"""
from alembic import op
import sqlalchemy as sa

# We use the low-level bcrypt lib so hashes match the pattern we enforce.
import bcrypt

revision = "5ec7bc0c5467"
down_revision = "4e1f245aa8c2"
branch_labels = None
depends_on = None

# bcrypt string regex we enforce at the DB level
BCRYPT_RE = r"^\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}$"

ADD_CHECK_SQL = rf"""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE c.conname = 'ck_users_password_bcrypt'
          AND n.nspname = 'public'
          AND t.relname = 'users'
    ) THEN
        ALTER TABLE public.users
        ADD CONSTRAINT ck_users_password_bcrypt
        CHECK (password_hash ~ '{BCRYPT_RE}');
    END IF;
END$$;
"""

DROP_CHECK_SQL = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE c.conname = 'ck_users_password_bcrypt'
          AND n.nspname = 'public'
          AND t.relname = 'users'
    ) THEN
        ALTER TABLE public.users
        DROP CONSTRAINT ck_users_password_bcrypt;
    END IF;
END$$;
"""

def _rehash_row_pw_as_bcrypt(conn, user_id: int, plaintext: str):
    # Interpret the existing column as plaintext and bcrypt it.
    hpw = bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    conn.execute(
        sa.text("UPDATE users SET password_hash = :hpw WHERE id = :uid"),
        {"hpw": hpw, "uid": user_id},
    )

def upgrade():
    conn = op.get_bind()

    # 1) Find rows that violate the upcoming CHECK
    bad_rows = conn.execute(
        sa.text(
            "SELECT id, password_hash FROM users "
            "WHERE NOT (password_hash ~ :pat)"
        ),
        {"pat": BCRYPT_RE},
    ).fetchall()

    # 2) For each, treat stored value as plaintext and b
