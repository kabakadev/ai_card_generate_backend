# routes/admin/utils.py
"""
Utility functions for admin operations.
"""
from __future__ import annotations

import random
import string
from datetime import datetime, timezone
from contextlib import contextmanager
from typing import Any


@contextmanager
def temp_echo_sql(engine, enabled: bool):
    """Temporarily toggle SQL echo for debugging."""
    if not hasattr(engine, "echo"):
        yield
        return
    
    old = engine.echo
    try:
        engine.echo = bool(enabled)
        yield
    finally:
        engine.echo = old


def iso_utc(dt: datetime | None) -> str | None:
    """
    Convert datetime to RFC3339 UTC string with trailing Z.
    
    FIXED: Always uses timezone-aware datetime.
    """
    if not dt:
        return None
    
    # Ensure timezone-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_now() -> datetime:
    """
    Get current UTC time as timezone-aware datetime.
    
    FIXED: Replaces datetime.utcnow() which returns naive datetime.
    """
    return datetime.now(timezone.utc)


def random_suffix(length: int = 6) -> str:
    """Generate random lowercase alphanumeric suffix."""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def generate_password(length: int = 12) -> str:
    """Generate random password with letters and digits."""
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=length))


def parse_bool_param(value: Any) -> bool | None:
    """
    Parse boolean from query parameter.
    
    Returns:
        True for: 1, "true", "yes", "on"
        False for: 0, "false", "no", "off"
        None for: invalid or None
    """
    if value is None:
        return None
    
    if isinstance(value, bool):
        return value
    
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    
    return None


def safe_error_message(exception: Exception, include_details: bool = False) -> str:
    """
    Generate safe error message for API responses.
    
    SECURITY: Never expose database internals in production.
    """
    from config import IS_LOCAL
    
    if include_details or IS_LOCAL:
        return str(exception)
    
    return "An internal error occurred. Please contact support."