# routes/catalog_routes.py
from flask import request, jsonify
from flask_restful import Resource
from flask_jwt_extended import jwt_required, get_jwt_identity
from config import db
from models import Deck, Flashcard, User

from .deck_routes import _resolve_user_id, parse_difficulty, _strip_or_empty
from services.catalog_data import CATALOG  # <-- import here


class CatalogListResource(Resource):
    """
    Public read-only catalog list.
    Returns a lightweight view of the curated catalog (no flashcard bodies).
    """
    def get(self):
        # Transform the backend dict into a list with ids and summary fields
        items = []
        for deck_id, tpl in CATALOG.items():
            items.append({
                "id": deck_id,
                "title": tpl.get("title"),
                "subject": tpl.get("subject"),
                "category": tpl.get("category"),
                "difficulty": tpl.get("difficulty"),
                "description": tpl.get("description"),
                "flashcard_count": len(tpl.get("flashcards", [])),
            })
        # Sort deterministically (optional): by subject then title
        items.sort(key=lambda d: (d.get("subject") or "", d.get("title") or ""))
        return {"catalog": items}, 200


class CatalogResource(Resource):
    @jwt_required()
    def post(self):
        """
        Bulk-create curated decks + flashcards from catalog.
        Request body: { "deck_ids": ["algebra-basics", "calc-derivatives", ...] }
        """
        identity = get_jwt_identity()
        user_id = _resolve_user_id(identity)
        if user_id is None:
            return {"error": "invalid token payload"}, 401

        data = request.get_json(force=True) or {}
        deck_ids = data.get("deck_ids", [])
        if not isinstance(deck_ids, list) or not deck_ids:
            return {"error": "deck_ids must be a non-empty list"}, 400

        user = User.query.get(user_id)
        if not user:
            return {"error": "User not found"}, 404

        created = []
        errors = []

        for deck_key in deck_ids:
            template = CATALOG.get(deck_key)
            if not template:
                errors.append({"deck_id": deck_key, "error": "not found in catalog"})
                continue

            try:
                deck = Deck(
                    title=_strip_or_empty(template["title"]),
                    description=_strip_or_empty(template["description"]),
                    subject=_strip_or_empty(template["subject"]),
                    category=_strip_or_empty(template["category"]),
                    difficulty=parse_difficulty(template["difficulty"]),
                    user_id=user_id,
                )
                db.session.add(deck)
                db.session.flush()

                for card in template["flashcards"]:
                    flashcard = Flashcard(
                        deck_id=deck.id,
                        front_text=card["front_text"],
                        back_text=card["back_text"],
                    )
                    db.session.add(flashcard)

                created.append({
                    "id": deck.id,
                    "title": deck.title,
                    "flashcards": len(template["flashcards"])
                })
            except Exception as e:
                errors.append({"deck_id": deck_key, "error": str(e)})

        db.session.commit()
        return {"created": created, "errors": errors}, 201
