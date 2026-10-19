"""
"Needs your attention": everything currently waiting on the logged-in
person, in one place.

There is no email/SMS in this app, so a Treasurer or Admin only finds out
about pending requests by opening the right page -- and the relevant
section can be buried deep in a long page. This module powers the home
screen card, the nav badge and the deep links that fix that.

Like the approval engine (services/approvals.py), it is a registry rather
than hardcoded logic: each kind of pending work is one entry in SOURCES
(which roles it concerns, which page it lives on, how to list it).
Adding a new kind of pending work later is adding an entry here.

Nothing is stored or cached. Every number is derived live from the same
list functions the working pages use, so the inbox can never disagree
with the page it links to.
"""
import logging
from datetime import datetime, timezone

from db import get_db
from services.savings import get_pending_transactions
from services.loans import (
    get_loans_pending_approval, get_loans_pending_disbursement, get_pending_repayments,
)
from services.admin import get_pending_users
from services.password_reset import get_pending_password_reset_requests
from services.approvals import get_pending_for_roles

logger = logging.getLogger(__name__)

LABELS = {
    'savings_deposit': 'Savings deposit',
    'loan_approval': 'Loan request',
    'loan_disbursement': 'Loan to disburse',
    'loan_repayment': 'Loan repayment',
    'approval': 'Approval needed',
    'signup': 'New member',
    'password_reset': 'Password reset',
}


# ---- one fetcher per kind of pending work -------------------------------
# Each returns raw items: id (a stable DOM anchor id the destination page
# puts on that exact row), user_id and/or title, optional amount/detail,
# and created_at (when it started waiting; None if unknown).

def _savings_items(ctx):
    return [{
        'id': f"savings-txn-{r['id']}", 'user_id': r['user_id'], 'amount': str(r['amount']),
        'detail': 'Waiting for you to confirm', 'created_at': r['submitted_at'],
    } for r in get_pending_transactions()]


def _loan_approval_items(ctx):
    return [{
        'id': f"loan-approve-{r['id']}", 'user_id': r['user_id'], 'amount': str(r['principal']),
        'detail': 'Endorsed by members, needs your approval', 'created_at': r['requested_at'],
    } for r in get_loans_pending_approval()]


def _loan_disbursement_items(ctx):
    return [{
        'id': f"loan-disburse-{r['id']}", 'user_id': r['user_id'], 'amount': str(r['principal']),
        'detail': 'Approved, ready to pay out', 'created_at': r['approved_at'],
    } for r in get_loans_pending_disbursement()]


def _repayment_items(ctx):
    return [{
        'id': f"loan-repayment-{r['id']}", 'user_id': r['user_id'], 'amount': str(r['amount']),
        'detail': 'Repayment waiting for you to confirm', 'created_at': r['submitted_at'],
    } for r in get_pending_repayments()]


def _approval_items(ctx):
    # Multi-party approvals (e.g. commitment fee paid from savings). The
    # approval's own plain-language summary is the title.
    return [{
        'id': f"approval-{r['id']}", 'title': r['summary'], 'created_at': r['created_at'],
    } for r in get_pending_for_roles(ctx['roles'], ctx['user_id'])]


def _signup_items(ctx):
    # users has no created_at column, so signups can't show a waiting time.
    return [{
        'id': f"signup-{u['username']}",
        'title': u['fullName'] or u['username'],
        'detail': f"@{u['username']}" + (f" · {u['team']}" if u.get('team') else ''),
        'created_at': None,
    } for u in get_pending_users()]


def _reset_items(ctx):
    return [{
        'id': f"reset-{r['id']}",
        'title': r['fullName'] or r['username'],
        'detail': f"@{r['username']} asked for a one-time password",
        'created_at': r['created_at'],
    } for r in get_pending_password_reset_requests()]


