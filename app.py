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
# CORS Configuration - COMPLETE FIX
# ------------------------------
FRONTEND_ORIGINS = [
    "https://predict-eplt6.netlify.app",  # Production frontend
    "http://localhost:3000",              # Local development
    "http://localhost:3001",              # Alternative local port
    "http://127.0.0.1:3000",              # Local IP
    "http://127.0.0.1:3001"               # Alternative local IP
]

# Global CORS configuration for all routes
CORS(
    app,
    origins=FRONTEND_ORIGINS,
    supports_credentials=True,
    allow_headers=["Content-Type", "Authorization", "X-Requested-With", "Accept"],
    expose_headers=["Content-Type", "Authorization"],
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
)

# ------------------------------
# Manual CORS Headers for OPTIONS requests
# ------------------------------
@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        response = jsonify()
        origin = request.headers.get('Origin')
        
        if origin in FRONTEND_ORIGINS:
            response.headers.add('Access-Control-Allow-Origin', origin)
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Requested-With, Accept')
        response.headers.add('Access-Control-Allow-Methods', 'GET, POST, PUT, PATCH, DELETE, OPTIONS')
        response.headers.add('Access-Control-Allow-Credentials', 'true')
        response.headers.add('Access-Control-Max-Age', '86400')
        return response

# ------------------------------
# Additional CORS security for all responses
# ------------------------------
@app.after_request
def add_cors_headers(response):
    origin = request.headers.get('Origin')
    
    if origin and origin in FRONTEND_ORIGINS:
        response.headers.add('Access-Control-Allow-Origin', origin)
        response.headers.add('Access-Control-Allow-Credentials', 'true')
        response.headers.add('Access-Control-Expose-Headers', 'Content-Type, Authorization')
    
    # Remove any problematic compression headers
    if response.headers.get('Content-Encoding') == 'br':
        response.headers.remove('Content-Encoding')
    
    return response

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
        "cors_enabled": True,
        "endpoints_available": True
    })

# ------------------------------
# Root Endpoint
# ------------------------------
@app.route("/")
def home():
    return jsonify({
        "message": "Football Prediction Platform API",
        "version": "1.0.0",
        "endpoints": {
            "health": "/api/health",
            "ping": "/ping",
            "auth": "/api/auth",
            "admin": "/api/admin",
            "fixtures": "/api/fixtures",
            "predictions": "/api/predictions",
            "leaderboard": "/api/leaderboard",
            "results": "/api/results"
        }
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
# Error Handlers with CORS
# ------------------------------
@app.errorhandler(404)
def not_found(error):
    response = jsonify({"error": "Endpoint not found", "status": 404})
    response.status_code = 404
    return response

@app.errorhandler(500)
def internal_error(error):
    response = jsonify({"error": "Internal server error", "status": 500})
    response.status_code = 500
    return response

@app.errorhandler(401)
def unauthorized(error):
    response = jsonify({"error": "Unauthorized", "status": 401})
    response.status_code = 401
    return response

# ------------------------------
# Debug Routes
# ------------------------------
@app.route("/api/debug/cors-test")
def debug_cors_test():
    """Test endpoint to verify CORS is working"""
    return jsonify({
        "message": "CORS test successful",
        "origin": request.headers.get('Origin'),
        "allowed_origins": FRONTEND_ORIGINS,
        "cors_working": True
    })

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
    
    print(" Football Prediction Platform API Starting...")
    print(f" Port: {port}")
    print(f" Allowed Origins: {FRONTEND_ORIGINS}")
    print(f" CORS: ENABLED")
    print(f" Credentials: SUPPORTED")
    print(f" Endpoints: /api/health, /ping, /api/debug/cors-test")
    print(" Server ready!")
    
    serve(app, host="0.0.0.0", port=port)