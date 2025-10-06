# models/review.py
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, ForeignKey, DateTime, Float, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from config import db  # you import db from config everywhere else

class Review(db.Model):
    __tablename__ = "reviews"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    card_id = Column(Integer, ForeignKey("flashcards.id", ondelete="CASCADE"), nullable=False, index=True)

    # spaced repetition state
    ease = Column(Float, nullable=False, default=2.5)
    interval_days = Column(Integer, nullable=False, default=1)

    # scheduling
    due_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    last_reviewed_at = Column(DateTime(timezone=True), nullable=True)

    # helpful relationship (Flashcard has front_text/back_text in your schema)
    card = relationship("Flashcard", backref="reviews", passive_deletes=True)

    __table_args__ = (
        UniqueConstraint("user_id", "card_id", name="uq_reviews_user_card"),
        Index("ix_reviews_user_due", "user_id", "due_at"),
    )
