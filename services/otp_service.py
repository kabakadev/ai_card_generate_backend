# services/otp_service.py — HTTPS-only email (Brevo) + OTP core
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from secrets import randbelow
from typing import Optional, Tuple

import requests
from flask import request
from sqlalchemy import text

from config import app, db, bcrypt
from models.security.otp_code import OTPCode

logger = logging.getLogger(__name__)

# ============================== #
# Time helpers (UTC, tz-aware)   #
# ============================== #

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _as_aware_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Coerce to tz-aware UTC for safe comparisons (assumes naive times are UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

# ============================== #
# Config / flags                 #
# ============================== #

def _truthy(val: object) -> bool:
    return str(val).lower() in {"1", "true", "yes", "on"}

def _dev_echo_enabled() -> bool:
    # Explicit flag wins; otherwise allow echo in debug
    return _truthy(os.getenv("OTP_DEV_ECHO_CODE")) or bool(app.debug)

# ============================== #
# Email (Brevo HTTPS only)       #
# ============================== #

_BREVO_ENDPOINT = "https://api.brevo.com/v3/smtp/email"

def _send_email(to_email: str, subject: str, text_body: str, html_body: Optional[str] = None) -> bool:
    """
    Send via Brevo HTTPS API. Requires:
      - BREVO_API_KEY
      - BREVO_SENDER_EMAIL (verified in Brevo)
      - BREVO_SENDER_NAME  (optional)
      - BREVO_TEMPLATE_ID  (optional, >0 to use template)
    """
    api_key = app.config.get("BREVO_API_KEY") or os.environ.get("BREVO_API_KEY")
    if not api_key:
        logger.error("BREVO_API_KEY missing; refusing to send email.")
        return False

    from_email = (
        app.config.get("BREVO_SENDER_EMAIL")
        or os.environ.get("BREVO_SENDER_EMAIL")
        or "no-reply@example.com"
    )
    from_name = (
        app.config.get("BREVO_SENDER_NAME")
        or os.environ.get("BREVO_SENDER_NAME")
        or "FlashLearn"
    )
    template_id = int(app.config.get("BREVO_TEMPLATE_ID", 0) or 0)

    payload: dict = {
        "sender": {"name": from_name, "email": from_email},
        "to": [{"email": to_email}],
    }

    if template_id > 0:
        payload["templateId"] = template_id
        # Optional: payload["params"] = {"otp": "...", "ttl": "..."}
    else:
        payload["subject"] = subject
        payload["textContent"] = text_body
        if html_body:
            payload["htmlContent"] = html_body

    try:
        r = requests.post(
            _BREVO_ENDPOINT,
            headers={
                "api-key": api_key,
                "accept": "application/json",
                "content-type": "application/json",
            },
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
        logger.info("Brevo send OK: status=%s body=%s", r.status_code, r.text[:300])
        return True
    except Exception:
        logger.exception("Brevo send failed")
        return False

# =================================================================== #
# DB compatibility: legacy NOT NULL public.otp_codes.code support     #
# =================================================================== #

_legacy_code_check_cache: dict[str, bool] = {}

def _otp_table_requires_plain_code() -> bool:
    """
    Returns True if `public.otp_codes.code` is NOT NULL.
    We never store the real code there; use a redacted placeholder to satisfy NOT NULL.
    """
    cache_key = "otp_requires_code"
    if cache_key in _legacy_code_check_cache:
        return _legacy_code_check_cache[cache_key]

    sql = text("""
        select is_nullable
        from information_schema.columns
        where table_schema = 'public'
          and table_name = 'otp_codes'
          and column_name = 'code'
        limit 1
    """)
    try:
        row = db.session.execute(sql).fetchone()
        requires = bool(row and (row[0] == "NO"))
        _legacy_code_check_cache[cache_key] = requires
        return requires
    except Exception:
        logger.exception("Failed to probe otp_codes.code nullability; assuming nullable/absent.")
        _legacy_code_check_cache[cache_key] = False
        return False

def _insert_otp_row(
    *,
    user_id: int,
    purpose: str,
    code_plain: str,  # not persisted (only for email + hashing)
    code_hash: str,
    expires_at: datetime,
    sent_to: str,
    ip: str,
    user_agent: str,
) -> int:
    """
    Insert an OTP row. If a legacy NOT NULL plaintext column exists, store a redacted placeholder instead.
    Returns newly created row ID.
    """
    needs_plain_code = _otp_table_requires_plain_code()
    created_at = _now_utc()
    attempts = 0
    max_attempts = 5
    consumed = False

    if needs_plain_code:
        redacted = "******"
        sql = text("""
            insert into public.otp_codes
                (user_id, purpose, code, code_hash, expires_at, attempts, max_attempts, consumed, sent_to, ip, user_agent, created_at)
            values
                (:user_id, :purpose, :code, :code_hash, :expires_at, :attempts, :max_attempts, :consumed, :sent_to, :ip, :user_agent, :created_at)
            returning id
        """)
        params = dict(
            user_id=user_id,
            purpose=purpose,
            code=redacted,
            code_hash=code_hash,
            expires_at=expires_at,
            attempts=attempts,
            max_attempts=max_attempts,
            consumed=consumed,
            sent_to=sent_to,
            ip=ip,
            user_agent=user_agent,
            created_at=created_at,
        )
        new_id = db.session.execute(sql, params).scalar_one()
        db.session.commit()
        return int(new_id)

    otp = OTPCode(
        user_id=user_id,
        purpose=purpose,
        code_hash=code_hash,
        expires_at=expires_at,
        attempts=attempts,
        max_attempts=max_attempts,
        consumed=consumed,
        sent_to=sent_to,
        ip=ip,
        user_agent=user_agent,
        created_at=created_at,
    )
    db.session.add(otp)
    db.session.commit()
    return int(otp.id)

# ============================== #
# OTP core                       #
# ============================== #

def _gen_code(n_digits: int = 6) -> str:
    """6-digit code with leading zeros preserved."""
    upper = 10 ** n_digits
    return f"{randbelow(upper):0{n_digits}d}"

def issue_otp(
    user,
    email: str,
    minutes: int = 5,
    purpose: str = "login",
    ip: Optional[str] = None,
    ua: Optional[str] = None,
):
    """
    Invalidate previous active OTPs for (user, purpose), create a new one, send email.
    Returns: (otp_obj_like, sent_bool, dev_code_or_None)
    """
    # Invalidate any previous active codes for this user/purpose
    OTPCode.query.filter_by(user_id=user.id, purpose=purpose, consumed=False).delete(synchronize_session=False)
    db.session.commit()

    code = _gen_code()
    code_hash = bcrypt.generate_password_hash(code).decode("utf-8")

    otp_id = _insert_otp_row(
        user_id=user.id,
        purpose=purpose,
        code_plain=code,
        code_hash=code_hash,
        expires_at=_now_utc() + timedelta(minutes=minutes),
        sent_to=email,
        ip=(ip or "")[:64],
        user_agent=(ua or "")[:512],
    )

    subject = "Your FlashLearn login code"
    text_body = (
        f"Your FlashLearn login code is: {code}\n\n"
        f"This code expires in {minutes} minutes. If you didn’t request it, ignore this email."
    )
    html_body = (
        f"<p>Your FlashLearn login code is: <strong>{code}</strong></p>"
        f"<p>This code expires in {minutes} minutes.</p>"
    )

    sent = _send_email(email, subject, text_body, html_body)

    # dev echo (for local debugging / integration testing)
    dev_code = code if _dev_echo_enabled() else None

    class _Obj:
        def __init__(self, id_: int) -> None:
            self.id = id_

    return _Obj(otp_id), sent, dev_code

def verify_and_consume_otp(user, otp_id: int, code: str) -> Tuple[bool, str]:
    """
    Verify OTP and consume it on success.
    Returns (ok, reason) where reason in {"ok","invalid_otp","already_used","expired","too_many_attempts","invalid_code"}.
    """
    otp = OTPCode.query.filter_by(id=otp_id, user_id=user.id, purpose="login").first()
    if not otp:
        return False, "invalid_otp"
    if otp.consumed:
        return False, "already_used"

    expires_at_utc = _as_aware_utc(otp.expires_at)
    if not expires_at_utc or _now_utc() > expires_at_utc:
        return False, "expired"

    if otp.attempts >= (otp.max_attempts or 5):
        return False, "too_many_attempts"

    otp.attempts += 1
    ok = bcrypt.check_password_hash(otp.code_hash, code)
    if ok:
        otp.consumed = True
    db.session.commit()
    return ok, ("ok" if ok else "invalid_code")

def issue_login_code(user, ttl_minutes: int = 5) -> dict:
    """
    Legacy wrapper used by routes: returns {'otp_id': int, 'dev_code': str|None, 'sent': bool}
    """
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    ua = request.headers.get("User-Agent", "")[:512]
    otp, sent, dev_code = issue_otp(
        user=user,
        email=user.email,
        minutes=ttl_minutes,
        purpose="login",
        ip=ip,
        ua=ua,
    )
    return {"otp_id": otp.id, "dev_code": dev_code, "sent": sent}

def verify_login_code(user, otp_id: int, code: str) -> Tuple[bool, str]:
    """Legacy wrapper returning (ok, reason)."""
    return verify_and_consume_otp(user, otp_id, code)
