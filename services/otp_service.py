# services/otp_service.py
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from datetime import datetime, timedelta, timezone
from secrets import randbelow
from typing import Optional, Tuple

import requests
from sqlalchemy import text

from config import app, db, bcrypt
from models.security.otp_code import OTPCode
from flask import request  # ensure this import exists

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# Time helpers (always timezone-aware; DB should use timestamptz)
# --------------------------------------------------------------------------------------
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _as_aware_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """
    Coerce any datetime to timezone-aware UTC for safe comparisons.
    Assumes naive values are already in UTC. If your DB stored local time,
    convert accordingly before replacing tzinfo.
    """
    if dt is None:
        return None
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

# --------------------------------------------------------------------------------------
# Email sending: prefer HTTPS (Brevo) and fall back to SMTP only if no API key provided
# --------------------------------------------------------------------------------------
def _brevo_configured() -> bool:
    return bool(os.environ.get("BREVO_API_KEY") or app.config.get("BREVO_API_KEY"))

def _send_email_brevo(to_email: str, subject: str, text_body: str, html_body: Optional[str] = None) -> bool:
    """
    Send via Brevo HTTPS API. Requires BREVO_API_KEY and sender identity.
    Docs: https://developers.brevo.com/reference/sendtransacemail
    """
    api_key = os.environ.get("BREVO_API_KEY") or app.config.get("BREVO_API_KEY")
    if not api_key:
        return False

    from_email = app.config.get("FROM_EMAIL") or os.environ.get("FROM_EMAIL") or "no-reply@example.com"
    from_name = app.config.get("FROM_NAME") or os.environ.get("FROM_NAME") or "FlashLearn"

    payload = {
        "sender": {"name": from_name, "email": from_email},
        "to": [{"email": to_email}],
        "subject": subject,
        "textContent": text_body,
    }
    if html_body:
        payload["htmlContent"] = html_body

    try:
        r = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "api-key": api_key,
                "accept": "application/json",
                "content-type": "application/json",
            },
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception:
        logger.exception("Brevo send failed")
        return False

def _smtp_configured() -> bool:
    # used only as a fallback when no BREVO_API_KEY
    return bool(app.config.get("SMTP_HOST") and app.config.get("SMTP_USER") and app.config.get("SMTP_PASSWORD"))

def _send_email_smtp(to_email: str, subject: str, body: str) -> bool:
    host = app.config.get("SMTP_HOST")
    port = int(app.config.get("SMTP_PORT", 587))
    user = app.config.get("SMTP_USER")
    pw   = app.config.get("SMTP_PASSWORD")
    use_tls = bool(app.config.get("SMTP_USE_TLS", True))
    from_addr = app.config.get("SMTP_FROM") or app.config.get("FROM_EMAIL") or "no-reply@example.com"
    from_name = app.config.get("SMTP_FROM_NAME", app.config.get("FROM_NAME", "FlashLearn"))

    if not (host and user and pw and from_addr):
        logger.warning("SMTP not fully configured; skipping real send.")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = to_email
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, port, timeout=20) as s:
            if use_tls:
                s.starttls()
            s.login(user, pw)
            s.send_message(msg)
        return True
    except Exception:
        logger.exception("SMTP send failed")
        return False

def _send_email(to_email: str, subject: str, text_body: str, html_body: Optional[str] = None) -> bool:
    # Prefer HTTPS API; fall back to SMTP if no API key set
    if _brevo_configured():
        return _send_email_brevo(to_email, subject, text_body, html_body)
    return _send_email_smtp(to_email, subject, text_body)

# --------------------------------------------------------------------------------------
# DB compatibility: handle legacy NOT NULL `otp_codes.code` column gracefully
# --------------------------------------------------------------------------------------
_legacy_code_check_cache: dict[str, bool] = {}

def _otp_table_requires_plain_code() -> bool:
    """
    Returns True if `public.otp_codes` has column `code` defined as NOT NULL.
    We cache the result in-process to avoid repeated information_schema queries.
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
        requires = bool(row and (row[0] == "NO"))  # NOT NULL
        _legacy_code_check_cache[cache_key] = requires
        return requires
    except Exception:
        # If the probe fails, assume not required (safer) and log
        logger.exception("Failed to probe otp_codes.code nullability; assuming nullable/absent.")
        _legacy_code_check_cache[cache_key] = False
        return False

def _insert_otp_row(
    *,
    user_id: int,
    purpose: str,
    code_plain: str,
    code_hash: str,
    expires_at: datetime,
    sent_to: str,
    ip: str,
    user_agent: str,
) -> int:
    """
    Inserts an OTP row. If legacy `code NOT NULL` exists, we include `code`
    but we DO NOT store the real code in plaintext—store a redacted token instead.
    Returns the new row's id.
    """
    needs_plain_code = _otp_table_requires_plain_code()

    created_at = _now_utc()
    attempts = 0
    max_attempts = 5
    consumed = False

    if needs_plain_code:
        # redacted placeholder to satisfy NOT NULL; not the actual code
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

    # Modern table: use ORM (no plaintext code column required)
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

# --------------------------------------------------------------------------------------
# OTP core
# --------------------------------------------------------------------------------------
def _gen_code(n_digits: int = 6) -> str:
    # 6-digit code with leading zeros preserved
    return f"{randbelow(1_000_000):06d}"

def issue_otp(
    user,
    email: str,
    minutes: int = 5,
    purpose: str = "login",
    ip: Optional[str] = None,
    ua: Optional[str] = None,
):
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
    html_body = f"<p>Your FlashLearn login code is: <strong>{code}</strong></p><p>This code expires in {minutes} minutes.</p>"

    sent = _send_email(email, subject, text_body, html_body)

    # Echo the code only in dev (or if you set OTP_DEV_ECHO_CODE=true)
    dev_echo = bool(app.config.get("OTP_DEV_ECHO_CODE", app.config.get("ENV", "development").lower() == "development"))
    dev_code = code if dev_echo else None

    # Return lightweight object-shape compatible with old code
    class _Obj:
        def __init__(self, id_: int) -> None:
            self.id = id_
    return _Obj(otp_id), sent, dev_code

def verify_and_consume_otp(user, otp_id: int, code: str) -> Tuple[bool, str]:
    otp = OTPCode.query.filter_by(id=otp_id, user_id=user.id, purpose="login").first()
    if not otp:
        return False, "invalid_otp"
    if otp.consumed:
        return False, "already_used"

    # timezone-safe comparison
    expires_at_utc = _as_aware_utc(otp.expires_at)
    if expires_at_utc is None:
        return False, "expired"
    if _now_utc() > expires_at_utc:
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
    """Legacy wrapper: returns {'otp_id': int, 'dev_code': str|None, 'sent': bool}"""
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
