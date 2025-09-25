from sqlalchemy.orm import validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy_serializer import SerializerMixin
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
import re
from config import db, bcrypt
from sqlalchemy.dialects.postgresql import JSONB
from datetime import datetime
from sqlalchemy.orm import relationship

class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    email_verified = db.Column(db.Boolean, nullable=False, default=False, server_default=db.text("false"))
    email_verified_at = db.Column(db.DateTime, nullable=True)
    _password_hash = db.Column("password_hash", db.String(255), nullable=False)
    is_demo = db.Column(db.Boolean, nullable=False, default=False)
    demo_expires_at = db.Column(db.DateTime, nullable=True)
    last_seen_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # PARENT-SIDE RELATIONSHIPS (single source of truth)
    decks = db.relationship('Deck', backref='user', passive_deletes=True)
    progress = db.relationship('Progress', backref='user', passive_deletes=True)
    otp_codes = db.relationship("OTPCode", backref='user', passive_deletes=True)
    trusted_devices = db.relationship("TrustedDevice", backref='user', passive_deletes=True)
    ai_generations = db.relationship("AIGeneration", backref='user', passive_deletes=True)
    payment_transactions = db.relationship("PaymentTransaction", backref='user', passive_deletes=True)
    payments = db.relationship("Payment", backref='user', passive_deletes=True)
    usage_limits = db.relationship("UsageLimits", backref='user', passive_deletes=True)

    # One-to-one subscription
    subscription = db.relationship(
        "Subscription",
        backref=db.backref("user"),
        uselist=False,
        passive_deletes=True
    )

    serialize_rules = ('-decks.user', '-progress.user')

    @hybrid_property
    def password_hash(self):
        return self._password_hash

    @password_hash.setter
    def password_hash(self, password):
        self._password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        return bcrypt.check_password_hash(self._password_hash, password)

    @validates("email")
    def validate_email(self, key, email):
        email_regex = r'^[\w\.-]+@[\w\.-]+\.\w+$'
        if not re.match(email_regex, email):
            raise ValueError("Invalid email format")
        return email.lower()

    @validates("username")
    def validate_username(self, key, username):
        if len(username) < 3 or len(username) > 50:
            raise ValueError("Username must be between 3 and 50 characters")
        return username

class Deck(db.Model, SerializerMixin):
    __tablename__ = 'decks'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    title = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    subject = db.Column(db.String(50))
    category = db.Column(db.String(50))
    difficulty = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, server_default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())
    is_default = db.Column(db.Boolean, default=False, nullable=False, server_default='0')

    flashcards = db.relationship('Flashcard', backref='deck', passive_deletes=True)
    ai_generations = db.relationship("AIGeneration", backref='deck', passive_deletes=True)

    serialize_rules = ('-user.decks', '-flashcards.deck')

class Flashcard(db.Model, SerializerMixin):
    __tablename__ = 'flashcards'

    id = db.Column(db.Integer, primary_key=True)
    deck_id = db.Column(db.Integer, db.ForeignKey('decks.id', ondelete='CASCADE'), nullable=False)
    front_text = db.Column(db.Text, nullable=False)
    back_text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, server_default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())

    progress = db.relationship('Progress', backref='flashcard', passive_deletes=True)

    serialize_rules = ('-deck.flashcards','-progress.flashcard')

class Progress(db.Model, SerializerMixin):
    __tablename__ = 'progress'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    deck_id = db.Column(db.Integer, db.ForeignKey('decks.id', ondelete='CASCADE'), nullable=False)
    flashcard_id = db.Column(db.Integer, db.ForeignKey('flashcards.id', ondelete='CASCADE'), nullable=False)

    study_count = db.Column(db.Integer, default=0, nullable=False)
    correct_attempts = db.Column(db.Integer, default=0, nullable=False)
    incorrect_attempts = db.Column(db.Integer, default=0, nullable=False)
    total_study_time = db.Column(db.Float, default=0.0, nullable=False)
    last_studied_at = db.Column(db.DateTime, server_default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())
    next_review_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())
    review_status = db.Column(db.Enum('new', 'learning', 'reviewing', 'mastered', name="review_status"), default='new', nullable=False)
    is_learned = db.Column(db.Boolean, default=False, nullable=False)
    interval = db.Column(db.Float, default=1.0)

    serialize_rules = ('-user.progress', '-deck.progress')
    __table_args__ = (db.UniqueConstraint('user_id', 'flashcard_id', name='unique_user_flashcard_progress'),)

class UserStats(db.Model, SerializerMixin):
    __tablename__ = 'user_stats'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), unique=True, nullable=False)

    weekly_goal = db.Column(db.Integer, default=0)
    mastery_level = db.Column(db.Float, default=0.0)
    study_streak = db.Column(db.Integer, default=0)
    focus_score = db.Column(db.Float, default=0.0)
    retention_rate = db.Column(db.Float, default=0.0)
    cards_mastered = db.Column(db.Integer, default=0)
    minutes_per_day = db.Column(db.Float, default=0.0)
    accuracy = db.Column(db.Float, default=0.0)

    user = db.relationship("User", backref=db.backref("stats", uselist=False, passive_deletes=True))

    serialize_rules = ('-user.stats',)

class Payment(db.Model):
    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete='CASCADE'), nullable=False, index=True)
    provider = db.Column(db.String(32), nullable=False, default="intasend")
    status = db.Column(db.String(32), nullable=False)
    amount_cents = db.Column(db.Integer, nullable=False)
    currency = db.Column(db.String(8), nullable=False, default="KES")
    checkout_id = db.Column(db.String(128), index=True)
    raw_payload = db.Column(JSONB)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

class UserCredits(db.Model):
    __tablename__ = "user_credits"

    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete='CASCADE'), primary_key=True)
    credits = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("credits_row", uselist=False, passive_deletes=True))

class AIGeneration(db.Model):
    __tablename__ = "ai_generations"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    deck_id = Column(Integer, ForeignKey("decks.id", ondelete="CASCADE"), nullable=True, index=True)
    source_type = Column(String(32), nullable=False)
    source_excerpt = Column(Text)
    prompt = Column(Text)
    model = Column(String(128))
    status = Column(String(32), nullable=False, default="queued")
    output = Column(JSONB)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

# Import other models
from .billing.subscription import Subscription
from .billing.payment_transaction import PaymentTransaction
from .billing.usage_limits import UsageLimits
from .security.otp_code import OTPCode
from .security.trusted_device import TrustedDevice

__all__ = [
    "User", "Deck", "Flashcard", "Progress", "UserStats",
    "Payment", "UserCredits", "AIGeneration",
    "Subscription", "PaymentTransaction", "UsageLimits",
    "OTPCode", "TrustedDevice",
]
