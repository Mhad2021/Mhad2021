"""Notification queue: fan alerts out to channels, then deliver with retries."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.crypto import decrypt_config
from app.core.timeutil import to_local, utcnow
from app.models import Alert, NotificationChannel, NotificationDelivery, User
from app.models.enums import ChannelType, DeliveryStatus
from app.services.notifications.providers import OutboundMessage, build_provider

logger = logging.getLogger(__name__)

# Retry schedule in seconds, indexed by attempt number.
_BACKOFF = [30, 120, 600, 1800, 3600]


def _backoff_for(attempt: int) -> timedelta:
    return timedelta(seconds=_BACKOFF[min(attempt, len(_BACKOFF) - 1)])


def _target_for(channel: NotificationChannel, recipient: Optional[User]) -> Optional[str]:
    """Per-recipient address, where the channel is addressed per person."""
    if recipient is None:
        return None
    if channel.channel_type is ChannelType.EMAIL:
        return recipient.email
    if channel.channel_type is ChannelType.WHATSAPP:
        return recipient.phone
    if channel.channel_type is ChannelType.SLACK:
        # Slack webhooks post to a fixed channel; a user id overrides it if set.
        return recipient.slack_user_id
    return None


def queue_alert(
    db: Session, alert: Alert, recipient: Optional[User] = None
) -> list[NotificationDelivery]:
    """Create a pending delivery per enabled channel that handles this alert."""
    deliveries: list[NotificationDelivery] = []
    now = utcnow()

    # The dashboard delivery always exists so the alert is visible in-app even
    # when every external integration is down or unconfigured.
    deliveries.append(
        NotificationDelivery(
            alert_id=alert.id,
            channel_id=None,
            channel_type=ChannelType.DASHBOARD,
            target=str(recipient.id) if recipient else None,
            status=DeliveryStatus.SENT,
            attempts=1,
            sent_at=now,
        )
    )

    channels = db.scalars(
        select(NotificationChannel).where(NotificationChannel.is_enabled.is_(True))
    )
    subject = db.get(User, alert.subject_id)
    subject_dept_id = subject.department_id if subject else None

    for channel in channels:
        if channel.channel_type is ChannelType.DASHBOARD:
            continue
        if not channel.handles(alert.alert_type, alert.severity):
            continue
        # A department-scoped channel only carries that department's alerts.
        if channel.department_id and channel.department_id != subject_dept_id:
            continue

        target = _target_for(channel, recipient)
        if channel.channel_type in (ChannelType.EMAIL, ChannelType.WHATSAPP) and not target:
            logger.info(
                "Skipping %s delivery for alert %s — recipient has no address.",
                channel.channel_type.value,
                alert.id,
            )
            continue

        deliveries.append(
            NotificationDelivery(
                alert_id=alert.id,
                channel_id=channel.id,
                channel_type=channel.channel_type,
                target=target,
                status=DeliveryStatus.PENDING,
                next_attempt_at=now,
            )
        )

    for delivery in deliveries:
        db.add(delivery)
    db.flush()
    return deliveries


def render(db: Session, alert: Alert, delivery: NotificationDelivery) -> OutboundMessage:
    subject = db.get(User, alert.subject_id)
    recipient = db.get(User, alert.recipient_id) if alert.recipient_id else None
    tz = (recipient.timezone if recipient else None) or (
        subject.timezone if subject else "UTC"
    )
    context = alert.context or {}

    return OutboundMessage(
        title=alert.title,
        body=alert.message,
        severity=alert.severity,
        employee_name=context.get("employee_name")
        or (subject.full_name if subject else "Employee"),
        department=context.get("department"),
        triggered_at_display=to_local(alert.triggered_at, tz).strftime(
            "%a %d %b, %H:%M (%Z)"
        ),
        dashboard_url=f"{settings.base_url.rstrip('/')}/alerts/{alert.id}",
        target=delivery.target,
        context=context,
    )


def deliver_one(db: Session, delivery: NotificationDelivery) -> bool:
    """Attempt one delivery. Returns True on success."""
    alert = db.get(Alert, delivery.alert_id)
    if alert is None:
        delivery.status = DeliveryStatus.SKIPPED
        delivery.last_error = "Alert no longer exists"
        return False

    channel = (
        db.get(NotificationChannel, delivery.channel_id) if delivery.channel_id else None
    )
    if channel is None or not channel.is_enabled:
        delivery.status = DeliveryStatus.SKIPPED
        delivery.last_error = "Channel removed or disabled"
        return False

    delivery.attempts += 1
    config = decrypt_config(channel.encrypted_config)
    config.update(channel.public_config or {})

    try:
        provider = build_provider(channel.channel_type, config)
        provider.send(render(db, alert, delivery))
    except Exception as exc:  # noqa: BLE001 - any provider error is a failed attempt
        error = f"{type(exc).__name__}: {exc}"[:800]
        logger.warning(
            "Delivery %s via %s failed (attempt %s): %s",
            delivery.id,
            channel.name,
            delivery.attempts,
            error,
        )
        delivery.last_error = error
        channel.last_error_at = utcnow()
        channel.last_error = error

        if delivery.attempts >= settings.notification_max_attempts:
            delivery.status = DeliveryStatus.FAILED
            delivery.next_attempt_at = None
        else:
            delivery.next_attempt_at = utcnow() + _backoff_for(delivery.attempts)
        return False

    delivery.status = DeliveryStatus.SENT
    delivery.sent_at = utcnow()
    delivery.next_attempt_at = None
    delivery.last_error = None
    channel.last_success_at = delivery.sent_at
    return True


def process_pending(db: Session, limit: int = 50) -> tuple[int, int]:
    """Drain the delivery queue. Returns (succeeded, failed)."""
    now = utcnow()
    pending = db.scalars(
        select(NotificationDelivery)
        .where(
            NotificationDelivery.status == DeliveryStatus.PENDING,
            NotificationDelivery.next_attempt_at.is_not(None),
            NotificationDelivery.next_attempt_at <= now,
        )
        .order_by(NotificationDelivery.next_attempt_at)
        .limit(limit)
    ).all()

    succeeded = failed = 0
    for delivery in pending:
        if deliver_one(db, delivery):
            succeeded += 1
        else:
            failed += 1
        db.commit()

    if succeeded or failed:
        logger.info("Notification queue: %s sent, %s failed", succeeded, failed)
    return succeeded, failed


def send_test(db: Session, channel: NotificationChannel, recipient: User) -> None:
    """Fire a sample alert down one channel so an admin can verify setup."""
    from app.models.enums import AlertSeverity

    config = decrypt_config(channel.encrypted_config)
    config.update(channel.public_config or {})
    provider = build_provider(channel.channel_type, config)

    problems = provider.validate()
    if problems:
        raise ValueError("; ".join(problems))

    provider.send(
        OutboundMessage(
            title="Test alert from Presence",
            body=(
                "This is a test. A real alert looks like: "
                "“John has been inactive for 12 minutes. "
                "No break is currently active.”"
            ),
            severity=AlertSeverity.INFO,
            employee_name="Test Employee",
            department="Test Department",
            triggered_at_display=to_local(utcnow(), recipient.timezone).strftime(
                "%a %d %b, %H:%M (%Z)"
            ),
            dashboard_url=f"{settings.base_url.rstrip('/')}/dashboard",
            target=_target_for(channel, recipient),
        )
    )
    channel.last_success_at = utcnow()
    channel.last_error = None
