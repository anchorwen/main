"""Periodic auto healthcheck for live ops stack.

Run from repo root:
  python scripts/live_auto_healthcheck.py --base-dir data --symbol XAUUSDc

Intended to be called by a scheduled task or cron every N minutes. Outputs a
JSON report with primary codes and an alert-level summary.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _collect_outbox(outbox_root: Path, *, limit: int = 20) -> tuple[int, list[str]]:
    if not outbox_root.exists():
        return 0, []
    paths = sorted(outbox_root.rglob("*.mt5.json"))
    sample = [str(p.as_posix()) for p in paths[:limit]]
    return len(paths), sample


def _collect_receipts(receipt_root: Path, *, limit: int = 10) -> tuple[int, list[str]]:
    if not receipt_root.exists():
        return 0, []
    paths = sorted(receipt_root.rglob("*.ack.json"))
    sample = [str(p.as_posix()) for p in paths[:limit]]
    return len(paths), sample


def _flag_status(flag_path: Path) -> dict[str, Any]:
    status: dict[str, Any] = {
        "exists": flag_path.exists(),
        "blocked_payload": None,
        "mtime_utc": None,
    }
    if status["exists"]:
        try:
            raw = json.loads(flag_path.read_text(encoding="utf-8"))
            status["blocked_payload"] = bool(raw.get("blocked", True))
        except (json.JSONDecodeError, OSError):
            status["blocked_payload"] = None
        try:
            mtime = flag_path.stat().st_mtime
            status["mtime_utc"] = datetime.fromtimestamp(mtime, UTC).isoformat()
        except OSError:
            pass
    return status


def _outbox_staleness_report(outbox_root: Path, *, max_age_minutes: int = 10) -> dict[str, Any]:
    """Find .mt5.json files older than max_age_minutes and report the count."""
    if not outbox_root.exists():
        return {"stale_count": 0, "stale_paths": [], "max_age_minutes": max_age_minutes}
    cutoff = datetime.now(UTC).replace(tzinfo=None).timestamp() - (max_age_minutes * 60)
    stale: list[str] = []
    for p in outbox_root.rglob("*.mt5.json"):
        try:
            if p.stat().st_mtime < cutoff:
                stale.append(str(p.as_posix()))
        except OSError:
            stale.append(str(p.as_posix()))
    return {
        "stale_count": len(stale),
        "stale_paths": sorted(stale),
        "max_age_minutes": max_age_minutes,
    }


def _run_policy_eval(base_dir: Path, symbol: str) -> dict[str, Any]:
    """Run live_dispatch_policy in eval-only mode, returning result or error."""
    try:
        from scripts.live_dispatch_policy import build_parser as policy_parser
        from scripts.live_dispatch_policy import load_gate_policy_config, run_policy
    except Exception:
        return {"error": "import_failed"}

    try:
        flag = str(base_dir / "live_dispatch_block.flag")
        p_args = policy_parser().parse_args(
            ["--base-dir", str(base_dir), "--symbol", symbol, "--eval-only", "--flag-path", flag]
        )
        config = load_gate_policy_config(None)
        _code, result = run_policy(p_args, gate_config=config)
        return result
    except Exception as exc:
        return {"error": str(exc)}


def _bridge_supervisor_status(base_dir: Path) -> dict[str, Any]:
    """Check if bridge supervisor log was written recently (< 5 minutes ago)."""
    log_path = base_dir / "reports" / "ops_logs" / "bridge_supervisor.log"
    status: dict[str, Any] = {
        "log_exists": log_path.exists(),
        "fresh": False,
        "note": "check if bridge log was written in last 5 min",
    }
    if log_path.exists():
        try:
            age_s = datetime.now(UTC).replace(tzinfo=None).timestamp() - log_path.stat().st_mtime
            status["age_seconds"] = round(age_s, 1)
            status["fresh"] = age_s < 300
        except OSError:
            pass
    return status


def _derive_alert_level(codes: list[str]) -> str:
    critical = {"POLICY_BLOCKED_ACTIVE", "OUTBOX_STALE"}
    warn = {
        "NO_OUTBOX_INTENTS",
        "POLICY_WOULD_BLOCK",
        "OUTBOX_NO_CONSUMER",
        "BRIDGE_LOG_STALE",
        "JOURNAL_ZERO_ROWS",
    }
    code_set = set(codes)
    if code_set & critical:
        return "CRITICAL"
    if code_set & warn:
        return "WARNING"
    return "OK"


def build_report(
    base_dir: Path,
    symbol: str,
    *,
    outbox_max_age_minutes: int = 10,
) -> dict[str, Any]:
    outbox_root = base_dir / "mt5_outbox"
    receipt_root = base_dir / "receipts"
    flag_path = base_dir / "live_dispatch_block.flag"

    pending, outbox_sample = _collect_outbox(outbox_root)
    receipt_count, receipt_sample = _collect_receipts(receipt_root)
    flag = _flag_status(flag_path)
    staleness = _outbox_staleness_report(outbox_root, max_age_minutes=outbox_max_age_minutes)
    policy = _run_policy_eval(base_dir, symbol)
    bridge = _bridge_supervisor_status(base_dir)

    codes: list[str] = []

    if pending == 0:
        codes.append("NO_OUTBOX_INTENTS")
    if staleness["stale_count"] > 0:
        codes.append("OUTBOX_STALE")
    if policy.get("dispatch_blocked"):
        codes.append("POLICY_WOULD_BLOCK")
    if flag["exists"] and flag.get("blocked_payload", True):
        codes.append("PROTECTION_FLAG_ACTIVE")
    if not bridge["fresh"]:
        codes.append("BRIDGE_LOG_STALE")
    if receipt_count == 0 and pending > 0:
        codes.append("OUTBOX_NO_CONSUMER")

    alert = _derive_alert_level(codes)

    return {
        "schema_version": "live_auto_healthcheck.v1",
        "generated_at": _utc_now_iso(),
        "base_dir": str(base_dir.resolve()),
        "symbol": symbol,
        "alert_level": alert,
        "primary_codes": codes,
        "flags": {
            "outbox_pending": pending,
            "outbox_sample_paths": outbox_sample,
            "outbox_staleness": staleness,
            "receipt_count": receipt_count,
            "receipt_sample_paths": receipt_sample,
            "flag": flag,
            "bridge_supervisor": bridge,
        },
        "policy_eval_only": policy,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="live_auto_healthcheck")
    p.add_argument("--base-dir", default="data")
    p.add_argument("--symbol", default="XAUUSDc")
    p.add_argument("--outbox-max-age-minutes", type=int, default=10)
    p.add_argument("--output", default=None, help="Write JSON report to file")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = Path(args.base_dir)
    report = build_report(
        base_dir=base,
        symbol=args.symbol,
        outbox_max_age_minutes=args.outbox_max_age_minutes,
    )
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(text)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")

    # Return non-zero if critical so scheduled tasks can detect issues
    if report["alert_level"] == "CRITICAL":
        return 2
    if report["alert_level"] == "WARNING":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