# `roles`: who the work is FOR (people who can act on it, not merely read
# it -- Secretary can read the loan lists but only the Treasurer decides
# them, so loans are not Secretary work). `target`: which frontend page
# the items live on ('treasurer' panel or 'users' management page).
SOURCES = [
    {'key': 'approval',          'roles': {'admin', 'treasurer', 'secretary'}, 'target': 'treasurer', 'fetch': _approval_items},
    {'key': 'savings_deposit',   'roles': {'treasurer'}, 'target': 'treasurer', 'fetch': _savings_items},
    {'key': 'loan_approval',     'roles': {'treasurer'}, 'target': 'treasurer', 'fetch': _loan_approval_items},
    {'key': 'loan_disbursement', 'roles': {'treasurer'}, 'target': 'treasurer', 'fetch': _loan_disbursement_items},
    {'key': 'loan_repayment',    'roles': {'treasurer'}, 'target': 'treasurer', 'fetch': _repayment_items},
    {'key': 'signup',            'roles': {'admin'},     'target': 'users',     'fetch': _signup_items},
    {'key': 'password_reset',    'roles': {'admin'},     'target': 'users',     'fetch': _reset_items},
]


def _as_datetime(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def merge_and_trim(items, limit):
    """Longest-waiting first (the one most overdue for attention), items
    with no known start time after those that have one, stable otherwise."""
    def key(item):
        dt = _as_datetime(item.get('created_at'))
        return (dt is None, dt.timestamp() if dt else 0)
    return sorted(items, key=key)[:limit]


def _resolve_titles(items):
    """Fill in member names for the (few) items being returned, using one
    query instead of one per row."""
    ids = sorted({i['user_id'] for i in items if i.get('user_id') and not i.get('title')})
    if not ids:
        return
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT id, full_name, username FROM users WHERE id = ANY(%s)", (ids,))
        names = {r['id']: (r['full_name'] or r['username']) for r in cur.fetchall()}
    for i in items:
        if not i.get('title'):
            i['title'] = names.get(i.get('user_id'), 'A member')


def _recover_connection():
    """
    Every source below shares ONE database connection for the request
    (db.get_db). In PostgreSQL a failed statement aborts the whole
    transaction: until it is rolled back, every further query on that
    connection also fails ("current transaction is aborted"). Without
    this, one broken source -- e.g. a table that hasn't been created yet
    on a fresh deploy -- would silently knock out every source after it,
    including perfectly healthy ones like the Treasurer's savings queue.
    """
    try:
        get_db().rollback()
    except Exception:
        logger.exception("Could not roll back after a failed inbox source")


def get_inbox(user_id, roles, limit=5):
    roles = set(roles)
    ctx = {'user_id': user_id, 'roles': sorted(roles)}

    all_items, counts, sections, degraded = [], {}, {}, []
    for src in SOURCES:
        if not (src['roles'] & roles):
            continue
        try:
            items = src['fetch'](ctx)
        except Exception:
            # One failing source must not blank the whole inbox -- but it
            # must not be silent either, so the caller can say so.
            logger.exception("Inbox source %s failed", src['key'])
            degraded.append(src['key'])
            _recover_connection()
            continue
        for it in items:
            it['kind'] = src['key']
            it['label'] = LABELS[src['key']]
            it['target'] = src['target']
        counts[src['key']] = len(items)
        sections[src['target']] = sections.get(src['target'], 0) + len(items)
        all_items.extend(items)

    top = merge_and_trim(all_items, limit)
    _resolve_titles(top)

    return {
        'total': sum(counts.values()),
        'counts': counts,
        'sections': sections,
        'degraded': degraded,
        'items': [{
            'id': i['id'], 'kind': i['kind'], 'label': i['label'], 'title': i['title'],
            'amount': i.get('amount'), 'detail': i.get('detail'),
            'created_at': _as_datetime(i.get('created_at')).isoformat() if i.get('created_at') else None,
            'target': i['target'],
        } for i in top],
    }
