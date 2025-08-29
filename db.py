import psycopg2
from psycopg2.extras import RealDictCursor
from flask import g, has_app_context
import os
from dotenv import load_dotenv

load_dotenv()

def get_db():
    conn_params = {
        "host": os.getenv("DB_HOST", "aws-0-eu-north-1.pooler.supabase.com"),
        "port": os.getenv("DB_PORT", "6543"),  # Pooler port
        "dbname": os.getenv("DB_NAME", "postgres"),
        "user": os.getenv("DB_USER"),  # e.g. postgres.jcfdrtzfqsiemsabaiwl
        "password": os.getenv("DB_PASSWORD"),
        "cursor_factory": RealDictCursor
    }

    if has_app_context():
        if 'db' not in g:
            g.db = psycopg2.connect(**conn_params)
        return g.db
    else:
        # For scripts like fetch_fixtures.py
        return psycopg2.connect(**conn_params)

def close_db(e=None):
    if has_app_context():
        db = g.pop('db', None)
        if db is not None:
            db.close()
