# services/otp_service.py
from __future__ import annotations

import logging, smtplib
from email.message import EmailMessage
from email.utils import formataddr
from datetime import datetime, timedelta
from secrets import randbelow

from config import app, db, bcrypt
from models.security.otp_code import OTPCode
from flask import request  # add at top of file if not present

logger = logging.getLogger(__name__)

def _smtp_configured() -> bool:
    return bool(app.config.get("SMTP_HOST") and app.config.get("SMTP_USER") and app.config.get("SMTP_PASSWORD"))

def _send_email_smtp(to_email: str, subject: str, body: str) -> bool:
    host = app.config.get("SMTP_HOST")
    port = int(app.config.get("SMTP_PORT", 587))
    user = app.config.get("SMTP_USER")
    pw   = app.config.get("SMTP_PASSWORD")
    use_tls = bool(app.config.get("SMTP_USE_TLS", True))
    from_addr = app.config.get("SMTP_FROM")
    from_name = app.config.get("SMTP_FROM_NAME", "FlashLearn")

    if not (host and user and pw and from_addr):
        logger.warning("SMTP not fully configured; skipping real send (dev mode).")
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

def _gen_code(n_digits: int = 6) -> str:
    return f"{randbelow(1_000_000):06d}"

def issue_otp(user, email: str, minutes: int = 5, purpose: str = "login", ip: str | None = None, ua: str | None = None):
    # Invalidate any previous active codes for this user/purpose
    OTPCode.query.filter_by(user_id=user.id, purpose=purpose, consumed=False).delete()
    db.session.commit()

    code = _gen_code()
    code_hash = bcrypt.generate_password_hash(code).decode("utf-8")

    otp = OTPCode(
        user_id=user.id,
        purpose=purpose,
        code_hash=code_hash,
        expires_at=datetime.utcnow() + timedelta(minutes=minutes),
        max_attempts=5,
        consumed=False,
        sent_to=email,
        ip=(ip or "")[:64],
        user_agent=(ua or "")[:512],
    )
    db.session.add(otp)
    db.session.commit()

    subject = "Your FlashLearn login code"
    body = (
        f"Your FlashLearn login code is: {code}\n\n"
        f"This code expires in {minutes} minutes. If you didn’t request it, ignore this email."
    )

    sent = _send_email_smtp(email, subject, body)

    # Echo the code only in dev (or if you set OTP_DEV_ECHO_CODE=true)
    dev_echo = bool(app.config.get("OTP_DEV_ECHO_CODE", app.config.get("ENV", "development").lower() == "development"))
    dev_code = code if dev_echo else None

    return otp, sent, dev_code

def verify_and_consume_otp(user, otp_id: int, code: str) -> tuple[bool, str]:
    otp = OTPCode.query.filter_by(id=otp_id, user_id=user.id, purpose="login").first()
    if not otp:
        return False, "invalid_otp"
    if otp.consumed:
        return False, "already_used"
    if datetime.utcnow() > otp.expires_at:
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
    # keep legacy return shape
    return {"otp_id": otp.id, "dev_code": dev_code, "sent": sent}

def verify_login_code(user, otp_id: int, code: str) -> tuple[bool, str]:
    """Legacy wrapper returning (ok, reason)."""
    return verify_and_consume_otp(user, otp_id, code)