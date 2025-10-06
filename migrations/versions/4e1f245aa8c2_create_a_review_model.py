"""create a review model

Revision ID: 4e1f245aa8c2
Revises: e77d419730cf
Create Date: 2025-09-27 23:41:54.167910
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "4e1f245aa8c2"
down_revision = "e77d419730cf"
branch_labels = None
depends_on = None


def _has_table(inspector, table_name, schema=None):
    try:
        return table_name in inspector.get_table_names(schema=schema)
    except Exception:
        return False


def _has_index(inspector, table_name, index_name, schema=None):
    try:
        for idx in inspector.get_indexes(table_name, schema=schema):
            if idx.get("name") == index_name:
                return True
    except Exception:
        pass
    return False


def _has_unique_constraint(inspector, table_name, constraint_name, schema=None):
    try:
        for uc in inspector.get_unique_constraints(table_name, schema=schema):
            if uc.get("name") == constraint_name:
                return True
    except Exception:
        pass
    return False


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # 1) Create reviews table if missing
    if not _has_table(insp, "reviews"):
        op.create_table(
            "reviews",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "card_id",
                sa.Integer(),
                sa.ForeignKey("flashcards.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("ease", sa.Float(), nullable=False, server_default=sa.text("2.5")),
            sa.Column(
                "interval_days", sa.Integer(), nullable=False, server_default=sa.text("1")
            ),
            sa.Column(
                "due_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "last_reviewed_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )

    # 2) Ensure unique (user_id, card_id)
    if not _has_unique_constraint(insp, "reviews", "uq_reviews_user_card"):
        op.create_unique_constraint(
            "uq_reviews_user_card", "reviews", ["user_id", "card_id"]
        )

    # 3) Ensure composite index (user_id, due_at)
    if not _has_index(insp, "reviews", "ix_reviews_user_due"):
        op.create_index(
            "ix_reviews_user_due", "reviews", ["user_id", "due_at"], unique=False
        )


def downgrade():
    # Only drop what we created, and guard for existence.
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # Drop index if present
    if _has_index(insp, "reviews", "ix_reviews_user_due"):
        try:
            op.drop_index("ix_reviews_user_due", table_name="reviews")
        except Exception:
            # Fallback for cases where op.drop_index fails due to IF EXISTS need
            op.execute("DROP INDEX IF EXISTS ix_reviews_user_due")

    # Drop unique constraint if present
    if _has_table(insp, "reviews"):
        if _has_unique_constraint(insp, "reviews", "uq_reviews_user_card"):
            try:
                op.drop_constraint(
                    "uq_reviews_user_card", "reviews", type_="unique"
                )
            except Exception:
                op.execute(
                    "ALTER TABLE reviews DROP CONSTRAINT IF EXISTS uq_reviews_user_card"
                )

        # Finally drop the table
        try:
            op.drop_table("reviews")
        except Exception:
            op.execute("DROP TABLE IF EXISTS reviews CASCADE")
