# services/feature_gates.py
from __future__ import annotations

from typing import Literal, Tuple, Optional
from datetime import datetime
from config import db
from models import User, UsageLimits, Subscription

Plan = Literal["free", "premium"]

# ----- Freemium limits (MVP) -----
FREE_TIER_MONTHLY_AI = 5        # 5 prompts / month
FREE_TIER_MAX_DECKS   = 3       # (optional UI constraint)

# ----- Helpers for the monthly usage model we implemented -----
def month_key_now() -> str:
    return datetime.utcnow().strftime("%Y-%m")

def ensure_month_usage(user_id: int, month_key: Optional[str] = None) -> UsageLimits:
    mk = month_key or month_key_now()
    row = UsageLimits.query.filter_by(user_id=user_id, month_key=mk).first()
    if not row:
        row = UsageLimits(user_id=user_id, month_key=mk, free_quota=FREE_TIER_MONTHLY_AI)
        db.session.add(row)
        db.session.commit()
    return row

def get_remaining_monthly_prompts(user_id: int) -> Tuple[int, UsageLimits]:
    row = ensure_month_usage(user_id)
    remaining = max(0, (row.free_quota or 0) - (row.ai_prompt_count or 0))
    return remaining, row

def increment_monthly_prompts(user_id: int, n: int = 1) -> UsageLimits:
    row = ensure_month_usage(user_id)
    row.ai_prompt_count = (row.ai_prompt_count or 0) + max(1, n)
    db.session.commit()
    return row

def free_tier_limits() -> dict:
    return {
        "monthly_ai_prompts": FREE_TIER_MONTHLY_AI,
        "max_decks": FREE_TIER_MAX_DECKS,
    }

# ----- Compatibility layer for “trial” fields (if present) -----
def _has_user_trial_fields(user: User) -> bool:
    # flashlearn User doesn't have these; the freemium repo did.
    return all(
        hasattr(user, name)
        for name in ("trial_end_date", "subscription_status", "current_plan")
    )

def is_trial_active(user: User) -> bool:
    """
    In flashlearn, User lacks trial fields; this returns False.
    If those fields exist (from your other backend), we use them.
    """
    if not _has_user_trial_fields(user):
        return False
    try:
        return (
            getattr(user, "subscription_status", None) == "trial"
            and getattr(user, "trial_end_date", None) is not None
            and user.trial_end_date >= datetime.utcnow()
        )
    except Exception:
        return False

# ----- Effective plan calculation -----
def is_subscription_active(user_id: int) -> bool:
    """
    Use our new Subscription table (Phase 2) to decide active/inactive.
    """
    sub: Subscription | None = Subscription.query.filter_by(user_id=user_id).first()
    if not sub:
        return False
    now = datetime.utcnow()
    return sub.status == "active" and sub.end_date is not None and sub.end_date > now

def get_effective_plan_for_user(user: User) -> Plan:
    """
    Mirrors the old behavior:
      - If trial fields exist and trial is active -> premium
      - Else if subscription row is active -> premium
      - Else -> free
    """
    # Use trial semantics only when present on User (compat mode)
    if is_trial_active(user):
        return "premium"

    if is_subscription_active(user.id):
        return "premium"

    # If user has “current_plan/ subscription_status” fields (old model):
    if _has_user_trial_fields(user):
        if getattr(user, "current_plan", None) == "premium" and getattr(user, "subscription_status", None) == "active":
            return "premium"

    return "free"

# ----- Gate check used by AI generation -----
def can_generate_now(user: User) -> Tuple[bool, dict]:
    """
    Returns (allowed: bool, context: dict)
    Context includes month_key, used, remaining, and effective plan.
    If plan is 'premium', you can allow unlimited (or set higher caps later).
    """
    plan = get_effective_plan_for_user(user)

    # Premium? allow (we still return remaining as large number for UI)
    if plan == "premium":
        mk = month_key_now()
        row = ensure_month_usage(user.id, mk)
        return True, {
            "plan": plan,
            "month_key": mk,
            "used": row.ai_prompt_count or 0,
            "remaining": 10**9,  # effectively unlimited for UI, change if you add caps
            "free_quota": row.free_quota or FREE_TIER_MONTHLY_AI,
        }

    # Free plan -> enforce monthly quota
    remaining, row = get_remaining_monthly_prompts(user.id)
    return (remaining > 0), {
        "plan": plan,
        "month_key": row.month_key,
        "used": row.ai_prompt_count or 0,
        "remaining": remaining,
        "free_quota": row.free_quota or FREE_TIER_MONTHLY_AI,
    }
