# routes/reviews_routes.py
from flask import request
from flask_restful import Resource
from flask_jwt_extended import jwt_required, get_jwt_identity

from config import db  # not strictly needed here, but consistent with your pattern
from models import User
from services.review_service import ReviewService


class ReviewsNext(Resource):
    @jwt_required()
    def get(self):
        """
        Return up to `limit` due reviews for the current user, ordered by due_at ASC.
        Query param: ?limit=10 (1..50)
        """
        # Current user (same style as ProtectedUser/DeleteUser)
        user_id = get_jwt_identity()
        user = User.query.get(user_id)
        if not user:
            return {"error": "user_not_found"}, 404

        # Parse & clamp limit
        try:
            limit = int(request.args.get("limit", 10))
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(limit, 50))

        # Fetch due items
        rows = ReviewService.get_due_cards(user_id=user.id, limit=limit)

        # Shape response; your Flashcard fields are front_text/back_text
        items = [
            {
                "review_id": r.id,
                "card_id": r.card_id,
                "due_at": r.due_at.isoformat() if r.due_at else None,
                "front": getattr(r.card, "front_text", None),
                "back": None,  # keep hidden until flip/submit
            }
            for r in rows
        ]
        return {"items": items}, 200
