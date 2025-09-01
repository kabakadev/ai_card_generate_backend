from flask_restful import Resource
from flask_jwt_extended import jwt_required, get_jwt_identity
from config import db
from models import User, Deck, UserStats, Progress


def _resolve_user_id(identity):
    """Support identity as int or {'id': int}."""
    if isinstance(identity, int):
        return identity
    if isinstance(identity, dict) and isinstance(identity.get("id"), int):
        return identity["id"]
    return None


class Dashboard(Resource):
    @jwt_required()
    def get(self):
        """Fetch the logged-in user's dashboard data."""
        identity = get_jwt_identity()
        user_id = _resolve_user_id(identity)
        if user_id is None:
            return {"error": "invalid token payload"}, 401

        user = User.query.filter_by(id=user_id).first()
        if not user:
            return {"error": "User not found"}, 404

        decks = Deck.query.filter_by(user_id=user_id).all()
        deck_data = []
        total_flashcards_studied = 0
        most_reviewed_deck = None
        most_reviews = 0

        for deck in decks:
            # all progress entries for this deck for this user
            progress_entries = Progress.query.filter_by(deck_id=deck.id, user_id=user_id).all()
            deck_study_count = sum(entry.study_count or 0 for entry in progress_entries)
            total_flashcards_studied += deck_study_count

            if deck_study_count > most_reviews:
                most_reviews = deck_study_count
                most_reviewed_deck = deck.title

            deck_data.append({
                "deck_id": deck.id,
                "deck_title": deck.title,
                "flashcards_studied": deck_study_count
            })

        # Ensure stats row exists
        stats = UserStats.query.filter_by(user_id=user_id).first()
        if not stats:
            stats = UserStats(user_id=user_id)
            db.session.add(stats)
            db.session.commit()

        # Aggregate metrics
        total_correct = db.session.query(db.func.sum(Progress.correct_attempts)).filter_by(user_id=user_id).scalar() or 0
        total_attempts = db.session.query(db.func.sum(Progress.study_count)).filter_by(user_id=user_id).scalar() or 0

        mastery_level = (total_correct / total_attempts) * 100 if total_attempts > 0 else 0.0
        retention_rate = mastery_level  # same heuristic as your current code

        total_study_time = db.session.query(db.func.sum(Progress.total_study_time)).filter_by(user_id=user_id).scalar() or 0.0
        target_time_per_flashcard = 1.0  # minutes
        focus_score = 0.0

        if total_flashcards_studied > 0:
            average_time_per_flashcard = total_study_time / float(total_flashcards_studied)
            # Higher average time → higher focus in your heuristic
            focus_score = (average_time_per_flashcard / target_time_per_flashcard) * 100.0

        # Persist computed stats back to row
        stats.mastery_level = mastery_level
        stats.retention_rate = retention_rate
        stats.focus_score = focus_score
        db.session.commit()

        response_data = {
            "username": user.username,
            "total_flashcards_studied": total_flashcards_studied,
            "most_reviewed_deck": most_reviewed_deck,
            "weekly_goal": stats.weekly_goal or 10,
            "mastery_level": mastery_level,
            "study_streak": stats.study_streak or 0,
            "focus_score": focus_score,
            "retention_rate": retention_rate,
            "cards_mastered": stats.cards_mastered or 0,
            "minutes_per_day": stats.minutes_per_day or 0.0,
            "accuracy": mastery_level,
            "decks": deck_data,
        }

        return response_data, 200
