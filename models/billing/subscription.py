# models/billing/subscription.py — DROP-IN UPGRADE
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import Enum as SAEnum
from sqlalchemy.ext.hybrid import hybrid_property
from config import db


UTC = timezone.utc
DEFAULT_PERIOD_DAYS = 30  # keep in sync with subscription_manager


def _utcnow() -> datetime:
    # Use aware UTC; if your DB columns are naive, it's still fine to compare consistently
    return datetime.now(tz=UTC)


class Subscription(db.Model):
    __tablename__ = "subscriptions"

    id = db.Column(db.Integer, primary_key=True)

    # You currently enforce exactly one subscription row per user (unique=True).
    # That aligns with our activate() logic (extend the single row).
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        unique=True,
        nullable=False,
        index=True,
    )

    plan_type = db.Column(
        SAEnum("monthly", name="plan_types_v1"),
        nullable=False,
        server_default="monthly",
    )

    status = db.Column(
        SAEnum("active", "expired", "cancelled", "pending", name="subscription_status_v1"),
        nullable=False,
        server_default="pending",
    )

    # Store timestamps as UTC (aware or naive is fine as long as you're consistent).
    start_date = db.Column(db.DateTime, nullable=True)
    end_date = db.Column(db.DateTime, nullable=True)

    amount = db.Column(db.Integer, nullable=False, server_default="100")  # KES integer amount
    currency = db.Column(db.String(3), nullable=False, server_default="KES")

    auto_renew = db.Column(db.Boolean, nullable=False, server_default=db.text("true"))

    created_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        server_default=db.func.now(),
        onupdate=db.func.now(),
    )

    user = db.relationship(
        "User",
        backref=db.backref("subscription_row", uselist=False),
    )

    # ----------------- utilities -----------------

    @staticmethod
    def calculate_end_date(start_date: datetime, plan_type: str) -> datetime:
        # Keep 30 days for "monthly" unless you switch to calendar months globally
        days = DEFAULT_PERIOD_DAYS if plan_type == "monthly" else DEFAULT_PERIOD_DAYS
        return start_date + timedelta(days=days)

    @hybrid_property
    def is_active(self) -> bool:
        now = _utcnow()
        # NOTE: compares against aware now(); if DB stores naive, Python will still compare correctly
        return self.status == "active" and bool(self.end_date) and self.end_date > now

    def days_remaining(self) -> int:
        now = _utcnow()
        if self.is_active and self.end_date:
            delta = self.end_date - now
            return max(0, int(delta.total_seconds() // 86400))
        return 0

    # ----------------- lifecycle helpers (optional) -----------------

    def activate_from(self, start: Optional[datetime] = None, *, amount: Optional[int] = None, currency: Optional[str] = None) -> None:
        """
        Start (or restart) a subscription period from `start` (default: now).
        Sets status=active and moves end_date forward by DEFAULT_PERIOD_DAYS.
        """
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
        """
        If active, extend end_date by one period; otherwise start from now.
        """
        now = _utcnow()
        if self.is_active and self.end_date:
            self.end_date = self.end_date + timedelta(days=DEFAULT_PERIOD_DAYS)
        else:
            self.start_date = now
            self.end_date = self.calculate_end_date(now, self.plan_type)
            self.status = "active"
        self.updated_at = now

    def expire_if_past(self) -> None:
        """
        Flip status to 'expired' if end_date has passed (idempotent).
        """
        if self.status == "active" and self.end_date and self.end_date <= _utcnow():
            self.status = "expired"
            self.updated_at = _utcnow()

    def cancel(self) -> None:
        """
        Mark as cancelled (does not change end_date).
        """
        self.status = "cancelled"
        self.updated_at = _utcnow()
