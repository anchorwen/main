#!/usr/bin/env python3
"""Data Health DingTalk Alert Adapter — standalone test & scheduled push.

Usage:
  # Test push (dry-run): print what WOULD be sent
  python scripts/send_data_health_alert.py --base-dir data_btc --symbol BTCUSDc --dry-run

  # Send actual DingTalk notification
  python scripts/send_data_health_alert.py --base-dir data_btc --symbol BTCUSDc

  # Send for both symbols
  python scripts/send_data_health_alert.py --base-dir data_btc --symbol BTCUSDc
  python scripts/send_data_health_alert.py --base-dir data --symbol XAUUSDc

Iron Law #3 (Decoupling): This script is the "Dispatcher" — it reads the
HealthReport (produced by the "Generator" DataHealthService) and formats it
for DingTalk delivery.  DataHealthService itself never touches DingTalk.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Data Health DingTalk Alert Adapter")
    p.add_argument("--base-dir", default="data_btc", help="Data directory")
    p.add_argument("--symbol", default="BTCUSDc", help="Trading symbol")
    p.add_argument("--dry-run", action="store_true", help="Print message only, do not send")
    p.add_argument("--webhook-url", default=None, help="Override DingTalk webhook URL")
    p.add_argument("--mode", choices=("full", "light"), default="full", help="Health check mode")
    return p


def load_webhook_url(base_dir: str, override: str | None = None) -> str:
    """Resolve DingTalk webhook URL from config or environment."""
    if override:
        return override
    import os
    env_url = os.environ.get("QUANTOS_DINGTALK_WEBHOOK_URL", "")
    if env_url:
        return env_url
    try:
        config_path = Path(base_dir).parent / "configs" / "live_btc.yaml"
        if base_dir.rstrip("/\\").endswith("data") and not base_dir.rstrip("/\\").endswith("data_btc"):
            config_path = Path(base_dir).parent / "configs" / "live.yaml"
        import yaml
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return str(cfg.get("alert", {}).get("channels", {}).get("dingtalk_webhook_url", ""))
    except Exception:
        return ""


def _emoji(status: str) -> str:
    return {"pass": "🟢", "warn": "🟡", "fail": "🔴", "missing": "⚫", "skipped": "⚪"}.get(status, "❓")


def format_health_report(report: dict[str, Any], symbol: str, mode: str) -> dict[str, str]:
    """Format a DataHealthService report into a DingTalk Markdown message."""
    agg = report.get("aggregated", {})
    total = agg.get("total_sources", 0)
    pass_c = agg.get("pass_count", 0)
    warn_c = agg.get("warn_count", 0)
    fail_c = agg.get("fail_count", 0)
    elapsed = report.get("elapsed_ms", 0)
    level = report.get("alert_level", "OK")

    level_emoji = {"OK": "🟢", "WARNING": "🟡", "CRITICAL": "🔴"}.get(level, "⚪")

    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"## {level_emoji} 数据健康告警 — {symbol} ({mode.upper()})",
        "",
        f"**Overall:** {level} | **Sources:** {total} checked ({pass_c}P {warn_c}W {fail_c}F) | **Latency:** {elapsed:.0f}ms",
        f"**Time:** {now}",
        "",
    ]

    # Failed sources (most important)
    fails = [s for s in report.get("sources", []) if s["status"] in ("fail", "missing")]
    if fails:
        lines.append("### 🔴 Critical Issues")
        for s in fails:
            msg = s.get("message", "")[:100]
            lines.append(f"- **{s['source']}** — {s['primary_code']}")
            if msg:
                lines.append(f"  > {msg}")
        lines.append("")

    # Warnings
    warns = [s for s in report.get("sources", []) if s["status"] == "warn"]
    if warns:
        lines.append("### 🟡 Warnings")
        for s in warns:
            lines.append(f"- **{s['source']}** — {s['primary_code']}")
        lines.append("")

    # Cross-checks
    cross = report.get("cross_checks", [])
    if cross:
        cross_warn = [c for c in cross if c["status"] != "pass"]
        if cross_warn:
            lines.append("### 🔗 Cross-Source Discrepancies")
            for c in cross_warn:
                lines.append(f"- {c['check_name']}: {c.get('message', '')[:120]}")
            lines.append("")

    # Orphans
    orphans = report.get("orphans", [])
    if orphans:
        lines.append("### 👻 Orphan Subsystems")
        for o in orphans:
            lines.append(f"- `{o['source_path']}`: {o.get('detail', '')[:120]}")
        lines.append("")

    # Trend summary
    if level == "OK":
        lines.append("✅ All data sources healthy.")
    elif level == "WARNING":
        lines.append(f"⚠️ {warn_c} sources need attention.")
    else:
        lines.append(f"🚨 {fail_c} CRITICAL failures require immediate action.")

    lines.append("")
    lines.append("---")
    lines.append("QuantOs 实盘告警系统")
    lines.append("QuantOs 实盘告警系统")

    title = f"QUANT OS 告警: DataHealth {symbol} — {level} ({pass_c}/{total} OK)"
    return {"title": title, "text": "\n".join(lines)}


def send_dingtalk(webhook_url: str, title: str, text: str) -> bool:
    """Send a Markdown message to DingTalk webhook."""
    if not webhook_url:
        print("ERROR: No DingTalk webhook URL configured.")
        return False
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            webhook_url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            ok = result.get("errcode") == 0
            if ok:
                print(f"DingTalk: sent OK")
            else:
                print(f"DingTalk: error {result.get('errcode')} — {result.get('errmsg', '')}")
            return ok
    except Exception as exc:
        print(f"DingTalk send failed: {exc}")
        return False


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from core.observability.data_health_service import DataHealthService

    svc = DataHealthService(base_dir=args.base_dir, symbol=args.symbol, mode=args.mode)
    if args.mode == "light":
        report_obj = svc.run_lightweight()
    else:
        report_obj = svc.run_full()
    svc.save_health_state(report_obj)

    # Serialize to dict
    report = {
        "schema_version": report_obj.schema_version,
        "generated_at": report_obj.generated_at,
        "base_dir": report_obj.base_dir,
        "symbol": report_obj.symbol,
        "alert_level": report_obj.alert_level,
        "elapsed_ms": report_obj.elapsed_ms,
        "aggregated": report_obj.aggregated,
        "primary_codes": report_obj.primary_codes,
        "sources": [
            {"source": s.source, "status": s.status.value, "primary_code": s.primary_code, "message": s.message}
            for s in report_obj.sources
        ],
        "cross_checks": [
            {"check_name": c.check_name, "status": c.status.value, "message": c.message}
            for c in report_obj.cross_checks
        ],
        "orphans": [{"source_path": o.source_path, "detail": o.detail} for o in report_obj.orphans],
    }

    msg = format_health_report(report, args.symbol, args.mode)

    if args.dry_run:
        print("=== DRY RUN — DingTalk message preview ===")
        print(f"Title: {msg['title']}")
        print()
        print(msg["text"])
        print("=== END PREVIEW ===")
        return 0

    webhook_url = load_webhook_url(args.base_dir, args.webhook_url)
    if not webhook_url:
        print("ERROR: No DingTalk webhook URL found. Set QUANTOS_DINGTALK_WEBHOOK_URL or pass --webhook-url.")
        return 1

    ok = send_dingtalk(webhook_url, msg["title"], msg["text"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
