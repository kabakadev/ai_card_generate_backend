# services/review_service.py
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from config import db
from models.review import Review

class ReviewService:
    @staticmethod
    def get_due_cards(user_id: int, limit: int = 10):
        now = datetime.now(timezone.utc)
        stmt = (
            select(Review)
            .options(joinedload(Review.card))  # to access Flashcard.front_text
            .where(Review.user_id == user_id, Review.due_at <= now)
            .order_by(Review.due_at.asc())
            .limit(limit)
        )
        return db.session.execute(stmt).scalars().all()
