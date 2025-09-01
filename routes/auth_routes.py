from flask import request, jsonify
from flask_restful import Resource
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
from config import db
from models import User
from sqlalchemy.exc import IntegrityError
import re
from datetime import timedelta
from flask_jwt_extended import create_access_token

# Email validation
def is_valid_email(email):
    email_regex = r'^[\w\.-]+@[\w\.-]+\.\w+$'
    return re.match(email_regex, email) is not None

# Username validation
def is_valid_username(username):
    return 3 <= len(username) <= 50
class Signup(Resource):
    def post(self):
        data = request.get_json()
        username, email, password = data.get("username"), data.get("email"), data.get("password")

        if not username or not email or not password:
            return {"error": "Missing required fields"}, 400
        if not is_valid_email(email):
            return {"error": "Invalid email format"}, 400
        if not is_valid_username(username):
            return {"error": "Username must be between 3 and 50 characters"}, 400
        # Check for existing username and email separately
        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            return {
                "error": "username_exists",
                "message": "Username already exists"
            }, 409
        
        existing_email = User.query.filter_by(email=email.lower()).first()
        if existing_email:
            return {
                "error": "email_exists",
                "message": "Email already exists"
            }, 409
        try:
            user = User(username=username, email=email)
            user.password_hash = password  # Hash password
            db.session.add(user)
            db.session.commit()
            return {"message": "User registered successfully"}, 201
        except IntegrityError:
            db.session.rollback()
            return {"error": "Username or email already exists"}, 409

class Login(Resource):
    def post(self):
        data = request.get_json()
        email, password = data.get("email"), data.get("password")

        if not email or not password:
            return {"error": "Email and password are required"}, 400

        user = User.query.filter_by(email=email.lower()).first()
        if user and user.check_password(password):
            print(f"User ID during login: {user.id}")  # Debugging line
            access_token = create_access_token(
                    identity=user.id,                      # simple int identity
                    expires_delta=timedelta(hours=48),
                    additional_claims={"username": user.username}  # optional
                )
            return {"message": "Login successful", "token": access_token}, 200

        return {"error": "Invalid email or password"}, 401
class ProtectedUser(Resource):
    @jwt_required()
    def get(self):
        """Fetch the current authenticated user's data."""
        current_user = get_jwt_identity()
        return jsonify(current_user)  # Directly return user data   