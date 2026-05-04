#!/usr/bin/env python3
"""Quant OS Central Hub — 中枢入口.

Assembles ServiceContainer → RuntimeLoop → DecisionCycleOrchestrator and
drives the live/shadow/training pipeline.

Commands:
  run          Launch the live decision loop (polling trigger source).
  status       Run health checks and print diagnostics.
  train        Trigger CRT batch training for all lanes.
  auto-recover Check gate state and attempt auto-recovery.
  features-update  Compute and persist features from MT5.
  daily-ops    Run full daily governance + monitoring pipeline.
  leaderboard  Show brain performance leaderboard.
  dashboard    Show full daily system dashboard.

Usage:
  python main.py run --env configs/environments/mt5.json
  python main.py status --env configs/environments/mt5.json
  python main.py train
  python main.py auto-recover --config configs/live.yaml
  python main.py daily-ops --dry-run
  python main.py leaderboard
  python main.py dashboard
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore

PROJECT_ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def utc_now_iso_z() -> str:
    """ISO-8601 UTC timestamp with Z suffix (no microseconds)."""
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _resolve(path_str: str) -> Path:
    """Resolve paths relative to PROJECT_ROOT if not absolute."""
    p = Path(path_str)
    if not p.is_absolute():
        return (PROJECT_ROOT / p).resolve()
    return p


def _load_json(path: Path) -> dict[str, Any]:
    """Load a JSON file, returning dict."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file, returning dict."""
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _build_env_config(args: argparse.Namespace) -> Any:
    """Build EnvironmentConfig from CLI --env or --config."""
    from core.deployment.environment_config import Environment, EnvironmentConfig

    config: dict[str, Any] = {}
    terminal_path = None
    default_symbol = "XAUUSD"
    fix_sender = ""
    fix_target = ""
    fix_venue = "FIX_LIVE"
    adapter_name_cfg = "stub"

    if args.env:
        env_path = _resolve(args.env)
        if not env_path.exists():
            print(f"[hub] Environment config not found: {env_path}", file=sys.stderr)
            sys.exit(2)
        env_data = _load_json(env_path)
        terminal_path = env_data.get("terminal_path")
        default_symbol = env_data.get("default_symbol", "XAUUSD")

        config_path = _resolve(getattr(args, "config", "configs/live.yaml"))
        if config_path.exists():
            config = _load_yaml(config_path)
    else:
        config_path = _resolve(args.config)
        if not config_path.exists():
            print(f"[hub] Config not found: {config_path}", file=sys.stderr)
            sys.exit(2)
        config = _load_yaml(config_path)
        mt5_ref = config.get("mt5", {}) if isinstance(config, dict) else {}
        terminal_path = mt5_ref.get("terminal_path") if isinstance(mt5_ref, dict) else None
        default_symbol = (
            mt5_ref.get("default_symbol", "XAUUSD") if isinstance(mt5_ref, dict) else "XAUUSD"
        )

        fix_ref = config.get("fix", {}) if isinstance(config, dict) else {}
        fix_sender = fix_ref.get("sender_comp_id", "") if isinstance(fix_ref, dict) else ""
        fix_target = fix_ref.get("target_comp_id", "") if isinstance(fix_ref, dict) else ""
        fix_venue = fix_ref.get("venue", "FIX_LIVE") if isinstance(fix_ref, dict) else "FIX_LIVE"

        adapter_ref = config.get("adapter", {}) if isinstance(config, dict) else {}
        adapter_name_cfg = (
            adapter_ref.get("name", "stub") if isinstance(adapter_ref, dict) else "stub"
        )

    env_name = getattr(args, "environment", None) or "development"
    env_enum = (
        Environment(env_name)
        if env_name in {e.value for e in Environment}
        else Environment.DEVELOPMENT
    )

    base_dir = getattr(args, "base_dir", None) or str(PROJECT_ROOT / "data")
    live_dispatch = getattr(args, "live", False) or (env_name == "production")

    extensions: dict[str, Any] = {}
    if terminal_path:
        extensions["mt5_terminal_path"] = terminal_path
    if default_symbol:
        extensions["default_symbol"] = default_symbol
    if config:
        mt5_cfg = config.get("mt5", {}) if isinstance(config.get("mt5"), dict) else {}
        norm_path = mt5_cfg.get("normalization_config")
        if norm_path:
            extensions["normalization_config"] = norm_path

        brains_cfg = config.get("brains", {}) if isinstance(config.get("brains"), dict) else {}
        registry_entries = brains_cfg.get("registry_entries", [])
        if registry_entries:
            extensions["brain_registry_entries"] = registry_entries

        features_cfg = (
            config.get("features", {}) if isinstance(config.get("features"), dict) else {}
        )
        feature_store_dir = features_cfg.get("store_dir", "")
        if feature_store_dir:
            extensions["feature_store_dir"] = feature_store_dir

    cli_adapter = getattr(args, "adapter", None)
    adapter_name = cli_adapter if cli_adapter else adapter_name_cfg

    return EnvironmentConfig(
        environment=env_enum,
        base_dir=base_dir,
        adapter_name=adapter_name,
        enable_feedback_loop=live_dispatch,
        enable_audit_log=True,
        enable_metrics=True,
        enable_idempotency=live_dispatch,
        live_read_only=not live_dispatch,
        live_dispatch_enabled=live_dispatch,
        live_allowed_symbols=tuple(getattr(args, "symbols", "") or "XAUUSD"),
        brain_registry_path=getattr(args, "brain_registry", None),
        fix_sender_comp_id=fix_sender,
        fix_target_comp_id=fix_target,
        fix_venue=fix_venue,
        extensions=extensions,
    )


