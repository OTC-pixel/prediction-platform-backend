"""
Commitment fee & prediction-eligibility logic (rebuild plan, Section 3).

Design note on the "matchday count" rule:
The plan gates by *matchdays since the deadline*, but the deadline is a
date/time while the rest of the app tracks progress as a matchday number
(matchday_tracker.current_matchday). To make that concrete without adding
a full season/schedule model, we capture `deadline_matchday` on the active
commitment_fee_config the first time anyone checks eligibility after the
deadline has passed -- it's "whatever matchday was current when the
deadline first came due". From then on:
  - matchdays_since_deadline = current_matchday - deadline_matchday
  - an exception, once granted, is valid for exactly one matchday: the
    matchday that was current/upcoming at the moment it was granted. It
    stops applying as soon as current_matchday moves past that.
  - once matchdays_since_deadline > 2, the grant endpoint itself refuses
    to create a new exception, even for the Treasurer.
"""
from datetime import datetime, timezone
from db import get_db


def _now():
    return datetime.now(timezone.utc)


def _current_matchday(cur):
    cur.execute("SELECT current_matchday FROM matchday_tracker WHERE id = 1")
    row = cur.fetchone()
    return (row["current_matchday"] if row else 0) or 0


def _get_active_config(cur):
    cur.execute(
        "SELECT * FROM commitment_fee_config WHERE active = TRUE ORDER BY id DESC LIMIT 1"
    )
    return cur.fetchone()


def _ensure_deadline_matchday_captured(cur, config):
    """
    Once the deadline has passed, freeze the matchday count it passed on,
    so later matchday progress doesn't retroactively change how many
    matchdays a user has been overdue for. No-op if not past deadline yet,
    or already captured.
    """
    if config is None or config["deadline_matchday"] is not None:
        return config
    if _now() < config["deadline"]:
        return config
    matchday = _current_matchday(cur)
    cur.execute(
        "UPDATE commitment_fee_config SET deadline_matchday = %s WHERE id = %s RETURNING *",
        (matchday, config["id"]),
    )
    return cur.fetchone()


# ---------- Treasurer role grant/revoke (admin action) ----------

def set_treasurer(username, is_treasurer):
    username = username.strip()
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET is_treasurer = %s WHERE username = %s",
            (1 if is_treasurer else 0, username),
        )
        conn.commit()
        return cur.rowcount > 0


def set_secretary(username, is_secretary):
    username = username.strip()
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET is_secretary = %s WHERE username = %s",
            (1 if is_secretary else 0, username),
        )
        conn.commit()
        return cur.rowcount > 0


# ---------- Fee config ----------

def set_fee_config(amount, deadline_iso, set_by_user_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("UPDATE commitment_fee_config SET active = FALSE WHERE active = TRUE")
        cur.execute(
            """
            INSERT INTO commitment_fee_config (amount, deadline, set_by, active)
            VALUES (%s, %s, %s, TRUE)
            RETURNING *
            """,
            (amount, deadline_iso, set_by_user_id),
        )
        row = cur.fetchone()
        conn.commit()
        return row


def get_active_fee_config():
    conn = get_db()
    with conn.cursor() as cur:
        config = _get_active_config(cur)
        config = _ensure_deadline_matchday_captured(cur, config)
        conn.commit()
        return config


# ---------- Payment status ----------

def get_payment_status_list():
    """Approved users joined with their has_paid status, for the Treasurer's view."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.id, u.username, u.full_name,
                   COALESCE(s.has_paid, FALSE) AS has_paid,
                   s.confirmed_at
            FROM users u
            LEFT JOIN commitment_fee_status s ON s.user_id = u.id
            WHERE u.is_approved = 1
            ORDER BY u.username
            """
        )
        return cur.fetchall()


