"""Inactivity detection, break overruns and missing-heartbeat handling.

These cover the guarantees the spec is actually built around: an employee who
walks away is noticed, an employee on an approved break is not, and an employee
who closes the tracking app is caught by the absence of a heartbeat.
"""
from __future__ import annotations

from datetime import UTC, timedelta

import pytest

from app.core.timeutil import utcnow
from app.models import ActivityEvent, Alert, NotificationDelivery
from app.models.enums import (
    ActivityState,
    AlertType,
    BreakEndReason,
    BreakType,
    EventType,
)
from app.services import agent_session, attendance, monitor


def _alerts_of(db, alert_type: AlertType) -> list[Alert]:
    return db.query(Alert).filter(Alert.alert_type == alert_type).all()


@pytest.fixture
def working_session(db, employee, policy, device, rewind):
    """An employee two hours into their day, actively working."""
    session = attendance.clock_in(db, employee, device=device)
    db.commit()
    rewind(session, timedelta(hours=2))
    return session


class TestIdleDetection:
    def test_idle_below_the_threshold_raises_nothing(
        self, db, employee, device, policy, working_session
    ):
        agent_session.process_heartbeat(db, employee, device, idle_seconds=300)
        db.commit()
        monitor.run_tick(db)
        db.commit()

        assert _alerts_of(db, AlertType.IDLE_NO_BREAK) == []

    def test_idle_past_the_threshold_alerts_the_team_leader(
        self, db, employee, leader, device, policy, working_session
    ):
        agent_session.process_heartbeat(db, employee, device, idle_seconds=720)
        db.commit()
        monitor.run_tick(db)
        db.commit()

        raised = _alerts_of(db, AlertType.IDLE_NO_BREAK)
        assert len(raised) == 1
        alert = raised[0]
        assert alert.recipient_id == leader.id
        assert alert.subject_id == employee.id
        # The wording the spec asks for.
        assert "John has been inactive for 12 minutes" in alert.message
        assert "No break is currently active" in alert.message

    def test_idle_run_is_backdated_so_active_time_is_not_overstated(
        self, db, employee, device, policy, working_session
    ):
        agent_session.process_heartbeat(db, employee, device, idle_seconds=720)
        db.commit()

        # Two hours on the clock, the last twelve minutes idle.
        assert working_session.active_seconds == pytest.approx(7200 - 720, abs=10)
        assert working_session.current_state is ActivityState.IDLE

    def test_the_same_idle_run_does_not_alert_twice(
        self, db, employee, device, policy, working_session
    ):
        agent_session.process_heartbeat(db, employee, device, idle_seconds=720)
        db.commit()
        for _ in range(4):
            monitor.run_tick(db)
            db.commit()

        assert len(_alerts_of(db, AlertType.IDLE_NO_BREAK)) == 1

    def test_a_long_absence_re_alerts_after_the_configured_window(
        self, db, employee, device, policy, working_session, rewind
    ):
        agent_session.process_heartbeat(db, employee, device, idle_seconds=720)
        db.commit()
        monitor.run_tick(db)
        db.commit()
        assert len(_alerts_of(db, AlertType.IDLE_NO_BREAK)) == 1

        # Still away 35 minutes later — past the 30-minute re-alert window.
        rewind(working_session, timedelta(minutes=35))
        working_session.last_heartbeat_at = utcnow()
        db.commit()
        monitor.run_tick(db)
        db.commit()

        assert len(_alerts_of(db, AlertType.IDLE_NO_BREAK)) == 2

    def test_returning_to_the_desk_resolves_the_alert(
        self, db, employee, device, policy, working_session
    ):
        agent_session.process_heartbeat(db, employee, device, idle_seconds=720)
        db.commit()
        monitor.run_tick(db)
        db.commit()
        assert _alerts_of(db, AlertType.IDLE_NO_BREAK)[0].resolved_at is None

        agent_session.process_heartbeat(db, employee, device, idle_seconds=3)
        db.commit()

        assert _alerts_of(db, AlertType.IDLE_NO_BREAK)[0].resolved_at is not None

    def test_a_locked_screen_counts_as_inactive(
        self, db, employee, device, policy, working_session
    ):
        agent_session.process_heartbeat(
            db, employee, device, idle_seconds=30, is_locked=True
        )
        db.commit()
        assert working_session.current_state is ActivityState.LOCKED


