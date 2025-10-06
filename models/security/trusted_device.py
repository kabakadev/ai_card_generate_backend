# models/security/trusted_device.py
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint
from config import db

class TrustedDevice(db.Model):
    __tablename__ = "trusted_devices"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    device_hash = Column(String(64), index=True, nullable=False)  # sha256 hex
    label = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used_at = Column(DateTime, default=datetime.utcnow)

    # NOTE: no child-side relationship; created via User.trusted_devices backref

    __table_args__ = (
        UniqueConstraint("user_id", "device_hash", name="uq_user_device"),
    )
