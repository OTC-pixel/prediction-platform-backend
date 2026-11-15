"""
One place that turns an unexpected exception into a clear JSON error.

Every API error response carries a human-readable `message` (the key the
frontend reads), a stable machine `code`, and the HTTP `status`. Raw
exception text is logged for us but never sent to users.

The important distinction for a hosted free-tier app: "the database is
unreachable" (a temporary condition -- retry shortly) is reported
differently from "our code hit a bug", instead of both collapsing into
an identical generic 500.
"""
import logging

import psycopg2
from psycopg2 import pool
from flask import jsonify

logger = logging.getLogger(__name__)

_DB_UNAVAILABLE = (psycopg2.OperationalError, psycopg2.InterfaceError, pool.PoolError)


def describe_exception(exc):
    """Returns (message, code, status) for an unexpected exception."""
    if isinstance(exc, _DB_UNAVAILABLE):
        return (
            "The database is temporarily unavailable. Please try again in a moment.",
            "DB_UNAVAILABLE",
            503,
        )
    return (
        "The server hit an unexpected problem. This isn't caused by anything you did, so try again shortly.",
        "SERVER_ERROR",
        500,
    )


def server_error(exc, action=None):
    """
    JSON response for an unexpected failure inside a route. `action` is
    only used for the server log (what we were trying to do); the
    frontend adds its own "Couldn't <action>" wording around `message`,
    so repeating it here would show it twice.
    """
    message, code, status = describe_exception(exc)
    logger.exception("Unexpected error while trying to %s", action or "handle request")
    return jsonify({"message": message, "code": code, "status": status}), status
