"""Alert delivery channels: Slack webhook, composite fan-out.

Usage:
  from core.observability.alert_channels import SlackAlertChannel, CompositeAlertChannel

  slack = SlackAlertChannel()                # reads QUANTOS_SLACK_WEBHOOK_URL from env
  composite = CompositeAlertChannel([slack, log_channel])
  alert_service = AlertService(channels=[composite])
"""

import json
import os
import urllib.request

from core.observability.alert_service import AlertChannel

_SLACK_WEBHOOK_ENV = "QUANTOS_SLACK_WEBHOOK_URL"


class SlackAlertChannel(AlertChannel):
    """Sends alerts to a Slack incoming webhook.

    Reads the webhook URL from the ``QUANTOS_SLACK_WEBHOOK_URL`` environment
    variable.  When the variable is absent or empty the channel is a silent
    no-op (``send()`` returns ``False``).

    Alerts are formatted as Slack Block Kit payloads with colour-coded
    severity bars.
    """

    SEVERITY_COLORS = {
        "critical": "#ef4444",
        "error": "#ef4444",
        "warning": "#eab308",
        "info": "#3b82f6",
    }

    def __init__(self, webhook_url: str | None = None, *, timeout: float = 5.0):
        self._url = webhook_url or os.getenv(_SLACK_WEBHOOK_ENV, "")
        self._timeout = timeout

    def send(self, alert: dict) -> bool:
        if not self._url:
            return False

        payload = self._format(alert)
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")

        try:
            req = urllib.request.Request(
                self._url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return 200 <= resp.status < 300
        except Exception:
            return False

    def _format(self, alert: dict) -> dict:
        severity = alert.get("severity", "warning")
        color = self.SEVERITY_COLORS.get(severity, "#94a3b8")
        rule = alert.get("rule_name", "unknown")
        fired_at = alert.get("fired_at", "")
        context = alert.get("context_snapshot", {})

        ctx_lines = "\n".join(f"• *{k}*: `{v}`" for k, v in context.items()) or "_no context_"

        return {
            "attachments": [
                {
                    "color": color,
                    "blocks": [
                        {
                            "type": "header",
                            "text": {
                                "type": "plain_text",
                                "text": f"QUANT OS Alert: {rule}",
                            },
                        },
                        {
                            "type": "section",
                            "fields": [
                                {"type": "mrkdwn", "text": f"*Severity:* `{severity}`"},
                                {"type": "mrkdwn", "text": f"*Fired:* {fired_at}"},
                            ],
                        },
                        {"type": "divider"},
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"*Context:*\n{ctx_lines}",
                            },
                        },
                    ],
                }
            ]
        }


class CompositeAlertChannel(AlertChannel):
    """Fans out alerts to multiple sub-channels.

    Each sub-channel's ``send()`` is wrapped in try/except — a single
    failing channel never blocks delivery to the others.
    """

    def __init__(self, channels: list[AlertChannel] | None = None):
        self._channels = list(channels) if channels else []

    def add(self, channel: AlertChannel) -> None:
        self._channels.append(channel)

    def send(self, alert: dict) -> bool:
        any_ok = False
        for ch in self._channels:
            try:
                if ch.send(alert):
                    any_ok = True
            except Exception:
                pass
        return any_ok
