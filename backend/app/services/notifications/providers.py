"""Outbound notification providers.

Each provider takes the decrypted channel config plus a rendered alert and
performs one delivery attempt. Raising any exception marks the attempt failed
and schedules a retry with exponential backoff.
"""
from __future__ import annotations

import logging
import smtplib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Optional

import httpx

from app.config import settings
from app.models.enums import AlertSeverity, ChannelType

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 15.0

_SEVERITY_COLOUR = {
    AlertSeverity.INFO: "#2563eb",
    AlertSeverity.WARNING: "#d97706",
    AlertSeverity.CRITICAL: "#dc2626",
}

_SEVERITY_EMOJI = {
    AlertSeverity.INFO: "ℹ️",
    AlertSeverity.WARNING: "🟠",
    AlertSeverity.CRITICAL: "🔴",
}


@dataclass
class OutboundMessage:
    """A rendered alert, ready to push down any channel."""

    title: str
    body: str
    severity: AlertSeverity
    employee_name: str
    department: Optional[str]
    triggered_at_display: str
    dashboard_url: str
    target: Optional[str] = None          # email address, phone, channel id
    context: Optional[dict[str, Any]] = None

    @property
    def plain_text(self) -> str:
        lines = [
            f"{_SEVERITY_EMOJI.get(self.severity, '')} {self.title}".strip(),
            "",
            self.body,
            "",
            f"Employee: {self.employee_name}",
        ]
        if self.department:
            lines.append(f"Department: {self.department}")
        lines.append(f"Time: {self.triggered_at_display}")
        lines.append(f"Open dashboard: {self.dashboard_url}")
        return "\n".join(lines)


class NotificationProvider(ABC):
    channel_type: ChannelType

    def __init__(self, config: dict[str, Any]):
        self.config = config

    @abstractmethod
    def send(self, message: OutboundMessage) -> None:
        """Deliver the message. Raise on failure."""

    def validate(self) -> list[str]:
        """Return a list of configuration problems, empty when usable."""
        return []


class DashboardProvider(NotificationProvider):
    """In-app only. The Alert row itself is the delivery, so this is a no-op."""

    channel_type = ChannelType.DASHBOARD

    def send(self, message: OutboundMessage) -> None:
        return None


class SlackProvider(NotificationProvider):
    """Slack incoming webhook. Renders a Block Kit card."""

    channel_type = ChannelType.SLACK

    def validate(self) -> list[str]:
        url = self.config.get("webhook_url", "")
        if not url:
            return ["webhook_url is required"]
        if not url.startswith("https://hooks.slack.com/"):
            return ["webhook_url must be a https://hooks.slack.com/... URL"]
        return []

    def send(self, message: OutboundMessage) -> None:
        fields = [
            {"type": "mrkdwn", "text": f"*Employee*\n{message.employee_name}"},
            {"type": "mrkdwn", "text": f"*Time*\n{message.triggered_at_display}"},
        ]
        if message.department:
            fields.append(
                {"type": "mrkdwn", "text": f"*Department*\n{message.department}"}
            )

        payload: dict[str, Any] = {
            "text": f"{message.title}: {message.body}",
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"{_SEVERITY_EMOJI.get(message.severity, '')} {message.title}"[:150],
                        "emoji": True,
                    },
                },
                {"type": "section", "text": {"type": "mrkdwn", "text": message.body}},
                {"type": "section", "fields": fields},
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Open dashboard"},
                            "url": message.dashboard_url,
                        }
                    ],
                },
            ],
        }
        if message.target:
            payload["channel"] = message.target

        response = httpx.post(
            self.config["webhook_url"], json=payload, timeout=HTTP_TIMEOUT
        )
        response.raise_for_status()


class TeamsProvider(NotificationProvider):
    """Microsoft Teams incoming webhook using an Adaptive Card."""

    channel_type = ChannelType.TEAMS

    def validate(self) -> list[str]:
        if not self.config.get("webhook_url"):
            return ["webhook_url is required"]
        return []

    def send(self, message: OutboundMessage) -> None:
        facts = [
            {"title": "Employee", "value": message.employee_name},
            {"title": "Time", "value": message.triggered_at_display},
        ]
        if message.department:
            facts.append({"title": "Department", "value": message.department})

        card = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": [
                            {
                                "type": "TextBlock",
                                "text": message.title,
                                "weight": "Bolder",
                                "size": "Medium",
                                "color": (
                                    "Attention"
                                    if message.severity is AlertSeverity.CRITICAL
                                    else "Warning"
                                ),
                                "wrap": True,
                            },
                            {"type": "TextBlock", "text": message.body, "wrap": True},
                            {"type": "FactSet", "facts": facts},
                        ],
                        "actions": [
                            {
                                "type": "Action.OpenUrl",
                                "title": "Open dashboard",
                                "url": message.dashboard_url,
                            }
                        ],
                    },
                }
            ],
        }
        response = httpx.post(
            self.config["webhook_url"], json=card, timeout=HTTP_TIMEOUT
        )
        response.raise_for_status()


