# services/emailer.py
import os, smtplib
from email.message import EmailMessage

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "no-reply@flashlearn.local")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() in {"1","true","yes","on"}

def send_email(to: str, subject: str, body: str) -> None:
    if not (SMTP_HOST and SMTP_PORT and SMTP_FROM):
        # No SMTP configured → silently no-op (dev-friendly)
        return
    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
        if SMTP_USE_TLS:
            s.starttls()
        if SMTP_USER:
            s.login(SMTP_USER, SMTP_PASS or "")
        s.send_message(msg)
