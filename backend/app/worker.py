"""Standalone monitoring worker.

Runs the monitor and notification jobs without serving HTTP. Exactly one of
these should run per deployment: a second instance would evaluate the same open
sessions and race to raise the same alerts.
"""
from __future__ import annotations

import logging
import signal
import sys
import threading

from app.config import settings
from app.scheduler import shutdown, start

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("app.worker")

_stop = threading.Event()


def _handle_signal(signum, _frame) -> None:
    logger.info("Received signal %s, shutting down", signum)
    _stop.set()


def main() -> int:
    from sqlalchemy import text

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        logger.critical("Cannot reach the database; worker will not start", exc_info=True)
        return 1
    finally:
        db.close()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handle_signal)

    start()
    logger.info("Monitoring worker running. Send SIGTERM to stop.")
    _stop.wait()
    shutdown()
    logger.info("Monitoring worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
