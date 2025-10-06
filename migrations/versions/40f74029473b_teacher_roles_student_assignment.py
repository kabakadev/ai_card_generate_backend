"""teacher roles + student assignment

Revision ID: 40f74029473b
Revises: 5ec7bc0c5467
Create Date: 2025-10-05 23:06:22.744523
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '40f74029473b'
down_revision = '5ec7bc0c5467'
branch_labels = None
depends_on = None


def upgrade():
    # --- 1) Ensure ENUM types exist BEFORE using them (idempotent) ---

    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'user_role') THEN
            CREATE TYPE user_role AS ENUM ('student','teacher','admin');
        END IF;
    END
    $$;
    """)

    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'student_deck_status') THEN
            CREATE TYPE student_deck_status AS ENUM ('active','paused','archived');
        END IF;
    END
    $$;
    """)

    # --- 2) users table additions ---
    op.add_column(
        'users',
        sa.Column(
            'role',
            postgresql.ENUM('student', 'teacher', 'admin', name='user_role', create_type=False),
            server_default='student',
            nullable=False,
        )
    )
    op.add_column('users', sa.Column('teacher_id', sa.Integer(), nullable=True))
    op.add_column('users', sa.Column('disabled_at', sa.DateTime(), nullable=True))
    op.create_foreign_key('fk_users_teacher', 'users', 'users', ['teacher_id'], ['id'], ondelete='SET NULL')
    op.create_index('ix_users_teacher_demo', 'users', ['teacher_id', 'is_demo'], unique=False)

    # --- 3) student_decks join table ---
    op.create_table(
        'student_decks',
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('deck_id', sa.Integer(), nullable=False),
        sa.Column('assigned_by_user_id', sa.Integer(), nullable=True),
        sa.Column(
            'status',
            postgresql.ENUM('active', 'paused', 'archived', name='student_deck_status', create_type=False),
            server_default='active',
            nullable=False
        ),
        sa.Column('assigned_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.ForeignKeyConstraint(['student_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['deck_id'], ['decks.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['assigned_by_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('student_id', 'deck_id'),
    )
    op.create_index('ix_student_decks_assigned_by_user_id', 'student_decks', ['assigned_by_user_id'], unique=False)
    op.create_index('ix_student_decks_student', 'student_decks', ['student_id'], unique=False)
    op.create_index('ix_student_decks_deck', 'student_decks', ['deck_id'], unique=False)


def downgrade():
    # Drop dependent objects first (reverse order)

    # student_decks
    op.drop_index('ix_student_decks_deck', table_name='student_decks')
    op.drop_index('ix_student_decks_student', table_name='student_decks')
    op.drop_index('ix_student_decks_assigned_by_user_id', table_name='student_decks')
    op.drop_table('student_decks')

    # users columns
    op.drop_index('ix_users_teacher_demo', table_name='users')
    op.drop_constraint('fk_users_teacher', 'users', type_='foreignkey')
    op.drop_column('users', 'disabled_at')
    op.drop_column('users', 'teacher_id')
    op.drop_column('users', 'role')

    # Now that nothing depends on the enums, drop the enum types (safe if unused)
    op.execute("DROP TYPE IF EXISTS student_deck_status")
    op.execute("DROP TYPE IF EXISTS user_role")
