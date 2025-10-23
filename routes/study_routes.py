from datetime import datetime
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

study_bp = Blueprint("study", __name__, url_prefix="/api/study")

# NOTE: For production replace with Redis or another persistent store.
_study_sessions = {}


def _resolve_user_id(identity):
    """Support identity as int or {'id': int}."""
    if isinstance(identity, int):
        return identity
    if isinstance(identity, dict) and isinstance(identity.get("id"), int):
        return identity["id"]
    return None


@study_bp.route("/session/start", methods=["POST"])
@jwt_required()
def start_study_session():
    """Start a new study session with optional tracking."""
    identity = get_jwt_identity()
    user_id = _resolve_user_id(identity)
    if user_id is None:
        return jsonify({"error": "invalid token payload"}), 401

    data = request.get_json() or {}
    deck_id = data.get("deck_id")
    track_reviews = bool(data.get("track_reviews", False))

    if not deck_id:
        return jsonify({"error": "deck_id required"}), 400

    session_key = f"{user_id}:{deck_id}"
    _study_sessions[session_key] = {
        "user_id": user_id,
        "deck_id": deck_id,
        "track_reviews": track_reviews,
        "started_at": datetime.utcnow().isoformat(),
    }

    print(f"[StudySession] Started: user={user_id}, deck={deck_id}, tracking={track_reviews}")

    return jsonify({
        "session_key": session_key,
        "track_reviews": track_reviews,
        "deck_id": deck_id,
    }), 200


@study_bp.route("/session/status", methods=["GET"])
@jwt_required()
def get_session_status():
    """Return the current study session tracking status for a user/deck."""
    identity = get_jwt_identity()
    user_id = _resolve_user_id(identity)
    if user_id is None:
        return jsonify({"error": "invalid token payload"}), 401

    deck_id = request.args.get("deck_id")
    if not deck_id:
        return jsonify({"error": "deck_id required"}), 400

    session_key = f"{user_id}:{deck_id}"
    session = _study_sessions.get(session_key, {})

    return jsonify({
        "track_reviews": session.get("track_reviews", False),
        "active": bool(session),
    }), 200
