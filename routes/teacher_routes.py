from __future__ import annotations

from datetime import timedelta, datetime
from typing import List

from flask import request, jsonify
from flask_restful import Resource
from flask_jwt_extended import jwt_required, get_jwt_identity

from config import db, limiter
from models import User, Deck, Flashcard, StudentDeck

# ---------------------------
# Helpers & guards
# ---------------------------

def _now():
    return datetime.utcnow()

def _resolve_user_id(identity):
    if isinstance(identity, int):
        return identity
    if isinstance(identity, dict) and isinstance(identity.get("id"), int):
        return identity["id"]
    return None

def _get_current_user():
    uid = get_jwt_identity()
    return User.query.get(uid)

def require_role(role: str):
    """Decorator to enforce a single role (e.g., 'teacher')."""
    def decorator(fn):
        def wrapper(*args, **kwargs):
            user = _get_current_user()
            if not user:
                return {"error": "unauthorized"}, 401
            if user.role != role:
                return {"error": "forbidden", "message": f"{role} role required"}, 403
            # attach for downstream use
            request.current_user = user
            return fn(*args, **kwargs)
        # preserve wrapped function attributes for Flask-RESTful (optional)
        wrapper.__name__ = fn.__name__
        return wrapper
    return decorator

def _json():
    return request.get_json(silent=True) or {}

def _404():
    return {"error": "not_found"}, 404

def _validate_teacher_owns_deck(teacher_id: int, deck_id: int) -> Deck | None:
    return Deck.query.filter_by(id=deck_id, user_id=teacher_id).first()

def _validate_student_belongs_to_teacher(teacher_id: int, student_id: int) -> User | None:
    return User.query.filter_by(id=student_id, is_demo=True, teacher_id=teacher_id).first()

# ---------------------------
# Rate limits
# ---------------------------

@limiter.limit("60 per minute", override_defaults=False)
def teacher_rate_limit():
    # no-op, used via decorator where desired
    pass

# ---------------------------
# Teacher: Demo Accounts
# ---------------------------

