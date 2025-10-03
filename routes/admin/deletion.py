# routes/admin/deletion.py
"""
Admin endpoint for direct user deletion by IDs.
"""
from __future__ import annotations

from flask import request
from flask_restful import Resource
from flask_limiter.util import get_remote_address

from config import app, db, limiter
from models import User
from .auth import require_admin, get_admin_email_from_request
from .utils import parse_bool_param, safe_error_message
from .deletion_core import execute_deletion_strategy
from .constants import (
    MAX_USER_IDS_PER_REQUEST,
    RATE_LIMIT_DELETE_BY_IDS,
)


class AdminDeleteUsersByIds(Resource):
    """
    Direct deletion by user IDs - most efficient for bulk operations.
    
    SECURITY: This is a destructive operation with no recovery.
    
    Body:
      {
        "user_ids": [1, 2, 3],
        "dry_run": false
      }
      
    Headers:
      X-Admin-Key: <ADMIN_API_KEY>
      X-Admin-Email: admin@company.com (optional)
      
    Query params:
      strategy: fast (default), slow, turbo
      commit_per: user (default), row (only for slow)
      sleep_ms: 0 (default, only for slow)
      echo_sql: false (default)
    """
    @limiter.limit(RATE_LIMIT_DELETE_BY_IDS, key_func=get_remote_address, override_defaults=False)
    def post(self):
        auth_error = require_admin(request)
        if auth_error:
            return auth_error

        admin_email = get_admin_email_from_request(request)
        payload = request.get_json(silent=True) or {}
        user_ids = payload.get("user_ids", [])
        dry_run = bool(payload.get("dry_run", False))

        if not isinstance(user_ids, list) or not user_ids:
            return {
                "error": "invalid_request",
                "message": "Provide 'user_ids' as a non-empty list"
            }, 400

        # SECURITY: Enforce batch size limit
        if len(user_ids) > MAX_USER_IDS_PER_REQUEST:
            return {
                "error": "batch_too_large",
                "message": f"Maximum {MAX_USER_IDS_PER_REQUEST} user IDs per request"
            }, 400

        # Validate and normalize IDs
        try:
            user_ids = [int(uid) for uid in user_ids]
        except (ValueError, TypeError):
            return {
                "error": "invalid_request",
                "message": "All user_ids must be integers"
            }, 400

        # Snapshot identities BEFORE deletion
        existing_rows = (
            db.session.query(User.id, User.username, User.email)
            .filter(User.id.in_(user_ids))
            .all()
        )
        
        found_ids = [row.id for row in existing_rows]
        missing_ids = sorted(set(user_ids) - set(found_ids))

        found_user_summaries = [{
            "id": r.id,
            "username": r.username,
            "email": r.email
        } for r in existing_rows]

        # Parse strategy parameters
        strategy = (request.args.get("strategy") or "fast").strip().lower()
        commit_per = (request.args.get("commit_per") or "user").strip().lower()
        try:
            sleep_ms = int(request.args.get("sleep_ms") or 0)
        except ValueError:
            sleep_ms = 0
        echo_sql = parse_bool_param(request.args.get("echo_sql")) or False

        if dry_run:
            return {
                "dry_run": True,
                "requested_ids": user_ids,
                "found_users": found_user_summaries,
                "missing_ids": missing_ids,
                "count_would_delete": len(found_ids),
                "strategy": strategy
            }, 200

        if not found_ids:
            return {
                "deleted": [],
                "missing_ids": missing_ids,
                "count_deleted": 0,
                "message": "No valid users found to delete"
            }, 200

        logger = app.logger
        logger.info(f"Admin {admin_email} deleting {len(found_ids)} users by ID")

        try:
            result = execute_deletion_strategy(
                found_ids,
                strategy=strategy,
                commit_per=commit_per,
                sleep_ms=sleep_ms,
                echo_sql=echo_sql
            )

            logger.info(f"Deleted {result.get('users')} users in {result['elapsed_s']}s via {result['mode']}")

            return {
                "success": True,
                "deleted_users": result.get("users", 0),
                "missing_ids": missing_ids,
                "related_records_deleted": {
                    "progress": result.get("progress"),
                    "flashcards": result.get("flashcards"),
                    "decks": result.get("decks")
                },
                "total_records_deleted": sum([
                    result.get("users") or 0,
                    result.get("progress") or 0,
                    result.get("flashcards") or 0,
                    result.get("decks") or 0,
                ]),
                "performance": result["mode"],
                "timing_seconds": result["elapsed_s"],
                "deleted_user_summaries": found_user_summaries
            }, 200

        except Exception as e:
            db.session.rollback()
            logger.error(f"User deletion by ID failed: {e}", exc_info=True)
            return {
                "error": "deletion_failed",
                "message": safe_error_message(e)
            }, 500