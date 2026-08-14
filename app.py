import os
import sys
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from routes.auth import auth_bp
from routes.admin import admin_bp
from routes.predictions import predictions_bp
from routes.leaderboard import leaderboard_bp
from routes.fixtures import fixtures_bp
from routes.results import results_bp
from routes.treasurer import treasurer_bp
from routes.savings import savings_bp
from db import close_db
from scheduler import start_scheduler

load_dotenv()

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ------------------------------
# CORS Configuration -- origins come from .env, not hardcoded, so the
# same code deploys to any environment (dev/staging/prod) without edits.
# ------------------------------
FRONTEND_ORIGINS = [
    o.strip() for o in os.environ.get("FRONTEND_ORIGINS", "").split(",") if o.strip()
]
if not FRONTEND_ORIGINS:
    raise RuntimeError("FRONTEND_ORIGINS is not set in .env (comma-separated list)")

CORS(
    app,
    origins=FRONTEND_ORIGINS,
    supports_credentials=True,
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
)


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
        "cors_enabled": True
    })


@app.route("/")
def home():
    return jsonify({
        "message": "Football Ladder API",
        "version": "2.0.0"
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
app.register_blueprint(treasurer_bp, url_prefix="/api/treasurer")
app.register_blueprint(savings_bp, url_prefix="/api/savings")

# ------------------------------
# Scheduler -- jobs run in-process inside an app context (see scheduler.py)
# ------------------------------
start_scheduler(app)


@app.errorhandler(404)
def not_found(error):
    logger.warning("404: %s %s", request.method, request.path)
    return jsonify({"error": "Endpoint not found", "status": 404}), 404


@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Internal server error", "status": 500}), 500


@app.teardown_appcontext
def teardown_db(exception):
    close_db()


if __name__ == "__main__":
    from waitress import serve
    port = int(os.environ.get("PORT", 5000))

    logger.info("Server starting")
    logger.info("Allowed origins: %s", FRONTEND_ORIGINS)

    serve(app, host="0.0.0.0", port=port)