class TestBreaksSuppressAlerts:
    def test_no_inactivity_alert_while_a_break_is_running(
        self, db, employee, device, policy, working_session
    ):
        attendance.start_break(db, working_session, BreakType.SHORT)
        db.commit()

        # Away from the keyboard for 15 minutes — but it is an approved break.
        agent_session.process_heartbeat(db, employee, device, idle_seconds=900)
        db.commit()
        monitor.run_tick(db)
        db.commit()

        assert _alerts_of(db, AlertType.IDLE_NO_BREAK) == []

    def test_lunch_break_also_suppresses_alerts(
        self, db, employee, device, policy, working_session
    ):
        attendance.start_break(db, working_session, BreakType.LUNCH)
        db.commit()
        agent_session.process_heartbeat(db, employee, device, idle_seconds=1800)
        db.commit()
        monitor.run_tick(db)
        db.commit()

        assert _alerts_of(db, AlertType.IDLE_NO_BREAK) == []

    def test_alerts_resume_once_the_break_ends(
        self, db, employee, device, policy, working_session, rewind
    ):
        period = attendance.start_break(db, working_session, BreakType.SHORT)
        db.commit()
        attendance.end_break(db, working_session, period)
        db.commit()

        rewind(working_session, timedelta(minutes=15))
        working_session.last_heartbeat_at = utcnow()
        db.commit()
        agent_session.process_heartbeat(db, employee, device, idle_seconds=900)
        db.commit()
        monitor.run_tick(db)
        db.commit()

        assert len(_alerts_of(db, AlertType.IDLE_NO_BREAK)) == 1


class TestBreakOverrun:
    def test_a_break_within_its_allowance_is_fine(
        self, db, employee, policy, working_session, rewind
    ):
        attendance.start_break(db, working_session, BreakType.SHORT)
        db.commit()
        rewind(working_session, timedelta(minutes=8))

        monitor.run_tick(db)
        db.commit()

        assert _alerts_of(db, AlertType.BREAK_OVERRUN) == []

    def test_overrunning_a_break_alerts_the_team_leader(
        self, db, employee, leader, policy, working_session, rewind
    ):
        attendance.start_break(db, working_session, BreakType.SHORT)
        db.commit()
        # 10 minutes allowed + 2 minutes grace; 15 minutes is over.
        rewind(working_session, timedelta(minutes=15))

        monitor.run_tick(db)
        db.commit()

        raised = _alerts_of(db, AlertType.BREAK_OVERRUN)
        assert len(raised) == 1
        assert raised[0].recipient_id == leader.id
        assert "has not returned" in raised[0].message

    def test_an_expired_break_is_closed_automatically(
        self, db, employee, policy, working_session, rewind
    ):
        period = attendance.start_break(db, working_session, BreakType.SHORT)
        db.commit()
        rewind(working_session, timedelta(minutes=15))

        monitor.run_tick(db)
        db.commit()
        db.refresh(period)

        assert period.ended_at is not None
        assert period.end_reason is BreakEndReason.AUTO_EXPIRED
        assert attendance.get_open_break(db, working_session.id) is None

    def test_overrun_alerts_only_once_per_break(
        self, db, employee, policy, working_session, rewind
    ):
        attendance.start_break(db, working_session, BreakType.LUNCH)
        db.commit()
        rewind(working_session, timedelta(minutes=75))

        for _ in range(3):
            monitor.run_tick(db)
            db.commit()

        assert len(_alerts_of(db, AlertType.BREAK_OVERRUN)) == 1


