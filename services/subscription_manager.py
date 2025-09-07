# services/subscription_manager.py — ROBUST, UTC-SAFE, DROP-IN
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
import logging

from config import db
from models import Subscription, User, PaymentTransaction

logger = logging.getLogger(__name__)

# ---- config --------------------------------------------------------
DEFAULT_PERIOD_DAYS = 30
UTC = timezone.utc


# ---- time helpers --------------------------------------------------
def _now_aware_utc() -> datetime:
    """Return timezone-aware UTC now()."""
    return datetime.now(tz=UTC)


def _as_aware_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize any datetime to aware UTC (assume naive values are UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Treat DB naive timestamps as UTC-naive and attach UTC tzinfo
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _period_end_from(start: datetime) -> datetime:
    return start + timedelta(days=DEFAULT_PERIOD_DAYS)


# ---- public API ----------------------------------------------------
def is_active(user_id: int) -> Tuple[bool, Optional[Subscription]]:
    """
    Robust check for active subscription.
    - Any subscription with end_date >= now(UTC) is considered active.
    - Ignores 'status' if dates prove activity (but logs when status is not 'active').
    - Includes a 10-minute grace after a successful TX to bypass cache/replica lag.
    Returns: (bool, Subscription|None)
    """
    now = _now_aware_utc()

    # Fresh success TX bypass (avoid stale caches)
    fresh_tx = (
        PaymentTransaction.query
        .filter_by(user_id=user_id, status="succeeded")
        .order_by(PaymentTransaction.updated_at.desc())
        .first()
    )
    if fresh_tx:
        ft = _as_aware_utc(getattr(fresh_tx, "updated_at", None))
        if ft and (now - ft) <= timedelta(minutes=10):
            sub = (
                Subscription.query
                .filter(Subscription.user_id == user_id)
                .order_by(Subscription.start_date.desc().nullslast())
                .first()
            )
            if sub:
                end = _as_aware_utc(getattr(sub, "end_date", None))
                if end and end >= now:
                    if getattr(sub, "status", None) != "active":
                        logger.info("is_active: treating sub id=%s as active by dates (status=%s)", sub.id, sub.status)
                    return True, sub

    # Normal path: most recent sub by start_date (or end_date if start is null)
    sub: Optional[Subscription] = (
        Subscription.query
        .filter(Subscription.user_id == user_id)
        .order_by(Subscription.start_date.desc().nullslast(), Subscription.end_date.desc().nullslast())
        .first()
    )

    if not sub:
        logger.debug("is_active FALSE user_id=%s (no subscriptions)", user_id)
        return False, None

    end = _as_aware_utc(getattr(sub, "end_date", None))
    if end and end >= now:
        # Dates say it's active — use that as source of truth
        if getattr(sub, "status", None) != "active":
            logger.info("is_active: sub id=%s active by dates but status=%s", sub.id, getattr(sub, "status", None))
        return True, sub

    logger.debug(
        "is_active FALSE user_id=%s (latest sub id=%s status=%s end=%s now=%s)",
        user_id, sub.id, getattr(sub, "status", None), end, now
    )
    return False, None


def activate(
    user_id: int,
    plan: str = "monthly",
    amount: Optional[int] = None,
    currency: Optional[str] = "KES",
) -> Subscription:
    """
    Upsert/extend a subscription period.
    - If there's an active sub (end_date >= now), extend by one period.
    - Else, (re)start now for DEFAULT_PERIOD_DAYS.
    - Stores times as aware UTC; tolerates naive DB values on read.
    - Safe to call repeatedly (idempotent per call).
    """
    now = _now_aware_utc()
    logger.info(
        "Activate subscription: user_id=%s plan=%s amount=%s currency=%s",
        user_id, plan, amount, currency
    )

    try:
        db.session.expire_all()  # avoid stale ORM state

        # Fetch latest sub for this user/plan, regardless of status
        sub: Optional[Subscription] = (
            Subscription.query
            .filter(Subscription.user_id == user_id, Subscription.plan_type == plan)
            .order_by(Subscription.start_date.desc().nullslast(), Subscription.end_date.desc().nullslast())
            .first()
        )

        if sub:
            # Normalize existing times for safe comparison
            end = _as_aware_utc(getattr(sub, "end_date", None))
        else:
            sub = Subscription(user_id=user_id, plan_type=plan)
            db.session.add(sub)
            end = None

        if end and end >= now:
            # Extend current active window by one period
            new_end = end + timedelta(days=DEFAULT_PERIOD_DAYS)
            sub.end_date = new_end  # store aware UTC
            # keep start_date as-is (when the cycle originally started)
            logger.info("Extending sub id=%s end: %s -> %s", sub.id, end, new_end)
        else:
            # (Re)start from now
            sub.start_date = now
            sub.end_date = _period_end_from(now)
            logger.info("Starting sub id=%s from now -> end=%s", getattr(sub, "id", None), sub.end_date)

        # Set/refresh metadata
        if hasattr(sub, "status"):
            sub.status = "active"
        if amount is not None and hasattr(sub, "amount"):
            sub.amount = amount
        if currency and hasattr(sub, "currency"):
            sub.currency = currency
        if hasattr(sub, "updated_at"):
            sub.updated_at = now
        if getattr(sub, "created_at", None) is None and hasattr(sub, "created_at"):
            sub.created_at = now
        if hasattr(sub, "auto_renew") and getattr(sub, "auto_renew", None) is None:
            sub.auto_renew = True
        if hasattr(sub, "provider") and getattr(sub, "provider", None) is None:
            sub.provider = "intasend"

        # Best-effort annotate user row
        try:
            user = User.query.get(user_id)
            if user:
                if hasattr(user, "plan_type"):
                    user.plan_type = plan
                if hasattr(user, "is_premium"):
                    user.is_premium = True
                if hasattr(user, "updated_at"):
                    user.updated_at = now
        except Exception as e:
            logger.warning("Non-fatal: failed to update user flags for user_id=%s: %s", user_id, e)

        db.session.commit()
        db.session.refresh(sub)

        # Post-commit sanity
        active_now, _ = is_active(user_id)
        if not active_now:
            logger.error(
                "Activation anomaly: is_active=False immediately after activation (user_id=%s, sub_id=%s)",
                user_id, sub.id
            )

        logger.info(
            "Activation persisted: sub_id=%s status=%s start=%s end=%s",
            sub.id, getattr(sub, "status", None), sub.start_date, sub.end_date
        )
        return sub

    except Exception as e:
        logger.exception("Activation failed user_id=%s: %s", user_id, e)
        db.session.rollback()
        raise


# ---- optional helper (used elsewhere) --------------------------------
def get_or_create_subscription(user_id: int, plan: str = "monthly") -> Subscription:
    """
    Return the most recent subscription row for a user/plan, creating a
    placeholder row if none exists (status='pending'), so UI can show
    something before first activation. Prefer not to call this in gates.
    """
    sub: Optional[Subscription] = (
        Subscription.query
        .filter_by(user_id=user_id, plan_type=plan)
        .order_by(Subscription.start_date.desc().nullslast(), Subscription.end_date.desc().nullslast())
        .first()
    )
    if sub:
        return sub

    now = _now_aware_utc()
    sub = Subscription(
        user_id=user_id,
        plan_type=plan,
        status="pending",
        created_at=now if hasattr(Subscription, "created_at") else None,
        updated_at=now if hasattr(Subscription, "updated_at") else None,
        auto_renew=True if hasattr(Subscription, "auto_renew") else None,
    )
    db.session.add(sub)
    db.session.commit()
    return sub
