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
        # An offer was priced against the old fee, so it must not outlive it.
        cur.execute("UPDATE commitment_fee_deduction_offer SET active = FALSE WHERE active = TRUE")
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
            SELECT u.id, u.username, u.full_name, u.team,
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


# ---------- Eligibility (used by the predictions submit path, and by the
# frontend's login-time deadline notification / full-app lockdown) ----------

# Calendar-day thresholds for the login-time notification banner. Purely a
# display concern -- separate from the matchday-based exception/lockout math
# above, which stays untouched. "none" also covers the already-locked case,
# since a full lockdown message replaces the countdown banner rather than
# stacking with it.
_RISK_LOW_MAX_DAYS = 14   # 8-14 days out
_RISK_MEDIUM_MAX_DAYS = 7  # 3-7 days out
_RISK_HIGH_MAX_DAYS = 2    # 0-2 days out


def _risk_level(days_remaining, locked):
    if locked:
        return "locked"
    if days_remaining is None or days_remaining > _RISK_LOW_MAX_DAYS:
        return "none"
    if days_remaining > _RISK_MEDIUM_MAX_DAYS:
        return "low"
    if days_remaining > _RISK_HIGH_MAX_DAYS:
        return "medium"
    return "high"


def get_user_eligibility(user_id):
    """
    Returns a dict:
      {eligible: bool, reason: str, has_paid: bool, deadline_passed: bool,
       exception_active: bool, matchdays_since_deadline: int|None,
       deadline: str|None (ISO), days_remaining: int|None (negative once
       overdue), risk_level: "none"|"low"|"medium"|"high"|"locked",
       locked: bool (== not eligible; kept as an explicit alias so the
       frontend doesn't have to infer lockdown from `eligible`)}
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
                "deadline": None,
                "days_remaining": None,
                "risk_level": "none",
                "locked": False,
            }

        config = _ensure_deadline_matchday_captured(cur, config)
        conn.commit()

        deadline_iso = config["deadline"].isoformat()
        days_remaining = (config["deadline"] - _now()).days

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
                "deadline": deadline_iso,
                "days_remaining": days_remaining,
                "risk_level": "none",
                "locked": False,
            }

        if not deadline_passed:
            return {
                "eligible": True,
                "reason": "Grace period -- before deadline",
                "has_paid": False,
                "deadline_passed": False,
                "exception_active": False,
                "matchdays_since_deadline": None,
                "deadline": deadline_iso,
                "days_remaining": days_remaining,
                "risk_level": _risk_level(days_remaining, locked=False),
                "locked": False,
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
                "deadline": deadline_iso,
                "days_remaining": days_remaining,
                "risk_level": "high",
                "locked": False,
            }

        return {
            "eligible": False,
            "reason": "Payment deadline hit -- pay your commitment fee from savings below, or contact an Admin",
            "has_paid": False,
            "deadline_passed": True,
            "exception_active": False,
            "matchdays_since_deadline": matchdays_since_deadline,
            "deadline": deadline_iso,
            "days_remaining": days_remaining,
            "risk_level": "locked",
            "locked": True,
        }


# ------------------------------------------------------------------
# Commitment-fee-via-savings (addendum to the rebuild plan above).
#
# Per a later governance change, commitment-fee handling (config,
# marking paid, exceptions) moved from Treasurer to Admin -- Treasurer
# is now read-only on this; see routes/treasurer.py's role_required
# decorators. This section is Admin's "offer locked users a way to
# pay from their own savings instead" path, which goes through the
# generic 3-party approval engine (services/approvals.py): Secretary
# and Treasurer sign off (either order), then Admin gives final
# approval, which actually performs the deduction.
#
# Important: the deduction amount is a SINGLE TOTAL the admin sets, not
# "fee + a separately computed surcharge". The app does not calculate
# or enforce any transfer/handling charge on top of the real fee --
# commitment-fee money and savings money sit in different physical
# accounts, so moving funds between them can have a real transfer
# cost, and the admin is simply reminded of that (see
# get_active_deduction_offer's `note` field and the UI that collects
# it) so they don't forget to fold it into the one amount they set.
# This is advice, not a rule the system computes or checks.
# ------------------------------------------------------------------

def set_deduction_offer(amount, note, set_by_user_id):
    """
    Sets the one global amount+note locked users are offered to pay
    their commitment fee out of savings this cycle. Tied to whichever
    commitment_fee_config is currently active; deactivates any prior
    offer the same way commitment_fee_config/savings_config do.
    """
    conn = get_db()
    with conn.cursor() as cur:
        config = _get_active_config(cur)
        if config is None:
            return False, "No commitment fee configured yet -- set that first"
        if float(amount) < float(config['amount']):
            return False, (
                f"The amount can't be less than the commitment fee itself "
                f"({float(config['amount']):,.0f} UGX). Anything above the fee is the extra "
                f"charge for moving the money between accounts."
            )

        cur.execute(
            "UPDATE commitment_fee_deduction_offer SET active = FALSE WHERE active = TRUE"
        )
        cur.execute(
            """
            INSERT INTO commitment_fee_deduction_offer (config_id, amount, note, set_by)
            VALUES (%s, %s, %s, %s)
            RETURNING *
            """,
            (config['id'], amount, note, set_by_user_id),
        )
        row = cur.fetchone()
        conn.commit()
        return True, row


def get_active_deduction_offer():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.* FROM commitment_fee_deduction_offer o
            JOIN commitment_fee_config c ON c.id = o.config_id
            WHERE o.active = TRUE AND c.active = TRUE
            ORDER BY o.id DESC LIMIT 1
            """
        )
        return cur.fetchone()


