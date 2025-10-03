# routes/admin/auth.py
"""
Authentication and authorization utilities for admin endpoints.
"""
from __future__ import annotations

import secrets
from flask import Request
from config import app


def is_admin_enabled() -> bool:
    """Check if admin endpoints are enabled in configuration."""
    return bool(app.config.get("ADMIN_ENDPOINTS_ENABLED", False))


def validate_admin_key(request: Request) -> bool:
    """
    Validate admin API key using constant-time comparison.
    
    SECURITY: Uses secrets.compare_digest to prevent timing attacks.
    """
    provided = request.headers.get("X-Admin-Key") or ""
    expected = app.config.get("ADMIN_API_KEY") or ""
    
    # Constant-time comparison prevents timing attacks
    return secrets.compare_digest(provided, expected)


def is_email_allowed(email: str) -> bool:
    """
    Check if email domain is in allowed list for admin operations.
    
    Returns True if:
    - No restrictions configured (empty list)
    - Wildcard "*" is in the list
    - Email's domain matches an allowed domain
    """
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return False
    
    domain = email.split("@", 1)[1]
    allowed = [d.strip().lower() for d in app.config.get("ADMIN_ALLOWED_EMAIL_DOMAINS", [])]
    
    # No restrictions or wildcard = allow all
    if not allowed or "*" in allowed:
        return True
    
    return domain in allowed


def get_admin_email_from_request(request: Request) -> str:
    """Extract admin email for audit logging."""
    return request.headers.get("X-Admin-Email", "admin-api-key-user")


def require_admin(request: Request) -> tuple[dict, int] | None:
    """
    Check admin authorization and return error response if unauthorized.
    
    Returns:
        None if authorized
        (error_dict, status_code) tuple if unauthorized
    """
    if not is_admin_enabled():
        return {
            "error": "forbidden",
            "message": "Admin endpoints are disabled"
        }, 403
    
    if not validate_admin_key(request):
        return {
            "error": "unauthorized", 
            "message": "Invalid or missing admin key"
        }, 401
    
    return None