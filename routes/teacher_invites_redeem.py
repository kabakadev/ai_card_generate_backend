from datetime import datetime
from flask import request
from flask_restful import Resource
from flask_jwt_extended import jwt_required, get_jwt_identity

from config import db
from models import User, TeacherInvite

class RedeemTeacherInvite(Resource):
    @jwt_required()
    def post(self):
        user_id = get_jwt_identity()
        user = User.query.get(user_id)
        if not user:
            return {"error": "user_not_found"}, 404

        data = request.get_json(silent=True) or {}
        code = (data.get("code") or "").strip()
        if not code:
            return {"error": "code_required"}, 400

        inv = TeacherInvite.query.filter_by(code=code).first()
        if not inv:
            return {"error": "invalid_code"}, 404
        if inv.revoked:
            return {"error": "revoked"}, 400
        if inv.expires_at and inv.expires_at < datetime.utcnow():
            return {"error": "expired"}, 400
        if inv.used_count >= inv.max_uses:
            return {"error": "exhausted"}, 400

        # Already privileged? No-op success.
        if user.role in ("teacher", "admin"):
            return {"ok": True, "role": user.role, "already": True}, 200

        user.role = "teacher"
        inv.used_count += 1
        db.session.commit()

        return {"ok": True, "role": "teacher"}, 200
