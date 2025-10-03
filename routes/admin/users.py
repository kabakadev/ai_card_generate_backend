# routes/admin/users.py
"""
Admin endpoints for user management and monitoring.
"""
from __future__ import annotations

from datetime import timedelta
from flask import request
from flask_restful import Resource
from flask_limiter.util import get_remote_address
from sqlalchemy import func, and_, select, or_, asc, desc
from sqlalchemy.sql import true

from config import app, db, limiter
from models import User
from .auth import require_admin, get_admin_email_from_request
from .utils import iso_utc, utc_now, parse_bool_param, safe_error_message
from .deletion_core import execute_deletion_strategy
from .constants import (
    MAX_EMAILS_PER_REQUEST,
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    DEFAULT_LIST_OFFSET,
    MIN_ONLINE_THRESHOLD_MINUTES,
    MAX_ONLINE_THRESHOLD_MINUTES,
    DEFAULT_ONLINE_THRESHOLD_MINUTES,
    MAX_ONLINE_USERS_LIMIT,
    RATE_LIMIT_DELETE_USERS,
    RATE_LIMIT_LIST_USERS,
    RATE_LIMIT_ONLINE_USERS,
    RATE_LIMIT_USER_STATS,
)


class AdminDeleteUsers(Resource):
    """
    Delete users by email with enhanced performance and security.
    
    SECURITY IMPROVEMENTS:
    - Batch size limits prevent DoS
    - Constant-time admin key comparison
    - No database error details exposed to clients
    - Timezone-aware datetime handling
    
    Body:
      {
        "emails": ["a@company.com", "b@gmail.com"],  # string or list
        "dry_run": false
      }

    Headers:
      X-Admin-Key: <ADMIN_API_KEY>
      X-Admin-Email: admin@company.com (optional, for audit logging)
      
    Query params:
      strategy: fast (default), slow, turbo
      commit_per: user (default), row (only for slow)
      sleep_ms: 0 (default, only for slow)
      echo_sql: false (default)
    """
    @limiter.limit(RATE_LIMIT_DELETE_USERS, key_func=get_remote_address, override_defaults=False)
    def post(self):
        auth_error = require_admin(request)
        if auth_error:
            return auth_error

        admin_email = get_admin_email_from_request(request)
        payload = request.get_json(silent=True) or {}
        emails = payload.get("emails", [])
        dry_run = bool(payload.get("dry_run", False))

        # Normalize emails
        if isinstance(emails, str):
            emails = [emails]
        emails = [(e or "").strip().lower() for e in emails if e and isinstance(e, str)]

        if not emails:
            return {
                "error": "invalid_request",
                "message": "Provide 'emails' (string or list)."
            }, 400

        # SECURITY: Enforce batch size limit
        if len(emails) > MAX_EMAILS_PER_REQUEST:
            return {
                "error": "batch_too_large",
                "message": f"Maximum {MAX_EMAILS_PER_REQUEST} emails per request"
            }, 400

        # Check domain permissions
        from .auth import is_email_allowed
        allowed, skipped_domain = [], []
        for e in emails:
            if is_email_allowed(e):
                allowed.append(e)
            else:
                skipped_domain.append(e)

        # Fetch only what we need BEFORE deletion
        existing_rows = (
            db.session.query(User.id, User.email, User.username)
            .filter(User.email.in_(allowed))
            .all()
            if allowed else []
        )

        found_ids = [row.id for row in existing_rows]
        deletable_emails = [row.email for row in existing_rows]
        deletable_usernames = [row.username for row in existing_rows]
        existing_email_set = {row.email.lower() for row in existing_rows}
        not_found = [e for e in allowed if e not in existing_email_set]

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
                "requested": emails,
                "deletable": deletable_emails,
                "usernames_deletable": deletable_usernames,
                "not_found": not_found,
                "skipped_domain": skipped_domain,
                "count_would_delete": len(found_ids),
                "strategy": strategy,
                "commit_per": commit_per if strategy == "slow" else None,
                "sleep_ms": sleep_ms if strategy == "slow" else None,
            }, 200

        if not found_ids:
            return {
                "deleted": [],
                "not_found": not_found,
                "skipped_domain": skipped_domain,
                "count_deleted": 0,
                "message": "No valid users found to delete",
                "strategy": strategy
            }, 200

        logger = app.logger
        logger.info(f"Admin {admin_email} deleting {len(found_ids)} users: {deletable_usernames}")

        try:
            result = execute_deletion_strategy(
                found_ids,
                strategy=strategy,
                commit_per=commit_per,
                sleep_ms=sleep_ms,
                echo_sql=echo_sql
            )

            logger.info(f"Deleted users={result.get('users')} in {result['elapsed_s']}s via {result['mode']}")

            return {
                "deleted": deletable_emails,
                "deleted_usernames": deletable_usernames,
                "not_found": not_found,
                "skipped_domain": skipped_domain,
                "count_deleted": result.get("users", 0),
                "related_records_deleted": {
                    "progress": result.get("progress"),
                    "flashcards": result.get("flashcards"),
                    "decks": result.get("decks"),
                },
                "timing_seconds": result["elapsed_s"],
                "performance": result["mode"]
            }, 200

        except Exception as e:
            db.session.rollback()
            logger.error(f"User deletion failed: {e}", exc_info=True)
            return {
                "error": "deletion_failed",
                "message": safe_error_message(e)
            }, 500


