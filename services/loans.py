r"""
Loans (rebuild plan, Section 6).

Lifecycle: pending -> endorsed -> approved -> disbursed -> repaid
                 \-> rejected (Treasurer can reject from pending/endorsed/approved)

"One active loan at a time" means: no loan in ('pending', 'endorsed',
'approved', 'disbursed') for that user. A rejected or fully repaid loan
doesn't block a new request.

The fee is captured at DISBURSEMENT, not at request time: the rules the
Treasurer has configured at that moment are copied onto the loan
(`loans.rules`, `loans.interest_rate`, `loans.due_at`), so a later rule
change never rewrites a loan that is already out.

MoKash-style engine: one flat facilitation fee per term (not a daily or
monthly rate). On top of principal + that fee, an append-only ledger
(`loan_charges`) records top-up principal, top-up fees, extension fees
and the late fee. Outstanding balance is always derived:
principal + base fee + ledger - confirmed repayments, never stored. A
loan closes (status -> 'repaid') the moment a confirmation brings that
number to <= 0.

Late fee: applied once by `process_overdue_loans` after the due date.
A loan counts as repaid on time when everything was SUBMITTED by the due
date, even if the Treasurer confirmed it afterwards.

Interest-collected aggregate (for the public group-fund figure): counted
only for fully repaid loans (principal*rate collected in full once
closed), not prorated across partial repayments on still-open loans.
Simpler and unambiguous; documented here since it's a real simplification
of "interest collected so far" rather than the only possible definition.
"""
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from psycopg2.extras import Json
from db import get_db
from services.savings import get_user_savings_balance

REQUIRED_ENDORSEMENTS = 4
ACTIVE_STATUSES = ('pending', 'endorsed', 'approved', 'disbursed')
CAP_PERCENT = 75
FEE_KINDS = ('topup_fee', 'extension_fee', 'late_fee')

# Rules applied to a loan that has none stored (older loans), with every
# fee type falling back to that loan's own fee.
DEFAULT_RULES = {
    'term_days': 30, 'topup_window_days': 20, 'max_topups': 2,
    'topup_fee_percent': 0, 'extension_fee_percent': 0, 'extension_days': 30,
    'max_extensions': 1, 'late_fee_percent': 0, 'default_after_days': 30,
}


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _d(x):
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _pct(amount, percent):
    """percent of amount, to the cent."""
    return (_d(amount) * _d(percent) / 100).quantize(Decimal('0.01'), ROUND_HALF_UP)


def _ceil_days(delta):
    return math.ceil(delta.total_seconds() / 86400)


def _get_active_loan_config(cur):
    cur.execute(
        "SELECT * FROM loan_config WHERE active = TRUE ORDER BY id DESC LIMIT 1"
    )
    return cur.fetchone()


# ---------- Config ----------

CONFIG_FIELDS = (
    'interest_rate', 'term_days', 'topup_window_days', 'max_topups',
    'topup_fee_percent', 'extension_fee_percent', 'extension_days',
    'max_extensions', 'late_fee_percent', 'default_after_days',
    'starter_limit', 'limit_step_percent',
)


def set_loan_config(values, set_by_user_id):
    """`values` holds any of CONFIG_FIELDS; anything omitted takes the
    column default (the optional fee overrides and starter_limit stay
    NULL)."""
    cols = [c for c in CONFIG_FIELDS if c in values]
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("UPDATE loan_config SET active = FALSE WHERE active = TRUE")
        cur.execute(
            f"""
            INSERT INTO loan_config ({", ".join(cols)}, set_by, active)
            VALUES ({", ".join(["%s"] * len(cols))}, %s, TRUE)
            RETURNING *
            """,
            [values[c] for c in cols] + [set_by_user_id],
        )
        row = cur.fetchone()
        conn.commit()
        return row


def get_active_loan_config():
    conn = get_db()
    with conn.cursor() as cur:
        return _get_active_loan_config(cur)


def effective_rules(config):
    """The rules a loan paid out under `config` will carry. A fee type
    left blank follows the main fee."""
    fee = config["interest_rate"]

    def fee_or(v):
        return float(v if v is not None else fee)

    return {
        'term_days': int(config['term_days']),
        'topup_window_days': int(config['topup_window_days']),
        'max_topups': int(config['max_topups']),
        'topup_fee_percent': fee_or(config['topup_fee_percent']),
        'extension_fee_percent': fee_or(config['extension_fee_percent']),
        'extension_days': int(config['extension_days']),
        'max_extensions': int(config['max_extensions']),
        'late_fee_percent': fee_or(config['late_fee_percent']),
        'default_after_days': int(config['default_after_days']),
    }


def _rules_of(loan):
    stored = loan.get('rules')
    if stored:
        return {**DEFAULT_RULES, **stored}
    fee = float(loan.get('interest_rate') or 0)
    return {**DEFAULT_RULES, 'topup_fee_percent': fee,
            'extension_fee_percent': fee, 'late_fee_percent': fee}


# ---------- Derived state ----------

def _base_fee(loan):
    return (loan["principal"] * loan["interest_rate"] / 100) if loan["interest_rate"] else Decimal(0)


def _charges(cur, loan_id):
    cur.execute(
        "SELECT * FROM loan_charges WHERE loan_id = %s ORDER BY created_at ASC, id ASC",
        (loan_id,),
    )
    return cur.fetchall()