def mark_paid(user_id, has_paid, confirmed_by_user_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO commitment_fee_status (user_id, has_paid, confirmed_by, confirmed_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET has_paid = EXCLUDED.has_paid,
                confirmed_by = EXCLUDED.confirmed_by,
                confirmed_at = EXCLUDED.confirmed_at
            """,
            (user_id, bool(has_paid), confirmed_by_user_id),
        )
        conn.commit()
        return True


# ---------- Exceptions ----------

def grant_exception(user_id, granted_by_user_id):
    conn = get_db()
    with conn.cursor() as cur:
        config = _get_active_config(cur)
        if config is None:
            return False, "No commitment fee configured"
        config = _ensure_deadline_matchday_captured(cur, config)

        current_matchday = _current_matchday(cur)

        if config["deadline_matchday"] is not None:
            matchdays_since_deadline = current_matchday - config["deadline_matchday"]
            if matchdays_since_deadline > 2:
                conn.commit()
                return False, "Past the 2-matchday hard cutoff -- no exception possible"

        # Unlocks exactly the current/next matchday, not a blanket pass.
        cur.execute(
            """
            INSERT INTO commitment_fee_exceptions (user_id, granted_for_matchday, granted_by)
            VALUES (%s, %s, %s)
            RETURNING *
            """,
            (user_id, current_matchday, granted_by_user_id),
        )
        row = cur.fetchone()
        conn.commit()
        return True, row


def get_exceptions_log():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.id, u.username, e.granted_for_matchday, g.username AS granted_by, e.granted_at
            FROM commitment_fee_exceptions e
            JOIN users u ON u.id = e.user_id
            LEFT JOIN users g ON g.id = e.granted_by
            ORDER BY e.granted_at DESC
            """
        )
        return cur.fetchall()


def get_fee_config_history():
    """Every fee/deadline change ever set, newest first -- part of the
    public audit trail required by the rebuild plan (Section 2, point 2)."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.amount, c.deadline, c.active, c.created_at, u.username AS set_by
            FROM commitment_fee_config c
            LEFT JOIN users u ON u.id = c.set_by
            ORDER BY c.created_at DESC
            """
        )
        return cur.fetchall()


# ---------- Eligibility (used by the predictions submit path) ----------

def get_user_eligibility(user_id):
    """
    Returns a dict:
      {eligible: bool, reason: str, has_paid: bool, deadline_passed: bool,
       exception_active: bool, matchdays_since_deadline: int|None}
    A user with no fee configured at all is always eligible (nothing to
    gate against yet).
    """
    conn = get_db()
    with conn.cursor() as cur:
        config = _get_active_config(cur)
        if config is None:
            return {
                "eligible": True,
                "reason": "No commitment fee configured",
                "has_paid": None,
                "deadline_passed": False,
                "exception_active": False,
                "matchdays_since_deadline": None,
            }

        config = _ensure_deadline_matchday_captured(cur, config)
        conn.commit()

        cur.execute(
            "SELECT has_paid FROM commitment_fee_status WHERE user_id = %s", (user_id,)
        )
        row = cur.fetchone()
        has_paid = bool(row["has_paid"]) if row else False

        deadline_passed = _now() >= config["deadline"]

        if has_paid:
            return {
                "eligible": True,
                "reason": "Paid",
                "has_paid": True,
                "deadline_passed": deadline_passed,
                "exception_active": False,
                "matchdays_since_deadline": None,
            }

        if not deadline_passed:
            return {
                "eligible": True,
                "reason": "Grace period -- before deadline",
                "has_paid": False,
                "deadline_passed": False,
                "exception_active": False,
                "matchdays_since_deadline": None,
            }

        current_matchday = _current_matchday(cur)
        matchdays_since_deadline = (
            current_matchday - config["deadline_matchday"]
            if config["deadline_matchday"] is not None
            else 0
        )

        cur.execute(
            """
            SELECT 1 FROM commitment_fee_exceptions
            WHERE user_id = %s AND granted_for_matchday = %s
            """,
            (user_id, current_matchday),
        )
        exception_active = cur.fetchone() is not None

        if exception_active:
            return {
                "eligible": True,
                "reason": "Treasurer exception active for this matchday",
                "has_paid": False,
                "deadline_passed": True,
                "exception_active": True,
                "matchdays_since_deadline": matchdays_since_deadline,
            }

        return {
            "eligible": False,
            "reason": "Unpaid and past deadline -- ask the Treasurer for an exception",
            "has_paid": False,
            "deadline_passed": True,
            "exception_active": False,
            "matchdays_since_deadline": matchdays_since_deadline,
        }
