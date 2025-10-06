"""add user timestamps

Revision ID: a49f2b22ce70
Revises: 3684dec309c6
Create Date: 2025-09-11 20:49:30.661620
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a49f2b22ce70'
down_revision = '3684dec309c6'
branch_labels = None
depends_on = None


def upgrade():
    # Disable statement timeout for this transaction to prevent long ALTER/UPDATE from being killed
    op.execute("SET LOCAL statement_timeout TO 0;")

    # 1) Add columns as NULLABLE first (avoids full-table rewrite with NOT NULL at add time)
    op.add_column('users', sa.Column('created_at', sa.DateTime(), nullable=True))
    op.add_column('users', sa.Column('updated_at', sa.DateTime(), nullable=True))

    # 2) Best-effort backfill created_at from earliest activity across related tables
    op.execute("""
    WITH first_activity AS (
      SELECT user_id, MIN(first_seen) AS created_guess
      FROM (
        SELECT user_id, MIN(created_at) AS first_seen FROM decks GROUP BY user_id
        UNION ALL
        SELECT user_id, MIN(created_at) FROM payments GROUP BY user_id
        UNION ALL
        SELECT user_id, MIN(created_at) FROM ai_generations GROUP BY user_id
        UNION ALL
        SELECT user_id, MIN(last_studied_at) FROM progress GROUP BY user_id
      ) t
      GROUP BY user_id
    )
    UPDATE users u
    SET created_at = fa.created_guess
    FROM first_activity fa
    WHERE u.id = fa.user_id AND u.created_at IS NULL;
    """)

    # 3) For any users still missing created_at (no activity anywhere), set to NOW()
    op.execute("UPDATE users SET created_at = NOW() WHERE created_at IS NULL;")

    # 4) Initialize updated_at = created_at (or NOW() if you prefer)
    op.execute("UPDATE users SET updated_at = created_at WHERE updated_at IS NULL;")

    # 5) Enforce NOT NULL and set DB-side defaults for future inserts
    op.alter_column(
        'users', 'created_at',
        existing_type=sa.DateTime(),
        nullable=False,
        server_default=sa.text('NOW()')
    )
    op.alter_column(
        'users', 'updated_at',
        existing_type=sa.DateTime(),
        nullable=False,
        server_default=sa.text('NOW()')
    )


def downgrade():
    # Drop in reverse order
    op.drop_column('users', 'updated_at')
    op.drop_column('users', 'created_at')
