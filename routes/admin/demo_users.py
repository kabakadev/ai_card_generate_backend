# routes/admin/demo_users.py
"""
Admin endpoints for demo user creation and management.
"""
from __future__ import annotations

import random
import string
from datetime import timedelta
from flask import request
from flask_restful import Resource
from flask_limiter.util import get_remote_address

from config import app, db, limiter, bcrypt
from models import User
from .auth import require_admin
from .utils import utc_now, safe_error_message
from .constants import (
    MIN_USERNAME_LENGTH,
    MAX_USERNAME_LENGTH,
    MAX_DEMO_USERS_PER_REQUEST,
    MAX_USERNAMES_PER_CHECK,
    DEFAULT_DEMO_PREFIX,
    DEFAULT_DEMO_DOMAIN,
    DEFAULT_DEMO_PASSWORD_LENGTH,
    DEMO_USERNAME_SUFFIX_LENGTH,
    RATE_LIMIT_CHECK_USERNAMES,
    RATE_LIMIT_CREATE_DEMO,
)


class AdminCheckUsernames(Resource):
    """
    Check which usernames exist in the database.
    
    Body:
      { "usernames": ["alice", "bob"] }  # string or list

    Headers:
      X-Admin-Key: <ADMIN_API_KEY>
    """
    @limiter.limit(RATE_LIMIT_CHECK_USERNAMES, key_func=get_remote_address, override_defaults=False)
    def post(self):
        auth_error = require_admin(request)
        if auth_error:
            return auth_error

        payload = request.get_json(silent=True) or {}
        usernames = payload.get("usernames", [])
        
        if isinstance(usernames, str):
            usernames = [usernames]
        usernames = [u.strip() for u in usernames if u and isinstance(u, str)]

        if not usernames:
            return {
                "error": "invalid_request",
                "message": "Provide 'usernames' (string or list)."
            }, 400

        # SECURITY: Enforce batch size limit
        if len(usernames) > MAX_USERNAMES_PER_CHECK:
            return {
                "error": "batch_too_large",
                "message": f"Maximum {MAX_USERNAMES_PER_CHECK} usernames per request"
            }, 400

        existing = db.session.query(User.username).filter(User.username.in_(usernames)).all()
        existing_set = {row[0] for row in existing}

        exists = [u for u in usernames if u in existing_set]
        not_found = [u for u in usernames if u not in existing_set]

        return {
            "exists": exists,
            "not_found": not_found
        }, 200


