from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey, Index
from config import db

class OTPCode(db.Model):
    __tablename__ = "otp_codes"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    purpose = Column(String(32), nullable=False, default="login")
    code_hash = Column(String(255), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=5)
    consumed = Column(Boolean, default=False)

    sent_to = Column(String(255))     # where we sent (email)
    ip = Column(String(64))
    user_agent = Column(String(512))

    created_at = Column(DateTime, default=datetime.utcnow)

    # NOTE: no child-side `user` relationship; created via User.otp_codes backref

Index("ix_otp_user_purpose_active", OTPCode.user_id, OTPCode.purpose, OTPCode.consumed)