# ---------------------------------------------------------------------------
# Command: run
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    """Launch the live decision loop.

    Builds ServiceContainer → RuntimeLoop → Orchestrator, then runs a
    polling loop that creates a trigger for each cycle.
    """
    config = _build_env_config(args)

    print(f"[hub] Bootstrapping ServiceContainer  env={config.environment.value}")
    from core.deployment.service_container import ServiceContainer

    container = ServiceContainer(config)
    container.build()
    container.build_runtime_loop()
    orchestrator = container.build_orchestrator()

    poll_interval = float(getattr(args, "interval", 1.0))
    symbol = (config.extensions or {}).get("default_symbol", "XAUUSD")
    max_cycles = int(getattr(args, "max_cycles", 0) or 0)

    print(
        f"[hub] Live loop starting  symbol={symbol}  interval={poll_interval}s  "
        f"max_cycles={max_cycles if max_cycles else 'unlimited'}  "
        f"dispatch={config.live_dispatch_enabled}"
    )

    running = True
    cycle_count = 0
    stop_requested = False

    def _on_signal(_sig, _frame):
        nonlocal stop_requested
        print("\n[hub] Signal received – shutting down gracefully...")
        stop_requested = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    while running and not stop_requested:
        try:
            trigger = {
                "symbol": symbol,
                "ts": utc_now_iso_z(),
                "source": "hub.poll_loop",
                "cycle_index": cycle_count,
            }

            feature_source = {
                "symbol": symbol,
                "ts": utc_now_iso_z(),
                "source": "hub.poll_loop",
            }

            outcome = orchestrator.run_cycle(trigger, feature_source)
            cycle_count += 1

            # Brief status per cycle
            verdict_status = (
                outcome.decision_result.verdict.status.value
                if outcome.decision_result
                and hasattr(outcome.decision_result.verdict, "status")
                and hasattr(outcome.decision_result.verdict.status, "value")
                else "N/A"
            )
            print(
                f"[hub] cycle={cycle_count:04d}  id={outcome.cycle_id}  "
                f"verdict={verdict_status}"
            )

            if max_cycles and cycle_count >= max_cycles:
                print(f"[hub] Reached max_cycles={max_cycles} – stopping.")
                running = False

            if running:
                time.sleep(poll_interval)

        except KeyboardInterrupt:
            print("\n[hub] Interrupted – shutting down.")
            running = False
        except Exception as exc:
            print(f"[hub] Cycle error: {exc}", file=sys.stderr)
            if running:
                time.sleep(poll_interval)

    print(f"[hub] Loop finished.  total_cycles={cycle_count}")
    return 0


