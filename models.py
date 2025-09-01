from sqlalchemy.orm import validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy_serializer import SerializerMixin
from sqlalchemy import ForeignKey
import re
from config import db, bcrypt

class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    _password_hash = db.Column("password_hash", db.String(255), nullable=False)

    decks = db.relationship('Deck', backref='user', cascade="all, delete-orphan")
    progress = db.relationship('Progress', backref='user', cascade="all, delete-orphan")

    serialize_rules = ('-decks.user', '-progress.user')

    @hybrid_property
    def password_hash(self):
        return self._password_hash

    @password_hash.setter
    def password_hash(self, password):
        self._password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        return bcrypt.check_password_hash(self._password_hash, password)

    # VALIDATIONS
    @validates("email")
    def validate_email(self, key, email):
        email_regex = r'^[\w\.-]+@[\w\.-]+\.\w+$'
        if not re.match(email_regex, email):
            raise ValueError("Invalid email format")
        return email.lower()  # Normalize to lowercase

    @validates("username")
    def validate_username(self, key, username):
        if len(username) < 3 or len(username) > 50:
            raise ValueError("Username must be between 3 and 50 characters")
        return username

class Deck(db.Model, SerializerMixin):
    __tablename__ = 'decks'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    title = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    subject = db.Column(db.String(50))
    category = db.Column(db.String(50))
    difficulty = db.Column(db.Integer)  # Range 1-5
    created_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, server_default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())
    is_default = db.Column(db.Boolean, default=False, nullable=False, server_default='0')
    
    flashcards = db.relationship('Flashcard', backref='deck', cascade="all, delete-orphan")

    serialize_rules = ('-user.decks', '-flashcards.deck')

class Flashcard(db.Model, SerializerMixin):
    __tablename__ = 'flashcards'

    id = db.Column(db.Integer, primary_key=True)
    deck_id = db.Column(db.Integer, db.ForeignKey('decks.id'), nullable=False)
    front_text = db.Column(db.Text, nullable=False)
    back_text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, server_default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())
    progress = db.relationship(
        'Progress', 
        backref='flashcard', 
        cascade='all, delete-orphan',
        passive_deletes=True
    )

    serialize_rules = ('-deck.flashcards','-progress.flashcard')

class Progress(db.Model, SerializerMixin):
    __tablename__ = 'progress'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    deck_id = db.Column(db.Integer, db.ForeignKey('decks.id'), nullable=False)
    flashcard_id = db.Column(db.Integer, db.ForeignKey('flashcards.id',ondelete='CASCADE'), nullable=False)

    # Fields with default values to ensure they are never None
    study_count = db.Column(db.Integer, default=0, nullable=False)
    correct_attempts = db.Column(db.Integer, default=0, nullable=False)  # Tracks correct answers
    incorrect_attempts = db.Column(db.Integer, default=0, nullable=False)  # Tracks incorrect answers
    total_study_time = db.Column(db.Float, default=0.0, nullable=False)  # Tracks total time spent on flashcard (in minutes)

    # Timestamps
    last_studied_at = db.Column(db.DateTime, server_default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())
    next_review_at = db.Column(db.DateTime, server_default=db.func.current_timestamp())

    # Review status and learning status
    review_status = db.Column(db.Enum('new', 'learning', 'reviewing', 'mastered', name="review_status"), default='new', nullable=False)
    is_learned = db.Column(db.Boolean, default=False, nullable=False)

    interval = db.Column(db.Float, default=1.0)

    # Serialization rules
    serialize_rules = ('-user.progress', '-deck.progress')

    # Unique constraint to ensure one progress entry per user-flashcard pair
    __table_args__ = (db.UniqueConstraint('user_id', 'flashcard_id', name='unique_user_flashcard_progress'),)

    def __init__(self, user_id, deck_id, flashcard_id, study_count=0, correct_attempts=0, incorrect_attempts=0, total_study_time=0.0, review_status='new', is_learned=False):
        """
        Constructor to ensure all fields are initialized with default values.
        """
        self.user_id = user_id
        self.deck_id = deck_id
        self.flashcard_id = flashcard_id
        self.study_count = study_count
        self.correct_attempts = correct_attempts
        self.incorrect_attempts = incorrect_attempts
        self.total_study_time = total_study_time
        self.review_status = review_status
        self.is_learned = is_learned
class UserStats(db.Model, SerializerMixin):
    __tablename__ = 'user_stats'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False)

    weekly_goal = db.Column(db.Integer, default=0)  # Target number of flashcards per week
    mastery_level = db.Column(db.Float, default=0.0)  # % of mastered cards
    study_streak = db.Column(db.Integer, default=0)  # Days in a row studied
    focus_score = db.Column(db.Float, default=0.0)  # Engagement metric (e.g., % of uninterrupted study sessions)
    retention_rate = db.Column(db.Float, default=0.0)  # % of retained flashcards
    cards_mastered = db.Column(db.Integer, default=0)  # Number of mastered flashcards
    minutes_per_day = db.Column(db.Float, default=0.0)  # Avg. minutes studied per day
    accuracy = db.Column(db.Float, default=0.0)  # Equivalent to mastery_level

    user = db.relationship("User", backref=db.backref("stats", uselist=False, cascade="all, delete-orphan"))

    serialize_rules = ('-user.stats',)


# --- NEW: AI + Payments models ---
from sqlalchemy.dialects.postgresql import JSONB
from datetime import datetime

class Payment(db.Model):
    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    provider = db.Column(db.String(32), nullable=False, default="intasend")
    status = db.Column(db.String(32), nullable=False)  # pending, paid, failed, refunded
    amount_cents = db.Column(db.Integer, nullable=False)
    currency = db.Column(db.String(8), nullable=False, default="KES")
    checkout_id = db.Column(db.String(128), index=True)  # reference from IntaSend
    raw_payload = db.Column(JSONB)  # store webhook+create payloads
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("payments", lazy="dynamic"))

class UserCredits(db.Model):
    __tablename__ = "user_credits"

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    credits = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("credits_row", uselist=False))

class AIGeneration(db.Model):
    __tablename__ = "ai_generations"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    deck_id = db.Column(db.Integer, db.ForeignKey("decks.id"), index=True)
    source_type = db.Column(db.String(32), nullable=False)  # text, url, notes
    source_excerpt = db.Column(db.Text)
    prompt = db.Column(db.Text)
    model = db.Column(db.String(128))
    status = db.Column(db.String(32), nullable=False)  # queued, complete, failed
    output = db.Column(JSONB)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("ai_generations", lazy="dynamic"))
    deck = db.relationship("Deck", backref=db.backref("ai_generations", lazy="dynamic"))