class EmailProvider(NotificationProvider):
    """SMTP. Falls back to the global SMTP settings when the channel omits them."""

    channel_type = ChannelType.EMAIL

    def _smtp_settings(self) -> dict[str, Any]:
        return {
            "host": self.config.get("host") or settings.smtp_host,
            "port": int(self.config.get("port") or settings.smtp_port),
            "username": self.config.get("username") or settings.smtp_username,
            "password": self.config.get("password") or settings.smtp_password,
            "from_address": self.config.get("from_address") or settings.smtp_from,
            "use_tls": self.config.get("use_tls", settings.smtp_use_tls),
        }

    def validate(self) -> list[str]:
        cfg = self._smtp_settings()
        problems = []
        if not cfg["host"]:
            problems.append("SMTP host is not configured")
        if not cfg["from_address"]:
            problems.append("from_address is required")
        return problems

    def send(self, message: OutboundMessage) -> None:
        recipient = message.target or self.config.get("to_address")
        if not recipient:
            raise ValueError("No recipient email address for this alert")

        cfg = self._smtp_settings()
        email = EmailMessage()
        email["Subject"] = message.title
        email["From"] = cfg["from_address"]
        email["To"] = recipient
        email.set_content(message.plain_text)
        email.add_alternative(_render_email_html(message), subtype="html")

        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=HTTP_TIMEOUT) as smtp:
            if cfg["use_tls"]:
                smtp.starttls()
            if cfg["username"]:
                smtp.login(cfg["username"], cfg["password"])
            smtp.send_message(email)


class WhatsAppProvider(NotificationProvider):
    """WhatsApp via Twilio's Messaging API (the usual business-API route)."""

    channel_type = ChannelType.WHATSAPP

    def validate(self) -> list[str]:
        problems = []
        for key in ("account_sid", "auth_token", "from_number"):
            if not self.config.get(key):
                problems.append(f"{key} is required")
        return problems

    def send(self, message: OutboundMessage) -> None:
        to_number = message.target or self.config.get("to_number")
        if not to_number:
            raise ValueError("No recipient phone number for this alert")

        sid = self.config["account_sid"]
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        response = httpx.post(
            url,
            auth=(sid, self.config["auth_token"]),
            data={
                "From": f"whatsapp:{self.config['from_number']}",
                "To": f"whatsapp:{to_number}",
                "Body": f"*{message.title}*\n\n{message.body}\n\n{message.dashboard_url}",
            },
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()


class WebhookProvider(NotificationProvider):
    """Generic JSON POST, for wiring into an existing internal system."""

    channel_type = ChannelType.WEBHOOK

    def validate(self) -> list[str]:
        if not self.config.get("url"):
            return ["url is required"]
        return []

    def send(self, message: OutboundMessage) -> None:
        headers = {"Content-Type": "application/json"}
        if token := self.config.get("auth_token"):
            headers["Authorization"] = f"Bearer {token}"

        response = httpx.post(
            self.config["url"],
            json={
                "title": message.title,
                "message": message.body,
                "severity": message.severity.value,
                "employee": message.employee_name,
                "department": message.department,
                "triggered_at": message.triggered_at_display,
                "dashboard_url": message.dashboard_url,
                "context": message.context or {},
            },
            headers=headers,
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()


def _render_email_html(message: OutboundMessage) -> str:
    colour = _SEVERITY_COLOUR.get(message.severity, "#334155")
    department_row = (
        f'<tr><td style="padding:4px 0;color:#64748b">Department</td>'
        f'<td style="padding:4px 0;color:#0f172a">{message.department}</td></tr>'
        if message.department
        else ""
    )
    return f"""<!doctype html>
<html><body style="margin:0;background:#f1f5f9;font-family:-apple-system,Segoe UI,Roboto,sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px">
    <table width="560" cellpadding="0" cellspacing="0"
           style="background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)">
      <tr><td style="background:{colour};height:4px"></td></tr>
      <tr><td style="padding:28px 32px 8px">
        <h1 style="margin:0;font-size:19px;color:#0f172a">{message.title}</h1>
      </td></tr>
      <tr><td style="padding:0 32px 20px">
        <p style="margin:0;font-size:15px;line-height:1.6;color:#334155">{message.body}</p>
      </td></tr>
      <tr><td style="padding:0 32px 24px">
        <table width="100%" style="font-size:13px;border-top:1px solid #e2e8f0;padding-top:12px">
          <tr><td style="padding:4px 0;color:#64748b">Employee</td>
              <td style="padding:4px 0;color:#0f172a">{message.employee_name}</td></tr>
          {department_row}
          <tr><td style="padding:4px 0;color:#64748b">Time</td>
              <td style="padding:4px 0;color:#0f172a">{message.triggered_at_display}</td></tr>
        </table>
      </td></tr>
      <tr><td style="padding:0 32px 32px">
        <a href="{message.dashboard_url}"
           style="display:inline-block;background:#0f172a;color:#fff;text-decoration:none;
                  padding:11px 20px;border-radius:8px;font-size:14px;font-weight:600">
          Open dashboard</a>
      </td></tr>
      <tr><td style="background:#f8fafc;padding:16px 32px;font-size:12px;color:#94a3b8">
        Sent by {settings.app_name}. This alert covers work-session status only —
        no message content, keystrokes or files are recorded.
      </td></tr>
    </table>
  </td></tr></table>
</body></html>"""


PROVIDERS: dict[ChannelType, type[NotificationProvider]] = {
    ChannelType.DASHBOARD: DashboardProvider,
    ChannelType.SLACK: SlackProvider,
    ChannelType.TEAMS: TeamsProvider,
    ChannelType.EMAIL: EmailProvider,
    ChannelType.WHATSAPP: WhatsAppProvider,
    ChannelType.WEBHOOK: WebhookProvider,
}


def build_provider(
    channel_type: ChannelType, config: dict[str, Any]
) -> NotificationProvider:
    provider_cls = PROVIDERS.get(channel_type)
    if provider_cls is None:
        raise ValueError(f"No provider registered for {channel_type}")
    return provider_cls(config)
