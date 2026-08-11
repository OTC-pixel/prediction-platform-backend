"""
Background job scheduling.

Previously each job spawned a full Python subprocess on a timer -- paying
interpreter startup cost every run, breaking normal exception handling
(errors only showed up as opaque subprocess stderr), and duplicating
load_dotenv()/DB setup on every single invocation. Jobs now run in-process
as plain function calls, each wrapped in an explicit app context so they
share the same connection pool and config as the rest of the app.
"""

from apscheduler.schedulers.background import BackgroundScheduler
import atexit
import logging

logger = logging.getLogger(__name__)


def _run_fetch_fixtures(app):
    with app.app_context():
        try:
            from services.fetch_fixtures import auto_update_if_due
            auto_update_if_due()
        except Exception:
            logger.exception("Fixture fetch job failed")


def _run_collect_and_evaluate_results(app):
    with app.app_context():
        try:
            from services.collect_results import run_once
            run_once()
        except Exception:
            logger.exception("Results collection job failed")


def start_scheduler(app):
    scheduler = BackgroundScheduler()
    scheduler.add_job(lambda: _run_fetch_fixtures(app), trigger="interval", hours=2)
    scheduler.add_job(lambda: _run_collect_and_evaluate_results(app), trigger="interval", hours=1)

    scheduler.start()
    logger.info("Scheduler started: fixtures every 2h, results+evaluation every 1h.")
    atexit.register(lambda: scheduler.shutdown())
