"""Background job scheduler.

Two jobs run for the lifetime of the process:
  * monitor  - detects idle runs, break overruns and missing heartbeats
  * notifier - drains the notification delivery queue with retries

``max_instances=1`` plus ``coalesce=True`` means a slow tick never stacks up.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.database import SessionLocal
from app.services import monitor

logger = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


def _run_monitor() -> None:
    db = SessionLocal()
    try:
        stats = monitor.run_tick(db)
        if stats["sessions"]:
            logger.debug("Monitor tick: %s", stats)
    except Exception:  # noqa: BLE001 - the scheduler must survive any failure
        logger.exception("Monitor tick failed")
        db.rollback()
    finally:
        db.close()


def _run_notifier() -> None:
    db = SessionLocal()
    try:
        monitor.run_notification_drain(db)
    except Exception:  # noqa: BLE001
        logger.exception("Notification drain failed")
        db.rollback()
    finally:
        db.close()


def start() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    scheduler = BackgroundScheduler(
        timezone="UTC",
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 30},
    )
    scheduler.add_job(
        _run_monitor,
        IntervalTrigger(seconds=settings.monitor_interval_seconds),
        id="monitor",
        name="Presence monitor",
        replace_existing=True,
    )
    scheduler.add_job(
        _run_notifier,
        IntervalTrigger(seconds=settings.notification_worker_interval_seconds),
        id="notifier",
        name="Notification queue",
        replace_existing=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "Scheduler started (monitor every %ss, notifier every %ss)",
        settings.monitor_interval_seconds,
        settings.notification_worker_interval_seconds,
    )
    return scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped")