class AdminCreateDemoUsers(Resource):
    """
    Bulk create demo/student users that skip OTP verification.
    
    SECURITY: Demo users should only be used in controlled environments.
    
    Headers:
      X-Admin-Key: <ADMIN_API_KEY>

    Body:
      {
        "count": 25,                       # OR provide explicit "usernames": ["alice","bob"]
        "prefix": "class9",
        "email_domain": "demo.flashlearn.local",
        "password": null,                  # optional; if provided we hash ONCE and reuse
        "expires_in_days": 90              # optional: sets demo_expires_at
      }

    Returns:
      { "users": [{ "username","email","password" }], "count": N }
    """
    @limiter.limit(RATE_LIMIT_CREATE_DEMO, key_func=get_remote_address, override_defaults=False)
    def post(self):
        auth_error = require_admin(request)
        if auth_error:
            return auth_error

        data = request.get_json(silent=True) or {}
        usernames_input = data.get("usernames") or []
        count = int(data.get("count") or 0)
        prefix = (data.get("prefix") or DEFAULT_DEMO_PREFIX).strip().lower()
        email_domain = (data.get("email_domain") or DEFAULT_DEMO_DOMAIN).strip().lower()
        static_password = data.get("password")
        expires_in_days = data.get("expires_in_days")
        
        demo_expires_at = None
        if expires_in_days:
            demo_expires_at = utc_now() + timedelta(days=int(expires_in_days))

        # Validate input
        if usernames_input and not isinstance(usernames_input, list):
            return {
                "error": "invalid_request",
                "message": "'usernames' must be a list"
            }, 400
            
        if not usernames_input and count <= 0:
            return {
                "error": "invalid_request",
                "message": "Provide 'usernames' or a positive 'count'."
            }, 400

        # Generate base usernames if only count provided
        if not usernames_input:
            usernames_input = [
                f"{prefix}-{''.join(random.choices(string.ascii_lowercase + string.digits, k=DEMO_USERNAME_SUFFIX_LENGTH))}"
                for _ in range(count)
            ]

        # SECURITY: Enforce batch size limit
        if len(usernames_input) > MAX_DEMO_USERS_PER_REQUEST:
            return {
                "error": "batch_too_large",
                "message": f"Maximum {MAX_DEMO_USERS_PER_REQUEST} demo users per request"
            }, 400

        # Prefetch existing usernames/emails in ONE query
        candidate_emails = [f"{u}@{email_domain}" for u in usernames_input]
        
        existing_rows = db.session.query(User.username, User.email).filter(
            (User.username.in_(usernames_input)) | (User.email.in_(candidate_emails))
        ).all()
        
        existing_usernames = {r[0] for r in existing_rows}
        existing_emails = {r[1] for r in existing_rows}

        # Track used names to avoid duplicates within batch
        used_usernames = set(existing_usernames)
        used_emails = set(existing_emails)

        # Prepare rows in memory
        rows = []
        api_return = []

        # If fixed password provided, hash ONCE and reuse (bcrypt is slow)
        hashed_once = None
        if static_password:
            hashed_once = bcrypt.generate_password_hash(static_password).decode("utf-8")

        for base in usernames_input:
            u = (base or "").strip().lower()
            
            # Validate username length
            if not (MIN_USERNAME_LENGTH <= len(u) <= MAX_USERNAME_LENGTH):
                return {
                    "error": "invalid_username",
                    "message": f"Username '{base}' must be {MIN_USERNAME_LENGTH}-{MAX_USERNAME_LENGTH} chars"
                }, 400

            # Ensure unique username locally
            if u in used_usernames:
                while True:
                    cand = f"{u}-{''.join(random.choices(string.ascii_lowercase + string.digits, k=4))}"
                    if cand not in used_usernames:
                        u = cand
                        break
            used_usernames.add(u)

            # Ensure unique email locally
            email = f"{u}@{email_domain}"
            if email in used_emails:
                while True:
                    cand_u = f"{u}-{''.join(random.choices(string.ascii_lowercase + string.digits, k=5))}"
                    cand_email = f"{cand_u}@{email_domain}"
                    if cand_email not in used_emails:
                        u = cand_u
                        email = cand_email
                        used_usernames.add(u)
                        break
            used_emails.add(email)

            # Handle password
            if static_password:
                pw_plain = static_password
                pw_hash = hashed_once
            else:
                pw_plain = "".join(random.choices(
                    string.ascii_letters + string.digits,
                    k=DEFAULT_DEMO_PASSWORD_LENGTH
                ))
                pw_hash = bcrypt.generate_password_hash(pw_plain).decode("utf-8")

            rows.append({
                "username": u,
                "email": email,
                "is_demo": True,
                "email_verified": True,
                "email_verified_at": utc_now(),
                "demo_expires_at": demo_expires_at,
                "password_hash": pw_hash,
            })
            
            api_return.append({
                "username": u,
                "email": email,
                "password": pw_plain
            })

        # BULK INSERT in one transaction
        try:
            db.session.execute(User.__table__.insert(), rows)
            db.session.commit()
            
            app.logger.info(f"Created {len(rows)} demo users with prefix '{prefix}'")
            
            return {
                "users": api_return,
                "count": len(api_return)
            }, 201
            
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Demo user creation failed: {e}", exc_info=True)
            return {
                "error": "creation_failed",
                "message": safe_error_message(e)
            }, 500