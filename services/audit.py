"""
Admin-visible audit log (rebuild plan, Section 8 / Phase 6).

This is deliberately distinct from the PUBLIC audit log already built in
Phase 2/3 (routes/treasurer.py's /audit-log, visible to every member --
just fee/deadline/surcharge-rule changes and exception grants). This one
is broader (any admin or Treasurer action) and narrower in visibility
(admin only, per the plan).

Scope note: this logs the highest-value, most consequential actions --
user approve/reject/delete, treasurer role grant/revoke, loan
approve/reject/disburse, and season close -- not literally every state
change in the app (e.g. individual savings-transaction confirmations
aren't logged here since they already have their own confirmed_by/
confirmed_at columns and a Treasurer-visible queue). Worth expanding if
a specific gap turns out to matter in practice.
"""
from db import get_db


def log_action(actor_id, action, target_type=None, target_id=None, metadata=None):
    """Fire-and-forget: a logging failure should never break the action
    it's describing, so this swallows its own errors."""
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_log (actor_id, action, target_type, target_id, metadata)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (actor_id, action, target_type, str(target_id) if target_id is not None else None, metadata),
            )
            conn.commit()
    except Exception as e:
        print(f"audit log write failed (non-fatal): {e}")


def get_audit_log(limit=200):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id, a.action, a.target_type, a.target_id, a.metadata, a.created_at,
                   u.username AS actor_username
            FROM audit_log a
            LEFT JOIN users u ON u.id = a.actor_id
            ORDER BY a.created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()
