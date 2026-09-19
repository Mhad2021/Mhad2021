"""Administrative command line.

    python -m app.cli init-db          create tables (dev; use Alembic in prod)
    python -m app.cli create-admin     create the first administrator
    python -m app.cli seed-demo        populate a demo org for evaluation
    python -m app.cli doctor           check the setup and report what is wrong
    python -m app.cli serve            run the server for everyone on this network
    python -m app.cli demo-data        fill a demo install with realistic data
    python -m app.cli monitor-once     run one monitoring pass and exit
    python -m app.cli check-config     validate configuration before deploying
"""
from __future__ import annotations

import argparse
import getpass
import sys
from datetime import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.core.security import hash_password, password_problems
from app.database import Base, SessionLocal, engine
from app.models import Department, User, WorkSchedule
from app.models.enums import Role
from app.services.policy import ensure_global_policy


def _explain_db_failure(exc: Exception) -> None:
    """Turn a raw driver traceback into something actionable.

    Forgetting the .env file is the most common first-run mistake, and the
    built-in default points at PostgreSQL — so the error talks about port 5432
    to someone who never asked for PostgreSQL.
    """
    env_file = Path(".env")
    using_postgres = settings.database_url.startswith("postgresql")

    print("\nCannot reach the database.", file=sys.stderr)
    print(f"  Configured: {settings.database_url.split('@')[-1]}\n", file=sys.stderr)

    if using_postgres and not env_file.exists():
        print(
            "There is no .env file here, so the PostgreSQL default is being used.\n"
            "For a local trial you want SQLite instead:\n\n"
            "    cp .env.development.example .env\n\n"
            "then run this command again.",
            file=sys.stderr,
        )
    elif using_postgres:
        print(
            "PostgreSQL is configured but not reachable. Either start it\n"
            "(docker compose up -d db), or switch to SQLite for a local trial:\n\n"
            "    cp .env.development.example .env",
            file=sys.stderr,
        )
    else:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)


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
        for code, username, name in [
            ("EMP-001", "john.doe", "John Doe"),
            ("EMP-002", "sara.khan", "Sara Khan"),
            ("EMP-003", "marco.rossi", "Marco Rossi"),
        ]:
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


def demo_data(args) -> None:
    """Populate the dashboards so a fresh install can actually be evaluated."""
    from app.demo import generate

    db = SessionLocal()
    try:
        stats = generate(db, weeks=args.weeks)
    finally:
        db.close()

    print(
        f"Demo data ready: {stats['employees']} employees, "
        f"{stats['days']} working days, {stats['sessions']} sessions, "
        f"{stats['alerts']} alerts.\n"
        "\nSign in and look at:\n"
        f"  {settings.base_url}/team/live   one employee in each live state\n"
        f"  {settings.base_url}/alerts      alerts the monitor raised\n"
        f"  {settings.base_url}/reports     attendance for the last fortnight\n"
        "\nAccounts (all password DemoPass2024):\n"
        "  demo.admin    admin, sees everything\n"
        "  demo.leader   team leader, receives the alerts\n"
        "  john.doe      employee, sees only their own day"
    )


def doctor(args) -> None:
    """Check every step of a local setup and say what is missing.

    Written because the failure modes are indistinguishable from the browser:
    a server that never started and a hostname that does not resolve both look
    like "this site can't be reached".
    """
    import socket

    from sqlalchemy import func, select

    from app.netinfo import all_lan_ips, mdns_hostname

    ok = "  [ok]  "
    bad = "  [--]  "
    warn = "  [!!]  "
    problems: list[str] = []

    print(f"\n{settings.app_name} setup check\n")

    # --- Configuration -----------------------------------------------------
    env_file = Path(".env")
    if env_file.exists():
        print(f"{ok}.env found")
    else:
        print(f"{bad}.env is missing")
        problems.append("Run:  cp .env.development.example .env")

    engine_name = settings.database_url.split(":")[0]
    print(f"{ok}database configured: {engine_name}")

    # --- Database ----------------------------------------------------------
    try:
        db = SessionLocal()
        try:
            from app.models import Alert, User, WorkSession

            users = db.scalar(select(func.count(User.id)))
            sessions = db.scalar(select(func.count(WorkSession.id)))
            alerts = db.scalar(select(func.count(Alert.id)))
        finally:
            db.close()
    except OperationalError as exc:
        # "no such table" means the file is fine but init-db never ran; a
        # refused connection means the database itself is not there.
        if "no such table" in str(exc).lower() or "does not exist" in str(exc).lower():
            print(f"{bad}database is empty — tables have not been created")
        else:
            print(f"{bad}cannot reach the database")
        problems.append("Run:  python -m app.cli init-db")
        users = sessions = alerts = None
    except Exception as exc:  # noqa: BLE001
        print(f"{bad}database reachable but unusable ({type(exc).__name__})")
        problems.append("Run:  python -m app.cli init-db")
        users = sessions = alerts = None
    else:
        print(f"{ok}database reachable")
        if users:
            print(f"{ok}{users} accounts, {sessions} sessions, {alerts} alerts")
        else:
            print(f"{warn}no accounts yet")
            problems.append(
                "Run:  python -m app.cli seed-demo && python -m app.cli demo-data"
            )

    # --- Network -----------------------------------------------------------
    print()
    addresses = all_lan_ips()
    hostname = mdns_hostname()
    port = args.port

    if hostname:
        print(f"{ok}this machine answers to: {hostname}")
    if addresses:
        print(f"{ok}network address: {addresses[0]}")
    else:
        print(f"{warn}no local network address — this machine may be offline")

    # --- Is anything already listening? ------------------------------------
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(1.5)
    listening = probe.connect_ex(("127.0.0.1", port)) == 0
    probe.close()

    if listening:
        print(f"{ok}something is already listening on port {port}")
    else:
        print(f"{warn}nothing is listening on port {port} — the server is not running")
        problems.append(f"Run:  python -m app.cli serve --port {port}")

    # --- Verdict -----------------------------------------------------------
    print()
    if problems:
        print("  Next steps, in order:\n")
        for step in problems:
            print(f"    {step}")
        print()
        return

    url = f"http://{hostname}:{port}" if hostname else f"http://{addresses[0]}:{port}"
    print("  Everything looks right. Open:\n")
    print(f"    on this machine:  http://localhost:{port}")
    print(f"    for your team:    {url}")
    print()


