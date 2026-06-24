"""Unified live dispatch policy: market calendar + journal quality (+ optional MT5 spread).

Single writer for live_dispatch_block.flag to avoid races with parallel guard scripts.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.guards.journal_quality import evaluate_guard
from scripts.market_calendar import evaluate_utc_blackout, load_calendar
from scripts.mt5_spread_probe import probe_spread
from scripts.trade_quality_report import build_report

SCHEMA_LIVE_DISPATCH_BLOCK = "live_dispatch_block.v2"
SCHEMA_LIVE_GATE_POLICY = "live_gate_policy.v1"
DEFAULT_CONFIG_PATH = "configs/live_gate_policy.json"
DEFAULT_AUTO_RECOVERY_CYCLES = 3


def load_gate_policy_config(config_path: str | Path | None) -> dict[str, Any]:
    """Load live gate policy config, returning defaults if file missing."""
    defaults: dict[str, Any] = {
        "journal_quality": {
            "max_rejection_rate": 0.2,
            "max_rejections": 3,
            "max_consecutive_rejected": 2,
            "min_samples": 5,
        },
        "spread_probe": {"max_spread_points": 10000.0},
        "auto_recovery": {
            "enabled": False,
            "consecutive_pass_cycles": DEFAULT_AUTO_RECOVERY_CYCLES,
        },
    }
    if config_path is None:
        return defaults
    p = Path(config_path)
    if not p.exists():
        return defaults
    try:
        loaded = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return defaults
    if loaded.get("schema_version") != SCHEMA_LIVE_GATE_POLICY:
        return defaults
    # Merge sections shallowly — loaded values override defaults
    for section in ("journal_quality", "spread_probe", "auto_recovery"):
        if section in loaded and isinstance(loaded[section], dict):
            defaults[section].update(loaded[section])
    return defaults


def _read_auto_recovery_state(state_path: Path) -> int:
    """Return consecutive zero-block passes from state file, or 0."""
    try:
        return int(state_path.read_text(encoding="utf-8").strip())
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        return 0


def _write_auto_recovery_state(state_path: Path, count: int) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(str(count), encoding="utf-8")


def _evaluate_auto_recovery(
    blocked: bool,
    flag_path: Path,
    config: dict[str, Any],
    base_dir: Path,
) -> dict[str, Any]:
    """Track consecutive passes and auto-clear flag when threshold met."""
    auto_cfg = config.get("auto_recovery", {})
    enabled = bool(auto_cfg.get("enabled", False))
    cycles = int(auto_cfg.get("consecutive_pass_cycles", DEFAULT_AUTO_RECOVERY_CYCLES))
    state_path = base_dir / "reports" / "ops_state" / "auto_recovery_state.txt"
    current_pass = _read_auto_recovery_state(state_path)

    auto_result: dict[str, Any] = {
        "enabled": enabled,
        "consecutive_pass_cycles_threshold": cycles,
        "current_consecutive_passes": current_pass,
        "flag_cleared": False,
    }

    if blocked:
        _write_auto_recovery_state(state_path, 0)
        auto_result["current_consecutive_passes"] = 0
        return auto_result

    # Not blocked — increment pass counter
    current_pass += 1
    _write_auto_recovery_state(state_path, current_pass)
    auto_result["current_consecutive_passes"] = current_pass

    if enabled and current_pass >= cycles and flag_path.exists():
        flag_path.unlink()
        auto_result["flag_cleared"] = True

    return auto_result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="live_dispatch_policy")
    parser.add_argument("--base-dir", default="data")
    parser.add_argument(
        "--journal-path", default=None, help="Defaults to <base-dir>/live_trade_journal.jsonl"
    )
    parser.add_argument("--date", default=None, help="UTC date key for journal quality report")
    parser.add_argument("--symbol", default="XAUUSDc")
    parser.add_argument(
        "--flag-path", default=None, help="Defaults to <base-dir>/live_dispatch_block.flag"
    )
    parser.add_argument(
        "--calendar-path", default=None, help="Defaults to <base-dir>/config/market_calendar.json"
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Gate policy config JSON (default: {DEFAULT_CONFIG_PATH}); CLI thresholds override config values",
    )
    parser.add_argument("--max-rejection-rate", type=float, default=None)
    parser.add_argument("--max-rejections", type=int, default=None)
    parser.add_argument("--max-consecutive-rejected", type=int, default=None)
    parser.add_argument("--min-samples", type=int, default=None)
    parser.add_argument("--clear-flag", action="store_true", help="Remove flag file and exit")
    parser.add_argument(
        "--eval-only",
        action="store_true",
        help="Compute blocked/reasons without writing or deleting live_dispatch_block.flag (safe read-only eval)",
    )
    parser.add_argument("--disable-market-calendar", action="store_true")
    parser.add_argument("--disable-journal-quality", action="store_true")
    parser.add_argument("--probe-spread", action="store_true")
    parser.add_argument("--mt5-terminal-path", default=None)
    parser.add_argument("--max-spread-points", type=float, default=None)
    parser.add_argument("--output", default=None)
    return parser


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _write_flag(flag_path: Path, payload: dict[str, Any]) -> None:
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _resolve_threshold(cli_value, config_value, default_value):
    """CLI > config file > hardcoded default."""
    if cli_value is not None:
        return cli_value
    if config_value is not None:
        return config_value
    return default_value


def run_policy(
    args: argparse.Namespace, *, gate_config: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any]]:
    base = Path(args.base_dir)
    journal_path = args.journal_path or str(base / "live_trade_journal.jsonl")
    flag_path = Path(args.flag_path or str(base / "live_dispatch_block.flag"))
    calendar_path = Path(args.calendar_path or str(base / "config" / "market_calendar.json"))

    cfg = gate_config or load_gate_policy_config(None)
    jq = cfg.get("journal_quality", {})
    sp = cfg.get("spread_probe", {})

    if args.clear_flag:
        if flag_path.exists():
            flag_path.unlink()
        # Also reset auto-recovery state when manually clearing
        state_path = base / "reports" / "ops_state" / "auto_recovery_state.txt"
        if state_path.exists():
            state_path.write_text("0", encoding="utf-8")
        payload = {"guard_triggered": False, "flag_cleared": True, "flag_path": str(flag_path)}
        return 0, payload

    now_utc = datetime.now(UTC).replace(tzinfo=None)
    symbol = args.symbol

    sources: dict[str, Any] = {}

    market_blocked = False
    market_reasons: list[str] = []
    if not args.disable_market_calendar:
        cal = load_calendar(calendar_path)
        market_blocked, market_reasons = evaluate_utc_blackout(
            now_utc=now_utc, symbol=symbol, config=cal
        )
        sources["market_calendar"] = {"blocked": market_blocked, "reasons": market_reasons}
    else:
        sources["market_calendar"] = {"blocked": False, "reasons": [], "disabled": True}

    max_rate = _resolve_threshold(args.max_rejection_rate, jq.get("max_rejection_rate"), 0.2)
    max_rej = _resolve_threshold(args.max_rejections, jq.get("max_rejections"), 3)
    max_cons = _resolve_threshold(
        args.max_consecutive_rejected, jq.get("max_consecutive_rejected"), 2
    )
    min_samp = _resolve_threshold(args.min_samples, jq.get("min_samples"), 5)
    max_spread_pts = _resolve_threshold(
        args.max_spread_points, sp.get("max_spread_points"), 10_000.0
    )

    journal_blocked = False
    journal_reasons: list[str] = []
    report: dict[str, Any] = {}
    if not args.disable_journal_quality:
        report = build_report(journal_path=journal_path, date_key=args.date, symbol=None)
        journal_blocked, journal_reasons = evaluate_guard(
            report=report,
            max_rejection_rate=float(max_rate),
            max_rejections=int(max_rej),
            max_consecutive_rejected=int(max_cons),
            min_samples=int(min_samp),
        )
        sources["journal_quality"] = {
            "blocked": journal_blocked,
            "reasons": journal_reasons,
            "report_summary": {
                "date_key": report.get("date_key"),
                "total": report.get("total"),
                "counts": report.get("counts"),
                "rejection_rate": report.get("rejection_rate"),
            },
        }
    else:
        sources["journal_quality"] = {"blocked": False, "reasons": [], "disabled": True}

    spread_src: dict[str, Any] = {"blocked": False, "reasons": [], "skipped": True}
    if args.probe_spread:
        spread_src = probe_spread(
            terminal_path=args.mt5_terminal_path,
            symbol=symbol,
            max_spread_points=float(max_spread_pts),
        )
        sources["spread_probe"] = spread_src
    else:
        sources["spread_probe"] = {"skipped": True, "note": "probe_spread_disabled"}

    spread_blocked = bool(spread_src.get("blocked"))

    reasons_out: list[str] = []
    if market_blocked:
        reasons_out.extend(market_reasons)
    if journal_blocked:
        reasons_out.extend(journal_reasons)
    if spread_blocked:
        reasons_out.extend(list(spread_src.get("reasons") or []))

    blocked = market_blocked or journal_blocked or spread_blocked

    auto_recovery = _evaluate_auto_recovery(
        blocked=blocked,
        flag_path=flag_path,
        config=cfg,
        base_dir=base,
    )

    if not args.eval_only:
        if blocked:
            payload_flag = {
                "schema_version": SCHEMA_LIVE_DISPATCH_BLOCK,
                "generated_at": _utc_now_iso(),
                "sources": sources,
                "blocked": True,
                "reasons": reasons_out,
            }
            _write_flag(flag_path, payload_flag)
        else:
            if flag_path.exists():
                flag_path.unlink()

    result = {
        "schema_version": "live_dispatch_policy.v1",
        "generated_at": _utc_now_iso(),
        "flag_path": str(flag_path),
        "journal_path": journal_path,
        "calendar_path": str(calendar_path),
        "dispatch_blocked": blocked,
        "reasons": reasons_out,
        "sources": sources,
        "eval_only": bool(args.eval_only),
        "auto_recovery": auto_recovery,
    }
    return (1 if blocked else 0), result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    gate_config = load_gate_policy_config(args.config)
    code, result = run_policy(args, gate_config=gate_config)
    rendered = json.dumps(result, indent=2, ensure_ascii=False)
    print(rendered)
    if args.output:
        outp = Path(args.output)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(rendered, encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