# ---------------------------------------------------------------------------
# Command: status
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    """Run health checks and print diagnostics."""
    config = _build_env_config(args)

    from core.deployment.service_container import ServiceContainer

    container = ServiceContainer(config)
    container.build()
    container.build_runtime_loop()

    health = container.health_check
    if health is None:
        print("[hub] ERROR: HealthCheckService not available", file=sys.stderr)
        return 1

    liveness = health.liveness()
    readiness = health.readiness()
    result = {
        "liveness": liveness,
        "readiness": readiness,
        "healthy": readiness.get("status") == "ready",
    }

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result["healthy"] else 1


# ---------------------------------------------------------------------------
# Command: train (preserved from legacy)
# ---------------------------------------------------------------------------


def cmd_train(args: argparse.Namespace) -> int:
    """Trigger CRT batch training for all lanes."""
    plan_path = args.plan
    if plan_path is None:
        plan_path = str(PROJECT_ROOT / "scripts" / "training" / "generate_batch_plan.py")
    print(f"[hub] Triggering batch training plan: {plan_path}")
    import subprocess

    plan_file = _resolve(plan_path)
    if not plan_file.exists():
        print(f"[hub] ERROR: plan generator not found: {plan_file}", file=sys.stderr)
        return 2

    proc = subprocess.run(
        [sys.executable, str(plan_file)],
        capture_output=False,
        check=False,
    )
    return proc.returncode


# ---------------------------------------------------------------------------
# Command: features-update
# ---------------------------------------------------------------------------


def cmd_features_update(args: argparse.Namespace) -> int:
    """Compute live features from MT5 and persist to LocalFeatureStore."""
    config = _build_env_config(args)

    print("[hub] Initializing MT5 for feature update...")
    from core.deployment.feature_update_producer import (
        build_v9_schema,
        produce_from_live_computer,
    )
    from core.features.computers.v9_live_computer import V9LiveFeatureComputer
    from core.features.local_feature_store import LocalFeatureStore
    from core.features.update_job import IncrementalFeatureUpdateJob

    mt5_path = (config.extensions or {}).get("mt5_terminal_path")
    if not mt5_path:
        print("[hub] ERROR: mt5_terminal_path not configured in extensions", file=sys.stderr)
        return 2

    default_symbol = (config.extensions or {}).get("default_symbol", "XAUUSD")
    store_dir = (config.extensions or {}).get("feature_store_dir")
    if not store_dir:
        print("[hub] ERROR: feature_store_dir not configured in extensions", file=sys.stderr)
        return 2

    import MetaTrader5 as mt5  # type: ignore[import-untyped]

    if not mt5.initialize(path=mt5_path):  # type: ignore[attr-defined]
        err = mt5.last_error()  # type: ignore[attr-defined]
        print(f"[hub] ERROR: MT5 initialize failed: {err}", file=sys.stderr)
        return 1

    store_path = Path(store_dir)
    if not store_path.is_absolute():
        store_path = PROJECT_ROOT.parent / store_path
    store = LocalFeatureStore(str(store_path))

    computer = V9LiveFeatureComputer(mt5, default_symbol)
    schema = build_v9_schema(default_symbol, "M1")

    print(f"[hub] Computing features for {default_symbol}...")
    job = IncrementalFeatureUpdateJob(
        feature_store=store,
        producer=lambda _: produce_from_live_computer(computer, schema, default_symbol),
        schema=schema,
    )
    result = job.run()

    print(
        f"[hub] Feature update complete: records_written={result.records_written}  symbol={result.symbol}  timeframe={result.timeframe}"
    )
    return 0 if result.records_written > 0 else 3


# ---------------------------------------------------------------------------
# Command: daily-ops
# ---------------------------------------------------------------------------


def cmd_daily_ops(args: argparse.Namespace) -> int:
    """Run the full daily operations pipeline."""
    from scripts.daily_ops import run_daily_ops

    report = run_daily_ops(
        base_dir=args.base_dir,
        skip_shadow=args.skip_shadow,
        skip_feedback=args.skip_feedback,
        skip_governance=args.skip_governance,
        skip_champion=args.skip_champion,
        skip_retraining=args.skip_retraining,
        skip_recap=args.skip_recap,
        dry_run=args.dry_run,
    )

    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(text)

    if args.output:
        out = Path(args.output)
        if not out.is_absolute():
            out = PROJECT_ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")

    if report["errors"] > 0:
        return 2
    if report["actions_total"] > 0:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Command: auto-recover (preserved from legacy)
