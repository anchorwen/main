#!/usr/bin/env python
"""Ω Unified Alert Dispatcher — DingTalk push with cooling, sanitization, source tracking.

Used by audit_data_integrity.py and monitor_feature_drift.py for consistent
alert delivery.  Webhook URL from DINGTALK_WEBHOOK_URL environment variable.

DQAF-20260616-005/GAP4: extracted from audit_data_integrity.py to avoid
duplicate push logic across monitoring components.

Usage:
    from scripts.alert_dispatcher import dispatch_alert, AlertCard

    card = AlertCard(
        source="audit",
        title="Data Integrity: Sev2 Warning",
        severity="Sev2",
        checks={"journal_mt5": "Sev2", "snapshots": "Sev3"},
    )
    dispatch_alert(card)
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ── Cooling state (persisted to disk for cross-invocation memory) ──
COOLING_FILE = Path(__file__).resolve().parent.parent / "data" / "state" / "alert_cooling.json"
COOLING_WINDOW = 3  # consecutive alerts before escalation
COOLING_ESCALATION_SEC = 14400  # 4 hours aggregated after 3 consecutive


@dataclass
class AlertCard:
    """Structured alert card for DingTalk push."""

    source: str  # "audit" | "drift"
    title: str
    severity: str  # "Sev1" | "Sev2" | "Sev3" | "OK"
    checks: dict[str, str] = field(default_factory=dict)  # check_name -> severity
    details: dict[str, Any] = field(default_factory=dict)  # extra context
    affected_consumers: list[str] | None = None  # DLR-001: consumers impacted by this alert
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).replace(tzinfo=None).isoformat()
    )


# ── Sanitization ──


def _sanitize(text: str) -> str:
    """Remove sensitive data: absolute PnL, account balances, position sizes."""
    import re

    text = re.sub(r"\$\d+\.?\d*", "$***", text)  # dollar amounts
    text = re.sub(r"\b\d+\.\d+USC\b", "***USC", text)  # account currency
    text = re.sub(r"balance=\d+\.?\d*", "balance=$***", text)  # account balance
    text = re.sub(r"equity=\d+\.?\d*", "equity=$***", text)  # account equity
    return text


# ── Cooling / aggregation ──


def _load_cooling_state() -> dict[str, Any]:
    if COOLING_FILE.exists():
        try:
            return json.loads(COOLING_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_cooling_state(state: dict[str, Any]) -> None:
    COOLING_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOLING_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _check_cooling(source: str, severity: str, check_key: str) -> tuple[bool, str]:
    """Check if this alert should be suppressed by cooling.

    Returns (should_suppress, reason).
    After 3 consecutive same-source+same-key+same-severity alerts,
    escalate to 4-hour aggregated summary.
    """
    state = _load_cooling_state()
    key = f"{source}:{check_key}:{severity}"
    now = time.time()

    entry = state.get(key, {"count": 0, "first_at": now, "last_at": now})
    entry["count"] += 1
    entry["last_at"] = now

    if entry["count"] <= COOLING_WINDOW:
        state[key] = entry
        _save_cooling_state(state)
        return False, ""

    # Escalated: only allow every COOLING_ESCALATION_SEC
    elapsed_since_last = now - entry.get("last_sent_at", 0)
    if elapsed_since_last < COOLING_ESCALATION_SEC:
        _save_cooling_state(state)
        return (
            True,
            f"cooling: {entry['count']} consecutive, next allowed in {COOLING_ESCALATION_SEC - elapsed_since_last:.0f}s",
        )

    entry["last_sent_at"] = now
    state[key] = entry
    _save_cooling_state(state)
    return False, f"(aggregated: {entry['count']} consecutive alerts)"


# ── DingTalk push ──


def _get_webhook_url() -> str | None:
    """Read webhook from environment variable, fallback to config file."""
    url = os.environ.get("DINGTALK_WEBHOOK_URL", "").strip()
    if url:
        return url
    # Fallback: read from live.yaml (legacy)
    try:
        cfg = Path("configs/live.yaml")
        if cfg.exists():
            with open(cfg, encoding="utf-8") as f:
                for line in f:
                    if "dingtalk_webhook_url:" in line:
                        return line.split(":", 1)[1].strip()
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        pass
    return None


def _build_markdown(card: AlertCard, cooling_note: str = "") -> str:
    """Build sanitized DingTalk Markdown card."""
    lines = [
        f"## {card.source.upper()}: {card.title}",
        f"> Severity: **{card.severity}** | Time: {card.timestamp[:19]}",
        "",
    ]
    if card.checks:
        lines.append("### Checks")
        for name, sev in sorted(card.checks.items()):
            icon = "[OK]" if sev == "OK" else "[WARN]" if "Sev" in str(sev) else "[SKIP]"
            lines.append(f"- {icon} {name}: {sev}")
        lines.append("")

    if card.details:
        lines.append("### Details")
        for k, v in sorted(card.details.items()):
            if isinstance(v, int | float | str):
                lines.append(f"- {k}: {v}")
        lines.append("")

    if cooling_note:
        lines.append(f"> {cooling_note}")

    checksum = hashlib.sha256(card.timestamp.encode()).hexdigest()[:8]
    lines.append(f"`checksum: {checksum}`")
    # FIX-20260805-006: DingTalk robot custom security keyword "QuantOs" — every
    # script-path alert must contain it or the API rejects with errcode=310000.
    # Footer mirrors alert_channels.py / send_data_health_alert.py convention.
    lines.append("---")
    lines.append("QuantOs 实盘告警系统")

    return _sanitize("\n".join(lines))


def dispatch_alert(card: AlertCard, *, dry_run: bool = False) -> bool:
    """Push alert card to DingTalk. Returns True if sent, False if suppressed/error."""
    webhook = _get_webhook_url()
    if not webhook:
        return False

    # Check cooling for each non-OK check
    suppressed_all = True
    cooling_notes = []
    for check_name, sev in card.checks.items():
        if sev in ("Sev1", "Sev2"):
            suppressed, note = _check_cooling(card.source, sev, check_name)
            if suppressed:
                cooling_notes.append(note)
            else:
                suppressed_all = False
                if note:
                    cooling_notes.append(note)

    if suppressed_all and cooling_notes:
        return False  # entirely suppressed by cooling

    markdown = _build_markdown(card, "; ".join(cooling_notes) if cooling_notes else "")

    if dry_run:
        print(f"[DRY-RUN] Would push to DingTalk: {card.source} {card.severity}")
        print(markdown[:500])
        return True

    payload = json.dumps(
        {
            "msgtype": "markdown",
            "markdown": {"title": f"[{card.source}] {card.title}", "text": markdown},
        }
    ).encode("utf-8")

    try:
        req = urllib.request.Request(
            webhook, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("errcode") == 0
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        return False