class TeacherCreateDemoAccounts(Resource):
    """
    Create one or more demo student accounts owned by the teacher.
    Body:
      {
        "usernames": ["alice","bob"]  # optional; otherwise provide count
        "count": 5,
        "prefix": "class9",
        "expires_in_days": 30
      }
    Returns:
      { "users": [{id, username, email, password}], "count": N }
    """
    decorators = [jwt_required(), require_role('teacher')]

    def post(self):
        teacher: User = request.current_user

        data = _json()
        usernames_input = data.get("usernames") or []
        count = int(data.get("count") or 0)
        prefix = (data.get("prefix") or "demo").strip().lower()
        expires_in_days = data.get("expires_in_days")

        if usernames_input and not isinstance(usernames_input, list):
            return {"error": "invalid_request", "message": "'usernames' must be a list"}, 400

        if not usernames_input and count <= 0:
            return {"error": "invalid_request", "message": "Provide 'usernames' or a positive 'count'."}, 400

        # generate usernames when only count provided
        import random, string
        if not usernames_input:
            usernames_input = [
                f"{prefix}-{''.join(random.choices(string.ascii_lowercase + string.digits, k=6))}"
                for _ in range(count)
            ]

        # de-dup within batch; also avoid collisions in DB
        existing_usernames = {u for (u,) in db.session.query(User.username).filter(User.username.in_(usernames_input)).all()}
        used = set(existing_usernames)
        final_rows = []
        api_return = []
        from config import bcrypt

        demo_expires_at = None
        if expires_in_days:
            demo_expires_at = _now() + timedelta(days=int(expires_in_days))

        for base in usernames_input:
            u = (base or "").strip().lower()
            if not (3 <= len(u) <= 50):
                return {"error": "invalid_username", "message": f"Username '{base}' must be 3-50 chars"}, 400
            # make unique locally
            if u in used:
                import random, string
                while True:
                    cand = f"{u}-{''.join(random.choices(string.ascii_lowercase + string.digits, k=4))}"
                    if cand not in used:
                        u = cand
                        break
            used.add(u)
            email = f"{u}@demo.flashlearn.local"

            # random password
            pw_plain = None
            pw_plain = "".join(__import__('random').choices("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=10))
            pw_hash = bcrypt.generate_password_hash(pw_plain).decode("utf-8")

            final_rows.append({
                "username": u,
                "email": email,
                "is_demo": True,
                "email_verified": True,
                "email_verified_at": _now(),
                "demo_expires_at": demo_expires_at,
                "password_hash": pw_hash,
                "role": "student",
                "teacher_id": teacher.id,
            })
            api_return.append({"username": u, "email": email, "password": pw_plain})

        try:
            db.session.execute(User.__table__.insert(), final_rows)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return {"error": "creation_failed", "message": str(e)}, 500

        # look up IDs to return (optional)
        rows = db.session.query(User.id, User.username, User.email).filter(
            User.username.in_([r["username"] for r in final_rows])
        ).all()
        id_map = {r.username: r.id for r in rows}
        for it in api_return:
            it["id"] = id_map.get(it["username"])

        return {"users": api_return, "count": len(api_return)}, 201


class TeacherListDemoAccounts(Resource):
    """
    List teacher-owned demo students (search + pagination).
    Query:
      q, page=1, per_page=20
    """
    decorators = [jwt_required(), require_role('teacher')]

    def get(self):
        teacher: User = request.current_user
        q = (request.args.get("q") or "").strip().lower()
        page = max(1, int(request.args.get("page") or 1))
        per_page = min(100, max(1, int(request.args.get("per_page") or 20)))

        query = User.query.filter_by(is_demo=True, teacher_id=teacher.id)
        if q:
            from sqlalchemy import func, or_
            needle = f"%{q}%"
            query = query.filter(or_(func.lower(User.username).ilike(needle), func.lower(User.email).ilike(needle)))

        pagination = query.order_by(User.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)

        items = [{
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "disabled_at": u.disabled_at.isoformat() if u.disabled_at else None,
            "demo_expires_at": u.demo_expires_at.isoformat() if u.demo_expires_at else None,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_seen_at": u.last_seen_at.isoformat() if u.last_seen_at else None,
        } for u in pagination.items]

        return {
            "items": items,
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total_pages": pagination.pages,
                "total_items": pagination.total,
                "has_next": pagination.has_next,
                "has_prev": pagination.has_prev,
            }
        }, 200


class TeacherUpdateDemoAccount(Resource):
    """
    Update limited fields on a teacher-owned demo student.
    Body: { "username"?, "password"?, "demo_expires_in_days"?, "disabled": true|false }
    """
    decorators = [jwt_required(), require_role('teacher')]

    def patch(self, student_id: int):
        teacher: User = request.current_user
        student = _validate_student_belongs_to_teacher(teacher.id, student_id)
        if not student:
            return _404()

        data = _json()
        changed = False

        if "username" in data:
            new_u = (data.get("username") or "").strip()
            if not (3 <= len(new_u) <= 50):
                return {"error": "invalid_username"}, 400
            # check unique
            if User.query.filter(User.username == new_u, User.id != student.id).first():
                return {"error": "username_exists"}, 409
            student.username = new_u
            changed = True

        if "password" in data and data["password"]:
            from config import bcrypt
            student.password_hash = bcrypt.generate_password_hash(data["password"]).decode("utf-8")
            changed = True

        if "demo_expires_in_days" in data and data["demo_expires_in_days"] is not None:
            try:
                days = int(data["demo_expires_in_days"])
                student.demo_expires_at = _now() + timedelta(days=days)
                changed = True
            except ValueError:
                return {"error": "invalid_days"}, 400

        if "disabled" in data:
            if bool(data["disabled"]):
                if not student.disabled_at:
                    student.disabled_at = _now()
            else:
                student.disabled_at = None
            changed = True

        if changed:
            db.session.commit()

        return {
            "id": student.id,
            "username": student.username,
            "email": student.email,
            "disabled_at": student.disabled_at.isoformat() if student.disabled_at else None,
            "demo_expires_at": student.demo_expires_at.isoformat() if student.demo_expires_at else None,
        }, 200


class TeacherDisableDemoAccount(Resource):
    """Explicit disable endpoint (idempotent)."""
    decorators = [jwt_required(), require_role('teacher')]

    def post(self, student_id: int):
        teacher: User = request.current_user
        student = _validate_student_belongs_to_teacher(teacher.id, student_id)
        if not student:
            return _404()
        if not student.disabled_at:
            student.disabled_at = _now()
            db.session.commit()
        return {"id": student.id, "disabled_at": student.disabled_at.isoformat()}, 200


class TeacherExtendDemoAccount(Resource):
    """Extend demo expiration by N days (policy-limited, enforce caps later)."""
    decorators = [jwt_required(), require_role('teacher')]

    def post(self, student_id: int):
        teacher: User = request.current_user
        student = _validate_student_belongs_to_teacher(teacher.id, student_id)
        if not student:
            return _404()

        data = _json()
        try:
            days = int(data.get("days") or 0)
        except ValueError:
            return {"error": "invalid_days"}, 400
        if days <= 0:
            return {"error": "days_must_be_positive"}, 400

        base = student.demo_expires_at or _now()
        student.demo_expires_at = base + timedelta(days=days)
        db.session.commit()
        return {"id": student.id, "demo_expires_at": student.demo_expires_at.isoformat()}, 200

# ---------------------------
# Teacher: Deck assignment
# ---------------------------

class TeacherAssignDeck(Resource):
    """
    Assign a teacher-owned deck to one or more of their demo students.
    Body: { "student_ids": [1,2,3] }
    """
    decorators = [jwt_required(), require_role('teacher')]

    def post(self, deck_id: int):
        teacher: User = request.current_user

        deck = _validate_teacher_owns_deck(teacher.id, deck_id)
        if not deck:
            return {"error": "deck_not_found"}, 404

        data = _json()
        student_ids: List[int] = data.get("student_ids") or []
        if not isinstance(student_ids, list) or not student_ids:
            return {"error": "invalid_request", "message": "student_ids must be a non-empty list"}, 400

        # validate each student belongs to teacher
        valid_students = db.session.query(User.id).filter(
            User.id.in_(student_ids),
            User.is_demo.is_(True),
            User.teacher_id == teacher.id
        ).all()
        valid_ids = {row.id for row in valid_students}
        missing = [sid for sid in student_ids if sid not in valid_ids]
        if missing:
            return {"error": "student_mismatch", "missing": missing}, 400

        # upsert (unique student_id, deck_id)
        created = 0
        from sqlalchemy import insert
        stmt = insert(StudentDeck.__table__).values([
            {
                "student_id": sid,
                "deck_id": deck.id,
                "assigned_by_user_id": teacher.id,
                "status": "active"
            } for sid in valid_ids
        ]).on_conflict_do_nothing(index_elements=["student_id", "deck_id"])

        db.session.execute(stmt)
        db.session.commit()
        created = len(valid_ids)  # duplicates no-op, that’s fine

        return {"assigned": created, "deck_id": deck.id, "student_ids": list(valid_ids)}, 200


class TeacherUnassignDeck(Resource):
    """
    Unassign a deck from given demo students.
    Body: { "student_ids": [1,2,3] }
    """
    decorators = [jwt_required(), require_role('teacher')]

    def post(self, deck_id: int):
        teacher: User = request.current_user
        deck = _validate_teacher_owns_deck(teacher.id, deck_id)
        if not deck:
            return {"error": "deck_not_found"}, 404

        data = _json()
        student_ids: List[int] = data.get("student_ids") or []
        if not isinstance(student_ids, list) or not student_ids:
            return {"error": "invalid_request", "message": "student_ids must be a non-empty list"}, 400

        # only unassign if those students belong to this teacher
        valid_students = db.session.query(User.id).filter(
            User.id.in_(student_ids),
            User.is_demo.is_(True),
            User.teacher_id == teacher.id
        ).all()
        valid_ids = [row.id for row in valid_students]
        if not valid_ids:
            return {"removed": 0}, 200

        db.session.query(StudentDeck).filter(
            StudentDeck.deck_id == deck.id,
            StudentDeck.student_id.in_(valid_ids)
        ).delete(synchronize_session=False)

        db.session.commit()
        return {"removed": len(valid_ids)}, 200


class TeacherListStudentDecks(Resource):
    """
    View the decks assigned to a specific teacher-owned demo student.
    """
    decorators = [jwt_required(), require_role('teacher')]

    def get(self, student_id: int):
        teacher: User = request.current_user
        student = _validate_student_belongs_to_teacher(teacher.id, student_id)
        if not student:
            return _404()

        rows = db.session.query(
            StudentDeck.deck_id,
            StudentDeck.status,
            StudentDeck.assigned_at,
            Deck.title,
            Deck.description,
            Deck.subject,
            Deck.category,
            Deck.difficulty,
        ).join(Deck, Deck.id == StudentDeck.deck_id).filter(
            StudentDeck.student_id == student.id
        ).order_by(StudentDeck.assigned_at.desc()).all()

        items = [{
            "deck_id": r.deck_id,
            "status": r.status,
            "assigned_at": r.assigned_at.isoformat() if r.assigned_at else None,
            "title": r.title,
            "description": r.description,
            "subject": r.subject,
            "category": r.category,
            "difficulty": r.difficulty,
        } for r in rows]

        return {"student_id": student.id, "items": items}, 200


# ---------------------------
# Optional: copy a default deck to teacher ownership
# ---------------------------

class TeacherCopyDeck(Resource):
    """
    Copy an existing deck (e.g., a global/default deck owned by system/admin)
    into the teacher's workspace, including its flashcards.
    Body: { "source_deck_id": 123 }
    """
    decorators = [jwt_required(), require_role('teacher')]

    def post(self):
        teacher: User = request.current_user
        data = _json()
        try:
            source_deck_id = int(data.get("source_deck_id"))
        except Exception:
            return {"error": "invalid_source_deck_id"}, 400

        # Source must NOT be already owned by this teacher (otherwise copy is redundant)
        src = Deck.query.filter(Deck.id == source_deck_id, Deck.user_id != teacher.id).first()
        if not src:
            return {"error": "deck_not_found_or_owned"}, 404

        # Create new deck
        new_deck = Deck(
            user_id=teacher.id,
            title=src.title,
            description=src.description,
            subject=src.subject,
            category=src.category,
            difficulty=src.difficulty,
            is_default=False,
        )
        db.session.add(new_deck)
        db.session.flush()  # get new_deck.id

        # Copy cards
        cards = Flashcard.query.filter_by(deck_id=src.id).all()
        payload = [
            {"deck_id": new_deck.id, "front_text": c.front_text, "back_text": c.back_text}
            for c in cards
        ]
        if payload:
            db.session.execute(Flashcard.__table__.insert(), payload)

        db.session.commit()

        return {
            "deck": {
                "id": new_deck.id,
                "title": new_deck.title,
                "copied_from": src.id,
                "cards_copied": len(payload)
            }
        }, 201