# ---------------------------------------------------------------------------


def cmd_auto_recover(args: argparse.Namespace) -> int:
    """Check gate state and attempt auto-recovery."""
    config_path = _resolve(args.config)
    if not config_path.exists():
        print(f"[hub] Config not found: {config_path}", file=sys.stderr)
        return 2
    config = _load_yaml(config_path)
    auto_recovery = config.get("gate", {}).get("auto_recovery", {})

    data_dir = _resolve(config.get("pipeline", {}).get("data_dir", "data"))
    flag_path = data_dir / "live_dispatch_block.flag"
    state_path = data_dir / "reports" / "ops_state" / "auto_recovery_state.txt"

    if not flag_path.exists():
        print("[hub] No dispatch block flag — system is unblocked.")
        return 0

    cycles = int(auto_recovery.get("consecutive_pass_cycles", 3))
    current_pass = 0
    try:
        current_pass = int(state_path.read_text(encoding="utf-8").strip())
    except Exception:
        pass

    print(
        f"[hub] auto_recovery: enabled={auto_recovery.get('enabled', False)}  "
        f"threshold={cycles}  current_pass={current_pass}"
    )

    if not auto_recovery.get("enabled", False):
        print("[hub] Auto-recovery DISABLED. Manual flag clear required.")
        return 1

    if current_pass >= cycles:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        flag_path.unlink(missing_ok=True)
        print(
            f"[hub] Auto-recovery TRIGGERED — flag cleared after "
            f"{current_pass}/{cycles} clean passes."
        )
        return 0
    else:
        print(
            f"[hub] Auto-recovery NOT triggered "
            f"({current_pass}/{cycles} clean passes required)."
        )
        return 1


# ---------------------------------------------------------------------------
# Command: leaderboard
# ---------------------------------------------------------------------------


def cmd_leaderboard(args: argparse.Namespace) -> int:
    """Show brain performance leaderboard."""
    from scripts.live_dashboard import build_dashboard

    report = build_dashboard(base_dir=args.base_dir, date_key=args.date)
    lb = report.get("leaderboard", {})

    if args.json:
        output = json.dumps(lb, indent=2, ensure_ascii=False, default=str)
    else:
        # Render leaderboard-focused text view
        output = _render_leaderboard_text(report)

    print(output)

    if args.output:
        out = Path(args.output)
        if not out.is_absolute():
            out = PROJECT_ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(output, encoding="utf-8")

    return 0


def _render_leaderboard_text(report: dict[str, Any]) -> str:
    """Extract and render just the leaderboard section from a dashboard report."""
    return report.get("text", "")


