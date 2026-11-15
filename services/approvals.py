"""
Generic multi-party approval engine.

Not specific to any one feature: a "workflow" is a named sequence of
stages, each stage a set of roles that must ALL approve (in any order
among themselves) before the request moves to the next stage. The
engine only knows about requests, stages, and roles -- it has no idea
what a "commitment fee deduction" is. See WORKFLOWS below for how a new
workflow type gets registered; adding one is adding a dict entry, not
touching this file's logic.

A role rejecting at any stage kills the whole request (status
'rejected') -- there's no partial-approval limbo state to manage.
"""
from db import get_db

ROLE_LABELS = {'admin': 'Admin', 'treasurer': 'Treasurer', 'secretary': 'Secretary'}


def _commitment_fee_deduction_summary(payload):
    amount = float(payload['amount'])
    text = (
        f"Deduct {amount:,.0f} UGX from {payload['username']}'s "
        f"savings for their commitment fee"
    )
    fee = payload.get('fee_amount')
    if fee is not None and amount > float(fee):
        text += f" ({float(fee):,.0f} fee + {amount - float(fee):,.0f} transfer charge)"
    return text


def _commitment_fee_deduction_can_complete(payload):
    from services.treasurer import can_complete_commitment_fee_deduction
    return can_complete_commitment_fee_deduction(payload)


def _commitment_fee_deduction_on_complete(payload, request_id, actor_id):
    # Imported here, not at module level, to avoid a circular import
    # (services/treasurer.py will import create_request from this file).
    from services.treasurer import complete_commitment_fee_deduction
    complete_commitment_fee_deduction(payload, request_id, actor_id)


# ----------------------------------------------------------------------
# Registry: one entry per workflow type. 'stages' is an ordered list of
# {'roles': [...]} -- every role listed must approve before the stage
# clears. 'summary' renders the one-sentence, non-technical description
# approvers see. 'on_complete' runs once, when the final stage clears,
# and is where the workflow's actual effect happens (e.g. the savings
# deduction) -- it receives the actor_id of whoever gave the final
# approval, since that's usually who should be recorded as having
# "done" the underlying action.
#
# Optional extras, all plain data/functions so nothing here is engine
# logic:
#   stage 'on_clear_message'  what the person who clears that stage is
#                             told (there are no notifications in this
#                             app, so the last approver of a stage is
#                             often the one who must tell the next
#                             person to act)
#   'can_complete'            (payload) -> (ok, message), checked at the
#                             moment of the final approval, because
#                             things can change while a request waits
#   'completed_message'       what the final approver is told on success
# ----------------------------------------------------------------------
WORKFLOWS = {
    'commitment_fee_savings_deduction': {
        'stages': [
            {   # both required, any order
                'roles': ['secretary', 'treasurer'],
                'on_clear_message': (
                    "Secretary and Treasurer have both approved. Please contact the Admin "
                    "so they can give the final approval."
                ),
            },
            {'roles': ['admin']},                   # final sign-off
        ],
        'summary': _commitment_fee_deduction_summary,
        'can_complete': _commitment_fee_deduction_can_complete,
        'on_complete': _commitment_fee_deduction_on_complete,
        'completed_message': (
            "Final approval recorded. The amount was deducted from the member's savings "
            "and their account is unlocked."
        ),
    },
}