def _total_owed(cur, loan):
    """principal + the base fee + every ledger line (top-up principal,
    top-up / extension / late fees)."""
    cur.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM loan_charges WHERE loan_id = %s",
        (loan["id"],),
    )
    return loan["principal"] + _base_fee(loan) + cur.fetchone()["total"]


def _confirmed_repaid(cur, loan_id):
    cur.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM loan_repayments WHERE loan_id = %s AND status = 'confirmed'",
        (loan_id,),
    )
    return cur.fetchone()["total"]


def loan_outstanding(cur, loan):
    """Still to pay on a loan, as the one definition everyone shares
    (the season export uses this too)."""
    return _total_owed(cur, loan) - _confirmed_repaid(cur, loan["id"])


def _derive(cur, loan, now=None):
    """Everything computed about a loan that has been paid out."""
    now = now or _now()
    rules = _rules_of(loan)
    charges = _charges(cur, loan["id"])
    topup_principal = sum((c["amount"] for c in charges if c["kind"] == "topup"), Decimal(0))
    fee_extra = sum((c["amount"] for c in charges if c["kind"] in FEE_KINDS), Decimal(0))
    principal_total = loan["principal"] + topup_principal
    base_fee = _base_fee(loan)
    total_owed = principal_total + base_fee + fee_extra

    cur.execute(
        "SELECT status, COALESCE(SUM(amount), 0) AS total FROM loan_repayments WHERE loan_id = %s GROUP BY status",
        (loan["id"],),
    )
    by_status = {r["status"]: r["total"] for r in cur.fetchall()}
    repaid = by_status.get("confirmed", Decimal(0))
    awaiting = by_status.get("pending", Decimal(0))
    outstanding = total_owed - repaid

    due_at = _aware(loan.get("due_at"))
    disbursed_at = _aware(loan.get("disbursed_at"))
    days_spent = max((now - disbursed_at).days, 0) if disbursed_at else 0
    days_left = _ceil_days(due_at - now) if due_at else None
    # Full days late, but never 0 once the due moment has passed: a loan a
    # few hours late is already "1 day late" (and already carries the fee).
    days_overdue = max((now - due_at).days, 1) if due_at and now > due_at else 0

    if outstanding <= 0 or due_at is None or days_overdue == 0:
        standing = "good"
    elif days_overdue >= rules["default_after_days"]:
        standing = "defaulted"
    else:
        standing = "late"

    cur.execute(
        "SELECT status, COUNT(*) AS n FROM loan_topups WHERE loan_id = %s GROUP BY status",
        (loan["id"],),
    )
    topup_counts = {r["status"]: r["n"] for r in cur.fetchall()}

    return {
        "rules": rules,
        "charges": charges,
        "principal_total": principal_total,
        "base_fee": base_fee,
        "fee_total": base_fee + fee_extra,
        "total_owed": total_owed,
        "repaid": repaid,
        "awaiting": awaiting,
        "outstanding": outstanding,
        "due_at": due_at,
        "days_spent": days_spent,
        "days_left": days_left,
        "days_overdue": days_overdue,
        "standing": standing,
        "topups_used": topup_counts.get("approved", 0),
        "topup_pending": topup_counts.get("pending", 0),
        "extensions_used": sum(1 for c in charges if c["kind"] == "extension_fee"),
    }


# ---------- Borrowing limit ----------

def _limit_info(cur, user_id, savings_balance, now=None):
    """What this member may borrow.

    limit = the lower of (a) CAP_PERCENT of their savings and (b) their
    tier limit. The tier limit starts at the Treasurer's `starter_limit`
    and steps up by `limit_step_percent` for every loan repaid on time
    (each late repayment takes one step back). With no starter limit set
    the tier is off and only the savings cap applies.
    """
    now = now or _now()
    cfg = _get_active_loan_config(cur)
    savings_cap = max(int(float(savings_balance) * CAP_PERCENT / 100), 0)

    cur.execute("SELECT * FROM loans WHERE user_id = %s AND status = 'repaid'", (user_id,))
    on_time = late = 0
    for l in cur.fetchall():
        was_late = l.get("late_fee_applied_at") is not None or (
            l.get("due_at") is not None and l.get("settled_at") is not None
            and _aware(l["settled_at"]) > _aware(l["due_at"])
        )
        if was_late:
            late += 1
        else:
            on_time += 1
    level = max(0, on_time - late)

    tier_limit = None
    starter = cfg["starter_limit"] if cfg else None
    if starter:
        step = _d(cfg["limit_step_percent"]) / 100
        raw = _d(starter) * (1 + step) ** level
        tier_limit = int(raw // 1000 * 1000) if raw >= 1000 else int(raw)

    limit = savings_cap if tier_limit is None else min(savings_cap, tier_limit)

    cur.execute("SELECT * FROM loans WHERE user_id = %s AND status = 'disbursed'", (user_id,))
    active = cur.fetchone()
    used = Decimal(0)
    standing = "good"
    if active:
        d = _derive(cur, active, now)
        used = d["principal_total"]
        standing = d["standing"]

    available = max(Decimal(limit) - used, Decimal(0))
    return {
        "savings_balance": savings_balance,
        "savings_cap": savings_cap,
        "cap_percent": CAP_PERCENT,
        "tier_limit": tier_limit,
        "tier_level": level,
        "on_time_repaid": on_time,
        "late_repaid": late,
        "limit": limit,
        "used": used,
        "available": available,
        "standing": standing,
        # what a NEW loan could be for (zero while one is already out)
        "max_request": 0 if active else limit,
    }


def _limit_reason(lim):
    """One plain sentence on what is setting this member's limit."""
    if lim["tier_limit"] is not None and lim["tier_limit"] < lim["savings_cap"]:
        return "It grows each time you repay a loan on time."
    return f"It is {lim['cap_percent']}% of your savings, so it grows as you save."


def get_borrowing_limit(user_id):
    balance = get_user_savings_balance(user_id)
    conn = get_db()
    with conn.cursor() as cur:
        return _limit_info(cur, user_id, balance)


# ---------- Request ----------

def request_loan(user_id, principal):
    try:
        principal = _d(principal)
    except InvalidOperation:
        return False, "Invalid amount", None
    if principal <= 0:
        return False, "Invalid amount", None

    balance = get_user_savings_balance(user_id)
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM loans WHERE user_id = %s AND status = ANY(%s)",
            (user_id, list(ACTIVE_STATUSES)),
        )
        if cur.fetchone():
            return False, "You already have an active loan", None

        lim = _limit_info(cur, user_id, balance)
        why = _limit_reason(lim)
        if lim["limit"] <= 0:
            return False, f"Not eligible yet: your limit is UGX 0. {why}", None
        if principal > lim["limit"]:
            return False, f"Not eligible for UGX {principal:,.0f}: your limit is UGX {lim['limit']:,}. {why}", None

        cur.execute(
            """
            INSERT INTO loans (user_id, principal, status)
            VALUES (%s, %s, 'pending')
            RETURNING *
            """,
            (user_id, principal),
        )
        row = cur.fetchone()
        conn.commit()
        return True, None, row


