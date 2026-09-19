"""Administrative command line.

    python -m app.cli init-db          create tables (dev; use Alembic in prod)
    python -m app.cli create-admin     create the first administrator
    python -m app.cli seed-demo        populate a demo org for evaluation
    python -m app.cli monitor-once     run one monitoring pass and exit
    python -m app.cli check-config     validate configuration before deploying
"""
from __future__ import annotations

import argparse
import getpass
import sys
from datetime import time

from sqlalchemy import select

from app.config import settings
from app.core.security import hash_password, password_problems
from app.database import Base, SessionLocal, engine
from app.models import Department, User, WorkSchedule
from app.models.enums import Role
from app.services.policy import ensure_global_policy


def init_db() -> None:
    import app.models  # noqa: F401  (registers every table)

    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        ensure_global_policy(db)
        db.commit()
    finally:
        db.close()
    print(f"Tables created on {engine.url.render_as_string(hide_password=True)}")


def create_admin(args) -> None:
    db = SessionLocal()
    try:
        username = args.username or input("Username: ").strip().lower()
        if db.scalar(select(User.id).where(User.username == username)):
            print(f"User '{username}' already exists.", file=sys.stderr)
            sys.exit(1)

        email = args.email or input("Email: ").strip().lower()
        full_name = args.name or input("Full name: ").strip()
        password = args.password or getpass.getpass("Password: ")

        problems = password_problems(password)
        if problems:
            print("Password " + ", ".join(problems), file=sys.stderr)
            sys.exit(1)

        admin = User(
            employee_code=args.code or "ADMIN-001",
            username=username,
            email=email,
            full_name=full_name,
            password_hash=hash_password(password),
            role=Role.ADMIN,
            timezone=args.timezone,
            must_change_password=False,
        )
        db.add(admin)
        ensure_global_policy(db)
        db.commit()
        print(f"Administrator '{username}' created. Sign in at {settings.base_url}/login")
    finally:
        db.close()


def seed_demo(args) -> None:
    """A small org to click through before rolling out for real."""
    db = SessionLocal()
    try:
        if db.scalar(select(User.id).where(User.username == "demo.admin")):
            print("Demo data already present.")
            return

        schedule = WorkSchedule(
            name="Standard office hours",
            start_time=time(9, 0),
            end_time=time(18, 0),
            timezone=args.timezone,
            workdays="1111100",
            grace_late_minutes=10,
            grace_early_minutes=10,
        )
        db.add(schedule)
        db.flush()

        admin = User(
            employee_code="ADMIN-001", username="demo.admin",
            email="admin@example.com", full_name="Alex Morgan",
            password_hash=hash_password(args.password), role=Role.ADMIN,
            timezone=args.timezone, schedule_id=schedule.id,
            must_change_password=False,
        )
        leader = User(
            employee_code="TL-001", username="demo.leader",
            email="leader@example.com", full_name="Priya Raman",
            password_hash=hash_password(args.password), role=Role.TEAM_LEADER,
            timezone=args.timezone, schedule_id=schedule.id,
            must_change_password=False,
        )
        db.add_all([admin, leader])
        db.flush()

        support = Department(
            name="Customer Support",
            description="Front-line support team",
            default_schedule_id=schedule.id,
            default_team_leader_id=leader.id,
        )
        db.add(support)
        db.flush()

        leader.department_id = support.id
        for index, (code, username, name) in enumerate(
            [
                ("EMP-001", "john.doe", "John Doe"),
                ("EMP-002", "sara.khan", "Sara Khan"),
                ("EMP-003", "marco.rossi", "Marco Rossi"),
            ]
        ):
            db.add(
                User(
                    employee_code=code, username=username,
                    email=f"{username}@example.com", full_name=name,
                    password_hash=hash_password(args.password), role=Role.EMPLOYEE,
                    department_id=support.id, team_leader_id=leader.id,
                    schedule_id=schedule.id, timezone=args.timezone,
                    must_change_password=False,
                )
            )

        ensure_global_policy(db)
        db.commit()
        print(
            "Demo organisation created.\n"
            f"  admin        demo.admin  / {args.password}\n"
            f"  team leader  demo.leader / {args.password}\n"
            f"  employees    john.doe, sara.khan, marco.rossi / {args.password}\n"
            "Change these before exposing the server to a network."
        )
    finally:
        db.close()


def monitor_once(args) -> None:
    from app.services import monitor

    db = SessionLocal()
    try:
        stats = monitor.run_tick(db)
        sent, failed = monitor.run_notification_drain(db)
        db.commit()
        print(f"Monitor: {stats}")
        print(f"Notifications: {sent} sent, {failed} failed")
    finally:
        db.close()


def check_config(args) -> None:
    problems: list[str] = []
    warnings: list[str] = []

    if "CHANGE-ME" in settings.secret_key or len(settings.secret_key) < 32:
        problems.append("SECRET_KEY is unset or too short (need 32+ random characters)")
    if not settings.encryption_key:
        warnings.append(
            "ENCRYPTION_KEY unset — integration credentials use a key derived "
            "from SECRET_KEY, so rotating SECRET_KEY would make them unreadable"
        )
    if settings.database_url.startswith("sqlite"):
        warnings.append("Using SQLite. Fine for evaluation, not for production.")
    if settings.is_production and not settings.secure_cookies:
        problems.append("SECURE_COOKIES must be true in production")
    if settings.is_production and settings.trusted_hosts == ["*"]:
        warnings.append("TRUSTED_HOSTS allows any host — set it to your real domain")

    try:
        from sqlalchemy import text

        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        print("✓ Database reachable")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"Database unreachable: {exc}")

    for warning in warnings:
        print(f"! {warning}")
    for problem in problems:
        print(f"✗ {problem}", file=sys.stderr)

    if problems:
        sys.exit(1)
    print("✓ Configuration looks good")


def main() -> None:
    parser = argparse.ArgumentParser(prog="presence", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create tables directly (development only)")

    p = sub.add_parser("create-admin", help="Create the first administrator")
    p.add_argument("--username"); p.add_argument("--email"); p.add_argument("--name")
    p.add_argument("--password"); p.add_argument("--code")
    p.add_argument("--timezone", default="UTC")

    p = sub.add_parser("seed-demo", help="Create a demo organisation")
    p.add_argument("--password", default="DemoPass2024")
    p.add_argument("--timezone", default="UTC")

    sub.add_parser("monitor-once", help="Run one monitoring pass and exit")
    sub.add_parser("check-config", help="Validate configuration and connectivity")

    args = parser.parse_args()
    {
        "init-db": lambda a: init_db(),
        "create-admin": create_admin,
        "seed-demo": seed_demo,
        "monitor-once": monitor_once,
        "check-config": check_config,
    }[args.command](args)


if __name__ == "__main__":
    main()
