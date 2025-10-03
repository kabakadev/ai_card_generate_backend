# routes/otp_routes.py
from flask import request
from flask_restful import Resource
from flask_jwt_extended import create_access_token
from flask_limiter.util import get_remote_address
from datetime import timedelta, datetime

from config import limiter, db, app
from models import User
from services.otp_service import issue_login_code, verify_login_code


# -------- helpers / rate-limit keys (match auth_routes style) --------
def _normalize_email():
    data = request.get_json(silent=True) or {}
    return (data.get("email") or "").strip().lower()[:120]


def _rl_key_email():
    """
    Deterministic per-account key: email only (lowercased).
    Falls back to IP if email is missing, so abuse without an email is still throttled.
    """
    email = _normalize_email()
    return f"acct:{email}" if email else f"acct-ip:{get_remote_address()}"


def _rl_key_email_ip_combo():
    """
    Secondary guard: combine IP + email to slow single-host brute force.
    """
    email = _normalize_email()
    ip = get_remote_address()
    return f"otp:{email}:{ip}" if email else f"otp-ip:{ip}"


# -------- EMAIL VERIFICATION (reusing /login/otp/* routes) --------
class RequestLoginOTP(Resource):
    """
    Send OTP to verify email for UNVERIFIED users.
    If the user is already verified, skip sending an OTP.
    """
    @limiter.limit("300 per minute; 500 per hour", key_func=_rl_key_email, override_defaults=False)
    @limiter.limit("100 per minute", key_func=_rl_key_email_ip_combo, override_defaults=False)
    def post(self):
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip().lower()

        if not email:
            return {"error": "email_required"}, 400

        user = User.query.filter_by(email=email).first()
        if not user:
            # Soft success to avoid user enumeration
            return {"status": "sent"}, 200

        if user.email_verified:
            return {"status": "skipped", "reason": "already_verified"}, 200

        # Issue OTP with current service signature (no purpose kwarg)
        res = issue_login_code(user)
        payload = {"status": "sent", "otp_id": res["otp_id"]}
        if res.get("dev_code") and app.debug:
            payload["dev_code"] = res["dev_code"]
        return payload, 200


class VerifyLoginOTP(Resource):
    """
    Verify the email verification OTP.
    On success, mark verified and issue a JWT (auto-login).
    """
    @limiter.limit("10 per minute", key_func=get_remote_address, override_defaults=False)
    def post(self):
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip().lower()
        otp_id = data.get("otp_id")
        code = (data.get("code") or "").strip()

        if not (email and otp_id and code):
            return {"error": "missing_fields"}, 400

        user = User.query.filter_by(email=email).first()
        if not user:
            return {"error": "invalid_user"}, 400

        ok, reason = verify_login_code(user, otp_id=int(otp_id), code=code)
        if not ok:
            return {"error": reason}, 400

        # Mark verified if not already
        if not user.email_verified:
            user.email_verified = True
            user.email_verified_at = datetime.utcnow()
            db.session.commit()

        # Issue JWT (auto-login after email verify)
        token = create_access_token(
            identity=user.id,
            expires_delta=timedelta(hours=48),
            additional_claims={"username": user.username},
        )
        return {"message": "Email verified", "access_token": token}, 200


# -------- PASSWORD RESET (unchanged behavior, still OTP-gated) --------
class ForgotPassword(Resource):
    @limiter.limit("3 per minute; 10 per hour", key_func=_rl_key_email, override_defaults=False)
    def post(self):
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip().lower()

        if not email:
            return {"error": "email_required"}, 400

        user = User.query.filter_by(email=email).first()
        if not user:
            return {"status": "sent"}, 200

        # Use current service signature (no purpose kwarg)
        res = issue_login_code(user)
        payload = {"status": "sent", "otp_id": res["otp_id"]}
        if res.get("dev_code") and app.debug:
            payload["dev_code"] = res["dev_code"]
        return payload, 200


class ResetPassword(Resource):
    @limiter.limit("10 per minute", key_func=get_remote_address, override_defaults=False)
    def post(self):
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip().lower()
        otp_id = data.get("otp_id")
        code = (data.get("code") or "").strip()
        new_password = data.get("new_password")

        if not (email and otp_id and code and new_password):
            return {"error": "missing_fields"}, 400

        user = User.query.filter_by(email=email).first()
        if not user:
            return {"error": "invalid_user"}, 400

        ok, reason = verify_login_code(user, otp_id=int(otp_id), code=code)
        if not ok:
            return {"error": reason}, 400

        user.password_hash = new_password  # property setter hashes
        db.session.commit()

        return {"message": "Password reset successful."}, 200
