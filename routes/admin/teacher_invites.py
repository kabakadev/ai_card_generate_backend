from datetime import datetime, timedelta
import os

from flask import request
from flask_restful import Resource

from config import app, db
from models import TeacherInvite

def _admin_guard() -> bool:
    key = request.headers.get("X-Admin-Key") or request.headers.get("x-admin-key")
    return bool(key and key == os.getenv("ADMIN_API_KEY"))

def _is_dev_env() -> bool:
    return app.config.get("ENV") in ["dev", "development", "local"]

class AdminCreateTeacherInvite(Resource):
    def post(self):
        # Keep it dev-only + require header key (same pattern as your Admin)
        if not _is_dev_env() or not _admin_guard():
            return {"error": "unauthorized"}, 401

        body = request.get_json(silent=True) or {}
        max_uses = int(body.get("max_uses", 1))
        expires_in_days = body.get("expires_in_days")
        expires_at = None
        if expires_in_days:
            expires_at = datetime.utcnow() + timedelta(days=int(expires_in_days))

        inv = TeacherInvite(
            code=TeacherInvite.generate_code(),
            max_uses=max_uses,
            expires_at=expires_at,
            revoked=False,
            created_by=None,  # Optionally set to admin user id if you later add admin auth
        )
        db.session.add(inv)
        db.session.commit()

        return {
            "id": inv.id,
            "code": inv.code,
            "max_uses": inv.max_uses,
            "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
        }, 201