class TestMissingHeartbeat:
    """An employee cannot look active by closing the tracking app."""

    def test_a_recent_heartbeat_keeps_the_session_online(
        self, db, employee, device, policy, working_session
    ):
        agent_session.process_heartbeat(db, employee, device, idle_seconds=10)
        db.commit()
        monitor.run_tick(db)
        db.commit()

        assert not working_session.is_offline

    def test_a_stale_heartbeat_marks_the_session_offline(
        self, db, employee, policy, working_session
    ):
        working_session.last_heartbeat_at = utcnow() - timedelta(minutes=5)
        db.commit()

        monitor.run_tick(db)
        db.commit()

        assert working_session.is_offline
        assert working_session.current_state is ActivityState.OFFLINE
        events = db.query(ActivityEvent).filter(
            ActivityEvent.event_type == EventType.HEARTBEAT_LOST
        ).all()
        assert len(events) == 1

    def test_offline_time_is_not_counted_as_worked(
        self, db, employee, policy, working_session
    ):
        """The gap is backdated to the last confirmed beat, not to when we noticed."""
        working_session.last_heartbeat_at = utcnow() - timedelta(minutes=20)
        db.commit()
        monitor.run_tick(db)
        db.commit()

        attendance.clock_out(db, working_session)
        db.commit()

        assert working_session.offline_seconds == pytest.approx(20 * 60, abs=30)
        assert working_session.active_seconds == pytest.approx(7200 - 1200, abs=30)

    def test_offline_alerts_the_team_leader_after_the_grace_period(
        self, db, employee, leader, policy, working_session
    ):
        working_session.last_heartbeat_at = utcnow() - timedelta(minutes=15)
        db.commit()

        monitor.run_tick(db)
        db.commit()

        raised = _alerts_of(db, AlertType.AGENT_OFFLINE)
        assert len(raised) == 1
        assert raised[0].recipient_id == leader.id
        assert "stopped reporting" in raised[0].message

    def test_briefly_offline_does_not_alert_immediately(
        self, db, employee, policy, working_session
    ):
        """Marked offline after 150s, but not escalated until 10 minutes."""
        working_session.last_heartbeat_at = utcnow() - timedelta(minutes=4)
        db.commit()

        monitor.run_tick(db)
        db.commit()

        assert working_session.is_offline
        assert _alerts_of(db, AlertType.AGENT_OFFLINE) == []

    def test_reconnecting_clears_the_offline_state_and_alert(
        self, db, employee, device, policy, working_session
    ):
        working_session.last_heartbeat_at = utcnow() - timedelta(minutes=15)
        db.commit()
        monitor.run_tick(db)
        db.commit()
        assert _alerts_of(db, AlertType.AGENT_OFFLINE)[0].resolved_at is None

        agent_session.process_heartbeat(db, employee, device, idle_seconds=5)
        db.commit()

        assert not working_session.is_offline
        assert _alerts_of(db, AlertType.AGENT_OFFLINE)[0].resolved_at is not None
        assert db.query(ActivityEvent).filter(
            ActivityEvent.event_type == EventType.RECONNECTED
        ).count() == 1

    def test_the_agent_reporting_its_own_shutdown_starts_the_offline_run_early(
        self, db, employee, device, policy, working_session
    ):
        agent_session.record_agent_event(
            db, employee, device, event_type=EventType.AGENT_STOPPED,
            payload={"reason": "user closed the app"},
        )
        db.commit()

        assert working_session.is_offline
        assert len(_alerts_of(db, AlertType.AGENT_TERMINATED)) == 1


class TestForgottenSessions:
    def test_a_session_left_open_too_long_is_closed_at_the_last_heartbeat(
        self, db, employee, policy, device, rewind
    ):
        session = attendance.clock_in(db, employee, device=device)
        db.commit()
        rewind(session, timedelta(hours=20))
        last_beat = session.last_heartbeat_at

        monitor.run_tick(db)
        db.commit()

        assert not session.is_open
        assert session.clock_out_at == pytest.approx(last_beat, abs=timedelta(seconds=2))
        assert len(_alerts_of(db, AlertType.MISSING_CLOCK_OUT)) == 1


class TestAlertRouting:
    def test_alerts_fall_back_to_the_department_leader(
        self, db, employee, leader, policy, device, rewind
    ):
        employee.team_leader_id = None  # department default should take over
        db.commit()

        session = attendance.clock_in(db, employee, device=device)
        db.commit()
        rewind(session, timedelta(hours=1))
        agent_session.process_heartbeat(db, employee, device, idle_seconds=700)
        db.commit()
        monitor.run_tick(db)
        db.commit()

        assert _alerts_of(db, AlertType.IDLE_NO_BREAK)[0].recipient_id == leader.id

    def test_every_alert_gets_a_dashboard_delivery(
        self, db, employee, policy, device, working_session
    ):
        agent_session.process_heartbeat(db, employee, device, idle_seconds=700)
        db.commit()
        monitor.run_tick(db)
        db.commit()

        alert = _alerts_of(db, AlertType.IDLE_NO_BREAK)[0]
        deliveries = db.query(NotificationDelivery).filter(
            NotificationDelivery.alert_id == alert.id
        ).all()
        assert any(d.channel_type.value == "dashboard" for d in deliveries)


