import os
import sys
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Import your blueprints
from routes.auth import auth_bp
from routes.admin import admin_bp
from routes.predictions import predictions_bp
from routes.leaderboard import leaderboard_bp
from routes.fixtures import fixtures_bp
from routes.results import results_bp
from db import close_db
from scheduler import start_scheduler

load_dotenv()

app = Flask(__name__)

# ------------------------------
# CORS Configuration - FIXED SYNTAX
# ------------------------------
FRONTEND_ORIGINS = [
    "https://predict-eplt6.netlify.app",
    "http://localhost:3000",
    "http://localhost:3001", 
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3001"
]

# ✅ CORRECT: Use 'origins' (plural) not 'origin'
CORS(
    app,
    origins=FRONTEND_ORIGINS,  # ← FIXED: 'origins' not 'origin'
    supports_credentials=True,
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
)

# ------------------------------
# Health Check Endpoints
# ------------------------------
@app.route("/ping")
def ping():
    return jsonify({"status": "alive", "message": "Server is up!"})

@app.route("/api/health")
def health_check():
    return jsonify({
        "status": "healthy", 
        "frontend_origins": FRONTEND_ORIGINS,
        "cors_enabled": True
    })

# ------------------------------
# Root Endpoint
# ------------------------------
@app.route("/")
def home():
    return jsonify({
        "message": "Football Prediction Platform API",
        "version": "1.0.0"
    })

# ------------------------------
# Blueprints Registration
# ------------------------------
app.register_blueprint(auth_bp, url_prefix="/api/auth")
app.register_blueprint(admin_bp, url_prefix="/api/admin")
app.register_blueprint(fixtures_bp, url_prefix="/api/fixtures")
app.register_blueprint(predictions_bp, url_prefix="/api/predictions")
app.register_blueprint(leaderboard_bp, url_prefix="/api/leaderboard")
app.register_blueprint(results_bp, url_prefix="/api/results")

# ------------------------------
# Scheduler
# ------------------------------
start_scheduler()

# ------------------------------
# Error Handlers
# ------------------------------
@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Endpoint not found", "status": 404}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Internal server error", "status": 500}), 500

# ------------------------------
# Teardown
# ------------------------------
@app.teardown_appcontext
def teardown_db(exception):
    close_db()

# ------------------------------
# Main Execution
# ------------------------------
if __name__ == "__main__":
    from waitress import serve
    port = int(os.environ.get("PORT", 5000))
    
    print("⚽ Server starting with fixed CORS configuration")
    print(f"🌐 Allowed origins: {FRONTEND_ORIGINS}")
    
    serve(app, host="0.0.0.0", port=port)