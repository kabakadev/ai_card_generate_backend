from flask import request
from flask_restful import Resource
from flask_jwt_extended import jwt_required, get_jwt_identity
from config import db
from models import Flashcard, Deck


# -------- helpers --------

def _resolve_user_id(identity):
    """Support identity as int or {'id': int}."""
    if isinstance(identity, int):
        return identity
    if isinstance(identity, dict) and isinstance(identity.get("id"), int):
        return identity["id"]
    return None


def _iso(dt):
    return dt.isoformat() if dt else None


# -------- resources --------

class FlashcardResource(Resource):
    @jwt_required()
    def get(self):
        """
        Retrieve flashcards for the authenticated user.

        Query params:
          - deck_id: int (required unless all=true to fetch across decks)
          - page: int (default 1)
          - per_page: int (default 10, max 100)
          - all: 'true' | 'false' (default 'false') If true, returns all cards (no pagination)
        """
        identity = get_jwt_identity()
        user_id = _resolve_user_id(identity)
        if user_id is None:
            return {"error": "invalid token payload"}, 401

        # Query params
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 10, type=int)
        per_page = min(max(per_page, 1), 100)
        all_cards = (request.args.get("all", "false") or "false").lower() == "true"

        deck_id_raw = request.args.get("deck_id")
        deck_id = None
        if deck_id_raw not in (None, "", "null", "undefined"):
            try:
                deck_id = int(deck_id_raw)
            except (TypeError, ValueError):
                return {"error": "deck_id must be an integer"}, 400

        # Base query: join Deck to enforce ownership
        query = Flashcard.query.join(Deck, Flashcard.deck_id == Deck.id).filter(Deck.user_id == user_id)

        # Filter by specific deck if provided
        if deck_id is not None:
            # Ensure the deck belongs to the user
            deck = Deck.query.filter_by(id=deck_id, user_id=user_id).first()
            if deck is None:
                return {"error": "Deck not found"}, 404
            query = query.filter(Flashcard.deck_id == deck_id)

        # all=true -> return all without pagination
        if all_cards:
            flashcards = query.order_by(Flashcard.updated_at.desc()).all()
            return {
                "items": [
                    {
                        "id": f.id,
                        "deck_id": f.deck_id,
                        "front_text": f.front_text,
                        "back_text": f.back_text,
                        "created_at": _iso(f.created_at),
                        "updated_at": _iso(f.updated_at),
                    }
                    for f in flashcards
                ]
            }, 200

        # Paginated response
        pagination = query.order_by(Flashcard.updated_at.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )

        return {
            "items": [
                {
                    "id": f.id,
                    "deck_id": f.deck_id,
                    "front_text": f.front_text,
                    "back_text": f.back_text,
                    "created_at": _iso(f.created_at),
                    "updated_at": _iso(f.updated_at),
                }
                for f in pagination.items
            ],
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total_pages": pagination.pages,
                "total_items": pagination.total,
                "has_next": pagination.has_next,
                "has_prev": pagination.has_prev,
            },
        }, 200

    @jwt_required()
    def post(self):
        """Create a new flashcard for the authenticated user."""
        identity = get_jwt_identity()
        user_id = _resolve_user_id(identity)
        if user_id is None:
            return {"error": "invalid token payload"}, 401

        data = request.get_json(force=True) or {}

        required_fields = ["deck_id", "front_text", "back_text"]
        missing = [f for f in required_fields if not data.get(f)]
        if missing:
            return {"error": f"Missing required fields: {', '.join(missing)}"}, 400

        # Validate deck_id and enforce ownership
        try:
            deck_id = int(data["deck_id"])
        except (TypeError, ValueError):
            return {"error": "deck_id must be an integer"}, 400

        deck = Deck.query.filter_by(id=deck_id, user_id=user_id).first()
        if not deck:
            return {"error": "Deck not found or not yours"}, 404

        front_text = (data.get("front_text") or "").strip()
        back_text = (data.get("back_text") or "").strip()
        if not front_text or not back_text:
            return {"error": "front_text and back_text cannot be empty"}, 400

        new_flashcard = Flashcard(
            deck_id=deck_id,
            front_text=front_text,
            back_text=back_text,
        )

        db.session.add(new_flashcard)
        db.session.commit()

        return {
            "id": new_flashcard.id,
            "deck_id": new_flashcard.deck_id,
            "front_text": new_flashcard.front_text,
            "back_text": new_flashcard.back_text,
            "created_at": _iso(new_flashcard.created_at),
            "updated_at": _iso(new_flashcard.updated_at),
        }, 201


class FlashcardDetailResource(Resource):
    @jwt_required()
    def put(self, id):
        """Update a flashcard by ID (only if it belongs to the authenticated user)."""
        identity = get_jwt_identity()
        user_id = _resolve_user_id(identity)
        if user_id is None:
            return {"error": "invalid token payload"}, 401

        data = request.get_json(force=True) or {}

        flashcard = (
            Flashcard.query.join(Deck, Flashcard.deck_id == Deck.id)
            .filter(Flashcard.id == id, Deck.user_id == user_id)
            .first()
        )
        if not flashcard:
            return {"error": "Flashcard not found"}, 404

        # Update fields if provided
        if "front_text" in data and data["front_text"] is not None:
            flashcard.front_text = (data["front_text"] or "").strip() or flashcard.front_text
        if "back_text" in data and data["back_text"] is not None:
            flashcard.back_text = (data["back_text"] or "").strip() or flashcard.back_text

        db.session.commit()

        return {
            "id": flashcard.id,
            "deck_id": flashcard.deck_id,
            "front_text": flashcard.front_text,
            "back_text": flashcard.back_text,
            "updated_at": _iso(flashcard.updated_at),
        }, 200

    @jwt_required()
    def delete(self, id):
        """Delete a flashcard by ID (only if it belongs to the authenticated user)."""
        identity = get_jwt_identity()
        user_id = _resolve_user_id(identity)
        if user_id is None:
            return {"error": "invalid token payload"}, 401

        flashcard = (
            Flashcard.query.join(Deck, Flashcard.deck_id == Deck.id)
            .filter(Flashcard.id == id, Deck.user_id == user_id)
            .first()
        )
        if not flashcard:
            return {"error": "Flashcard not found"}, 404

        db.session.delete(flashcard)
        db.session.commit()

        return {"message": "Flashcard deleted successfully"}, 200
