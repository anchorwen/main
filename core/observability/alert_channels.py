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

    SEVERITY_PREFIX = {
        "critical": "🔴 [CRITICAL]",
        "error": "🟠 [ERROR]",
        "warning": "🟡 [WARNING]",
        "info": "🔵 [INFO]",
    }

    SEVERITY_CN = {
        "critical": "严重",
        "error": "错误",
        "warning": "警告",
        "info": "信息",
    }

    RULE_NAME_CN = {
        "high_error_rate": "高错误率",
        "circuit_breaker_open": "断路器已断开",
        "bridge_heartbeat_missed": "MT5桥接心跳丢失",
        "brain_frozen": "大脑冻结",
        "position_limit_near": "仓位接近上限",
        "cycle_stall": "周期停滞",
        "system_online": "系统上线",
        "daily_loss_exceeded": "日亏损超限",
        "consecutive_losses": "连续亏损超限",
        "win_rate_collapse": "胜率崩塌",
        "strategy_degradation": "策略性能下降",
    }

    CONTEXT_KEY_CN = {
        "error_rate": "周期错误率",
        "circuit_state": "断路器状态",
        "frozen_brain_count": "冻结大脑数",
        "position_utilization": "仓位利用率",
        "bridge_last_ack_seconds": "桥接最后响应",
        "cycle_duration_seconds": "周期耗时",
        "message": "消息",
        "rules_active": "活跃规则",
        "channels": "通知通道",
        "daily_pnl_usd": "当日盈亏(USD)",
        "consecutive_losses": "连续亏损次数",
        "rolling_win_rate": "滚动胜率",
        "strategy_pnl": "策略盈亏(USD)",
        "strategy_win_rate": "策略胜率",
        "total_trades_window": "窗口内交易数",
    }

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
        except Exception:
            return False

    def _format(self, alert: dict) -> dict:
        severity = alert.get("severity", "warning")
        prefix = self.SEVERITY_PREFIX.get(severity, "⚪")
        severity_cn = self.SEVERITY_CN.get(severity, severity)
        rule = alert.get("rule_name", "unknown")
        rule_cn = self.RULE_NAME_CN.get(rule, rule)
        fired_at = alert.get("fired_at", "")
        context = alert.get("context_snapshot", {})
        aggregated = alert.get("aggregated_count", 0)

        ctx_lines = (
            "\n".join(f"- **{self.CONTEXT_KEY_CN.get(k, k)}**: `{v}`" for k, v in context.items())
            or "_无上下文_"
        )

        agg_line = (
            f"\n\n> ⚠️ 同类告警在过去窗口内发生了 **{aggregated}** 次" if aggregated > 1 else ""
        )

        return {
            "msgtype": "markdown",
            "markdown": {
                "title": f"QUANT OS 告警: {rule_cn}",
                "text": (
                    f"## {prefix} QUANT OS 告警: {rule_cn}\n\n"
                    f"**严重级别:** `{severity_cn}`  \n"
                    f"**触发时间:** {fired_at}  \n\n"
                    f"**上下文:**\n{ctx_lines}"
                    f"{agg_line}"
                    f"\n\n---\nQuantOs 实盘告警系统"
                ),
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
