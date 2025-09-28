# app.py — Clean application factory pattern
import logging
from flask import Flask, jsonify
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

# Import configuration and extensions
from config import app, db, api

logger = logging.getLogger(__name__)

def register_routes():
    """Register all API routes in organized groups."""
    # Authentication routes
    from routes.auth_routes import Signup, Login, ProtectedUser, DeleteUser
    api.add_resource(Signup, "/signup")
    api.add_resource(Login, "/login")
    api.add_resource(ProtectedUser, "/user")

    # Content routes
    from routes.deck_routes import DecksResource, DeckResource
    from routes.flashcard_routes import FlashcardResource, FlashcardDetailResource
    api.add_resource(DecksResource, "/decks")
    api.add_resource(DeckResource, "/decks/<int:deck_id>")
    api.add_resource(FlashcardResource, "/flashcards")
    api.add_resource(FlashcardDetailResource, "/flashcards/<int:id>")

    # Dashboard and progress routes
    from routes.dashboard_routes import Dashboard
    from routes.progress_routes import ProgressResource
    from routes.stats_routes import UserStatsResource
    api.add_resource(Dashboard, "/dashboard")
    api.add_resource(ProgressResource, "/progress", "/progress/<int:progress_id>", 
                     "/progress/deck/<int:deck_id>", "/progress/flashcard/<int:flashcard_id>")
    api.add_resource(UserStatsResource, "/user/stats")

    # AI routes
    from routes.ai_routes import AIGenerateFlashcards
    api.add_resource(AIGenerateFlashcards, "/ai/generate", "/ai/generate/",
                     "/ai/generate-flashcards", "/ai/generate-flashcards/")

    # OTP and password reset routes
    from routes.otp_routes import RequestLoginOTP, VerifyLoginOTP, ForgotPassword, ResetPassword
    api.add_resource(RequestLoginOTP, "/login/otp/request")
    api.add_resource(VerifyLoginOTP, "/login/otp/verify")
    api.add_resource(ForgotPassword, "/forgot-password")
    api.add_resource(ResetPassword, "/reset-password")

    # Billing routes
    from routes.payments_routes import BillingCheckout, BillingStatus, VerifyPayment, DebugIntaSendStatus
    from routes.webhooks import IntaSendWebhook
    api.add_resource(BillingCheckout, "/billing/checkout")
    api.add_resource(BillingStatus, "/billing/status")
    api.add_resource(VerifyPayment, "/billing/verify")
    api.add_resource(IntaSendWebhook, "/billing/webhooks/intasend", "/billing/webhooks/intasend/")
    api.add_resource(DebugIntaSendStatus, "/debug/intasend/status")

    # Admin routes (only if enabled)
    if app.config.get("ADMIN_ENDPOINTS_ENABLED", False):
        from routes.admin_routes import (
            AdminDeleteUsers, AdminCheckUsernames, AdminCreateDemoUsers,
            AdminListUsers, AdminOnlineUsers, AdminUserStats
        )
        api.add_resource(AdminDeleteUsers, "/admin/users/delete")
        api.add_resource(AdminCheckUsernames, "/admin/usernames/check")
        api.add_resource(AdminCreateDemoUsers, "/admin/demo/batch_create")
        api.add_resource(AdminListUsers, "/admin/users/list")
        api.add_resource(AdminOnlineUsers, "/admin/users/online")
        api.add_resource(AdminUserStats, "/admin/users/stats")
        logger.info("Admin routes registered")

    # Catalog routes
    from routes.catalog_routes import CatalogResource,CatalogListResource
    api.add_resource(CatalogResource, "/catalog/seed")
    api.add_resource(CatalogListResource, "/catalog")  # <-- NEW

    # Reviews / SRS routes
    from routes.reviews_routes import ReviewsNext
    api.add_resource(ReviewsNext, "/reviews/next")



def register_error_handlers():
    """Register global error handlers."""
    
    @app.errorhandler(404)
    def not_found(error):
        return jsonify({
            "error": "Not Found",
            "message": "The requested resource was not found"
        }), 404

    @app.errorhandler(500)
    def internal_error(error):
        logger.error(f"Internal server error: {error}")
        db.session.rollback()
        return jsonify({
            "error": "Internal Server Error",
            "message": "An unexpected error occurred"
        }), 500

    @app.errorhandler(SQLAlchemyError)
    def database_error(error):
        logger.error(f"Database error: {error}")
        db.session.rollback()
        return jsonify({
            "error": "Database Error",
            "message": "A database error occurred"
        }), 500

def register_core_routes():
    """Register core application routes."""
    
    @app.route('/')
    def home():
        """API documentation endpoint."""
        return jsonify({
            "message": "Welcome to FlashLearn API!",
            "version": "1.0",
            "environment": app.config.get("ENV", "unknown"),
            "endpoints": {
                "authentication": {
                    "signup": "/signup",
                    "login": "/login",
                    "user_profile": "/user",
                    "otp_login": "/login/otp/request",
                    "verify_otp": "/login/otp/verify",
                    "forgot_password": "/forgot-password",
                    "reset_password": "/reset-password"
                },
                "content": {
                    "decks": "/decks",
                    "flashcards": "/flashcards",
                    "dashboard": "/dashboard",
                    "progress": "/progress",
                    "ai_generate": "/ai/generate"
                },
                "billing": {
                    "checkout": "/billing/checkout",
                    "status": "/billing/status",
                    "verify": "/billing/verify"
                },
                "utilities": {
                    "health": "/health",
                    "db_ping": "/db-ping",
                    "stats": "/user/stats"
                }
            }
        })

    @app.route("/health")
    def health_check():
        """Health check endpoint for monitoring."""
        try:
            # Test database connection
            db.session.execute(text("SELECT 1"))
            db_status = "healthy"
        except Exception as e:
            logger.error(f"Health check database error: {e}")
            db_status = "unhealthy"
            return jsonify({
                "status": "unhealthy",
                "database": db_status,
                "timestamp": "now"
            }), 503

        return jsonify({
            "status": "healthy",
            "database": db_status,
            "environment": app.config.get("ENV"),
            "timestamp": "now"
        }), 200

    @app.route("/db-ping")
    def db_ping():
        """Database connectivity test."""
        try:
            result = db.session.execute(text("SELECT version()"))
            version_info = result.fetchone()
            return jsonify({
                "database": "connected",
                "version": str(version_info[0]) if version_info else "unknown"
            }), 200
        except Exception as e:
            logger.error(f"Database ping failed: {e}")
            return jsonify({
                "database": "disconnected",
                "error": str(e)
            }), 500

    @app.route('/init-db')
    def init_db():
        """Initialize database tables (development only)."""
        if not app.config.get("ENV") in ["dev", "development", "local"]:
            return jsonify({"error": "Not available in production"}), 403
            
        try:
            db.create_all()
            logger.info("Database tables created successfully")
            return jsonify({"message": "Database tables created successfully!"}), 200
        except Exception as e:
            logger.error(f"Database initialization failed: {e}")
            return jsonify({"error": f"Database initialization failed: {str(e)}"}), 500

def create_app():
    """Application factory function."""
    # Register all components
    register_core_routes()
    register_routes()
    register_error_handlers()
    
    logger.info("FlashLearn API application initialized successfully")
    return app

# Initialize the application
create_app()

if __name__ == "__main__":
    app.run(debug=True)