# ---------- Endorsement ----------

def endorse_loan(loan_id, endorser_user_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM loans WHERE id = %s FOR UPDATE", (loan_id,))
        loan = cur.fetchone()
        if not loan:
            return False, "Loan not found"
        if loan["status"] != "pending":
            return False, "This loan is no longer open for endorsement"
        if loan["user_id"] == endorser_user_id:
            return False, "You can't endorse your own loan request"

        cur.execute(
            "SELECT 1 FROM loan_endorsements WHERE loan_id = %s AND endorser_user_id = %s",
            (loan_id, endorser_user_id),
        )
        if cur.fetchone():
            return False, "You've already endorsed this loan"

        cur.execute(
            "INSERT INTO loan_endorsements (loan_id, endorser_user_id) VALUES (%s, %s)",
            (loan_id, endorser_user_id),
        )

        cur.execute(
            "SELECT COUNT(*) AS count FROM loan_endorsements WHERE loan_id = %s",
            (loan_id,),
        )
        count = cur.fetchone()["count"]

        if count >= REQUIRED_ENDORSEMENTS:
            cur.execute(
                "UPDATE loans SET status = 'endorsed' WHERE id = %s", (loan_id,)
            )

        conn.commit()
        return True, None


def get_loans_pending_endorsement(viewer_user_id):
    """Loans still in 'pending' -- visible to all members since peer
    endorsement requires visibility. Includes whether the viewer has
    already endorsed, and excludes the viewer's own request."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT l.id, l.user_id, u.username, u.full_name, l.principal, l.requested_at
            FROM loans l
            JOIN users u ON u.id = l.user_id
            WHERE l.status = 'pending' AND l.user_id != %s
            ORDER BY l.requested_at ASC
            """,
            (viewer_user_id,),
        )
        loans = cur.fetchall()
        result = []
        for loan in loans:
            cur.execute(
                "SELECT COUNT(*) AS count FROM loan_endorsements WHERE loan_id = %s",
                (loan["id"],),
            )
            count = cur.fetchone()["count"]
            cur.execute(
                "SELECT 1 FROM loan_endorsements WHERE loan_id = %s AND endorser_user_id = %s",
                (loan["id"], viewer_user_id),
            )
            already_endorsed = cur.fetchone() is not None
            result.append({
                **loan,
                "endorsement_count": count,
                "endorsements_needed": REQUIRED_ENDORSEMENTS,
                "already_endorsed": already_endorsed,
            })
        return result


# ---------- Approval / disbursement / rejection ----------

def get_loans_pending_approval():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT l.*, u.username, u.full_name FROM loans l
            JOIN users u ON u.id = l.user_id
            WHERE l.status = 'endorsed'
            ORDER BY l.requested_at ASC
            """
        )
        return cur.fetchall()


def get_loans_pending_disbursement():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT l.*, u.username, u.full_name FROM loans l
            JOIN users u ON u.id = l.user_id
            WHERE l.status = 'approved'
            ORDER BY l.approved_at ASC
            """
        )
        return cur.fetchall()