def request_commitment_fee_deduction(user_id):
    """
    A locked user asking to pay their commitment fee out of their own
    savings balance, at the currently offered amount. Validates
    up-front (offer exists, user is actually locked/unpaid, balance
    covers it) before creating the approval request, so the 3-party
    chain is never started on a request that can't succeed.
    """
    from services.approvals import create_request
    from services.savings import get_user_savings_balance

    conn = get_db()
    with conn.cursor() as cur:
        offer = get_active_deduction_offer()
        if not offer:
            return False, "No savings-deduction option has been set up yet"

        cur.execute("SELECT username, is_approved FROM users WHERE id = %s", (user_id,))
        user_row = cur.fetchone()
        if not user_row or not user_row['is_approved']:
            return False, "User not found"

        eligibility = get_user_eligibility(user_id)
        if eligibility.get('has_paid'):
            return False, "Your commitment fee is already marked as paid"
        if not eligibility.get('locked'):
            return False, "Your account isn't locked yet -- nothing to pay via savings"

        # One open request at a time per user, so re-clicking doesn't
        # pile up duplicate approval chains.
        cur.execute(
            """
            SELECT id FROM approval_requests
            WHERE request_type = 'commitment_fee_savings_deduction'
              AND status = 'pending'
              AND payload->>'user_id' = %s
            """,
            (str(user_id),),
        )
        if cur.fetchone():
            return False, "You already have a request waiting on approval"

        balance = get_user_savings_balance(user_id)
        if float(balance) < float(offer['amount']):
            return False, (
                f"Your savings balance ({float(balance):,.0f} UGX) doesn't cover "
                f"the {float(offer['amount']):,.0f} UGX needed"
            )

        config = _get_active_config(cur)
        payload = {
            'user_id': user_id,
            'username': user_row['username'],
            'amount': str(offer['amount']),
            'fee_amount': str(config['amount']) if config else None,
            'note': offer['note'],
        }
        request_id = create_request('commitment_fee_savings_deduction', payload, user_id)
        return True, request_id


def complete_commitment_fee_deduction(payload, request_id, actor_id):
    """
    The approval engine's on_complete callback for the
    commitment_fee_savings_deduction workflow -- runs once, the moment
    Admin gives the final sign-off. Performs the actual deduction and
    unlocks the account; actor_id is the admin who just approved it, so
    they're recorded as having confirmed the payment (same as a normal
    manual mark-paid).
    """
    from services.savings import record_admin_deduction

    user_id = int(payload['user_id'])
    amount = float(payload['amount'])

    record_admin_deduction(
        user_id, amount, actor_id,
        idempotency_key=f"commitment-deduction-approval-{request_id}",
    )
    mark_paid(user_id, True, actor_id)


