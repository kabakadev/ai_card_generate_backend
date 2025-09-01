from flask import request
from flask_restful import Resource
from flask_jwt_extended import jwt_required, get_jwt_identity
from config import db
from models import Deck, User
from datetime import datetime

# ---------------------------
# Helpers
# ---------------------------

def _resolve_user_id(identity):
    if isinstance(identity, int):
        return identity
    if isinstance(identity, dict) and isinstance(identity.get("id"), int):
        return identity["id"]
    return None

# Allow human-friendly names or numbers for difficulty
DIFFICULTY_NAME_TO_INT = {
    "beginner": 1,
    "intermediate": 2,
    "advanced": 3,
    # optional extra aliases covering full 1–5 range
    "very easy": 1,
    "easy": 2,
    "medium": 3,
    "normal": 3,
    "hard": 4,
    "very hard": 5,
}

def parse_difficulty(value):
    """
    Accepts: int 1..5, numeric strings "1".."5", or names like "Beginner".
    Returns: int 1..5 or raises ValueError with a helpful message.
    """
    if value is None:
        raise ValueError("difficulty is required")

    # Already an int
    if isinstance(value, int):
        if 1 <= value <= 5:
            return value
        raise ValueError("difficulty must be an integer between 1 and 5")

    # Numeric string
    if isinstance(value, str) and value.strip().isdigit():
        iv = int(value.strip())
        if 1 <= iv <= 5:
            return iv
        raise ValueError("difficulty must be 1..5")

    # Named difficulty
    if isinstance(value, str):
        key = value.strip().lower()
        if key in DIFFICULTY_NAME_TO_INT:
            return DIFFICULTY_NAME_TO_INT[key]

    allowed = sorted(set(DIFFICULTY_NAME_TO_INT.keys()))
    raise ValueError(
        "invalid difficulty. Use 1..5 or one of: " + ", ".join(allowed)
    )

def _strip_or_empty(val):
    return val.strip() if isinstance(val, str) else (val or "")

# ---------------------------
# Resources
# ---------------------------

class DecksResource(Resource):
    @jwt_required()
    def get(self):
        """Get all decks for the authenticated user with pagination support."""
        identity = get_jwt_identity()
        user_id = _resolve_user_id(identity)
        if user_id is None:
            return {"error": "invalid token payload"}, 401

        # Get pagination parameters from query string
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 10, type=int)

        # Limit per_page to prevent excessive loads
        per_page = min(per_page, 50)

        # Query with pagination
        pagination = Deck.query.filter_by(user_id=user_id).order_by(
            Deck.updated_at.desc()
        ).paginate(
            page=page,
            per_page=per_page,
            error_out=False
        )

        decks = pagination.items

        return {
            "items": [
                {
                    "id": deck.id,
                    "title": deck.title,
                    "description": deck.description,
                    "subject": deck.subject,
                    "category": deck.category,
                    "difficulty": deck.difficulty,  # always an int 1..5
                    "created_at": deck.created_at.isoformat() if deck.created_at else None,
                    "updated_at": deck.updated_at.isoformat() if deck.updated_at else None,
                }
                for deck in decks
            ],
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total_pages": pagination.pages,
                "total_items": pagination.total,
                "has_next": pagination.has_next,
                "has_prev": pagination.has_prev
            }
        }, 200

    @jwt_required()
    def post(self):
        """Create a new deck for the authenticated user."""
        data = request.get_json(force=True) or {}
        identity = get_jwt_identity()
        user_id = _resolve_user_id(identity)
        if user_id is None:
            return {"error": "invalid token payload"}, 401

        required_fields = ["title", "description", "subject", "category", "difficulty"]
        missing = [f for f in required_fields if not data.get(f)]
        if missing:
            return {"error": f"Missing required fields: {', '.join(missing)}"}, 400

        user = User.query.get(user_id)
        if not user:
            return {"error": "User not found"}, 404

        try:
            difficulty_int = parse_difficulty(data.get("difficulty"))
        except ValueError as e:
            return {"error": str(e)}, 400

        new_deck = Deck(
            title=_strip_or_empty(data.get("title")),
            description=_strip_or_empty(data.get("description")),
            subject=_strip_or_empty(data.get("subject")),
            category=_strip_or_empty(data.get("category")),
            difficulty=difficulty_int,
            user_id=user_id,
        )

        db.session.add(new_deck)
        db.session.commit()

        return {
            "id": new_deck.id,
            "title": new_deck.title,
            "description": new_deck.description,
            "subject": new_deck.subject,
            "category": new_deck.category,
            "difficulty": new_deck.difficulty,  # int 1..5
            "user_id": new_deck.user_id,
            "created_at": new_deck.created_at.isoformat() if new_deck.created_at else None,
            "updated_at": new_deck.updated_at.isoformat() if new_deck.updated_at else None
        }, 201

class DeckResource(Resource):
    @jwt_required()
    def get(self, deck_id):
        """Retrieve a single deck by ID for the authenticated user."""
        identity = get_jwt_identity()
        user_id = _resolve_user_id(identity)
        if user_id is None:
            return {"error": "invalid token payload"}, 401

        deck = Deck.query.filter_by(id=deck_id, user_id=user_id).first()
        if not deck:
            return {"error": "Deck not found"}, 404

        return {
            "id": deck.id,
            "title": deck.title,
            "description": deck.description,
            "subject": deck.subject,
            "category": deck.category,
            "difficulty": deck.difficulty,
            "created_at": deck.created_at.isoformat() if deck.created_at else None,
            "updated_at": deck.updated_at.isoformat() if deck.updated_at else None
        }, 200

    @jwt_required()
    def put(self, deck_id):
        """Update an existing deck."""
        identity = get_jwt_identity()
        user_id = _resolve_user_id(identity)
        if user_id is None:
            return {"error": "invalid token payload"}, 401

        data = request.get_json(force=True) or {}

        deck = Deck.query.filter_by(id=deck_id, user_id=user_id).first()
        if not deck:
            return {"error": "Deck not found"}, 404

        # Update deck fields if provided
        for field in ["title", "description", "subject", "category"]:
            if field in data and data[field] is not None:
                val = data[field]
                setattr(deck, field, val.strip() if isinstance(val, str) else val)

        if "difficulty" in data and data["difficulty"] is not None:
            try:
                deck.difficulty = parse_difficulty(data["difficulty"])
            except ValueError as e:
                return {"error": str(e)}, 400

        db.session.commit()

        return {
            "id": deck.id,
            "title": deck.title,
            "description": deck.description,
            "subject": deck.subject,
            "category": deck.category,
            "difficulty": deck.difficulty,
            "updated_at": deck.updated_at.isoformat() if deck.updated_at else None
        }, 200

    @jwt_required()
    def delete(self, deck_id):
        """Delete an existing deck."""
        identity = get_jwt_identity()
        user_id = _resolve_user_id(identity)
        if user_id is None:
            return {"error": "invalid token payload"}, 401

        deck = Deck.query.filter_by(id=deck_id, user_id=user_id).first()
        if not deck:
            return {"error": "Deck not found"}, 404

        db.session.delete(deck)
        db.session.commit()

        return {"message": "Deck deleted successfully"}, 200
