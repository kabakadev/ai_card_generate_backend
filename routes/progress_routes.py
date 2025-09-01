from flask import request
from flask_restful import Resource
from flask_jwt_extended import jwt_required, get_jwt_identity
from config import db
from models import Progress, UserStats


def _resolve_user_id(identity):
    """Support identity as int or {'id': int}."""
    if isinstance(identity, int):
        return identity
    if isinstance(identity, dict) and isinstance(identity.get("id"), int):
        return identity["id"]
    return None


def _iso(dt):
    return dt.isoformat() if dt else None


class ProgressResource(Resource):
    @jwt_required()
    def get(self, deck_id=None, flashcard_id=None):
        """Retrieve progress for a specific deck or flashcard (or all for the user)."""
        identity = get_jwt_identity()
        user_id = _resolve_user_id(identity)
        if user_id is None:
            return {"error": "invalid token payload"}, 401

        query = Progress.query.filter_by(user_id=user_id)

        if deck_id is not None:
            query = query.filter_by(deck_id=deck_id)
        if flashcard_id is not None:
            query = query.filter_by(flashcard_id=flashcard_id)

        progress_entries = query.all()

        if not progress_entries:
            return {"message": "No progress found."}, 200

        return [
            {
                "id": p.id,
                "deck_id": p.deck_id,
                "flashcard_id": p.flashcard_id,
                "study_count": p.study_count,
                "correct_attempts": p.correct_attempts,
                "incorrect_attempts": p.incorrect_attempts,
                "total_study_time": p.total_study_time,
                "last_studied_at": _iso(p.last_studied_at),
                "next_review_at": _iso(p.next_review_at),
                "review_status": p.review_status,
                "is_learned": p.is_learned,
            }
            for p in progress_entries
        ], 200

    @jwt_required()
    def post(self):
        """Track user progress for a flashcard."""
        identity = get_jwt_identity()
        user_id = _resolve_user_id(identity)
        if user_id is None:
            return {"error": "invalid token payload"}, 401

        data = request.get_json(force=True) or {}

        required = ["flashcard_id", "deck_id"]
        missing = [k for k in required if k not in data]
        if missing:
            return {"error": f"Missing fields: {', '.join(missing)}"}, 400

        flashcard_id = data["flashcard_id"]
        deck_id = data["deck_id"]

        progress = Progress.query.filter_by(
            user_id=user_id,
            flashcard_id=flashcard_id,
        ).first()

        if not progress:
            progress = Progress(
                user_id=user_id,
                flashcard_id=flashcard_id,
                deck_id=deck_id,
                study_count=0,
                total_study_time=0.0,
                correct_attempts=0,
                incorrect_attempts=0,
                review_status="new",
                is_learned=False,
            )
            db.session.add(progress)

        # Update counters
        progress.study_count += 1
        progress.total_study_time += float(data.get("time_spent", 0) or 0)
        if data.get("was_correct"):
            progress.correct_attempts += 1
        else:
            progress.incorrect_attempts += 1

        # Simple mastery heuristic
        if progress.correct_attempts >= 3:
            progress.review_status = "mastered"
            progress.is_learned = True

        db.session.commit()

        # Update UserStats aggregates
        stats = UserStats.query.filter_by(user_id=user_id).first()
        if not stats:
            stats = UserStats(user_id=user_id)
            db.session.add(stats)

        total_correct = db.session.query(db.func.sum(Progress.correct_attempts)).filter_by(user_id=user_id).scalar() or 0
        total_attempts = db.session.query(db.func.sum(Progress.study_count)).filter_by(user_id=user_id).scalar() or 0

        stats.mastery_level = round((total_correct / total_attempts) * 100, 2) if total_attempts > 0 else 0.0
        stats.cards_mastered = Progress.query.filter_by(user_id=user_id, review_status="mastered").count()
        stats.retention_rate = stats.mastery_level

        total_study_time = db.session.query(db.func.sum(Progress.total_study_time)).filter_by(user_id=user_id).scalar() or 0.0
        target_time_per_flashcard = 1.0
        if total_attempts > 0:
            average_time_per_flashcard = total_study_time / float(total_attempts)
            stats.focus_score = round((average_time_per_flashcard / target_time_per_flashcard) * 100, 2)
        else:
            stats.focus_score = 0.0

        db.session.commit()

        return {
            "id": progress.id,
            "user_id": progress.user_id,
            "flashcard_id": progress.flashcard_id,
            "deck_id": progress.deck_id,
            "study_count": progress.study_count,
            "correct_attempts": progress.correct_attempts,
            "incorrect_attempts": progress.incorrect_attempts,
            "total_study_time": progress.total_study_time,
            "review_status": progress.review_status,
            "is_learned": progress.is_learned,
        }, 200
