from datetime import datetime
from flask import request, current_app
from flask_restful import Resource
from flask_jwt_extended import jwt_required, get_jwt_identity
from config import db
from models import Progress, UserStats, ReviewLog
from services.stats_service import StatsService


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

        track_reviews = bool(data.get("track_reviews", True))

        if not track_reviews:
            current_app.logger.info(
                "Progress tracking skipped for user %s (tracking disabled)", user_id
            )
            return {
                "message": "Review not tracked (tracking disabled)",
                "tracked": False,
            }, 200

        required = ["flashcard_id", "deck_id"]
        missing = [k for k in required if k not in data]
        if missing:
            return {"error": f"Missing fields: {', '.join(missing)}"}, 400

        flashcard_id = data["flashcard_id"]
        deck_id = data["deck_id"]
        time_spent_seconds = int(data.get("time_spent_seconds", data.get("time_spent", 0)) or 0)
        was_correct = bool(data.get("was_correct", False))

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

        time_spent_minutes = time_spent_seconds / 60.0

        try:
            # Update counters
            progress.study_count += 1
            progress.total_study_time += time_spent_minutes
            if was_correct:
                progress.correct_attempts += 1
            else:
                progress.incorrect_attempts += 1

            # Simple mastery heuristic
            if progress.correct_attempts >= 3:
                progress.review_status = "mastered"
                progress.is_learned = True

            # Create review log entry for audit trail
            review_log = ReviewLog(
                user_id=user_id,
                flashcard_id=flashcard_id,
                deck_id=deck_id,
                was_correct=was_correct,
                time_spent_seconds=time_spent_seconds,
                created_at=datetime.utcnow(),
            )
            db.session.add(review_log)

            # Update UserStats aggregates via StatsService
            stats = UserStats.query.filter_by(user_id=user_id).first()
            if not stats:
                stats = UserStats(user_id=user_id)
                db.session.add(stats)

            db.session.flush()

            accuracy_ratio = StatsService.get_accuracy(user_id)
            mastery_level = accuracy_ratio * 100.0
            retention_rate = mastery_level

            time_studied = StatsService.get_time_studied(user_id)
            total_minutes = time_studied.get("total_minutes", 0.0)
            total_seconds = time_studied.get("total_seconds", 0)
            daily_minutes = time_studied.get("daily_minutes", 0.0)
            total_reviews = StatsService.get_total_reviews(user_id)

            weak_cards = StatsService.get_weak_cards(user_id, limit=5)
            focus_score = 100.0
            if weak_cards:
                focus_score = max(0.0, min(100.0, 100.0 - (len(weak_cards) * 20.0)))

            stats.mastery_level = round(mastery_level, 2)
            stats.retention_rate = round(retention_rate, 2)
            stats.focus_score = round(focus_score, 2)
            stats.accuracy = round(mastery_level, 2)
            stats.minutes_per_day = round(daily_minutes, 2)
            stats.cards_mastered = Progress.query.filter_by(user_id=user_id, review_status="mastered").count()

            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Failed to record review for user %s flashcard %s", user_id, flashcard_id)
            return {"error": "Failed to record progress. Please try again."}, 500

        review_payload = {
            "id": review_log.id,
            "user_id": review_log.user_id,
            "flashcard_id": review_log.flashcard_id,
            "deck_id": review_log.deck_id,
            "was_correct": review_log.was_correct,
            "time_spent_seconds": review_log.time_spent_seconds,
            "created_at": review_log.created_at.isoformat() if review_log.created_at else None,
        }

        progress_payload = {
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
        }

        stats_payload = {
            "accuracy": round(accuracy_ratio, 4),
            "accuracy_pct": round(mastery_level, 2),
            "retention_rate": round(retention_rate, 2),
            "focus_score": round(focus_score, 2),
            "total_minutes_last_7_days": round(total_minutes, 2),
            "total_seconds_last_7_days": int(total_seconds),
            "total_reviews": total_reviews,
            "weak_cards_count": len(weak_cards),
        }

        response_data = {
            **progress_payload,
            "was_correct": was_correct,
            "time_spent_seconds": time_spent_seconds,
            "message": "Progress and review logged successfully",
            "review_log": review_payload,
            "stats": stats_payload,
            "tracked": True,
        }

        return response_data, 200
