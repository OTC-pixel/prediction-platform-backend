"""
Single source of truth for database access.

Design:
- One pooled psycopg2 connection pool, created once at import time.
- Every consumer (routes, services, and background jobs) gets a connection
  through get_db(), and NEVER calls conn.close() directly -- Flask's
  teardown_appcontext (registered in app.py) returns it to the pool
  automatically at the end of the request/app-context block.
- Background jobs (the scheduler) are required to run inside an explicit
  `with app.app_context():` block so this same pooling story applies to
  them too -- there is no separate "unpooled" path anywhere in the app.
"""

from flask import has_app_context, g
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
import os
from dotenv import load_dotenv

load_dotenv()

DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "5"))
DB_POOL_MIN = int(os.getenv("DB_POOL_MIN", "1"))
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "10"))

conn_params = {
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT"),
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "cursor_factory": RealDictCursor,
    # Without this, a slow/unreachable DB hangs the whole app at import
    # time with zero log output -- confirmed live during testing.
    "connect_timeout": DB_CONNECT_TIMEOUT,
}

required = ["host", "dbname", "user", "password"]
missing = [k for k in required if not conn_params.get(k)]
if missing:
    raise RuntimeError(
        f"Missing required DB config in .env: {', '.join(missing)}"
    )

db_pool = psycopg2.pool.SimpleConnectionPool(DB_POOL_MIN, DB_POOL_MAX, **conn_params)


def get_db():
    """
    Get the app-context-scoped pooled connection.

    Must be called inside an application context -- a real HTTP request,
    or an explicit `with app.app_context():` block for background jobs.
    Raises loudly instead of silently falling back to an untracked
    connection, so pooling behavior is never ambiguous.
    """
    if not has_app_context():
        raise RuntimeError(
            "get_db() called outside of an application context. "
            "Background jobs must run inside `with app.app_context():`."
        )
    if "db" not in g:
        g.db = db_pool.getconn()
    return g.db


def close_db(e=None):
    """
    Return the app-context-scoped connection to the pool.
    Registered on app.teardown_appcontext -- nothing else should call this.
    """
    db = g.pop("db", None)
    if db is not None:
        db_pool.putconn(db)
