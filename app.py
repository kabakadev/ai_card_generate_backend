from flask import Flask, jsonify
from config import app, db, api
from routes.auth_routes import Signup, Login, ProtectedUser
from routes.deck_routes import DecksResource, DeckResource
from routes.flashcard_routes import FlashcardResource, FlashcardDetailResource
from routes.dashboard_routes import Dashboard
from routes.progress_routes import ProgressResource
from routes.stats_routes import UserStatsResource
from routes.ai_routes import AIGenerateFlashcards
from sqlalchemy import text  # add this import at the top of app.py

@app.route('/')
def home():
    return jsonify({
        "message": "Welcome to the Flashcard App!",
        "endpoints": {
            "signup": "/signup",
            "login": "/login",
            "user": "/user",
            "decks": "/decks",
            "dashboard": "/dashboard",
            "progress": "/progress",
            "user_stats": "/user/stats"
        }
    })
@app.route("/health")
def health_check():
    return jsonify({"status": "ok"}), 200
@app.get("/db-ping")
def db_ping():
    try:
        db.session.execute(text("SELECT 1"))
        return {"db": "ok"}, 200
    except Exception as e:
        return {"db": "error", "detail": str(e)}, 500


@app.get("/debug/hf")
def debug_hf():
    return {
        "HF_API_URL_set": bool(app.config.get("HF_API_URL")),
        "HF_TOKEN_set": bool(app.config.get("HF_TOKEN"))
    }

# Register all routes
api.add_resource(Signup, "/signup")
api.add_resource(Login, "/login")
api.add_resource(ProtectedUser, "/user") 
api.add_resource(DecksResource, "/decks")
api.add_resource(DeckResource, "/decks/<int:deck_id>")
api.add_resource(FlashcardResource, "/flashcards")
api.add_resource(FlashcardDetailResource, "/flashcards/<int:id>")
api.add_resource(Dashboard, "/dashboard")
api.add_resource(ProgressResource, "/progress", "/progress/<int:progress_id>", "/progress/deck/<int:deck_id>", "/progress/flashcard/<int:flashcard_id>")
api.add_resource(UserStatsResource, "/user/stats")
api.add_resource(
    AIGenerateFlashcards,
    "/ai/generate", "/ai/generate/",
    "/ai/generate-flashcards", "/ai/generate-flashcards/",
)

@app.route('/init-db')
def init_db():
    try:
        db.create_all()
        return jsonify({"message": "Database tables created successfully!"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500




if __name__ == "__main__":
    app.run(debug=True)