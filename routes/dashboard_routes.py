from flask_restful import Resource
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import func, case

from config import db
from models import User, Deck, UserStats, ReviewLog, Flashcard
from services.stats_service import StatsService
from services.feature_gates import (
    get_remaining_monthly_prompts,
    get_remaining_weekly_quizzes,
    premium_benefits,
    get_effective_plan_for_user,
    PREMIUM_TIER_MONTHLY_AI,
    FREE_TIER_MONTHLY_AI,
    FREE_TIER_WEEKLY_QUIZZES,
)
from services.subscription_manager import is_active  # 🔴 NEW


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

        # 🔹 Subscription is the single source of truth
        sub_active, sub = is_active(user_id)
        sub_plan_type = getattr(sub, "plan_type", None) if sub else None
        plan = get_effective_plan_for_user(user)

        decks = Deck.query.filter_by(user_id=user_id).all()
        deck_review_rows = db.session.query(
            ReviewLog.deck_id,
            func.count(ReviewLog.id).label("total"),
            func.sum(case((ReviewLog.was_correct == True, 1), else_=0)).label("correct"),
        ).filter(ReviewLog.user_id == user_id).group_by(ReviewLog.deck_id).all()
        deck_review_map = {row.deck_id: row for row in deck_review_rows}

        deck_data = []
        total_flashcards_studied = 0
        most_reviewed_deck = None
        most_reviews = 0

        for deck in decks:
            deck_id = deck.id
            total_cards = len(deck.flashcards)

            unique_cards_reviewed = db.session.query(
                func.count(func.distinct(ReviewLog.flashcard_id))
            ).filter(
                ReviewLog.user_id == user_id,
                ReviewLog.deck_id == deck_id
            ).scalar() or 0

            mastered_subquery = db.session.query(
                ReviewLog.flashcard_id.label("flashcard_id"),
                func.count(ReviewLog.id).label("attempts"),
                func.avg(case((ReviewLog.was_correct == True, 1.0), else_=0.0)).label("accuracy")
            ).filter(
                ReviewLog.user_id == user_id,
                ReviewLog.deck_id == deck_id
            ).group_by(ReviewLog.flashcard_id).subquery()

            mastered_count = db.session.query(
                func.count(mastered_subquery.c.flashcard_id)
            ).filter(
                mastered_subquery.c.accuracy >= 0.8,
                mastered_subquery.c.attempts >= 3
            ).scalar() or 0

            learning_count = max(0, unique_cards_reviewed - mastered_count)
            not_started_count = max(0, total_cards - unique_cards_reviewed)

            deck_stats = deck_review_map.get(deck_id)
            total_reviews_deck = int(deck_stats.total) if deck_stats and deck_stats.total else 0
            total_flashcards_studied += unique_cards_reviewed

            if total_reviews_deck > most_reviews:
                most_reviews = total_reviews_deck
                most_reviewed_deck = deck.title

            deck_data.append({
                "deck_id": deck_id,
                "deck_title": deck.title,
                "flashcards_count": total_cards,
                "flashcards_studied": unique_cards_reviewed,
                "total_reviews": total_reviews_deck,
                "progress": {
                    "mastered": mastered_count,
                    "learning": learning_count,
                    "not_started": not_started_count,
                },
            })

        accuracy_ratio = StatsService.get_accuracy(user_id)
        total_reviews = StatsService.get_total_reviews(user_id)
        time_data_week = StatsService.get_time_studied(user_id, days=7)
        time_data_today = StatsService.get_time_studied(user_id, days=1)
        daily_accuracy = StatsService.get_daily_accuracy(user_id, days=7)
        weak_cards_basic = StatsService.get_weak_cards(user_id, limit=5)

        mastery_level = round(accuracy_ratio * 100, 2)
        retention_rate = mastery_level
        total_minutes_week = time_data_week["total_minutes"]
        time_studied_today = time_data_today["total_minutes"]
        avg_time_per_card = round(time_data_week["total_minutes"] / total_reviews, 2) if total_reviews > 0 else 0.0

        weak_cards_detail = []
        if weak_cards_basic:
            flashcard_ids = [item["flashcard_id"] for item in weak_cards_basic]
            flashcards = Flashcard.query.filter(Flashcard.id.in_(flashcard_ids)).all()
            flashcard_map = {fc.id: fc for fc in flashcards}

            for item in weak_cards_basic:
                flashcard = flashcard_map.get(item["flashcard_id"])
                deck_obj = flashcard.deck if flashcard else None
                weak_cards_detail.append({
                    "flashcard_id": item["flashcard_id"],
                    "question": getattr(flashcard, "front_text", None),
                    "deck_id": deck_obj.id if deck_obj else None,
                    "deck_name": getattr(deck_obj, "title", None),
                    "accuracy": item["accuracy"],
                    "attempts": item["total_attempts"],
                    "correct": item["correct"],
                })

        weak_card_count = len(weak_cards_detail)
        focus_score = max(0.0, min(100.0, 100.0 - (weak_card_count * 20.0)))

        stats_record = UserStats.query.filter_by(user_id=user_id).first()
        if not stats_record:
            stats_record = UserStats(user_id=user_id)
            db.session.add(stats_record)

        stats_record.mastery_level = mastery_level
        stats_record.retention_rate = retention_rate
        stats_record.focus_score = focus_score
        stats_record.accuracy = mastery_level
        stats_record.minutes_per_day = time_data_week["daily_minutes"]
        db.session.commit()

        weekly_goal_value = stats_record.weekly_goal  # int (default 0)
        stats_block = {
            "accuracy": round(accuracy_ratio, 4),
            "total_reviews": total_reviews,
            "time_studied_today": round(time_studied_today, 2),
            "time_studied_week": round(total_minutes_week, 2),
            "time_studied_week_daily": time_data_week["daily"],
            "avg_time_per_card": avg_time_per_card,
            "daily_accuracy": daily_accuracy,
            "weak_cards": weak_cards_detail,
            "focus_score": round(focus_score, 2),
            "weekly_goal": weekly_goal_value,
        }

        # ---------- Usage / plan limits ----------
        ai_remaining, ai_row = get_remaining_monthly_prompts(user_id)
        quiz_remaining, quiz_row = get_remaining_weekly_quizzes(user_id)

        ai_used = ai_row.ai_prompt_count or 0
        if plan == "premium":
            ai_limit = PREMIUM_TIER_MONTHLY_AI
            ai_remaining_value = max(0, ai_limit - ai_used)
        else:
            ai_limit = ai_row.free_quota or FREE_TIER_MONTHLY_AI
            ai_remaining_value = ai_remaining

        quiz_used = quiz_row.quiz_count or 0
        if plan == "premium":
            quiz_limit = "unlimited"
            quiz_remaining_value = "unlimited"
        else:
            quiz_limit = FREE_TIER_WEEKLY_QUIZZES
            quiz_remaining_value = quiz_remaining

        usage_info = {
            "plan": plan,
            "plan_type": sub_plan_type if sub_active else None,
            "subscription": {
                "active": sub_active,
                "plan_type": sub_plan_type,
            },
            "ai_generation": {
                "used": ai_used,
                "limit": ai_limit,
                "remaining": ai_remaining_value,
                "month_key": ai_row.month_key,
            },
            "quizzes": {
                "used": quiz_used,
                "limit": quiz_limit,
                "remaining": quiz_remaining_value,
                "week_key": quiz_row.week_key,
            },
        }

        print(
            f"[Dashboard] Returning stats for user {user_id}: "
            f"plan={plan}, sub_active={sub_active}, sub_plan_type={sub_plan_type}, "
            f"accuracy={accuracy_ratio:.4f}, total_reviews={total_reviews}, "
            f"time_week={total_minutes_week}min, quizzes_used={quiz_used}"
        )

        if plan == "premium":
            response_data = {
                "username": user.username,
                "total_flashcards_studied": total_flashcards_studied,
                "most_reviewed_deck": most_reviewed_deck,
                "decks": deck_data,
                "stats": stats_block,
                "usage": usage_info,
            }
            return response_data, 200

        simplified_decks = [
            {
                "deck_id": deck["deck_id"],
                "deck_title": deck["deck_title"],
                "flashcards_studied": deck["flashcards_studied"],
            }
            for deck in deck_data
        ]

        response_data = {
            "username": user.username,
            "stats": {
                "accuracy": stats_block["accuracy"],
                "total_reviews": stats_block["total_reviews"],
                "weekly_goal": stats_block["weekly_goal"],
            },
            "decks": simplified_decks,
            "usage": usage_info,
            "upgrade_prompt": {
                "message": "Upgrade to Premium for detailed analytics",
                "benefits": premium_benefits(),
            },
        }
        return response_data, 200
