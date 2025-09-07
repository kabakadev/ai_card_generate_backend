# config.py — DROP-IN (headers + cookies across dev & prod)
from __future__ import annotations

import os
from datetime import timedelta
from dotenv import load_dotenv
from flask import Flask
from flask_bcrypt import Bcrypt
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from flask_migrate import Migrate
from flask_restful import Api
from flask_sqlalchemy import SQLAlchemy

load_dotenv()

app = Flask(__name__)

# ---------------- Base config ----------------
# DB
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///app.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# JWT
app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY", "supersecretkey")
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=48)
app.config["JWT_DECODE_LEEWAY"] = 60
app.config["JWT_HEADER_TYPE"] = "Bearer"

# Accept BOTH headers and cookies
app.config["JWT_TOKEN_LOCATION"] = ["headers", "cookies"]
# Cookie names (defaults are fine; you can rename if you want)
# app.config["JWT_ACCESS_COOKIE_NAME"] = "access_token_cookie"
# app.config["JWT_REFRESH_COOKIE_NAME"] = "refresh_token_cookie"

# Detect environment (affects cookie flags)
ENV = os.getenv("FLASK_ENV") or os.getenv("ENV") or "development"
IS_LOCAL = ENV.lower() in {"dev", "development", "local"} or os.getenv("LOCAL_DEV", "true").lower() in {"1","true","yes","on"}

# Cookie flags:
# - Local dev (localhost / 127.0.0.1): SameSite=Lax, Secure=False (so cookies work over http)
# - Hosted (Netlify → your API domain): SameSite=None, Secure=True (required by browsers for cross-site)
if IS_LOCAL:
    app.config["JWT_COOKIE_SECURE"] = False
    app.config["JWT_COOKIE_SAMESITE"] = "Lax"
    app.config["JWT_COOKIE_CSRF_PROTECT"] = False  # you can enable + handle CSRF tokens if desired
else:
    app.config["JWT_COOKIE_SECURE"] = True
    app.config["JWT_COOKIE_SAMESITE"] = "None"
    # If you enable CSRF, ensure FE sends X-CSRF-TOKEN from the CSRF cookie for modifying methods
    app.config["JWT_COOKIE_CSRF_PROTECT"] = False  # set True if you wire CSRF on FE

# Optional cookie domain (set only if you serve API on subdomain and need sharing)
JWT_COOKIE_DOMAIN = os.getenv("JWT_COOKIE_DOMAIN", "").strip()
if JWT_COOKIE_DOMAIN:
    app.config["JWT_COOKIE_DOMAIN"] = JWT_COOKIE_DOMAIN

# ---------------- IntaSend / Billing ----------------
app.config["INTASEND_PUBLIC_KEY"] = os.getenv("INTASEND_PUBLIC_KEY", "")
app.config["INTASEND_SECRET_KEY"] = os.getenv("INTASEND_SECRET_KEY", "")
app.config["INTASEND_TEST_MODE"] = os.getenv("INTASEND_TEST_MODE", "true").lower() in {"1","true","yes","on"}
app.config["BILLING_PLAN_MONTHLY_KES"] = int(os.getenv("BILLING_PLAN_MONTHLY_KES"))
app.config["BILLING_CURRENCY"] = os.getenv("BILLING_CURRENCY", "KES")

def _mask(s: str, keep: int = 4) -> str:
    if not s:
        return ""
    return ("*" * max(0, len(s) - keep)) + s[-keep:]

app.logger.warning(
    "[Billing] env=%s test_mode=%s public=%s secret=%s currency=%s plan=%s",
    ENV,
    app.config["INTASEND_TEST_MODE"],
    _mask(app.config["INTASEND_PUBLIC_KEY"]),
    _mask(app.config["INTASEND_SECRET_KEY"]),
    app.config["BILLING_CURRENCY"],
    app.config["BILLING_PLAN_MONTHLY_KES"],
)

# ---------------- Third-party keys ----------------
app.config["GROQ_API_KEY"] = os.getenv("GROQ_API_KEY")

# ---------------- Extensions ----------------
jwt = JWTManager(app)
db = SQLAlchemy(app)
migrate = Migrate(app, db)
bcrypt = Bcrypt(app)
api = Api(app)

# ---------------- CORS (headers + cookies across dev origins) ----------------
# Add/adjust as needed (Netlify domain already included)
FRONTEND_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://aiflashcard254.netlify.app",
]
extra_origins = os.getenv("CORS_EXTRA_ORIGINS", "")
if extra_origins:
    FRONTEND_ORIGINS.extend([o.strip() for o in extra_origins.split(",") if o.strip()])

CORS(
    app,
    resources={r"/*": {"origins": FRONTEND_ORIGINS}},
    supports_credentials=True,                     # allow cookies w/ XHR/fetch
    allow_headers=["Content-Type", "Authorization"],
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    max_age=86400,                                 # cache preflight 1 day
)
