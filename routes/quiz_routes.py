from __future__ import annotations

from typing import Optional, Sequence

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity

from models import User, Deck, Quiz
from services.feature_gates import (
    can_take_quiz,
    increment_weekly_quizzes,
    get_effective_plan_for_user,
    get_remaining_weekly_quizzes,
)
from services.quiz_service import QuizService


quiz_bp = Blueprint("quiz", __name__, url_prefix="/api/quiz")


def _resolve_user_id(identity: object) -> Optional[int]:
    if isinstance(identity, int):
        return identity
    if isinstance(identity, dict) and isinstance(identity.get("id"), int):
        return identity["id"]
    return None


def _parse_deck_ids(value) -> Optional[Sequence[int]]:
    if not isinstance(value, list) or not value:
        return None
    deck_ids = []
    for item in value:
        try:
            deck_ids.append(int(item))
        except (TypeError, ValueError):
            return None
    return deck_ids


@quiz_bp.route("/generate", methods=["POST"])
@jwt_required()
def generate_quiz():
    """Generate a quiz after enforcing freemium usage limits."""
    identity = get_jwt_identity()
    user_id = _resolve_user_id(identity)
    if user_id is None:
        return jsonify({"error": "invalid token payload"}), 401

    user = User.query.get(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    payload = request.get_json(force=True) or {}
    deck_ids = _parse_deck_ids(payload.get("deck_ids"))
    total_questions = payload.get("total_questions")
    quiz_type = payload.get("quiz_type", "multiple_choice")
    time_limit_seconds = payload.get("time_limit_seconds")

    if not deck_ids:
        return jsonify({"error": "deck_ids must be a non-empty list of deck ids"}), 400
    try:
        total_questions = int(total_questions)
        if total_questions < 1 or total_questions > 50:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "total_questions must be an integer between 1 and 50"}), 400

    if quiz_type not in {"multiple_choice", "written", "mixed"}:
        return jsonify({"error": "quiz_type must be 'multiple_choice', 'written', or 'mixed'"}), 400

    if time_limit_seconds is not None:
        try:
            time_limit_seconds = int(time_limit_seconds)
            if time_limit_seconds <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "time_limit_seconds must be a positive integer or null"}), 400

    allowed, context = can_take_quiz(user)
    if not allowed:
        return jsonify({
            "error": "Quiz limit reached",
            "message": "Upgrade to Premium for unlimited quizzes",
            "usage": {
                "used": context["used"],
                "limit": context["limit"],
                "remaining": context["remaining"],
                "week_key": context["week_key"],
            },
            "upgrade_url": "/billing",
        }), 403

    decks = Deck.query.filter(Deck.user_id == user_id, Deck.id.in_(deck_ids)).all()
    if len(decks) != len(deck_ids):
        return jsonify({"error": "One or more decks not found or access denied"}), 404

    try:
        quiz, questions = QuizService.generate_quiz(
            user_id=user_id,
            deck_ids=deck_ids,
            total_questions=total_questions,
            quiz_type=quiz_type,
            time_limit_seconds=time_limit_seconds,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    plan = get_effective_plan_for_user(user)
    if plan == "free":
        increment_weekly_quizzes(user_id)
        remaining, usage_row = get_remaining_weekly_quizzes(user_id)
        quiz_usage = {
            "used": usage_row.quiz_count or 0,
            "limit": context["limit"],
            "remaining": remaining,
            "week_key": usage_row.week_key,
        }
    else:
        quiz_usage = {
            "used": context["used"],
            "limit": "unlimited",
            "remaining": "unlimited",
            "week_key": context["week_key"],
        }

    return jsonify({
        "quiz": quiz.to_dict(),
        "questions": questions,
        "usage": quiz_usage,
    }), 200


@quiz_bp.route("/<int:quiz_id>/answer", methods=["POST"])
@jwt_required()
def submit_quiz_answer(quiz_id: int):
    """Submit an answer for a quiz question."""
    identity = get_jwt_identity()
    user_id = _resolve_user_id(identity)
    if user_id is None:
        return jsonify({"error": "invalid token payload"}), 401

    quiz: Quiz | None = Quiz.query.filter_by(id=quiz_id, user_id=user_id).first()
    if not quiz:
        return jsonify({"error": "Quiz not found"}), 404

    payload = request.get_json(force=True) or {}
    answer_id = payload.get("answer_id")
    user_answer = payload.get("user_answer")
    time_spent_seconds = payload.get("time_spent_seconds")

    try:
        answer_id = int(answer_id)
    except (TypeError, ValueError):
        return jsonify({"error": "answer_id must be an integer"}), 400

    if not isinstance(user_answer, str) or not user_answer.strip():
        return jsonify({"error": "user_answer must be a non-empty string"}), 400

    try:
        time_spent_seconds = int(time_spent_seconds)
        if time_spent_seconds < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "time_spent_seconds must be a non-negative integer"}), 400

    try:
        result = QuizService.submit_answer(
            quiz_id=quiz_id,
            answer_id=answer_id,
            user_answer=user_answer,
            time_spent_seconds=time_spent_seconds,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify(result), 200


@quiz_bp.route("/<int:quiz_id>/complete", methods=["POST"])
@jwt_required()
def complete_quiz(quiz_id: int):
    """Finalize a quiz and return a summary."""
    identity = get_jwt_identity()
    user_id = _resolve_user_id(identity)
    if user_id is None:
        return jsonify({"error": "invalid token payload"}), 401

    quiz: Quiz | None = Quiz.query.filter_by(id=quiz_id, user_id=user_id).first()
    if not quiz:
        return jsonify({"error": "Quiz not found"}), 404

    try:
        summary = QuizService.complete_quiz(quiz_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify(summary), 200
