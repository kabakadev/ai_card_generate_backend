# routes/admin_routes.py
from __future__ import annotations

from datetime import datetime, timedelta
import random
import string

from flask import request
from flask_restful import Resource
from flask_limiter.util import get_remote_address

from sqlalchemy import func, and_, select, delete  # 2.x style core ops

from config import app, db, limiter, bcrypt  # <- import bcrypt so we can hash once when needed
from sqlalchemy.dialects.postgresql import insert as pg_insert  # optional if you later want ON CONFLICT
from models import User, Deck, Progress, Flashcard
from contextlib import contextmanager
import time
from sqlalchemy.orm import joinedload,aliased
from sqlalchemy.sql import true
from sqlalchemy import select, func, and_, or_, literal, desc, asc



# ---------------- helpers ----------------
@contextmanager
def _temp_echo_sql(engine, enabled: bool):
    """Temporarily toggle SQL echo just for this block."""
    if not hasattr(engine, "echo"):
        yield
        return
    old = engine.echo
    try:
        engine.echo = bool(enabled)
        yield
    finally:
        engine.echo = old

def _admin_enabled() -> bool:
    return bool(app.config.get("ADMIN_ENDPOINTS_ENABLED", False))

def _valid_admin_key(req) -> bool:
    return (req.headers.get("X-Admin-Key") or "") == app.config.get("ADMIN_API_KEY")

def _allowed_email(email: str) -> bool:
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return False
    domain = email.split("@", 1)[1]
    allowed = [d.strip().lower() for d in app.config.get("ADMIN_ALLOWED_EMAIL_DOMAINS", [])]
    if not allowed or "*" in allowed:
        return True  # wildcard / empty = allow all
    return domain in allowed

def _get_admin_email_from_request(req) -> str:
    """Extract admin email for audit logging."""
    return req.headers.get("X-Admin-Email", "admin-api-key-user")

def _rand_suffix(n: int = 6) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))

def _gen_password(n: int = 12) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=n))

