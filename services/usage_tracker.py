# services/usage_tracker.py — DROP-IN
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional, Tuple, Dict, Any

from config import db
from models import UsageLimits

# Subscription truth source (already UTC/atomic in subscription_manager)
from services.subscription_manager import is_active

# Free-tier helpers
from services.feature_gates import (
    get_effective_plan_for_user,    # UI flavor / trials (non-authoritative)
    ensure_month_usage,             # ensure UsageLimits row for current month
    get_remaining_monthly_prompts,  # (remaining, row)
    increment_monthly_prompts,      # bump SQL counter
    FREE_TIER_MONTHLY_AI,
)

# Optional Redis for a daily soft-cap on free users
REDIS_URL = os.getenv("REDIS_URL", "")
_redis = None
if REDIS_URL:
    try:
        import redis  # type: ignore
        _redis = redis.from_url(REDIS_URL, decode_responses=True)
    except Exception:
        _redis = None  # continue without Redis


# ---------- helpers ----------

def _uid(user: Any) -> int:
    """Support both user.id and user.user_id shapes."""
    if hasattr(user, "id") and isinstance(getattr(user, "id"), int):
        return int(user.id)
    if hasattr(user, "user_id") and isinstance(getattr(user, "user_id"), int):
        return int(user.user_id)
    raise ValueError("Could not derive user id from user object (expects .id or .user_id).")

def _day_key() -> str:
    # naive UTC (consistent with server side)
    return datetime.utcnow().strftime("%Y-%m-%d")

def _month_key() -> str:
    return datetime.utcnow().strftime("%Y-%m")

def _r_key(prefix: str, user_id: int, suffix: Optional[str] = None) -> str:
    k = f"{prefix}:{user_id}"
    return f"{k}:{suffix}" if suffix else k


# ---------- public API ----------

def can_generate_now(user: Any) -> Tuple[bool, Dict[str, Any]]:
    """
    Primary guard for AI generation.

    Decision order:
      1) If the user has an ACTIVE subscription (from DB), allow immediately (no cap).
      2) Otherwise, enforce free-tier monthly quota in SQL.
      3) Optionally apply a daily soft-cap via Redis (if configured).

    Returns (allowed, context) where context is UI-friendly.
    """
    user_id = _uid(user)

    # Avoid stale reads (important right after payment verification)
    db.session.expire_all()

    # 1) Subscription is the source of truth
    active, sub = is_active(user_id)
    if active:
        row = ensure_month_usage(user_id)  # keep analytics flowing
        ctx = {
            "plan": "premium",
            "month_key": row.month_key,
            "used": row.ai_prompt_count or 0,
            "remaining": None,          # unlimited
            "free_quota": None,         # not applicable
            "source": "subscription",
            "current_period_end": sub.end_date.isoformat() + "Z" if getattr(sub, "end_date", None) else None,
        }
        return True, ctx

    # 2) Free plan — authoritative monthly quota in SQL
    remaining, row = get_remaining_monthly_prompts(user_id)
    remaining = max(0, int(remaining or 0))
    allowed = remaining > 0

    ctx: Dict[str, Any] = {
        "plan": "free",
        "month_key": row.month_key,
        "used": int(row.ai_prompt_count or 0),
        "remaining": remaining,
        "free_quota": int(row.free_quota or FREE_TIER_MONTHLY_AI),
        "source": "monthly-sql",
    }

    # 3) Optional daily soft-cap via Redis
    daily_cap_env = os.getenv("FREE_TIER_DAILY_AI", "")
    if allowed and _redis and daily_cap_env:
        try:
            daily_cap = max(0, int(daily_cap_env))
            today = _day_key()
            today_key = _r_key("ai_today", user_id, today)
            today_count = _redis.incr(today_key)  # atomic inc
            _redis.expire(today_key, 60 * 60 * 26)  # ~26h TTL to straddle midnight safely

            if today_count > daily_cap:
                # roll back and block for today (soft-cap UX)
                _redis.decr(today_key)
                allowed = False
                ctx.update({"remaining_daily": 0, "daily_cap": daily_cap, "source": "redis+monthly"})
            else:
                ctx.update({
                    "remaining_daily": max(0, daily_cap - today_count),
                    "daily_cap": daily_cap,
                    "source": "redis+monthly",
                })
        except Exception:
            # Redis hiccups fall back silently to monthly SQL
            pass

    return allowed, ctx


def increment_after_success(user: Any, n: int = 1) -> Dict[str, Any]:
    """
    Call AFTER a successful generation.

    - Always increments SQL monthly counter for analytics (even for premium).
    - Mirrors to Redis monthly bucket if available (metrics only).
    - Returns a context dict similar to can_generate_now().
    """
    user_id = _uid(user)

    # Increment authoritative monthly counter in SQL
    row = increment_monthly_prompts(user_id, n=max(1, int(n)))

    # Re-check plan for the response context (no caching)
    db.session.expire_all()
    active, sub = is_active(user_id)
    plan = "premium" if active else "free"

    ctx: Dict[str, Any] = {
        "plan": plan,
        "month_key": row.month_key,
        "used": int(row.ai_prompt_count or 0),
        "free_quota": None if active else int(row.free_quota or FREE_TIER_MONTHLY_AI),
        "remaining": None if active else max(0, int(row.free_quota or FREE_TIER_MONTHLY_AI) - int(row.ai_prompt_count or 0)),
        "source": "subscription" if active else "monthly-sql",
    }

    # Redis monthly mirror (optional, metrics only)
    if _redis:
        try:
            mk = _month_key()
            m_key = _r_key("ai_month", user_id, mk)
            m_count = _redis.incrby(m_key, max(1, int(n)))
            _redis.expire(m_key, 60 * 60 * 24 * 40)  # ~40 days
            ctx["redis_month_count"] = int(m_count)
            if not active:
                ctx["source"] = "redis+monthly"
        except Exception:
            pass

    return ctx


def snapshot(user: Any) -> Dict[str, Any]:
    """
    Read-only snapshot for UI: {plan, month_key, used, remaining, free_quota, ...}
    Trust subscription first; otherwise show free-tier view.
    """
    user_id = _uid(user)

    # Keep reads fresh
    db.session.expire_all()

    # Subscription truth first
    active, sub = is_active(user_id)
    row = ensure_month_usage(user_id)  # ensure the row exists for counters

    if active:
        snap = {
            "plan": "premium",
            "month_key": row.month_key,
            "used": int(row.ai_prompt_count or 0),
            "remaining": None,           # unlimited
            "free_quota": None,
            "current_period_end": sub.end_date.isoformat() + "Z" if getattr(sub, "end_date", None) else None,
            "source": "subscription",
        }
    else:
        remaining, row2 = get_remaining_monthly_prompts(user_id)
        snap = {
            "plan": "free",
            "month_key": row2.month_key,
            "used": int(row2.ai_prompt_count or 0),
            "remaining": max(0, int(remaining or 0)),
            "free_quota": int(row2.free_quota or FREE_TIER_MONTHLY_AI),
            "source": "monthly-sql",
        }

    # Optional daily info augmentation for free plan
    daily_cap_env = os.getenv("FREE_TIER_DAILY_AI", "")
    if _redis and daily_cap_env and snap["plan"] == "free":
        try:
            daily_cap = max(0, int(daily_cap_env))
            today_key = _r_key("ai_today", user_id, _day_key())
            today_count = int(_redis.get(today_key) or 0)
            snap.update({
                "daily_cap": daily_cap,
                "used_today": today_count,
                "remaining_daily": max(0, daily_cap - today_count),
                "source": "redis+monthly",
            })
        except Exception:
            pass

    return snap
