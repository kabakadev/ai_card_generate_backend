# config.py — Improved security and organization
from __future__ import annotations

import os
import logging
from datetime import timedelta
from dotenv import load_dotenv
from flask import Flask

# Load environment variables first
load_dotenv()

# Configure logging before anything else
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------- Environment Detection ----------------
ENV = os.getenv("FLASK_ENV", "development").lower()
IS_PRODUCTION = ENV == "production"
IS_LOCAL = ENV in {"dev", "development", "local"}

app.config["ENV"] = ENV
app.debug = IS_LOCAL and not IS_PRODUCTION

# ---------------- Security Configuration ----------------
# JWT Configuration
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not JWT_SECRET_KEY:
    if IS_PRODUCTION:
        raise ValueError("JWT_SECRET_KEY must be set in production!")
    else:
        JWT_SECRET_KEY = "dev-only-insecure-key"
        logger.warning("Using insecure JWT key in development")

app.config.update({
    "JWT_SECRET_KEY": JWT_SECRET_KEY,
    "JWT_ACCESS_TOKEN_EXPIRES": timedelta(hours=48),
    "JWT_DECODE_LEEWAY": 60,
    "JWT_HEADER_TYPE": "Bearer",
    "JWT_TOKEN_LOCATION": ["headers"],
    "RATELIMIT_HEADERS_ENABLED": True,
})

# ---------------- Database Configuration ----------------
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    if IS_PRODUCTION:
        raise ValueError("DATABASE_URL must be set in production!")
    else:
        DATABASE_URL = "sqlite:///app.db"
        logger.warning("Using SQLite in development")

app.config.update({
    "SQLALCHEMY_DATABASE_URI": DATABASE_URL,
    "SQLALCHEMY_TRACK_MODIFICATIONS": False,
    "SQLALCHEMY_ENGINE_OPTIONS": {
        "pool_size": 10,
        "pool_timeout": 20,
        "pool_recycle": -1,
        "max_overflow": 0
    }
})

# ---------------- Admin Configuration (FIXED SECURITY) ----------------
def _parse_admin_domains():
    """Parse admin allowed domains with security validation."""
    domains_str = os.getenv("ADMIN_ALLOWED_EMAIL_DOMAINS", "")
    
    if not domains_str:
        return []
    
    # SECURITY: Never allow wildcard in production
    if domains_str.strip() == "*":
        if IS_PRODUCTION:
            raise ValueError("Wildcard admin domains not allowed in production!")
        else:
            logger.warning("🚨 SECURITY: Wildcard admin domains enabled in development")
            return ["*"]
    
    # Parse comma-separated domains
    domains = [d.strip().lower() for d in domains_str.split(",") if d.strip()]
    logger.info(f"Admin allowed domains: {domains}")
    return domains

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "dev-only-key")
if ADMIN_API_KEY == "dev-only-key" and IS_PRODUCTION:
    raise ValueError("ADMIN_API_KEY must be changed in production!")

app.config.update({
    "ADMIN_API_KEY": ADMIN_API_KEY,
    "ADMIN_ALLOWED_EMAIL_DOMAINS": _parse_admin_domains(),
    "ADMIN_ENDPOINTS_ENABLED": os.getenv("ADMIN_ENDPOINTS_ENABLED", "true" if IS_LOCAL else "false").lower() in {"1", "true", "yes", "on"}
})

# ---------------- Billing Configuration ----------------
app.config.update({
    "INTASEND_PUBLIC_KEY": os.getenv("INTASEND_PUBLIC_KEY", ""),
    "INTASEND_SECRET_KEY": os.getenv("INTASEND_SECRET_KEY", ""),
    "INTASEND_TEST_MODE": os.getenv("INTASEND_TEST_MODE", "true").lower() in {"1", "true", "yes", "on"},
    "BILLING_PLAN_MONTHLY_KES": int(os.getenv("BILLING_PLAN_MONTHLY_KES", "100")),
    "BILLING_CURRENCY": os.getenv("BILLING_CURRENCY", "KES"),
})

# ---------------- OTP Configuration ----------------
app.config.update({
    "OTP_TTL_SECONDS": int(os.getenv("OTP_TTL_SECONDS", "300")),
    "OTP_MAX_ATTEMPTS": int(os.getenv("OTP_MAX_ATTEMPTS", "5")),
    "OTP_ECHO_IN_LOGS": IS_LOCAL,  # Only in development
})

# ---------------- SMTP Configuration ----------------
app.config.update({
    "SMTP_HOST": os.getenv("SMTP_HOST", ""),
    "SMTP_PORT": int(os.getenv("SMTP_PORT", "587")),
    "SMTP_USE_TLS": os.getenv("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes", "on"},
    "SMTP_USER": os.getenv("SMTP_USER", ""),
    "SMTP_PASSWORD": os.getenv("SMTP_PASSWORD", ""),
    "SMTP_FROM": os.getenv("SMTP_FROM", "noreply@flashlearn.local"),
    "SMTP_FROM_NAME": os.getenv("SMTP_FROM_NAME", "FlashLearn"),
})

# ---------------- Third-party APIs ----------------
app.config["GROQ_API_KEY"] = os.getenv("GROQ_API_KEY")

# ---------------- CORS Configuration ----------------
def _get_cors_origins():
    """Get CORS origins with environment-specific defaults."""
    base_origins = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    
    if not IS_LOCAL:
        base_origins.append("https://aiflashcard254.netlify.app")
    
    extra_origins = os.getenv("CORS_EXTRA_ORIGINS", "")
    if extra_origins:
        base_origins.extend([o.strip() for o in extra_origins.split(",") if o.strip()])
    
    return base_origins

CORS_ORIGINS = _get_cors_origins()

# ---------------- Extensions Initialization ----------------
from flask_bcrypt import Bcrypt
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from flask_migrate import Migrate
from flask_restful import Api
from flask_sqlalchemy import SQLAlchemy
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Initialize extensions
jwt = JWTManager(app)
db = SQLAlchemy(app)
migrate = Migrate(app, db)
bcrypt = Bcrypt(app)
api = Api(app)

# Rate Limiter with Redis support for production
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["200 per hour"],
    storage_uri=os.getenv("REDIS_URL") if os.getenv("REDIS_URL") else None,
)

# CORS Configuration
CORS(
    app,
    resources={r"/*": {"origins": CORS_ORIGINS}},
    supports_credentials=False,
    allow_headers=["Content-Type", "Authorization", "X-Admin-Key"],
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    max_age=86400,
)

# ---------------- Security Logging ----------------
def _mask_sensitive(value: str, keep: int = 4) -> str:
    """Mask sensitive values for logging."""
    if not value or len(value) <= keep:
        return "***"
    return ("*" * (len(value) - keep)) + value[-keep:]

# Log configuration (without sensitive values)
logger.info(f"Environment: {ENV}")
logger.info(f"Debug mode: {app.debug}")
logger.info(f"Database: {'PostgreSQL' if 'postgresql' in DATABASE_URL else 'SQLite'}")
logger.info(f"Admin endpoints: {'enabled' if app.config['ADMIN_ENDPOINTS_ENABLED'] else 'disabled'}")
logger.info(f"CORS origins: {CORS_ORIGINS}")

SQLALCHEMY_ECHO = True


# Log billing config with masked keys
if app.config["INTASEND_PUBLIC_KEY"]:
    logger.info(
        f"Billing: test_mode={app.config['INTASEND_TEST_MODE']}, "
        f"public_key={_mask_sensitive(app.config['INTASEND_PUBLIC_KEY'])}, "
        f"currency={app.config['BILLING_CURRENCY']}"
    )