def approve_loan(loan_id, approved_by_user_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM loans WHERE id = %s FOR UPDATE", (loan_id,))
        loan = cur.fetchone()
        if not loan:
            return False, "Loan not found"
        if loan["status"] != "endorsed":
            return False, "Loan must reach the endorsement threshold before it can be approved"
        cur.execute(
            "UPDATE loans SET status = 'approved', approved_by = %s, approved_at = NOW() WHERE id = %s",
            (approved_by_user_id, loan_id),
        )
        conn.commit()
        return True, None


def reject_loan(loan_id, rejected_by_user_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM loans WHERE id = %s FOR UPDATE", (loan_id,))
        loan = cur.fetchone()
        if not loan:
            return False, "Loan not found"
        if loan["status"] not in ("pending", "endorsed", "approved"):
            return False, f"Loan is already {loan['status']}"
        cur.execute(
            "UPDATE loans SET status = 'rejected', rejected_by = %s, rejected_at = NOW() WHERE id = %s",
            (rejected_by_user_id, loan_id),
        )
        conn.commit()
        return True, None


def disburse_loan(loan_id, disbursed_by_user_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM loans WHERE id = %s FOR UPDATE", (loan_id,))
        loan = cur.fetchone()
        if not loan:
            return False, "Loan not found"
        if loan["status"] != "approved":
            return False, "Loan must be approved before it can be disbursed"

        config = _get_active_loan_config(cur)
        if config is None:
            return False, "No loan rules have been configured"

        rules = effective_rules(config)
        cur.execute(
            """
            UPDATE loans
            SET status = 'disbursed', interest_rate = %s, disbursed_by = %s, disbursed_at = NOW(),
                rules = %s, due_at = NOW() + make_interval(days => %s)
            WHERE id = %s
            """,
            (config["interest_rate"], disbursed_by_user_id, Json(rules), rules["term_days"], loan_id),
        )
        conn.commit()
        return True, None


# ---------- Repayments ----------

def submit_repayment(loan_id, user_id, amount, idempotency_key):
    if amount is None or float(amount) <= 0:
        return False, "Invalid amount", None

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM loan_repayments WHERE idempotency_key = %s", (idempotency_key,)
        )
        existing = cur.fetchone()
        if existing:
            return True, "Already submitted", existing

        cur.execute("SELECT * FROM loans WHERE id = %s FOR UPDATE", (loan_id,))
        loan = cur.fetchone()
        if not loan:
            return False, "Loan not found", None
        if loan["user_id"] != user_id:
            return False, "This isn't your loan", None
        if loan["status"] != "disbursed":
            return False, "This loan isn't open for repayment", None

        # Never accept more than is still owed. Payments already waiting
        # for the Treasurer count too, otherwise two quick submissions
        # could together overshoot the balance.
        try:
            amount_dec = _d(amount)
        except InvalidOperation:
            return False, "Invalid amount", None
        d = _derive(cur, loan)
        remaining = d["outstanding"] - d["awaiting"]
        if remaining <= 0:
            return False, "Everything you owe is already covered by payments waiting for confirmation", None
        if amount_dec > remaining:
            return False, f"That is more than you still owe. You can pay up to UGX {remaining:,.0f} (payments waiting for confirmation are counted).", None

        cur.execute(
            """
            INSERT INTO loan_repayments (loan_id, amount, idempotency_key)
            VALUES (%s, %s, %s)
            RETURNING *
            """,
            (loan_id, amount_dec, idempotency_key),
        )
        row = cur.fetchone()
        conn.commit()
        return True, None, row


def confirm_repayment(repayment_id, confirmed_by_user_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM loan_repayments WHERE id = %s FOR UPDATE", (repayment_id,))
        repayment = cur.fetchone()
        if not repayment:
            return False, "Repayment not found"
        if repayment["status"] != "pending":
            return False, f"Repayment is already {repayment['status']}"

        cur.execute(
            """
            UPDATE loan_repayments
            SET status = 'confirmed', confirmed_by = %s, confirmed_at = NOW()
            WHERE id = %s
            """,
            (confirmed_by_user_id, repayment_id),
        )

        cur.execute("SELECT * FROM loans WHERE id = %s FOR UPDATE", (repayment["loan_id"],))
        loan = cur.fetchone()
        if loan_outstanding(cur, loan) <= 0:
            # settled_at = when the LAST needed payment was submitted, not
            # when it was confirmed -- confirmation delay is the Treasurer's,
            # and must never make a member's repayment count as late.
            cur.execute(
                """
                UPDATE loans SET status = 'repaid', closed_at = NOW(),
                    settled_at = (SELECT MAX(submitted_at) FROM loan_repayments
                                  WHERE loan_id = %s AND status = 'confirmed')
                WHERE id = %s
                """,
                (loan["id"], loan["id"]),
            )

        conn.commit()
        return True, None


def reject_repayment(repayment_id, confirmed_by_user_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM loan_repayments WHERE id = %s FOR UPDATE", (repayment_id,))
        repayment = cur.fetchone()
        if not repayment:
            return False, "Repayment not found"
        if repayment["status"] != "pending":
            return False, f"Repayment is already {repayment['status']}"
        cur.execute(
            """
            UPDATE loan_repayments
            SET status = 'rejected', confirmed_by = %s, confirmed_at = NOW()
            WHERE id = %s
            """,
            (confirmed_by_user_id, repayment_id),
        )
        conn.commit()
        return True, None


def get_pending_repayments():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.*, l.user_id, u.username, u.full_name FROM loan_repayments r
            JOIN loans l ON l.id = r.loan_id
            JOIN users u ON u.id = l.user_id
            WHERE r.status = 'pending'
            ORDER BY r.submitted_at ASC
            """
        )
        rows = cur.fetchall()
        for r in rows:
            cur.execute("SELECT * FROM loans WHERE id = %s", (r["loan_id"],))
            loan = cur.fetchone()
            if loan and loan["status"] == "disbursed":
                after = loan_outstanding(cur, loan) - r["amount"]
                r["balance_after"] = max(after, Decimal(0))
            else:
                r["balance_after"] = None
        return rows


# ---------- Top-ups ----------

def _topup_block_reason(d, limit_available, days_spent=None):
    """Why a top-up can't be requested right now, or None if it can.
    `days_spent` overrides "today" so that, when the Treasurer decides
    later, the window is judged at the moment the member asked: a slow
    approval must never void a request made in time."""
    r = d["rules"]
    if d["standing"] != "good":
        return "Top-ups are closed while a loan is overdue"
    if (d["days_spent"] if days_spent is None else days_spent) > r["topup_window_days"]:
        return f"Top-ups are only available in the first {r['topup_window_days']} days"
    if d["topups_used"] >= r["max_topups"]:
        return f"You have used all {r['max_topups']} top-ups on this loan"
    if limit_available <= 0:
        return "You have no unused limit left"
    return None


def get_topup_options(cur, loan, d, limit):
    """For the member's loan view: can they top up, how much, at what fee."""
    available = limit["available"]
    reason = _topup_block_reason(d, available)
    if reason is None and d["topup_pending"]:
        reason = "You already have a top-up waiting for the Treasurer"
    return {
        "can_topup": reason is None,
        "topup_block_reason": reason,
        "topup_available": available if reason is None else Decimal(0),
        "topup_fee_percent": d["rules"]["topup_fee_percent"],
        "topups_left": max(d["rules"]["max_topups"] - d["topups_used"], 0),
    }


def request_topup(loan_id, user_id, amount):
    try:
        amount = _d(amount)
    except InvalidOperation:
        return False, "Invalid amount", None
    if amount <= 0:
        return False, "Invalid amount", None

    balance = get_user_savings_balance(user_id)
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM loans WHERE id = %s FOR UPDATE", (loan_id,))
        loan = cur.fetchone()
        if not loan:
            return False, "Loan not found", None
        if loan["user_id"] != user_id:
            return False, "This isn't your loan", None
        if loan["status"] != "disbursed":
            return False, "Top-ups are only available on a loan that has been paid out", None

        d = _derive(cur, loan)
        if d["topup_pending"]:
            return False, "You already have a top-up waiting for the Treasurer", None
        lim = _limit_info(cur, user_id, balance)
        reason = _topup_block_reason(d, lim["available"])
        if reason:
            return False, reason, None
        if amount > lim["available"]:
            return False, f"You can top up by at most UGX {lim['available']:,.0f}", None

        cur.execute(
            "INSERT INTO loan_topups (loan_id, amount) VALUES (%s, %s) RETURNING *",
            (loan_id, amount),
        )
        row = cur.fetchone()
        conn.commit()
        return True, None, row


def get_pending_topups():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.*, l.user_id, u.username, u.full_name FROM loan_topups t
            JOIN loans l ON l.id = t.loan_id
            JOIN users u ON u.id = l.user_id
            WHERE t.status = 'pending'
            ORDER BY t.requested_at ASC
            """
        )
        rows = cur.fetchall()
        for t in rows:
            cur.execute("SELECT * FROM loans WHERE id = %s", (t["loan_id"],))
            loan = cur.fetchone()
            rules = _rules_of(loan)
            t["fee"] = _pct(t["amount"], rules["topup_fee_percent"])
        return rows


def approve_topup(topup_id, decided_by_user_id):
    """Treasurer approves AND pays out in one step: the top-up principal
    and its fee are written to the ledger. The due date does not move."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM loan_topups WHERE id = %s FOR UPDATE", (topup_id,))
        topup = cur.fetchone()
        if not topup:
            return False, "Top-up not found"
        if topup["status"] != "pending":
            return False, f"Top-up is already {topup['status']}"

        cur.execute("SELECT * FROM loans WHERE id = %s FOR UPDATE", (topup["loan_id"],))
        loan = cur.fetchone()
        if loan["status"] != "disbursed":
            return False, "That loan is no longer open"

        d = _derive(cur, loan)
        # Window judged when the member asked; standing and the remaining
        # limit judged now.
        days_at_request = max((_aware(topup["requested_at"]) - _aware(loan["disbursed_at"])).days, 0)
        lim = _limit_info(cur, loan["user_id"], get_user_savings_balance(loan["user_id"]))
        reason = _topup_block_reason(d, lim["available"], days_spent=days_at_request)
        if reason:
            return False, f"Can't approve: {reason[0].lower()}{reason[1:]}"
        if topup["amount"] > lim["available"]:
            return False, f"Can't approve: the member's unused limit is now only UGX {lim['available']:,.0f}"

        fee = _pct(topup["amount"], d["rules"]["topup_fee_percent"])
        cur.execute(
            "INSERT INTO loan_charges (loan_id, kind, amount, note, created_by) VALUES (%s, 'topup', %s, %s, %s)",
            (loan["id"], topup["amount"], f"Top-up #{topup['id']}", decided_by_user_id),
        )
        if fee > 0:
            cur.execute(
                "INSERT INTO loan_charges (loan_id, kind, amount, note, created_by) VALUES (%s, 'topup_fee', %s, %s, %s)",
                (loan["id"], fee, f"Fee on top-up #{topup['id']}", decided_by_user_id),
            )
        cur.execute(
            "UPDATE loan_topups SET status = 'approved', decided_by = %s, decided_at = NOW() WHERE id = %s",
            (decided_by_user_id, topup_id),
        )
        conn.commit()
        return True, None


def reject_topup(topup_id, decided_by_user_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM loan_topups WHERE id = %s FOR UPDATE", (topup_id,))
        topup = cur.fetchone()
        if not topup:
            return False, "Top-up not found"
        if topup["status"] != "pending":
            return False, f"Top-up is already {topup['status']}"
        cur.execute(
            "UPDATE loan_topups SET status = 'rejected', decided_by = %s, decided_at = NOW() WHERE id = %s",
            (decided_by_user_id, topup_id),
        )
        conn.commit()
        return True, None


# ---------- Extension ----------

def extension_options(d):
    """Can the member extend, and what would it cost."""
    r = d["rules"]
    fee = _pct(d["principal_total"], r["extension_fee_percent"])
    reason = None
    if d["outstanding"] <= 0:
        reason = "Nothing left to pay"
    elif d["due_at"] is None or d["days_overdue"] > 0:
        reason = "Extensions have to be taken before the due date"
    elif d["extensions_used"] >= r["max_extensions"]:
        reason = "This loan has already been extended the maximum number of times"
    return {
        "can_extend": reason is None,
        "extend_block_reason": reason,
        "extension_fee": fee,
        "extension_days": r["extension_days"],
        "extensions_left": max(r["max_extensions"] - d["extensions_used"], 0),
    }


def extend_loan(loan_id, user_id):
    """Take another term for another fee. Instant -- it is a rule, not a
    favour -- and recorded in the ledger so the Treasurer can see it."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM loans WHERE id = %s FOR UPDATE", (loan_id,))
        loan = cur.fetchone()
        if not loan:
            return False, "Loan not found", None
        if loan["user_id"] != user_id:
            return False, "This isn't your loan", None
        if loan["status"] != "disbursed":
            return False, "Only a loan that is out can be extended", None

        d = _derive(cur, loan)
        opt = extension_options(d)
        if not opt["can_extend"]:
            return False, opt["extend_block_reason"], None

        if opt["extension_fee"] > 0:
            cur.execute(
                "INSERT INTO loan_charges (loan_id, kind, amount, note, created_by) VALUES (%s, 'extension_fee', %s, %s, %s)",
                (loan_id, opt["extension_fee"], f"Extended by {opt['extension_days']} days", user_id),
            )
        else:
            # A zero fee still has to be countable against max_extensions.
            cur.execute(
                "INSERT INTO loan_charges (loan_id, kind, amount, note, created_by) VALUES (%s, 'extension_fee', 0, %s, %s)",
                (loan_id, f"Extended by {opt['extension_days']} days", user_id),
            )
        cur.execute(
            "UPDATE loans SET due_at = due_at + make_interval(days => %s) WHERE id = %s RETURNING due_at",
            (opt["extension_days"], loan_id),
        )
        new_due = cur.fetchone()["due_at"]
        conn.commit()
        return True, None, {"fee": opt["extension_fee"], "due_at": new_due}


# ---------- Late fee (scheduled) ----------

def process_overdue_loans():
    """Charge the late fee, once, on every loan that has passed its due
    date still owing money. Idempotent: `late_fee_applied_at` is set in
    the same transaction as the ledger line, and the row is locked.

    A loan whose member already SUBMITTED enough payments by the due date
    is skipped: it is only waiting on the Treasurer to confirm, and that
    delay must not cost the member a fee."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM loans
            WHERE status = 'disbursed' AND due_at IS NOT NULL
              AND due_at < NOW() AND late_fee_applied_at IS NULL
            """
        )
        ids = [r["id"] for r in cur.fetchall()]
    conn.commit()

    applied = 0
    for loan_id in ids:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM loans WHERE id = %s FOR UPDATE", (loan_id,))
            loan = cur.fetchone()
            if (not loan or loan["status"] != "disbursed" or loan["late_fee_applied_at"] is not None
                    or loan["due_at"] is None or _aware(loan["due_at"]) >= _now()):
                conn.commit()
                continue
            d = _derive(cur, loan)
            if d["outstanding"] <= 0:
                conn.commit()
                continue
            cur.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS total FROM loan_repayments
                WHERE loan_id = %s AND status IN ('confirmed', 'pending') AND submitted_at <= %s
                """,
                (loan_id, loan["due_at"]),
            )
            if cur.fetchone()["total"] >= d["total_owed"]:
                conn.commit()
                continue

            fee = _pct(d["principal_total"], d["rules"]["late_fee_percent"])
            if fee > 0:
                cur.execute(
                    "INSERT INTO loan_charges (loan_id, kind, amount, note) VALUES (%s, 'late_fee', %s, %s)",
                    (loan_id, fee, "Not repaid by the due date"),
                )
            cur.execute("UPDATE loans SET late_fee_applied_at = NOW() WHERE id = %s", (loan_id,))
            conn.commit()
            applied += 1
    return applied


# ---------- Privacy-scoped reads ----------

def _serialize_state(d):
    """The derived fields every loan view shares."""
    return {
        "principal_total": d["principal_total"],
        "fee_total": d["fee_total"],
        "total_owed": d["total_owed"],
        "repaid": d["repaid"],
        "awaiting_confirmation": d["awaiting"],
        "outstanding": d["outstanding"],
        "due_at": d["due_at"],
        "days_spent": d["days_spent"],
        "days_left": d["days_left"],
        "days_overdue": d["days_overdue"],
        "standing": d["standing"],
        "topups_used": d["topups_used"],
        "extensions_used": d["extensions_used"],
        "charges": d["charges"],
    }


def get_user_loans(user_id):
    """A member's own loans, with derived balance, due date, standing and
    repayment history -- private to the owner (and the officers, via the
    portfolio)."""
    balance = get_user_savings_balance(user_id)
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM loans WHERE user_id = %s ORDER BY requested_at DESC",
            (user_id,),
        )
        loans = cur.fetchall()
        limit = _limit_info(cur, user_id, balance)
        result = []
        for loan in loans:
            cur.execute(
                "SELECT * FROM loan_repayments WHERE loan_id = %s ORDER BY submitted_at ASC",
                (loan["id"],),
            )
            repayments = cur.fetchall()
            cur.execute(
                "SELECT COUNT(*) AS count FROM loan_endorsements WHERE loan_id = %s",
                (loan["id"],),
            )
            endorsement_count = cur.fetchone()["count"]
            row = {**loan, "endorsement_count": endorsement_count, "repayments": repayments,
                   "total_owed": None, "outstanding": None}
            if loan["status"] == "disbursed":
                d = _derive(cur, loan)
                row.update(_serialize_state(d))
                row.update(get_topup_options(cur, loan, d, limit))
                row.update(extension_options(d))
                cur.execute(
                    "SELECT * FROM loan_topups WHERE loan_id = %s ORDER BY requested_at ASC",
                    (loan["id"],),
                )
                row["topups"] = cur.fetchall()
            elif loan["status"] == "repaid":
                total_owed = _total_owed(cur, loan)
                row["total_owed"] = total_owed
                row["outstanding"] = total_owed - _confirmed_repaid(cur, loan["id"])
            result.append(row)
        return result


def get_all_loans_for_treasurer():
    """Full visibility for officers."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT l.*, u.username, u.full_name FROM loans l
            JOIN users u ON u.id = l.user_id
            ORDER BY l.requested_at DESC
            """
        )
        loans = cur.fetchall()
        result = []
        for loan in loans:
            total_owed = outstanding = None
            if loan["status"] in ("disbursed", "repaid"):
                total_owed = _total_owed(cur, loan)
                outstanding = total_owed - _confirmed_repaid(cur, loan["id"])
            result.append({**loan, "total_owed": total_owed, "outstanding": outstanding})
        return result


def get_interest_collected():
    """Aggregate figure for the public group-fund view: fees from fully
    repaid loans (base fee + top-up, extension and late fees). Counted
    only once a loan is closed, not prorated across partial repayments --
    a deliberate simplification, kept because it is unambiguous."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM loans WHERE status = 'repaid'")
        total = Decimal(0)
        for loan in cur.fetchall():
            total += _base_fee(loan)
            cur.execute(
                "SELECT COALESCE(SUM(amount), 0) AS t FROM loan_charges WHERE loan_id = %s AND kind = ANY(%s)",
                (loan["id"], list(FEE_KINDS)),
            )
            total += cur.fetchone()["t"]
        return total


# ---------- Portfolio (officer view) ----------

AGING_BUCKETS = (
    ("current", "Current", 0, 0),
    ("d1_7", "1–7 days late", 1, 7),
    ("d8_30", "8–30 days late", 8, 30),
    ("d31_plus", "31+ days late", 31, None),
)


def get_portfolio():
    """Everything the Treasurer, Secretary and Admin need on one page:
    every loan currently out with who owes what, when it is due and how
    late it is, the queues the Treasurer works through, headline totals
    and a bank-style aging breakdown."""
    conn = get_db()
    now = _now()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT l.*, u.username, u.full_name FROM loans l
            JOIN users u ON u.id = l.user_id
            WHERE l.status = 'disbursed'
            ORDER BY l.disbursed_at ASC
            """
        )
        out = cur.fetchall()
        active = []
        for loan in out:
            cur.execute(
                "SELECT * FROM loan_repayments WHERE loan_id = %s ORDER BY submitted_at ASC",
                (loan["id"],),
            )
            repayments = cur.fetchall()
            d = _derive(cur, loan, now)
            active.append({**loan, **_serialize_state(d), "fee": d["fee_total"], "repayments": repayments})

        cur.execute("SELECT COUNT(*) AS n FROM loans WHERE status = 'repaid'")
        repaid_count = cur.fetchone()["n"]

    buckets = []
    for key, label, lo, hi in AGING_BUCKETS:
        members = [a for a in active if a["days_overdue"] >= lo and (hi is None or a["days_overdue"] <= hi)]
        buckets.append({
            "key": key, "label": label, "count": len(members),
            "outstanding": sum((a["outstanding"] for a in members), Decimal(0)),
        })
    overdue = [a for a in active if a["days_overdue"] > 0]

    queue = {
        "approval": get_loans_pending_approval(),
        "disbursement": get_loans_pending_disbursement(),
        "repayments": get_pending_repayments(),
        "topups": get_pending_topups(),
    }
    summary = {
        "active_count": len(active),
        "lent_out": sum((a["principal_total"] for a in active), Decimal(0)),
        "outstanding": sum((a["outstanding"] for a in active), Decimal(0)),
        "overdue_count": len(overdue),
        "overdue_amount": sum((a["outstanding"] for a in overdue), Decimal(0)),
        "due_soon_count": sum(1 for a in active if a["days_left"] is not None and 0 <= a["days_left"] <= 7),
        "repaid_count": repaid_count,
        "interest_collected": get_interest_collected(),
        "needs_action": sum(len(v) for v in queue.values()),
    }
    return {"summary": summary, "aging": buckets, "active": active, "queue": queue}