def _bool_param(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return None

# ---- internal bulk delete helper (used by both endpoints) ----
def _bulk_delete_users_by_ids(found_ids: list[int]) -> dict:
    """
    Performs fast, explicit deletes of child tables (for performance),
    then deletes Users. One transaction; returns row counts.
    """
    # 1) Delete progress owned by these users
    res1 = db.session.execute(
        delete(Progress).where(Progress.user_id.in_(found_ids))
    )
    progress_deleted = res1.rowcount or 0

    # 2) Delete flashcards of these users via their decks
    deck_ids_sel = select(Deck.id).where(Deck.user_id.in_(found_ids))
    res2 = db.session.execute(
        delete(Flashcard).where(Flashcard.deck_id.in_(deck_ids_sel))
    )
    flashcards_deleted = res2.rowcount or 0

    # 3) Delete decks
    res3 = db.session.execute(
        delete(Deck).where(Deck.user_id.in_(found_ids))
    )
    decks_deleted = res3.rowcount or 0

    # 4) Finally delete users (DB will cascade anything else that has FK ondelete=CASCADE)
    res4 = db.session.execute(
        delete(User).where(User.id.in_(found_ids))
    )
    users_deleted = res4.rowcount or 0

    return {
        "progress": progress_deleted,
        "flashcards": flashcards_deleted,
        "decks": decks_deleted,
        "users": users_deleted,
    }

def _delete_users_slow(found_ids: list[int], commit_per: str = "user", sleep_ms: int = 0) -> dict:
    """
    INTENTIONALLY SLOW: ORM loops + frequent commits + optional sleeps.
    commit_per: "user" (commit after each user) or "row" (even worse: after each row)
    """
    t0 = time.perf_counter()
    total = {"progress": 0, "flashcards": 0, "decks": 0, "users": 0}

    # Load full user objects and their children lazily (and produce N+1 on purpose).
    users = (
        db.session.query(User)
        .filter(User.id.in_(found_ids))
        # joinedload here would make it *less* slow; keep default lazy loads to show chatter
        .all()
    )

    for u in users:
        # 1) delete progress one-by-one
        for p in db.session.query(Progress).filter_by(user_id=u.id).all():
            db.session.delete(p); total["progress"] += 1
            if commit_per == "row":
                db.session.commit()
                if sleep_ms: time.sleep(sleep_ms / 1000.0)

        # 2) delete flashcards by traversing decks; still one-by-one
        for d in db.session.query(Deck).filter_by(user_id=u.id).all():
            for c in db.session.query(Flashcard).filter_by(deck_id=d.id).all():
                db.session.delete(c); total["flashcards"] += 1
                if commit_per == "row":
                    db.session.commit()
                    if sleep_ms: time.sleep(sleep_ms / 1000.0)
            db.session.delete(d); total["decks"] += 1
            if commit_per == "row":
                db.session.commit()
                if sleep_ms: time.sleep(sleep_ms / 1000.0)

        # 3) finally delete the user
        db.session.delete(u); total["users"] += 1

        if commit_per == "user":
            db.session.commit()
            if sleep_ms: time.sleep(sleep_ms / 1000.0)
        # ✅ add this block
        if commit_per == "row":
            db.session.commit()
            if sleep_ms: time.sleep(sleep_ms / 1000.0)

    if commit_per not in {"user", "row"}:
        # one big commit at end (still slow due to ORM loops), if caller misconfigures
        db.session.commit()

    total["elapsed_s"] = round(time.perf_counter() - t0, 3)
    total["mode"] = f"slow(commit_per={commit_per}, sleep_ms={sleep_ms})"
    return total


def _delete_users_turbo(found_ids: list[int]) -> dict:
    """
    TURBO: delete only from users and let FK ON DELETE CASCADE clear children.
    """
    t0 = time.perf_counter()
    res = db.session.execute(delete(User).where(User.id.in_(found_ids)))
    db.session.commit()
    return {
        "progress": None, "flashcards": None, "decks": None,
        "users": res.rowcount or 0,
        "elapsed_s": round(time.perf_counter() - t0, 3),
        "mode": "turbo(fk_cascade)"
    }


def _delete_users_fast(found_ids: list[int]) -> dict:
    """
    FAST: call your existing set-based helper and add timing.
    """
    t0 = time.perf_counter()
    counts = _bulk_delete_users_by_ids(found_ids)  # your current optimized path
    db.session.commit()
    counts["elapsed_s"] = round(time.perf_counter() - t0, 3)
    counts["mode"] = "fast(set_based)"
    return counts

# ---------------- endpoints ----------------
class AdminDeleteUsers(Resource):
    """
    OPTIMIZED: Delete users by email with enhanced performance.

    Body:
      {
        "emails": ["a@company.com", "b@gmail.com"],  # string or list
        "dry_run": false
      }

    Headers:
      X-Admin-Key: <ADMIN_API_KEY>
      X-Admin-Email: admin@company.com (optional, for audit logging)
    """
    @limiter.limit("300 per minute", key_func=get_remote_address, override_defaults=False)
    def post(self):
        if not _admin_enabled():
            return {"error": "forbidden", "message": "Admin endpoints disabled"}, 403
        if not _valid_admin_key(request):
            return {"error": "unauthorized", "message": "Invalid admin key"}, 401

        admin_email = _get_admin_email_from_request(request)
        payload = request.get_json(silent=True) or {}
        emails = payload.get("emails", [])
        dry_run = bool(payload.get("dry_run", False))

        if isinstance(emails, str):
            emails = [emails]
        emails = [(e or "").strip().lower() for e in emails if e and isinstance(e, str)]

        if not emails:
            return {"error": "invalid_request", "message": "Provide 'emails' (string or list)."}, 400

        # Check domain permissions
        allowed, skipped_domain = [], []
        for e in emails:
            if _allowed_email(e):
                allowed.append(e)
            else:
                skipped_domain.append(e)

        # Fetch only what we need and snapshot it BEFORE deletion
        # (Never touch ORM objects after bulk deletes!)
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
        strategy = (request.args.get("strategy") or "fast").strip().lower()
        commit_per = (request.args.get("commit_per") or "user").strip().lower()
        
        try:
            sleep_ms = int(request.args.get("sleep_ms") or 0)
        except ValueError:
            sleep_ms = 0
        echo_sql = (request.args.get("echo_sql") or "").strip().lower() in {"1","true","yes","on"}


        if dry_run:
            return {
                "dry_run": True,
                "requested": emails,
                "deletable": deletable_emails,
                "usernames_deletable": deletable_usernames,
                "not_found": not_found,
                "skipped_domain": skipped_domain,
                "count_would_delete": len(found_ids),
                "strategy":strategy,
                "commit_per":commit_per if strategy == "slow" else None,
                "sleep_ms":sleep_ms if strategy == "slow" else None,
            }, 200

        if not found_ids:
            return {
                "deleted": [],
                "not_found": not_found,
                "skipped_domain": skipped_domain,
                "count_deleted": 0,
                "message": "No valid users found to delete",
                "strategy":strategy
            }, 200

        logger = app.logger
        logger.info(f"Admin {admin_email} deleting {len(found_ids)} users: {deletable_usernames}")

        try:
            with _temp_echo_sql(db.engine, echo_sql):
                if strategy == "slow":
                    result = _delete_users_slow(found_ids, commit_per=commit_per, sleep_ms=sleep_ms)
                elif strategy == "turbo":
                    result = _delete_users_turbo(found_ids)
                else:
                    result = _delete_users_fast(found_ids)

            logger.info(f"Deleted users={result.get('users')} in {result['elapsed_s']}s via {result['mode']}")

            return {
                "deleted": deletable_emails,           # snapshot taken pre-delete
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
            app.logger.error(f"Error during bulk user deletion: {e}")
            return {
                "error": "deletion_failed",
                "message": f"Database error: {str(e)}"
            }, 500


class AdminDeleteUsersByIds(Resource):
    """
    NEW: Direct deletion by user IDs (most efficient for bulk operations).

    Body:
      {
        "user_ids": [1, 2, 3],
        "dry_run": false
      }
    """
    @limiter.limit("500 per minute", key_func=get_remote_address, override_defaults=False)
    def post(self):
        if not _admin_enabled():
            return {"error": "forbidden", "message": "Admin endpoints disabled"}, 403
        if not _valid_admin_key(request):
            return {"error": "unauthorized", "message": "Invalid admin key"}, 401

        admin_email = _get_admin_email_from_request(request)
        payload = request.get_json(silent=True) or {}
        user_ids = payload.get("user_ids", [])
        dry_run = bool(payload.get("dry_run", False))

        if not isinstance(user_ids, list) or not user_ids:
            return {"error": "invalid_request", "message": "Provide 'user_ids' as a non-empty list"}, 400

        # Validate and normalize IDs to ints
        try:
            user_ids = [int(uid) for uid in user_ids]
        except (ValueError, TypeError):
            return {"error": "invalid_request", "message": "All user_ids must be integers"}, 400

        # Snapshot the identities BEFORE deletion
        existing_rows = (
            db.session.query(User.id, User.username, User.email)
            .filter(User.id.in_(user_ids))
            .all()
        )
        found_ids = [row.id for row in existing_rows]
        missing_ids = sorted(set(user_ids) - set(found_ids))

        found_user_summaries = [
            {"id": r.id, "username": r.username, "email": r.email}
            for r in existing_rows
        ]

        strategy = (request.args.get("strategy") or "fast").strip().lower()
        commit_per = (request.args.get("commit_per") or "user").strip().lower()
        try:
            sleep_ms = int(request.args.get("sleep_ms") or 0)
        except ValueError:
            sleep_ms = 0
        echo_sql = (request.args.get("echo_sql") or "").strip().lower() in {"1","true","yes","on"}

        if dry_run:
            return {
                "dry_run": True,
                "requested_ids": user_ids,
                "found_users": found_user_summaries,
                "missing_ids": missing_ids,
                "count_would_delete": len(found_ids)
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
            with _temp_echo_sql(db.engine, echo_sql):
                if strategy == "slow":
                    result = _delete_users_slow(found_ids, commit_per=commit_per, sleep_ms=sleep_ms)
                elif strategy == "turbo":
                    result = _delete_users_turbo(found_ids)
                else:
                    result = _delete_users_fast(found_ids)

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
            app.logger.error(f"Error during bulk user deletion by ID: {e}")
            return {"error": "deletion_failed", "message": f"Database error: {str(e)}"}, 500


class AdminCheckUsernames(Resource):
    """
    DEV-ONLY: Check which usernames exist.

    Body:
      { "usernames": ["alice", "bob"] }  # string or list

    Headers:
      X-Admin-Key: <ADMIN_API_KEY>
    """
    @limiter.limit("30 per minute", key_func=get_remote_address, override_defaults=False)
    def post(self):
        if not _admin_enabled():
            return {"error": "forbidden", "message": "Admin endpoints disabled"}, 403
        if not _valid_admin_key(request):
            return {"error": "unauthorized", "message": "Invalid admin key"}, 401

        payload = request.get_json(silent=True) or {}
        usernames = payload.get("usernames", [])
        if isinstance(usernames, str):
            usernames = [usernames]
        usernames = [u.strip() for u in usernames if u and isinstance(u, str)]

        if not usernames:
            return {"error": "invalid_request", "message": "Provide 'usernames' (string or list)."}, 400

        existing = db.session.query(User.username).filter(User.username.in_(usernames)).all()
        existing_set = {row[0] for row in existing}

        exists = [u for u in usernames if u in existing_set]
        not_found = [u for u in usernames if u not in existing_set]

        return {"exists": exists, "not_found": not_found}, 200


class AdminCreateDemoUsers(Resource):
    """
    DEV/ADMIN: bulk create demo/student users that skip OTP.

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
    @limiter.limit("10 per minute", key_func=get_remote_address, override_defaults=False)  # was 2; raise for demo
    def post(self):
        if not _admin_enabled():
            return {"error": "forbidden", "message": "Admin endpoints disabled"}, 403
        if not _valid_admin_key(request):
            return {"error": "unauthorized", "message": "Invalid admin key"}, 401

        data = request.get_json(silent=True) or {}
        usernames_input = data.get("usernames") or []
        count = int(data.get("count") or 0)
        prefix = (data.get("prefix") or "demo").strip().lower()
        email_domain = (data.get("email_domain") or "demo.flashlearn.local").strip().lower()
        static_password = data.get("password")
        expires_in_days = data.get("expires_in_days")
        demo_expires_at = None
        if expires_in_days:
            demo_expires_at = datetime.utcnow() + timedelta(days=int(expires_in_days))

        # Build base usernames if only count is provided
        if usernames_input and not isinstance(usernames_input, list):
            return {"error": "invalid_request", "message": "'usernames' must be a list"}, 400
        if not usernames_input and count <= 0:
            return {"error": "invalid_request", "message": "Provide 'usernames' or a positive 'count'."}, 400
        if not usernames_input:
            usernames_input = [f"{prefix}-{''.join(random.choices(string.ascii_lowercase+string.digits, k=6))}"
                               for _ in range(count)]

        # --- 1) Prefetch existing usernames/emails in ONE query
        # Build the candidate emails from candidate usernames (before we mutate them)
        candidate_emails = [f"{u}@{email_domain}" for u in usernames_input]

        existing_rows = db.session.query(User.username, User.email).filter(
            (User.username.in_(usernames_input)) | (User.email.in_(candidate_emails))
        ).all()
        existing_usernames = {r[0] for r in existing_rows}
        existing_emails = {r[1] for r in existing_rows}

        # We’ll also track usernames/emails we generate in this batch to avoid duplicates *within* the batch.
        used_usernames = set(existing_usernames)
        used_emails = set(existing_emails)

        # --- 2) Prepare rows in memory, resolving collisions without extra DB hits
        rows = []
        api_return = []  # for returning plaintext creds for demo accounts

        # If a fixed password is provided, hash ONCE and reuse (bcrypt is slow by design)
        hashed_once = None
        if static_password:
            hashed_once = bcrypt.generate_password_hash(static_password).decode("utf-8")

        for base in usernames_input:
            u = (base or "").strip().lower()
            if not (3 <= len(u) <= 50):
                return {"error": "invalid_username", "message": f"Bad username: {base}"}, 400

            # ensure unique username locally
            if u in used_usernames:
                # add a short random suffix until it's unique (no DB query)
                while True:
                    cand = f"{u}-{''.join(random.choices(string.ascii_lowercase+string.digits, k=4))}"
                    if cand not in used_usernames:
                        u = cand
                        break
            used_usernames.add(u)

            email = f"{u}@{email_domain}"
            # ensure unique email locally
            if email in used_emails:
                while True:
                    cand_u = f"{u}-{''.join(random.choices(string.ascii_lowercase+string.digits, k=5))}"
                    cand_email = f"{cand_u}@{email_domain}"
                    if cand_email not in used_emails:
                        u = cand_u
                        email = cand_email
                        used_usernames.add(u)
                        break
            used_emails.add(email)

            # password handling:
            # password handling:
            if static_password:
                pw_plain = static_password
                # hash ONCE and reuse (already computed above)
                pw_hash = hashed_once
            else:
                # per-user random demo password
                pw_plain = "".join(random.choices(string.ascii_letters + string.digits, k=12))
                # hash each one (bcrypt is slower, but demo batch sizes are usually fine)
                pw_hash = bcrypt.generate_password_hash(pw_plain).decode("utf-8")

            rows.append({
                "username": u,
                "email": email,
                "is_demo": True,
                "email_verified": True,           # bypass OTP for demo users
                "email_verified_at": datetime.utcnow(),
                "demo_expires_at": demo_expires_at,
                "password_hash": pw_hash,
                # include any server_default columns if needed (created_at via DB default is fine)
            })
            api_return.append({"username": u, "email": email, "password": pw_plain})

        # --- 3) BULK INSERT in one go (Core is fastest)
        # Works across both SQLite and Postgres.
        db.session.execute(User.__table__.insert(), rows)
        db.session.commit()

        return {"users": api_return, "count": len(api_return)}, 201

class AdminListUsers(Resource):
    @limiter.limit("30 per minute", key_func=get_remote_address, override_defaults=False)
    def get(self):
        if not _admin_enabled():
            return {"error":"forbidden","message":"Admin endpoints disabled"}, 403
        if not _valid_admin_key(request):
            return {"error":"unauthorized","message":"Invalid admin key"}, 401

        # --- inputs
        q = (request.args.get("q") or "").strip()
        is_demo = _bool_param(request.args.get("is_demo"))
        email_verified = _bool_param(request.args.get("email_verified"))
        active_within = request.args.get("active_within")
        limit = max(1, min(int(request.args.get("limit") or 50), 200))
        offset = max(0, int(request.args.get("offset") or 0))
        sort = (request.args.get("sort") or "created_at_desc").strip().lower()

        # Optional keyset params (prefer over big offsets)
        after_created_at = request.args.get("after_created_at")  # ISO string
        after_id = request.args.get("after_id")  # int

        # --- filters
        conds = []

        if q:
            needle = f"%{q.lower()}%"
            # These will use trigram GIN indexes we created on lower(email/username)
            conds.append(
                or_(func.lower(User.email).ilike(needle),
                    func.lower(User.username).ilike(needle))
            )

        if is_demo is not None:
            conds.append(User.is_demo.is_(true() if is_demo else False))

        if email_verified is not None:
            conds.append(User.email_verified.is_(true() if email_verified else False))

        if active_within:
            try:
                mins = max(1, int(active_within))
                threshold = datetime.utcnow() - timedelta(minutes=mins)
                conds.append(and_(User.last_seen_at.isnot(None),
                                  User.last_seen_at >= threshold))
            except Exception:
                pass

        where_clause = and_(*conds) if conds else literal(True)

        # --- sorting
        if sort == "created_at_asc":
            order_clause = asc(User.created_at)
        elif sort == "last_seen_asc":
            order_clause = asc(User.last_seen_at).nulls_last()
        elif sort == "last_seen_desc":
            order_clause = desc(User.last_seen_at).nulls_last()
        else:
            order_clause = desc(User.created_at)

        # --- COUNT: run without ORDER/LIMIT for speed
        count_stmt = select(func.count()).select_from(
            select(User.id).where(where_clause).subquery()
        )
        total = db.session.execute(count_stmt).scalar_one()

        # --- KEYSET (seek) pagination (optional, takes precedence if provided)
        keyset_cond = None
        if after_created_at and after_id and sort.startswith("created_at"):
            try:
                # compare tuples for stable seek (created_at DESC, id DESC)
                # when DESC: (created_at, id) < (after_created_at, after_id)
                # when ASC:  (created_at, id) > (after_created_at, after_id)
                from dateutil.parser import isoparse  # if available
                after_dt = datetime.fromisoformat(after_created_at.replace("Z","")) if "dateutil" not in globals() else isoparse(after_created_at)
            except Exception:
                after_dt = None

            if after_dt:
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

        page_where = and_(where_clause, keyset_cond) if keyset_cond is not None else where_clause

        # --- PAGE query: select only columns you return
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
            .order_by(order_clause, desc(User.id) if "desc" in str(order_clause).lower() else asc(User.id))
            .limit(limit)
        )

        # If not using keyset, apply OFFSET (fine for small pages; avoid huge offsets)
        if keyset_cond is None and offset:
            page_stmt = page_stmt.offset(offset)

        rows = db.session.execute(page_stmt).all()

        items = []
        for r in rows:
            # r is a Row; access by index or attr name
            u = r
            items.append({
                "id": u.id,
                "email": u.email,
                "username": u.username,
                "is_demo": bool(u.is_demo),
                "email_verified": bool(u.email_verified),
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "last_seen": u.last_seen_at.isoformat() if u.last_seen_at else None,
                "demo_expires_at": u.demo_expires_at.isoformat() if u.demo_expires_at else None,
            })

        # For keyset pagination, return the cursor for next page
        next_cursor = None
        if rows:
            last = rows[-1]
            if sort.startswith("created_at"):
                next_cursor = {
                    "after_created_at": last.created_at.isoformat() if last.created_at else None,
                    "after_id": last.id
                }

        return {
            "total": int(total),
            "limit": limit,
            "offset": offset if keyset_cond is None else None,
            "sort": sort,
            "items": items,
            "next_cursor": next_cursor,  # frontend can pass these back for seek pagination
        }, 200


class AdminOnlineUsers(Resource):
    @limiter.limit("190 per minute", key_func=get_remote_address, override_defaults=False)
    def get(self):
        if not _admin_enabled(): return {"error":"forbidden","message":"Admin endpoints disabled"}, 403
        if not _valid_admin_key(request): return {"error":"unauthorized","message":"Invalid admin key"}, 401

        within = max(1, min(int(request.args.get("within") or 5), 120))
        limit = max(1, min(int(request.args.get("limit") or 200), 1000))
        threshold = datetime.utcnow() - timedelta(minutes=within)

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
            "last_seen": r.last_seen_at.isoformat() if r.last_seen_at else None,
        } for r in rows]

        # a separate fast COUNT if you still want total-in-window
        count_stmt = select(func.count()).select_from(
            select(User.id).where(User.last_seen_at.isnot(None), User.last_seen_at >= threshold).subquery()
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
    Aggregate stats for dashboard tiles.
    Headers: X-Admin-Key
    Query:  within=<minutes>  (default 5 for "online now" window)
    """
    @limiter.limit("10 per minute", key_func=get_remote_address, override_defaults=False)
    def get(self):
        if not _admin_enabled():
            return {"error": "forbidden", "message": "Admin endpoints disabled"}, 403
        if not _valid_admin_key(request):
            return {"error": "unauthorized", "message": "Invalid admin key"}, 401

        now = datetime.utcnow()
        within = int(request.args.get("within") or 5)
        within = max(1, min(within, 120))
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
            "as_of": now.isoformat() + "Z",
            "within_minutes": within,       # for "Online (≈Xm)" label
            "total_users": total,
            "verified_users": verified,
            "demo_users": demo,
            "real_users": real,
            "online_now": online_now,
            "active_last_24h": active_24h,
            "new_last_24h": new_24h
        }, 200
