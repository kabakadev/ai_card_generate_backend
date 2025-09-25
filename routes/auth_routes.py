# routes/auth_routes.py — header-only JWT, per-user rate limits, last_seen updates
from flask import request
from flask_restful import Resource
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
from sqlalchemy.exc import IntegrityError
from datetime import timedelta, datetime
from flask_limiter.util import get_remote_address
import re

from config import db, limiter
from models import User

# ---------------- Validators ----------------
def is_valid_email(email: str) -> bool:
    email_regex = r'^[\w\.-]+@[\w\.-]+\.\w+$'
    return re.match(email_regex, (email or "")) is not None

def is_valid_username(username: str) -> bool:
    return bool(username) and 3 <= len(username) <= 50

# ---------------- Rate-limit key funcs ----------------
def _rl_key_email():
    """
    Deterministic per-account key: email only (lowercased).
    Ensures the 6th attempt within a minute -> 429, regardless of IP rotation.
    """
    try:
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip().lower()[:120]
        if email:
            return f"acct:{email}"
    except Exception:
        pass
    # If no email is supplied, fall back to IP so abuse still throttles.
    return f"acct-ip:{get_remote_address()}"

def _rl_key_login_ip_combo():
    """
    Secondary guard: combine IP + email to slow single-host brute force.
    """
    try:
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip().lower()[:120]
    except Exception:
        email = ""
    ip = get_remote_address()
    return f"login:{email}:{ip}" if email else f"login-ip:{ip}"

# ---------------- Resources ----------------
class Signup(Resource):
    # Basic anti-abuse on signup (per IP)
    @limiter.limit("20 per hour", key_func=get_remote_address, override_defaults=False)
    def post(self):
        data = request.get_json() or {}
        username = data.get("username")
        email = (data.get("email") or "").lower()
        password = data.get("password")

        if not username or not email or not password:
            return {"error": "Missing required fields"}, 400
        if not is_valid_email(email):
            return {"error": "Invalid email format"}, 400
        if not is_valid_username(username):
            return {"error": "Username must be between 3 and 50 characters"}, 400

        # Check for existing username and email separately
        if User.query.filter_by(username=username).first():
            return {"error": "username_exists", "message": "Username already exists"}, 409
        if User.query.filter_by(email=email).first():
            return {"error": "email_exists", "message": "Email already exists"}, 409

        try:
            user = User(username=username, email=email)
            user.password_hash = password  # property setter hashes
            db.session.add(user)
            db.session.commit()
            return {"message": "User registered successfully"}, 201
        except IntegrityError:
            db.session.rollback()
            return {"error": "Username or email already exists"}, 409

class Login(Resource):
    # Per-account hard limit (deterministic): 5/min regardless of IP
    @limiter.limit("5 per minute", key_func=_rl_key_email, override_defaults=False)
    # Optional additional guard: per IP+email
    @limiter.limit("10 per minute", key_func=_rl_key_login_ip_combo, override_defaults=False)
    def post(self):
        data = request.get_json() or {}
        email = (data.get("email") or "").lower()
        password = data.get("password")

        if not email or not password:
            return {"error": "Email and password are required"}, 400

        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            return {"error": "Invalid email or password"}, 401

        # If email is NOT verified, require verification (but do NOT send a login OTP here)
        if not user.email_verified:
            return {"verification_required": True, "next": "verify_email"}, 200

        # If verified, issue token immediately (no OTP on normal login)
        token = create_access_token(
            identity=user.id,
            expires_delta=timedelta(hours=48),
            additional_claims={"username": user.username},
        )
        return {"access_token": token}, 200

class ProtectedUser(Resource):
    @jwt_required()
    def get(self):
        """
        Return current user's profile and update last_seen_at.
        """
        user_id = get_jwt_identity()
        user = User.query.get(user_id)
        if not user:
            return {"error": "user_not_found"}, 404

        # Update last_seen_at on each authenticated fetch
        user.last_seen_at = datetime.utcnow()
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()  # keep response even if commit fails

        return {
            "id": user.id,
            "email": user.email,
            "username": user.username,
            "email_verified": bool(user.email_verified),
            "is_demo": bool(user.is_demo),
            "last_seen_at": user.last_seen_at.isoformat() if user.last_seen_at else None,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        }, 200

class DeleteUser(Resource):
    @jwt_required()
    def delete(self):
        """
        Delete the currently authenticated user's account.
        """
        user_id = get_jwt_identity()
        user = User.query.get(user_id)
        if not user:
            return {"error": "user_not_found"}, 404
        db.session.delete(user)
        db.session.commit()
        return {"message": "Account deleted."}, 200