def create_request(request_type, payload, requested_by):
    if request_type not in WORKFLOWS:
        raise ValueError(f"Unknown approval workflow type: {request_type}")

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO approval_requests (request_type, payload, requested_by)
            VALUES (%s, %s::jsonb, %s)
            RETURNING id
            """,
            (request_type, _to_jsonb(payload), requested_by),
        )
        request_id = cur.fetchone()['id']
        conn.commit()
        return request_id


def _to_jsonb(payload):
    import json
    return json.dumps(payload)


def _current_stage_roles(request_type, stage_index):
    stages = WORKFLOWS[request_type]['stages']
    if stage_index >= len(stages):
        return []
    return stages[stage_index]['roles']


def get_pending_for_roles(roles, user_id):
    """
    Every pending request whose CURRENT stage requires at least one role
    this caller holds, and which this user hasn't already approved at
    that stage (relevant if someone holds two of the relevant roles --
    they still only get to act once per stage).
    """
    if not roles:
        return []

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM approval_requests WHERE status = 'pending' ORDER BY created_at ASC"
        )
        requests = cur.fetchall()

        results = []
        for r in requests:
            stage_roles = _current_stage_roles(r['request_type'], r['current_stage'])
            matching_roles = [role for role in roles if role in stage_roles]
            if not matching_roles:
                continue

            # Has this user already acted at this stage under any of
            # their matching roles? If so, don't show it again -- they
            # already did their part this stage.
            cur.execute(
                """
                SELECT role FROM approval_actions
                WHERE request_id = %s AND stage = %s AND user_id = %s
                """,
                (r['id'], r['current_stage'], user_id),
            )
            already_acted_roles = {row['role'] for row in cur.fetchall()}
            remaining_roles = [role for role in matching_roles if role not in already_acted_roles]
            if not remaining_roles:
                continue

            workflow = WORKFLOWS.get(r['request_type'])
            summary = workflow['summary'](r['payload']) if workflow else r['request_type']

            results.append({
                'id': r['id'],
                'request_type': r['request_type'],
                'summary': summary,
                'created_at': r['created_at'].isoformat() if r['created_at'] else None,
                'acting_as': remaining_roles,  # which of the caller's roles can act here
            })

        return results


def decide(request_id, role, user_id, decision):
    """
    Record `role`'s decision (by `user_id`, who must actually hold that
    role -- checked by the route layer via role_required) on
    `request_id`. decision is 'approve' or 'reject'.

    Returns (ok: bool, message: str).
    """
    if decision not in ('approve', 'reject'):
        return False, 'Invalid decision'

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM approval_requests WHERE id = %s", (request_id,))
        req = cur.fetchone()
        if not req:
            return False, 'Request not found'
        if req['status'] != 'pending':
            return False, f"This request is already {req['status']}"

        stage_roles = _current_stage_roles(req['request_type'], req['current_stage'])
        if role not in stage_roles:
            return False, 'This request is not waiting on your role right now'

        if decision == 'reject':
            cur.execute(
                "UPDATE approval_requests SET status = 'rejected', resolved_at = NOW() WHERE id = %s",
                (request_id,),
            )
            cur.execute(
                """
                INSERT INTO approval_actions (request_id, stage, role, user_id, decision)
                VALUES (%s, %s, %s, %s, 'reject')
                ON CONFLICT (request_id, stage, role) DO NOTHING
                """,
                (request_id, req['current_stage'], role, user_id),
            )
            conn.commit()
            return True, 'Request rejected'

        workflow = WORKFLOWS[req['request_type']]
        total_stages = len(workflow['stages'])
        is_last_stage = req['current_stage'] + 1 >= total_stages

        # Who has already approved at this stage, and would this approval
        # complete it?
        cur.execute(
            """
            SELECT role FROM approval_actions
            WHERE request_id = %s AND stage = %s AND decision = 'approve'
            """,
            (request_id, req['current_stage']),
        )
        approved_after = {row['role'] for row in cur.fetchall()} | {role}
        stage_cleared = set(stage_roles).issubset(approved_after)

        # Final sign-off: make sure it can still succeed BEFORE recording
        # anything, so a request that has become impossible stays pending
        # (and the approver is told why) instead of being half-completed.
        if stage_cleared and is_last_stage and workflow.get('can_complete'):
            ok, why = workflow['can_complete'](req['payload'])
            if not ok:
                return False, why

        cur.execute(
            """
            INSERT INTO approval_actions (request_id, stage, role, user_id, decision)
            VALUES (%s, %s, %s, %s, 'approve')
            ON CONFLICT (request_id, stage, role) DO NOTHING
            """,
            (request_id, req['current_stage'], role, user_id),
        )

        if not stage_cleared:
            remaining = [ROLE_LABELS.get(r, r) for r in stage_roles if r not in approved_after]
            conn.commit()
            return True, f"Your approval is recorded. Still waiting on: {', '.join(remaining)}."

        if is_last_stage:
            # Do the real work FIRST, then mark the request approved. If
            # the work fails, the exception propagates (clear JSON error
            # via the app's global handler), the approval above is rolled
            # back with the connection, and the request stays pending --
            # never "approved" with nothing actually done. The work itself
            # is idempotent (keyed by request id), so a retry can't
            # double-apply it.
            on_complete = workflow.get('on_complete')
            if on_complete:
                on_complete(req['payload'], request_id, user_id)
            cur.execute(
                "UPDATE approval_requests SET status = 'approved', resolved_at = NOW() WHERE id = %s",
                (request_id,),
            )
            conn.commit()
            return True, workflow.get('completed_message') or 'Final approval recorded -- request completed'

        cur.execute(
            "UPDATE approval_requests SET current_stage = %s WHERE id = %s",
            (req['current_stage'] + 1, request_id),
        )
        conn.commit()
        message = workflow['stages'][req['current_stage']].get('on_clear_message')
        return True, message or 'Your approval was recorded -- moved to the next stage'