def get_my_deduction_request(user_id):
    """
    Latest savings-deduction request this user made, with just enough
    progress info for the user's own screen (no workflow internals):
    status, which stage it's at, and which roles have approved so far.
    Returns None if they've never made one.
    """
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, status, current_stage, created_at
            FROM approval_requests
            WHERE request_type = 'commitment_fee_savings_deduction'
              AND payload->>'user_id' = %s
            ORDER BY id DESC LIMIT 1
            """,
            (str(user_id),),
        )
        req = cur.fetchone()
        if not req:
            return None

        cur.execute(
            "SELECT role FROM approval_actions WHERE request_id = %s AND decision = 'approve'",
            (req['id'],),
        )
        approved_roles = sorted({r['role'] for r in cur.fetchall()})

        return {
            'id': req['id'],
            'status': req['status'],
            'current_stage': req['current_stage'],
            'approved_roles': approved_roles,
            'created_at': req['created_at'].isoformat() if req['created_at'] else None,
        }


def get_deduction_offer_view():
    """
    The offer as a member (or Admin) sees it: the single total the Admin
    set, the real commitment fee, and the difference between them --
    the extra charge, which exists because the fee and savings live in
    different accounts. The extra charge is DERIVED here for display; the
    Admin only ever sets the total (see the note above set_deduction_offer).
    """
    offer = get_active_deduction_offer()
    if not offer:
        return None
    conn = get_db()
    with conn.cursor() as cur:
        config = _get_active_config(cur)
    fee = float(config['amount']) if config else None
    amount = float(offer['amount'])
    extra = max(0.0, amount - fee) if fee is not None else None
    return {
        'id': offer['id'],
        'amount': str(offer['amount']),
        'note': offer['note'],
        'fee_amount': None if fee is None else str(config['amount']),
        'extra_charge': None if extra is None else f"{extra:.2f}",
        'created_at': offer['created_at'].isoformat(),
    }


def can_complete_commitment_fee_deduction(payload):
    """
    Re-checked at the moment of the FINAL approval. Days can pass between
    a member asking and the Admin signing off; their savings may have
    changed, or they may have been marked paid another way. Returns
    (ok, message) -- a message here is shown to the Admin and nothing is
    recorded, so the request simply stays pending until they reject it.
    """
    from services.savings import get_user_savings_balance

    user_id = int(payload['user_id'])
    amount = float(payload['amount'])
    name = payload.get('username', 'This member')

    if get_user_eligibility(user_id).get('has_paid'):
        return False, (
            f"{name} is already marked as paid, so there is nothing to deduct. "
            f"Reject this request."
        )
    balance = float(get_user_savings_balance(user_id))
    if balance < amount:
        return False, (
            f"{name}'s savings ({balance:,.0f} UGX) no longer cover the {amount:,.0f} UGX "
            f"needed, so this can't be completed. Reject it, or ask them to top up and request again."
        )
    return True, None


def get_commitment_overview():
    """
    Who is (or is about to be) locked, computed with the SAME rule as
    get_user_eligibility so the Admin Home card can never disagree with
    what members actually experience: unpaid + past the deadline + no
    exception for the current matchday. Before the deadline nothing is
    locked yet, so `unpaid` lists who would be.
    """
    conn = get_db()
    with conn.cursor() as cur:
        config = _get_active_config(cur)
        if config is None:
            return {'configured': False}

        now = _now()
        passed = now >= config['deadline']
        hours_remaining = (config['deadline'] - now).total_seconds() / 3600

        members = [{
            'user_id': r['id'], 'username': r['username'],
            'full_name': r['full_name'], 'team': r.get('team'),
        } for r in get_payment_status_list() if not r['has_paid']]

        excused_ids = set()
        if passed and members:
            matchday = _current_matchday(cur)
            cur.execute(
                "SELECT user_id FROM commitment_fee_exceptions WHERE granted_for_matchday = %s",
                (matchday,),
            )
            excused_ids = {row['user_id'] for row in cur.fetchall()}

        locked = [m for m in members if passed and m['user_id'] not in excused_ids]
        excused = [m for m in members if passed and m['user_id'] in excused_ids]

        return {
            'configured': True,
            'fee_amount': str(config['amount']),
            'deadline': config['deadline'].isoformat(),
            'deadline_passed': passed,
            'hours_remaining': round(hours_remaining, 1),
            'locked': locked,
            'excused': excused,
            'unpaid': [] if passed else members,
        }
