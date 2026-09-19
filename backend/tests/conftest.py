"""Shared test fixtures. Each test gets a fresh in-memory database."""
from __future__ import annotations

import os
from datetime import time, timedelta

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SECRET_KEY", "test-secret-key-at-least-32-characters-long")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("PRESENCE_DISABLE_SCHEDULER", "1")

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password
from app.database import Base
from app.models import (
    ActivityInterval,
    Department,
    Device,
    PolicySettings,
    User,
    WorkSchedule,
)
from app.models.enums import Role


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    import app.models  # noqa: F401

    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)


@pytest.fixture
def db(engine):
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


@pytest.fixture
def policy(db):
    row = PolicySettings(department_id=None)
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def schedule(db):
    row = WorkSchedule(
        name="Office hours",
        start_time=time(9, 0),
        end_time=time(18, 0),
        timezone="UTC",
        workdays="1111100",
        grace_late_minutes=10,
        grace_early_minutes=10,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def leader(db):
    row = User(
        employee_code="TL-1",
        username="leader",
        email="leader@example.com",
        full_name="Priya Raman",
        password_hash=hash_password("TestPass12345"),
        role=Role.TEAM_LEADER,
        must_change_password=False,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def department(db, leader, schedule):
    row = Department(
        name="Support",
        default_team_leader_id=leader.id,
        default_schedule_id=schedule.id,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def employee(db, leader, department, schedule):
    row = User(
        employee_code="EMP-1",
        username="john.doe",
        email="john@example.com",
        full_name="John Doe",
        password_hash=hash_password("TestPass12345"),
        role=Role.EMPLOYEE,
        department_id=department.id,
        team_leader_id=leader.id,
        schedule_id=schedule.id,
        must_change_password=False,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def device(db, employee):
    row = Device(
        user_id=employee.id,
        device_uid="TEST-LAPTOP-0001",
        token_hash="not-used-directly-in-service-tests",
        hostname="WKS-JOHN",
        platform="Windows",
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def rewind(db):
    """Shift a session and everything hanging off it backwards in time.

    Tests run in milliseconds; real sessions span hours. Rewinding a session
    lets a test assert on "two hours of work" without waiting for it.
    """

    def _rewind(session, delta: timedelta):
        session.clock_in_at -= delta
        if session.clock_out_at:
            session.clock_out_at -= delta
        if session.state_since:
            session.state_since -= delta
        if session.last_heartbeat_at:
            session.last_heartbeat_at -= delta
        if session.offline_since:
            session.offline_since -= delta
        if session.scheduled_start_at:
            session.scheduled_start_at -= delta
        if session.scheduled_end_at:
            session.scheduled_end_at -= delta
        for interval in db.query(ActivityInterval).filter(
            ActivityInterval.session_id == session.id
        ):
            interval.started_at -= delta
            if interval.ended_at:
                interval.ended_at -= delta
        for period in session.breaks:
            period.started_at -= delta
            if period.ended_at:
                period.ended_at -= delta
        db.commit()
        db.refresh(session)
        return session

    return _rewind