class AdminListUsers(Resource):
    """
    List and search users with efficient pagination.
    
    Query params:
      q: Search query (username or email)
      is_demo: Filter by demo status (true/false)
      email_verified: Filter by verification status (true/false)
      active_within: Minutes since last activity
      limit: Results per page (1-200, default 50)
      offset: Skip N results (prefer keyset pagination)
      sort: created_at_desc (default), created_at_asc, last_seen_asc, last_seen_desc
      
    Keyset pagination (efficient for large offsets):
      after_created_at: ISO timestamp
      after_id: User ID
    """
    @limiter.limit(RATE_LIMIT_LIST_USERS, key_func=get_remote_address, override_defaults=False)
    def get(self):
        auth_error = require_admin(request)
        if auth_error:
            return auth_error

        # Parse inputs
        q = (request.args.get("q") or "").strip()
        is_demo = parse_bool_param(request.args.get("is_demo"))
        email_verified = parse_bool_param(request.args.get("email_verified"))
        active_within = request.args.get("active_within")
        
        limit = max(1, min(int(request.args.get("limit") or DEFAULT_LIST_LIMIT), MAX_LIST_LIMIT))
        offset = max(0, int(request.args.get("offset") or DEFAULT_LIST_OFFSET))
        sort = (request.args.get("sort") or "created_at_desc").strip().lower()

        # Keyset pagination params
        after_created_at = request.args.get("after_created_at")
        after_id = request.args.get("after_id")

        # Build filters
        conds = []

        if q:
            needle = f"%{q.lower()}%"
            conds.append(
                or_(
                    func.lower(User.email).ilike(needle),
                    func.lower(User.username).ilike(needle)
                )
            )

        if is_demo is not None:
            conds.append(User.is_demo.is_(true() if is_demo else False))

        if email_verified is not None:
            conds.append(User.email_verified.is_(true() if email_verified else False))

        if active_within:
            try:
                mins = max(1, int(active_within))
                threshold = utc_now() - timedelta(minutes=mins)
                conds.append(
                    and_(
                        User.last_seen_at.isnot(None),
                        User.last_seen_at >= threshold
                    )
                )
            except ValueError:
                pass

        from sqlalchemy.sql import literal
        where_clause = and_(*conds) if conds else literal(True)

        # Sorting
        if sort == "created_at_asc":
            order_clause = asc(User.created_at)
        elif sort == "last_seen_asc":
            order_clause = asc(User.last_seen_at).nulls_last()
        elif sort == "last_seen_desc":
            order_clause = desc(User.last_seen_at).nulls_last()
        else:
            order_clause = desc(User.created_at)

        # COUNT without ordering for performance
        count_stmt = select(func.count(User.id)).where(where_clause)
        total = db.session.execute(count_stmt).scalar_one()

        # Keyset pagination (efficient seek)
        keyset_cond = None
        if after_created_at and after_id and sort.startswith("created_at"):
            try:
                from datetime import datetime
                after_dt = datetime.fromisoformat(after_created_at.replace("Z", ""))
                
                if sort == "created_at_desc":
                    keyset_cond = or_(
                        User.created_at < after_dt,
                        and_(User.created_at == after_dt, User.id < int(after_id)),
                    )
                elif sort == "created_at_asc":
                    keyset_cond = or_(
                        User.created_at > after_dt,
                        and_(User.created_at == after_dt, User.id > int(after_id)),
                    )
            except (ValueError, TypeError):
                pass

        page_where = and_(where_clause, keyset_cond) if keyset_cond is not None else where_clause

        # Page query with only needed columns
        cols = (
            User.id,
            User.email,
            User.username,
            User.is_demo,
            User.email_verified,
            User.created_at,
            User.last_seen_at,
            User.demo_expires_at,
        )

        page_stmt = (
            select(*cols)
            .where(page_where)
            .order_by(order_clause, desc(User.id) if "desc" in sort else asc(User.id))
            .limit(limit)
        )

        if keyset_cond is None and offset:
            page_stmt = page_stmt.offset(offset)

        rows = db.session.execute(page_stmt).all()

        items = [{
            "id": r.id,
            "email": r.email,
            "username": r.username,
            "is_demo": bool(r.is_demo),
            "email_verified": bool(r.email_verified),
            "created_at": iso_utc(r.created_at),
            "last_seen": iso_utc(r.last_seen_at),
            "demo_expires_at": iso_utc(r.demo_expires_at),
        } for r in rows]

        # Next page cursor for keyset pagination
        next_cursor = None
        if rows and sort.startswith("created_at"):
            last = rows[-1]
            next_cursor = {
                "after_created_at": iso_utc(last.created_at),
                "after_id": last.id
            }

        return {
            "total": int(total),
            "limit": limit,
            "offset": offset if keyset_cond is None else None,
            "sort": sort,
            "items": items,
            "next_cursor": next_cursor,
        }, 200


