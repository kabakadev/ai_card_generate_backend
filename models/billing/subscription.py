# models/billing/subscription.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy import Enum as SAEnum
from sqlalchemy.ext.hybrid import hybrid_property
from config import db

UTC = timezone.utc
PLAN_PERIOD_DAYS = {
    "daily": 1,
    "monthly": 30,
}
DEFAULT_PERIOD_DAYS = PLAN_PERIOD_DAYS["monthly"]

def _utcnow() -> datetime:
    return datetime.now(tz=UTC)

class Subscription(db.Model):
    __tablename__ = "subscriptions"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )

    plan_type = db.Column(
        SAEnum("daily", "monthly", name="plan_types_v1"),
        nullable=False,
        server_default="monthly",
    )

    status = db.Column(
        SAEnum("active", "expired", "cancelled", "pending", name="subscription_status_v1"),
        nullable=False,
        server_default="pending",
    )

    start_date = db.Column(db.DateTime, nullable=True)
    end_date = db.Column(db.DateTime, nullable=True)

    amount = db.Column(db.Integer, nullable=False, server_default="100")
    currency = db.Column(db.String(3), nullable=False, server_default="KES")

    auto_renew = db.Column(db.Boolean, nullable=False, server_default=db.text("true"))

    created_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())
    updated_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now(), onupdate=db.func.now())

    # NOTE: no explicit relationship here; `User.subscription` defines it via backref.

    @staticmethod
    def calculate_end_date(start_date: datetime, plan_type: str) -> datetime:
        days = PLAN_PERIOD_DAYS.get(plan_type, DEFAULT_PERIOD_DAYS)
        return start_date + timedelta(days=days)

    @hybrid_property
    def is_active(self) -> bool:
        now = _utcnow()
        return self.status == "active" and bool(self.end_date) and self.end_date > now

    def days_remaining(self) -> int:
        now = _utcnow()
        if self.is_active and self.end_date:
            delta = self.end_date - now
            return max(0, int(delta.total_seconds() // 86400))
        return 0

    def activate_from(self, start: Optional[datetime] = None, *, amount: Optional[int] = None, currency: Optional[str] = None) -> None:
        now = _utcnow()
        start_dt = start or now
        self.start_date = start_dt
        self.end_date = self.calculate_end_date(start_dt, self.plan_type)
        self.status = "active"
        self.updated_at = now
        if amount is not None:
            self.amount = int(amount)
        if currency:
            self.currency = currency

    def extend_one_period(self) -> None:
        now = _utcnow()
        if self.is_active and self.end_date:
            days = PLAN_PERIOD_DAYS.get(self.plan_type, DEFAULT_PERIOD_DAYS)
            self.end_date = self.end_date + timedelta(days=days)
        else:
            self.start_date = now
            self.end_date = self.calculate_end_date(now, self.plan_type)
            self.status = "active"
        self.updated_at = now

    def expire_if_past(self) -> None:
        if self.status == "active" and self.end_date and self.end_date <= _utcnow():
            self.status = "expired"
            self.updated_at = _utcnow()

    def cancel(self) -> None:
        self.status = "cancelled"
        self.updated_at = _utcnow()
