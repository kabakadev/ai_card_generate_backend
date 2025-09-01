# routes/stats_routes.py
from flask import request
from flask_restful import Resource
from flask_jwt_extended import jwt_required, get_jwt_identity
from flask_cors import cross_origin
from config import db
from models import UserStats


def _resolve_user_id(identity):
    """Support identity as int or {'id': int}."""
    if isinstance(identity, int):
        return identity
    if isinstance(identity, dict) and isinstance(identity.get("id"), int):
        return identity["id"]
    return None


class UserStatsResource(Resource):
    # CORS preflight (helps PUT from browser)
    @cross_origin()
    def options(self):
        return {}, 204

    @cross_origin()
    @jwt_required()
    def put(self):
        """Update user stats, such as weekly goal."""
        identity = get_jwt_identity()
        user_id = _resolve_user_id(identity)
        if user_id is None:
            return {"error": "invalid token payload"}, 401

        data = request.get_json(force=True) or {}

        stats = UserStats.query.filter_by(user_id=user_id).first()
        if not stats:
            stats = UserStats(user_id=user_id)
            db.session.add(stats)

        # allow partial updates; coerce to expected types where sensible
        if "weekly_goal" in data:
            try:
                stats.weekly_goal = int(data["weekly_goal"])
            except (ValueError, TypeError):
                return {"error": "weekly_goal must be an integer"}, 400

        if "mastery_level" in data:
            try:
                stats.mastery_level = float(data["mastery_level"])
            except (ValueError, TypeError):
                return {"error": "mastery_level must be a number"}, 400

        if "study_streak" in data:
            try:
                stats.study_streak = int(data["study_streak"])
            except (ValueError, TypeError):
                return {"error": "study_streak must be an integer"}, 400

        if "focus_score" in data:
            try:
                stats.focus_score = float(data["focus_score"])
            except (ValueError, TypeError):
                return {"error": "focus_score must be a number"}, 400

        if "retention_rate" in data:
            try:
                stats.retention_rate = float(data["retention_rate"])
            except (ValueError, TypeError):
                return {"error": "retention_rate must be a number"}, 400

        if "cards_mastered" in data:
            try:
                stats.cards_mastered = int(data["cards_mastered"])
            except (ValueError, TypeError):
                return {"error": "cards_mastered must be an integer"}, 400

        if "minutes_per_day" in data:
            try:
                stats.minutes_per_day = float(data["minutes_per_day"])
            except (ValueError, TypeError):
                return {"error": "minutes_per_day must be a number"}, 400

        if "accuracy" in data:
            try:
                stats.accuracy = float(data["accuracy"])
            except (ValueError, TypeError):
                return {"error": "accuracy must be a number"}, 400

        db.session.commit()

        return {
            "id": stats.id,
            "user_id": stats.user_id,
            "weekly_goal": stats.weekly_goal,
            "mastery_level": stats.mastery_level,
            "study_streak": stats.study_streak,
            "focus_score": stats.focus_score,
            "retention_rate": stats.retention_rate,
            "cards_mastered": stats.cards_mastered,
            "minutes_per_day": stats.minutes_per_day,
            "accuracy": stats.accuracy,
        }, 200
