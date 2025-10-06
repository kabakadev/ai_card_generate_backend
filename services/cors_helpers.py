# services/cors_helpers.py
"""CORS utilities for credentialed requests."""

import logging
from flask import request, current_app, Response

logger = logging.getLogger(__name__)


def get_allowed_origin_from_request() -> str | None:
    """
    Check if request origin is in allowed list.
    
    Returns:
        The allowed origin or None if not allowed
    """
    origin = (request.headers.get("Origin") or "").rstrip("/")
    
    if not origin:
        return None
    
    # Get allowed origins from config
    allowed = set()
    
    cors_allow = current_app.config.get("CORS_ALLOW_ORIGINS") or []
    frontend_origins = current_app.config.get("FRONTEND_ORIGINS") or []
    
    allowed.update(cors_allow)
    allowed.update(frontend_origins)
    
    # Check if origin matches any allowed origin
    for allowed_origin in allowed:
        if origin == allowed_origin.rstrip("/"):
            return origin
    
    logger.warning(
        f"Rejected origin: {origin}",
        extra={"origin": origin, "allowed": list(allowed)}
    )
    return None


def add_cors_headers(response: Response) -> Response:
    """
    Add CORS headers for credentialed requests.
    
    Args:
        response: Flask response object
        
    Returns:
        Response with CORS headers attached
    """
    origin = get_allowed_origin_from_request()
    
    # Only reflect specific allowed origins, never '*'
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
    
    response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    
    return response