# ---------- Light summaries (Home line, nav badge, Cash Dashboard) ----------

DUE_SOON_DAYS = 7   # the officer "due soon" window (matches the portfolio filter)
NUDGE_DAYS = 3      # a member is nudged when their own loan is this close to due


def _disbursed_states(cur, now):
    """(loan row, derived state) for every loan that is out."""
    cur.execute(
        """
        SELECT l.*, u.username, u.full_name FROM loans l
        JOIN users u ON u.id = l.user_id
        WHERE l.status = 'disbursed'
        """
    )
    return [(l, _derive(cur, l, now)) for l in cur.fetchall()]


def get_loan_signal(user_id, officer=False):
    """The few numbers the Home screen and the nav badge need, without
    loading the whole loans page.

    `mine`: the member's own open loan (any stage), with an `attention`
    flag when it is overdue or about to fall due.
    `officer`: how many loans are late / due soon across the group."""
    now = _now()
    conn = get_db()
    result = {"mine": None, "officer": None}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM loans WHERE user_id = %s AND status = ANY(%s) ORDER BY requested_at DESC LIMIT 1",
            (user_id, list(ACTIVE_STATUSES)),
        )
        loan = cur.fetchone()
        if loan:
            mine = {"status": loan["status"], "principal": loan["principal"], "attention": None}
            if loan["status"] == "disbursed":
                d = _derive(cur, loan, now)
                mine.update({
                    "outstanding": d["outstanding"],
                    "awaiting_confirmation": d["awaiting"],
                    "due_at": d["due_at"],
                    "days_left": d["days_left"],
                    "days_overdue": d["days_overdue"],
                    "standing": d["standing"],
                })
                if d["days_overdue"] > 0:
                    mine["attention"] = "overdue"
                elif d["days_left"] is not None and d["days_left"] <= NUDGE_DAYS and d["outstanding"] > 0:
                    mine["attention"] = "due_soon"
            elif loan["status"] == "pending":
                cur.execute("SELECT COUNT(*) AS n FROM loan_endorsements WHERE loan_id = %s", (loan["id"],))
                mine["endorsement_count"] = cur.fetchone()["n"]
            result["mine"] = mine

        if officer:
            states = [d for _, d in _disbursed_states(cur, now)]
            late = [d for d in states if d["days_overdue"] > 0]
            result["officer"] = {
                "overdue_count": len(late),
                "overdue_amount": sum((d["outstanding"] for d in late), Decimal(0)),
                "due_soon_count": sum(1 for d in states if d["days_overdue"] == 0 and d["days_left"] is not None
                                      and d["days_left"] <= DUE_SOON_DAYS),
            }
    return result


