# services/db_direct.py
import psycopg2
import os
from dotenv import load_dotenv
load_dotenv()

def get_direct_connection():
    return psycopg2.connect(
        host=os.getenv('DB_HOST'),
        port=os.getenv('DB_PORT'),
        dbname=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
        sslmode='require'  # required for Supabase
    )