# ---------------------------------------------------------------------------
# Command: dashboard
# ---------------------------------------------------------------------------


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Show full daily system dashboard."""
    from scripts.live_dashboard import build_dashboard

    report = build_dashboard(base_dir=args.base_dir, date_key=args.date)

    if args.json:
        json_report = {k: v for k, v in report.items() if k != "text"}
        output = json.dumps(json_report, indent=2, ensure_ascii=False, default=str)
    else:
        output = report["text"]

    print(output)

    if args.output:
        out = Path(args.output)
        if not out.is_absolute():
            out = PROJECT_ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(output, encoding="utf-8")

    return 0 if len(report.get("errors", [])) == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="main",
        description="Quant OS Central Hub — 中枢入口",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- run ----
    run_cmd = sub.add_parser("run", help="Launch the live decision loop")
    run_cmd.add_argument(
        "--env",
        default="configs/environments/mt5.json",
        help="Path to environment config JSON (e.g., mt5.json)",
    )
    run_cmd.add_argument(
        "--config",
        default="configs/live.yaml",
        help="Fallback path to live.yaml (used if --env not found)",
    )
    run_cmd.add_argument(
        "--environment",
        default="development",
        choices=["development", "simulation", "production", "test", "replay"],
        help="Deployment environment",
    )
    run_cmd.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Enable live dispatch (real orders)",
    )
    run_cmd.add_argument(
        "--adapter",
        default="stub",
        choices=["stub", "file_queue", "mt5", "fix"],
        help="Communication adapter type",
    )
    run_cmd.add_argument(
        "--symbols",
        default="XAUUSD",
        help="Comma-separated live allowed symbols",
    )
    run_cmd.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Polling interval in seconds between cycles",
    )
    run_cmd.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="Maximum cycles to run (0 = unlimited)",
    )
    run_cmd.add_argument(
        "--base-dir",
        default=None,
        help="Base directory for data/ledgers (default: PROJECT_ROOT/data)",
    )
    run_cmd.add_argument(
        "--brain-registry",
        default=None,
        help="Path to brain registry entries JSON",
    )

    # ---- status ----
    status_cmd = sub.add_parser("status", help="Run health checks")
    status_cmd.add_argument(
        "--env",
        default="configs/environments/mt5.json",
        help="Path to environment config JSON",
    )
    status_cmd.add_argument(
        "--config",
        default="configs/live.yaml",
        help="Fallback path to live.yaml",
    )
    status_cmd.add_argument(
        "--environment",
        default="development",
        choices=["development", "simulation", "production", "test", "replay"],
    )
    status_cmd.add_argument(
        "--adapter", default="stub", choices=["stub", "file_queue", "mt5", "fix"]
    )
    status_cmd.add_argument("--base-dir", default=None)

    # ---- train ----
    train_cmd = sub.add_parser("train", help="Trigger CRT batch training for all lanes")
    train_cmd.add_argument("--plan", default=None, help="Path to batch plan JSON (optional)")

    # ---- auto-recover ----
    ar_cmd = sub.add_parser("auto-recover", help="Check gate state and attempt auto-recovery")
    ar_cmd.add_argument("--config", default="configs/live.yaml", help="Path to live.yaml")

    # ---- features-update ----
    feat_cmd = sub.add_parser(
        "features-update",
        help="Compute and persist features to LocalFeatureStore from MT5",
    )
    feat_cmd.add_argument("--config", default="configs/live.yaml", help="Path to live.yaml")
    feat_cmd.add_argument(
        "--env",
        default="configs/environments/mt5.json",
        help="Path to environment config JSON",
    )

    # ---- daily-ops ----
    daily_cmd = sub.add_parser("daily-ops", help="Run full daily operations pipeline")
    daily_cmd.add_argument("--base-dir", default="data", help="Base data directory")
    daily_cmd.add_argument(
        "--dry-run", action="store_true", help="Assess without applying transitions"
    )
    daily_cmd.add_argument("--skip-shadow", action="store_true")
    daily_cmd.add_argument("--skip-feedback", action="store_true")
    daily_cmd.add_argument("--skip-governance", action="store_true")
    daily_cmd.add_argument("--skip-champion", action="store_true")
    daily_cmd.add_argument("--skip-retraining", action="store_true")
    daily_cmd.add_argument("--skip-recap", action="store_true")
    daily_cmd.add_argument("--output", type=Path, default=None, help="Write report JSON to file")

    # ---- leaderboard ----
    lb_cmd = sub.add_parser("leaderboard", help="Show brain performance leaderboard")
    lb_cmd.add_argument("--base-dir", default="data", help="Base data directory")
    lb_cmd.add_argument("--date", default=None, help="UTC date key; default=today")
    lb_cmd.add_argument("--json", action="store_true", help="Output JSON instead of text")
    lb_cmd.add_argument("--output", type=Path, default=None, help="Write output to file")

    # ---- dashboard ----
    dash_cmd = sub.add_parser("dashboard", help="Show full daily system dashboard")
    dash_cmd.add_argument("--base-dir", default="data", help="Base data directory")
    dash_cmd.add_argument("--date", default=None, help="UTC date key; default=today")
    dash_cmd.add_argument("--json", action="store_true", help="Output JSON instead of text")
    dash_cmd.add_argument("--output", type=Path, default=None, help="Write output to file")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point — dispatches to subcommand handler."""
    parser = build_parser()
    args = parser.parse_args(argv)

    commands: dict[str, Callable[[argparse.Namespace], int]] = {
        "run": cmd_run,
        "status": cmd_status,
        "train": cmd_train,
        "auto-recover": cmd_auto_recover,
        "features-update": cmd_features_update,
        "daily-ops": cmd_daily_ops,
        "leaderboard": cmd_leaderboard,
        "dashboard": cmd_dashboard,
    }

    handler = commands.get(args.command)
    if handler is None:
        print(f"[hub] Unknown command: {args.command}", file=sys.stderr)
        return 1

    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