def get_loan_cash_summary():
    """Loan side of the Cash Dashboard, so the Treasurer can tally the
    group account:

        expected in account = savings + surcharge collected
                              - money sent out as loans + loan payments received

    Everything is derived from the records, nothing is stored."""
    now = _now()
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(SUM(principal), 0) AS t FROM loans WHERE status IN ('disbursed', 'repaid')")
        paid_out = cur.fetchone()["t"]
        cur.execute(
            """
            SELECT COALESCE(SUM(c.amount), 0) AS t FROM loan_charges c
            JOIN loans l ON l.id = c.loan_id
            WHERE c.kind = 'topup' AND l.status IN ('disbursed', 'repaid')
            """
        )
        paid_out += cur.fetchone()["t"]
        cur.execute("SELECT COALESCE(SUM(amount), 0) AS t FROM loan_repayments WHERE status = 'confirmed'")
        received = cur.fetchone()["t"]

        states = [d for _, d in _disbursed_states(cur, now)]
        late = [d for d in states if d["days_overdue"] > 0]
        return {
            "paid_out": paid_out,
            "payments_received": received,
            "outstanding": sum((d["outstanding"] for d in states), Decimal(0)),
            "overdue_amount": sum((d["outstanding"] for d in late), Decimal(0)),
            "overdue_count": len(late),
            "active_count": len(states),
        }


def get_members_loan_overview():
    """user_id -> what that member currently owes and how late, for the
    Cash Dashboard roster."""
    now = _now()
    conn = get_db()
    with conn.cursor() as cur:
        return {
            loan["user_id"]: {"outstanding": d["outstanding"], "standing": d["standing"],
                              "days_overdue": d["days_overdue"]}
            for loan, d in _disbursed_states(cur, now)
        }