def serve(args) -> None:
    """Run the server so other machines on the same network can reach it.

    ``uvicorn`` bound to 127.0.0.1 is reachable only from this machine, which
    is the single most common reason a team cannot connect. This binds to every
    interface and prints the address to hand out.
    """
    import uvicorn

    from app.netinfo import all_lan_ips, mdns_hostname

    addresses = all_lan_ips()
    hostname = mdns_hostname()
    port = args.port

    print()
    print(f"  {settings.app_name} is starting on port {port}.")
    print()

    if addresses:
        primary = addresses[0]
        ip_url = f"http://{primary}:{port}"

        if hostname:
            # Prefer the name: this machine's IP will change when the office
            # router renews its lease, and every agent pointed at the old
            # address would stop reporting until it was reconfigured by hand.
            name_url = f"http://{hostname}:{port}"
            print("  Give your team this address:")
            print(f"      {name_url}")
            print()
            print(f"      (by IP: {ip_url} — but the name is safer, because")
            print("       this machine's IP changes when the router renews it)")
            recommended = name_url
        else:
            print("  Give your team this address:")
            print(f"      {ip_url}")
            print()
            print("      Reserve this IP on your router, or it will change and")
            print("      every tracker will stop reporting until reconfigured.")
            recommended = ip_url

        if len(addresses) > 1:
            others = ", ".join(f"http://{a}:{port}" for a in addresses[1:])
            print(f"      (also reachable on {others})")
        print()
        print(f"  On this machine:  http://localhost:{port}")

        if "localhost" in settings.base_url or "127.0.0.1" in settings.base_url:
            print()
            print("  ! BASE_URL is set to localhost, so the 'Open dashboard' link in")
            print("    alerts will not work for anyone else. Set it in .env to:")
            print(f"        BASE_URL={recommended}")
    else:
        print("  ! No local network address found — this machine may be offline.")
        print(f"    Only http://localhost:{port} will work.")

    print()
    print("  Everyone must be on the same Wi-Fi. Tracking stops for the whole")
    print("  team while this machine is asleep, so keep the lid open:")
    print("      macOS:    caffeinate -s python -m app.cli serve")
    print("      Windows:  set Sleep to Never in Power Options")
    print()
    print("  Press Ctrl+C to stop.")
    print()

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",  # noqa: S104 - binding to the LAN is the entire point
        port=port,
        reload=args.reload,
        log_level="warning" if not args.verbose else "info",
        proxy_headers=True,
    )


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
    p.add_argument("--username")
    p.add_argument("--email")
    p.add_argument("--name")
    p.add_argument("--password")
    p.add_argument("--code")
    p.add_argument("--timezone", default="UTC")

    p = sub.add_parser("seed-demo", help="Create a demo organisation")
    p.add_argument("--password", default="DemoPass2024")
    p.add_argument("--timezone", default="UTC")

    p = sub.add_parser("doctor", help="Check the setup and report what is wrong")
    p.add_argument("--port", type=int, default=8000)

    p = sub.add_parser(
        "serve", help="Run the server for everyone on this network"
    )
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true", help="restart on code changes")
    p.add_argument("--verbose", action="store_true")

    p = sub.add_parser("demo-data", help="Fill a demo install with realistic data")
    p.add_argument("--weeks", type=int, default=2,
                   help="How many weeks of history to generate (default 2)")

    sub.add_parser("monitor-once", help="Run one monitoring pass and exit")
    sub.add_parser("check-config", help="Validate configuration and connectivity")

    args = parser.parse_args()
    handlers = {
        "init-db": lambda a: init_db(),
        "create-admin": create_admin,
        "seed-demo": seed_demo,
        "doctor": doctor,
        "serve": serve,
        "demo-data": demo_data,
        "monitor-once": monitor_once,
        "check-config": check_config,
    }

    try:
        handlers[args.command](args)
    except OperationalError as exc:
        # A raw driver traceback about port 5432 is useless to someone who
        # never asked for PostgreSQL. Say what is actually wrong.
        _explain_db_failure(exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
