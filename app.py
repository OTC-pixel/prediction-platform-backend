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
# CORS Configuration - FIXED
# ------------------------------
FRONTEND_ORIGINS = [
    "https://predict-eplt6.netlify.app",
    "http://localhost:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3001"
]

# Simplified CORS configuration
CORS(
    app,
    origins=FRONTEND_ORIGINS,
    supports_credentials=True,
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
)

# Handle preflight OPTIONS requests
@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        response = jsonify()
        origin = request.headers.get('Origin')
        
        if origin in FRONTEND_ORIGINS:
            response.headers.add('Access-Control-Allow-Origin', origin)
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Requested-With')
        response.headers.add('Access-Control-Allow-Methods', 'GET, POST, PUT, PATCH, DELETE, OPTIONS')
        response.headers.add('Access-Control-Allow-Credentials', 'true')
        response.headers.add('Access-Control-Max-Age', '86400')
        return response

# ------------------------------
# Health Check Endpoint (ADD THIS FIRST)
# ------------------------------
@app.route("/ping")
def ping():
    return {"status": "alive", "message": "Server is up!"}

@app.route("/api/health")
def health_check():
    return {
        "status": "healthy", 
        "frontend_origins": FRONTEND_ORIGINS,
        "cors_enabled": True,
        "endpoints_available": True
    }

# ------------------------------
# Root Endpoint
# ------------------------------
@app.route("/")
def home():
    return {
        "message": "Football Prediction Platform API",
        "version": "1.0.0",
        "endpoints": {
            "health": "/api/health",
            "ping": "/ping",
            "auth": "/api/auth",
            "admin": "/api/admin",
            "fixtures": "/api/fixtures",
            "predictions": "/api/predictions"
        }
    }

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
    return {"error": "Endpoint not found", "status": 404}, 404

@app.errorhandler(500)
def internal_error(error):
    return {"error": "Internal server error", "status": 500}, 500

# ------------------------------
# Teardown
# ------------------------------
@app.teardown_appcontext
def teardown_db(exception):
    close_db()

# ------------------------------
# Debug Routes (for testing)
# ------------------------------
@app.route("/api/debug/routes")
def debug_routes():
    routes = []
    for rule in app.url_map.iter_rules():
        routes.append({
            "endpoint": rule.endpoint,
            "methods": list(rule.methods),
            "path": str(rule)
        })
    return jsonify({"routes": routes})

# ------------------------------
# Main Execution
# ------------------------------
if __name__ == "__main__":
    from waitress import serve
    port = int(os.environ.get("PORT", 5000))
    
    print(f" Starting Football Prediction Platform API on port {port}")
    print(f" Allowed CORS origins: {FRONTEND_ORIGINS}")
    print(f" Health check: http://localhost:{port}/api/health")
    print(f" Debug routes: http://localhost:{port}/api/debug/routes")
    
    serve(app, host="0.0.0.0", port=port)