class AdminOnlineUsers(Resource):
    """
    Get currently online users.
    
    Query params:
      within: Minutes (1-120, default 5)
      limit: Max results (1-1000, default 200)
    """
    @limiter.limit(RATE_LIMIT_ONLINE_USERS, key_func=get_remote_address, override_defaults=False)
    def get(self):
        auth_error = require_admin(request)
        if auth_error:
            return auth_error

        within = int(request.args.get("within") or DEFAULT_ONLINE_THRESHOLD_MINUTES)
        within = max(MIN_ONLINE_THRESHOLD_MINUTES, min(within, MAX_ONLINE_THRESHOLD_MINUTES))
        
        limit = int(request.args.get("limit") or 200)
        limit = max(1, min(limit, MAX_ONLINE_USERS_LIMIT))
        
        threshold = utc_now() - timedelta(minutes=within)

        cols = (User.id, User.email, User.username, User.is_demo, User.email_verified, User.last_seen_at)
        stmt = (
            select(*cols)
            .where(User.last_seen_at.isnot(None), User.last_seen_at >= threshold)
            .order_by(User.last_seen_at.desc().nulls_last())
            .limit(limit)
        )
        rows = db.session.execute(stmt).all()

        items = [{
            "id": r.id,
            "email": r.email,
            "username": r.username,
            "is_demo": bool(r.is_demo),
            "email_verified": bool(r.email_verified),
            "last_seen": iso_utc(r.last_seen_at),
        } for r in rows]

        # Fast count for total in window
        count_stmt = select(func.count(User.id)).where(
            User.last_seen_at.isnot(None),
            User.last_seen_at >= threshold
        )
        total_in_window = db.session.execute(count_stmt).scalar_one()

        return {
            "within_minutes": within,
            "count": len(items),
            "total_in_window": int(total_in_window),
            "items": items,
        }, 200


class AdminUserStats(Resource):
    """
    Aggregate user statistics for admin dashboard.
    
    Query params:
      within: Minutes for "online now" window (default 5)
    """
    @limiter.limit(RATE_LIMIT_USER_STATS, key_func=get_remote_address, override_defaults=False)
    def get(self):
        auth_error = require_admin(request)
        if auth_error:
            return auth_error

        now = utc_now()
        within = int(request.args.get("within") or DEFAULT_ONLINE_THRESHOLD_MINUTES)
        within = max(MIN_ONLINE_THRESHOLD_MINUTES, min(within, MAX_ONLINE_THRESHOLD_MINUTES))
        
        cutoff_online = now - timedelta(minutes=within)
        cutoff_24h = now - timedelta(hours=24)

        total = db.session.query(func.count(User.id)).scalar() or 0
        verified = db.session.query(func.count(User.id)).filter(User.email_verified == True).scalar() or 0
        demo = db.session.query(func.count(User.id)).filter(User.is_demo == True).scalar() or 0
        real = total - demo

        online_now = db.session.query(func.count(User.id)).filter(
            User.last_seen_at.isnot(None),
            User.last_seen_at >= cutoff_online
        ).scalar() or 0

        active_24h = db.session.query(func.count(User.id)).filter(
            User.last_seen_at.isnot(None),
            User.last_seen_at >= cutoff_24h
        ).scalar() or 0

        new_24h = db.session.query(func.count(User.id)).filter(
            User.created_at.isnot(None),
            User.created_at >= cutoff_24h
        ).scalar() or 0

        return {
            "as_of": iso_utc(now),
            "within_minutes": within,
            "total_users": total,
            "verified_users": verified,
            "demo_users": demo,
            "real_users": real,
            "online_now": online_now,
            "active_last_24h": active_24h,
            "new_last_24h": new_24h
        }, 200