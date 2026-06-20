"""Alert delivery channels: Slack webhook, composite fan-out.

Usage:
  from core.observability.alert_channels import SlackAlertChannel, CompositeAlertChannel

  slack = SlackAlertChannel()                # reads QUANTOS_SLACK_WEBHOOK_URL from env
  composite = CompositeAlertChannel([slack, log_channel])
  alert_service = AlertService(channels=[composite])
"""

import json
import os
import urllib.parse
import urllib.request

from core.observability.alert_service import AlertChannel
from core.observability.localization import RuleRegistry
from core.runtime.fault_handler import fail_open_guard

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
        except Exception:  # BLE001:FOG
            with fail_open_guard("alert_channels:send"):
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


_DINGTALK_WEBHOOK_ENV = "QUANTOS_DINGTALK_WEBHOOK_URL"
_DINGTALK_SECRET_ENV = "QUANTOS_DINGTALK_SECRET"


class DingTalkAlertChannel(AlertChannel):
    """Sends alerts to a DingTalk incoming webhook.

    Reads the webhook URL from ``QUANTOS_DINGTALK_WEBHOOK_URL`` and the
    optional signing secret from ``QUANTOS_DINGTALK_SECRET``.  When the URL
    is absent the channel is a silent no-op.

    Alerts are formatted as DingTalk Markdown messages with severity colour
    indicators.
    """

    # ── Localization: delegated to SSOT RuleRegistry (D2) ──
    # Class-level aliases preserved for backward compatibility.
    SEVERITY_PREFIX = RuleRegistry.SEVERITY_PREFIX
    SEVERITY_CN = RuleRegistry.SEVERITY_CN
    RULE_NAME_CN = RuleRegistry.RULE_NAME_CN
    CONTEXT_KEY_CN = RuleRegistry.CONTEXT_KEY_CN

    def __init__(
        self,
        webhook_url: str = "",
        secret: str = "",
        *,
        timeout: float = 5.0,
    ):
        self._url = webhook_url or os.getenv(_DINGTALK_WEBHOOK_ENV, "")
        self._secret = secret or os.getenv(_DINGTALK_SECRET_ENV, "")
        self._timeout = timeout

    def send(self, alert: dict) -> bool:
        if not self._url:
            return False

        payload = self._format(alert)
        url = self._url
        if self._secret:
            url = self._sign_url(url, self._secret)

        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        try:
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return 200 <= resp.status < 300
        except Exception:  # BLE001:FOG
            with fail_open_guard("alert_channels:send"):
                return False
    def _format(self, alert: dict) -> dict:
        severity = alert.get("severity", "warning")
        prefix = RuleRegistry.severity_prefix(severity)
        severity_cn = RuleRegistry.severity_cn(severity)
        rule = alert.get("rule_name", "unknown")
        rule_cn = RuleRegistry.rule_name(rule)
        fired_at = alert.get("fired_at", "")
        symbol = alert.get("symbol", "")
        context = alert.get("context_snapshot", {})
        aggregated = alert.get("aggregated_count", 0)

        symbol_line = f"**【{symbol}】**\n\n" if symbol else ""

        # ── Type A: direct notification (title + text already formatted) ──
        title = alert.get("title", "")
        text = alert.get("text", "")
        if title and text:
            agg_line = (
                f"\n\n> ⚠️ 同类告警在过去窗口内发生了 **{aggregated}** 次"
                if aggregated > 1
                else ""
            )
            return {
                "msgtype": "markdown",
                "markdown": {
                    "title": title,
                    "text": (
                        f"{symbol_line}"
                        f"{text}\n\n"
                        f"**触发时间:** {fired_at}{agg_line}"
                        f"\n\n---\nQuantOs 实盘告警系统"
                    ),
                },
            }

        # ── Build markdown body ──
        body_lines: list[str] = []
        if symbol_line:
            body_lines.append(symbol_line.rstrip("\n"))
        body_lines.append(f"## {prefix} QUANT OS 告警: {rule_cn}\n")
        body_lines.append(f"**严重级别:** `{severity_cn}`  ")
        body_lines.append(f"**触发时间:** {fired_at}  \n")

        # ── Structured sections (D1): render list[dict] as dedicated bullet blocks ──
        structured_keys = RuleRegistry.STRUCTURED_CONTEXT_KEYS
        scalar_context: dict[str, object] = {}
        for k, v in context.items():
            if k in structured_keys and isinstance(v, list) and v:
                body_lines.append(self._render_structured_section(k, v))
            elif k not in structured_keys:
                scalar_context[k] = v
            # If a structured key is present but empty, skip it silently

        # ── Summary: remaining scalar context keys ──
        if scalar_context:
            body_lines.append("**📊 汇总:**  ")
            for k, v in scalar_context.items():
                cn_key = RuleRegistry.context_key(k)
                val_str = str(v) if not isinstance(v, str | int | float) else v
                body_lines.append(f"- **{cn_key}**: `{val_str}`  ")

        # ── Type B: runbook SOP section ──
        runbook = alert.get("runbook", {})
        if runbook.get("available"):
            body_lines.append(self._render_runbook(runbook))

        # ── Aggregation notice + footer ──
        agg_line = (
            f"\n> ⚠️ 同类告警在过去窗口内发生了 **{aggregated}** 次"
            if aggregated > 1
            else ""
        )
        body_lines.append(f"{agg_line}\n\n---\nQuantOs 实盘告警系统")

        return {
            "msgtype": "markdown",
            "markdown": {
                "title": f"QUANT OS 告警: {rule_cn}",
                "text": "\n".join(body_lines),
            },
        }

    @staticmethod
    def _sign_url(url: str, secret: str) -> str:
        import hashlib
        import time as _time

        timestamp = str(round(_time.time() * 1000))
        sign_str = f"{timestamp}\n{secret}"
        sign = hashlib.sha256(sign_str.encode("utf-8")).digest()
        import base64

        signature = base64.b64encode(sign).decode("utf-8")
        return f"{url}&timestamp={timestamp}&sign={urllib.parse.quote(signature)}"

    @staticmethod
    def _render_structured_section(key: str, items: list[dict[str, str]]) -> str:
        """Render a structured context key as a dedicated Markdown bullet section.

        Called by ``_format()`` when context values are ``list[dict]``
        (Iron Law #13 D1 — structured payloads, not \\n-joined strings).
        """
        emoji_map: dict[str, str] = {
            "data_health_failed_sources": "🔴 故障源",
            "data_health_warned_sources": "🟡 警告源",
        }
        heading = emoji_map.get(key, f"📋 {RuleRegistry.context_key(key)}")
        lines = [f"### {heading}"]
        for item in items:
            src = item.get("source", "?")
            code = item.get("code", "")
            msg = item.get("message", "")
            line = f"- **{src}** — `{code}`"
            if msg:
                line += f"\n  > {msg}"
            lines.append(line)
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _render_runbook(runbook: dict) -> str:
        """Render runbook SOP actions, diagnostic commands, and escalation path.

        Iron Law #13 D3 (Closed-Loop Remediation): when a rule fires with a
        runbook, the channel renders the full SOP so the operator can act
        without leaving the DingTalk interface.  In a future phase the
        ``runbook_id`` field will also be consumed by the Self-Healing Engine
        for automated remediation.
        """
        lines: list[str] = ["---", "### 📖 故障处置手册 (Runbook)"]
        title = runbook.get("title", "")
        summary = runbook.get("summary", "")
        if title:
            lines.append(f"**{title}**")
        if summary:
            lines.append(f"> {summary}")
            lines.append("")

        actions: list[dict] = runbook.get("actions", [])
        if actions:
            lines.append("**SOP 操作步骤:**")
            for a in actions:
                order = a.get("order", "?")
                action = a.get("action", "?")
                desc = a.get("description", "")
                priority = a.get("priority", "")
                prio_badge = f" `[{priority}]`" if priority else ""
                lines.append(f"{order}. **{action}**{prio_badge}")
                if desc:
                    lines.append(f"   > {desc}")
            lines.append("")

        diag_cmds: list[str] = runbook.get("diagnostic_commands", [])
        if diag_cmds:
            lines.append("**诊断命令:**")
            for cmd in diag_cmds:
                lines.append(f"```bash\n{cmd}\n```")
            lines.append("")

        escalation: list[str] = runbook.get("escalation_path", [])
        if escalation:
            path_str = " → ".join(escalation)
            lines.append(f"**升级路径:** {path_str}")
            lines.append("")

        return "\n".join(lines)


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
            except Exception:  # BLE001:FOG
                with fail_open_guard("alert_channels:send"):
                    pass
        return any_ok
