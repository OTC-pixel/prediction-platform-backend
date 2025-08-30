from flask import has_app_context, g
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
import os
from dotenv import load_dotenv

load_dotenv()

conn_params = {
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT"),
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "cursor_factory": RealDictCursor
}

# Create pool once
db_pool = psycopg2.pool.SimpleConnectionPool(
    1, 10,  # min and max connections
    **conn_params
)

def get_db():
    """Get a connection from the pool."""
    if has_app_context():
        if 'db' not in g:
            g.db = db_pool.getconn()
        return g.db
    else:
        # For scripts not running in Flask context
        return db_pool.getconn()

def close_db(e=None):
    """Return connection to the pool."""
    if has_app_context():
        db = g.pop('db', None)
        if db is not None:
            db_pool.putconn(db)