class TestScheduleAlerts:
    """The admin UI exposes these as toggles, so they must actually do something."""

    def _workday_at(self, hour: int, minute: int = 0):
        from datetime import datetime, time

        day = utcnow().date()
        while day.weekday() > 4:
            day -= timedelta(days=1)
        return datetime.combine(day, time(hour, minute), tzinfo=UTC)

    def test_a_late_arrival_alerts_the_team_leader(
        self, db, employee, leader, policy, schedule
    ):
        attendance.clock_in(db, employee, at=self._workday_at(9, 35))
        db.commit()

        raised = _alerts_of(db, AlertType.LATE_ARRIVAL)
        assert len(raised) == 1
        assert raised[0].recipient_id == leader.id
        assert "35 minutes" in raised[0].message

    def test_arriving_within_grace_raises_nothing(
        self, db, employee, policy, schedule
    ):
        attendance.clock_in(db, employee, at=self._workday_at(9, 5))
        db.commit()

        assert _alerts_of(db, AlertType.LATE_ARRIVAL) == []

    def test_the_late_arrival_toggle_is_respected(
        self, db, employee, policy, schedule
    ):
        policy.alert_on_late_arrival = False
        db.commit()

        attendance.clock_in(db, employee, at=self._workday_at(9, 45))
        db.commit()

        assert _alerts_of(db, AlertType.LATE_ARRIVAL) == []

    def test_an_early_departure_alerts_the_team_leader(
        self, db, employee, leader, policy, schedule
    ):
        session = attendance.clock_in(db, employee, at=self._workday_at(9, 0))
        db.commit()
        attendance.clock_out(db, session, at=self._workday_at(15, 0))
        db.commit()

        raised = _alerts_of(db, AlertType.EARLY_DEPARTURE)
        assert len(raised) == 1
        assert raised[0].recipient_id == leader.id

    def test_the_early_departure_toggle_is_respected(
        self, db, employee, policy, schedule
    ):
        policy.alert_on_early_departure = False
        db.commit()

        session = attendance.clock_in(db, employee, at=self._workday_at(9, 0))
        db.commit()
        attendance.clock_out(db, session, at=self._workday_at(14, 0))
        db.commit()

        assert _alerts_of(db, AlertType.EARLY_DEPARTURE) == []

    def test_no_show_alerting_is_off_by_default(self, db, employee, policy, schedule):
        monitor.check_no_shows(db, utcnow())
        db.commit()

        assert _alerts_of(db, AlertType.NO_SHOW) == []

    def test_no_show_alerts_when_enabled_and_overdue(
        self, db, employee, leader, policy, schedule
    ):
        policy.alert_on_no_show = True
        policy.no_show_after_minutes = 30
        db.commit()

        # Well past the 09:00 start, still inside the working day.
        monitor.check_no_shows(db, self._workday_at(11, 0))
        db.commit()

        raised = _alerts_of(db, AlertType.NO_SHOW)
        assert len(raised) == 1
        assert raised[0].recipient_id == leader.id

    def test_no_show_does_not_fire_for_someone_who_clocked_in(
        self, db, employee, policy, schedule
    ):
        policy.alert_on_no_show = True
        db.commit()
        attendance.clock_in(db, employee, at=self._workday_at(9, 0))
        db.commit()

        monitor.check_no_shows(db, self._workday_at(11, 0))
        db.commit()

        assert _alerts_of(db, AlertType.NO_SHOW) == []

    def test_no_show_alerts_only_once_per_day(
        self, db, employee, policy, schedule
    ):
        policy.alert_on_no_show = True
        policy.no_show_after_minutes = 30
        db.commit()

        for hour in (11, 12, 13):
            monitor.check_no_shows(db, self._workday_at(hour, 0))
            db.commit()

        assert len(_alerts_of(db, AlertType.NO_SHOW)) == 1

    def test_no_show_stops_once_the_working_day_is_over(
        self, db, employee, policy, schedule
    ):
        policy.alert_on_no_show = True
        db.commit()

        # 20:00 is past the 18:00 end — the daily report covers it from here.
        monitor.check_no_shows(db, self._workday_at(20, 0))
        db.commit()

        assert _alerts_of(db, AlertType.NO_SHOW) == []
