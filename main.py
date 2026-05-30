#!/usr/bin/env python3
"""Quant OS Central Hub — 中枢入口.

Three commands you need:

  python main.py live        启动实盘（bridge + intent 双进程）
  python main.py daily-ops   每日运维（shadow → paper → feedback → online → governance）
  python main.py status      快速诊断（health check → diagnostics → leaderboard）

Extended commands:

  python main.py train           Trigger CRT batch training for all lanes.
  python main.py features-update Compute and persist features from MT5.
  python main.py leaderboard     Show brain performance leaderboard.
  python main.py dashboard       Show full daily system dashboard.
  python main.py auto-recover    Check gate state and attempt auto-recovery.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _effective_pnl(entry: dict[str, Any]) -> float:
    """Extract realized P&L from a journal entry, falling back to detail.pnl."""
    pnl = entry.get("pnl")
    if pnl is not None:
        return float(pnl)
    detail = entry.get("detail", {})
    if isinstance(detail, dict):
        detail_pnl = detail.get("pnl")
        if detail_pnl is not None:
            return float(detail_pnl)
    return 0.0


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
# Command: status
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    """Run health checks and print diagnostics (includes live MT5 data when available)."""
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

    result: dict[str, Any] = {
        "liveness": liveness,
        "readiness": readiness,
        "healthy": readiness.get("status") == "ready",
    }

    # ── Live MT5 diagnostics (best-effort, non-blocking) ──
    mt5_diag: dict[str, Any] | None = _probe_mt5(config, args)
    if mt5_diag:
        result["mt5"] = mt5_diag

    # ── Journal statistics ──
    journal_diag = _probe_journal(config, args)
    if journal_diag:
        result["journal"] = journal_diag

    # ── Governance progress ──
    gov_diag = _probe_governance(config, args)
    if gov_diag:
        result["governance"] = gov_diag

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result["healthy"] else 1


def _probe_mt5(config: Any, args: argparse.Namespace) -> dict[str, Any] | None:
    """Probe MT5 for live position/account data. Returns None if unavailable."""
    try:
        import MetaTrader5 as mt5
    except Exception:
        return {"error": "MetaTrader5 package not installed"}

    # Resolve terminal path
    terminal_path = ""
    extensions = getattr(config, "extensions", {}) or {}
    terminal_path = extensions.get("mt5_terminal_path", "")
    if not terminal_path:
        # Try loading from live.yaml mt5 section as fallback
        cfg_path = args.config if hasattr(args, "config") else "configs/live.yaml"
        try:
            import yaml

            with open(_resolve(cfg_path), encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh)
            terminal_path = cfg.get("live_trading", {}).get("mt5_terminal_path", "") or cfg.get(
                "mt5", {}
            ).get("terminal_path", "")
        except Exception:
            pass

    if not terminal_path:
        return {"error": "mt5_terminal_path not configured"}

    if not mt5.initialize(path=terminal_path):
        err = mt5.last_error()
        return {"connected": False, "error": str(err)}

    try:
        diag: dict[str, Any] = {"connected": True}

        # Account
        acc = mt5.account_info()
        if acc:
            diag["account"] = {
                "login": acc.login,
                "balance": round(acc.balance, 2),
                "equity": round(acc.equity, 2),
                "margin": round(acc.margin, 2),
                "free_margin": round(acc.margin_free, 2),
                "margin_level_pct": round(acc.margin_level, 1) if acc.margin_level else None,
            }

        # Positions
        positions = mt5.positions_get()
        diag["positions_count"] = len(positions) if positions else 0
        if positions:
            pos_list = []
            for pos in positions:
                pos_list.append(
                    {
                        "ticket": pos.ticket,
                        "symbol": pos.symbol,
                        "type": "BUY" if pos.type == 0 else "SELL",
                        "volume": pos.volume,
                        "open_price": pos.price_open,
                        "current_price": pos.price_current,
                        "sl": pos.sl,
                        "tp": pos.tp,
                        "profit": round(pos.profit, 2),
                        "swap": round(pos.swap, 2),
                    }
                )
            diag["positions"] = pos_list

        # Pending orders
        orders = mt5.orders_get()
        diag["pending_orders_count"] = len(orders) if orders else 0
        if orders:
            ord_list = []
            for o in orders:
                ord_list.append(
                    {
                        "ticket": o.ticket,
                        "symbol": o.symbol,
                        "type": o.type,
                        "volume": o.volume_initial,
                        "price": o.price_open,
                        "sl": o.sl,
                        "tp": o.tp,
                    }
                )
            diag["pending_orders"] = ord_list

        # Symbol info (XAUUSDc)
        symbol = getattr(args, "symbol", None) or "XAUUSDc"
        tick = mt5.symbol_info_tick(symbol)
        if tick:
            diag["symbol"] = {
                "name": symbol,
                "bid": tick.bid,
                "ask": tick.ask,
                "spread": round(tick.ask - tick.bid, 5) if tick.ask and tick.bid else None,
                "time": str(tick.time) if tick.time else None,
            }

        return diag
    finally:
        mt5.shutdown()


def _probe_journal(config: Any, args: argparse.Namespace) -> dict[str, Any] | None:
    """Analyse live trade journal for statistics."""
    base_dir = getattr(config, "base_dir", "data")
    journal_path = _resolve(base_dir) / "live_trade_journal.jsonl"
    if not journal_path.exists():
        return None

    try:
        import json
        from collections import Counter

        entries = []
        for line in journal_path.read_text(encoding="utf-8").strip().split("\n"):
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        if not entries:
            return None

        actions = Counter(e.get("action") for e in entries)
        statuses = Counter(e.get("ack_status") for e in entries)

        # Count labeled trades (real closes, not auto-orphan)
        labeled = [
            e
            for e in entries
            if e.get("action") == "close"
            and e.get("label")
            and not str(e.get("label", "")).startswith("auto_orphan")
        ]
        labels = Counter(e.get("label") for e in labeled)

        # Open positions (without close)
        opens: dict[str, dict] = {}
        for e in entries:
            if e.get("action") == "open" and e.get("ack_status") == "accepted":
                opens[e["message_id"]] = e
        for e in entries:
            if e.get("action") == "close":
                oid = e.get("open_message_id")
                if oid and oid in opens:
                    del opens[oid]

        total_pnl = sum(_effective_pnl(e) for e in labeled)

        # Brain attribution
        attribution: dict[str, Any] = {}
        try:
            from core.brains.services.brain_attribution_service import BrainAttributionService

            pnl_path = _resolve(base_dir) / "brain_pnl_ledger.json"
            attr_svc = BrainAttributionService(
                journal_path=journal_path,
                pnl_ledger_path=pnl_path if pnl_path.exists() else None,
            )
            attribution = attr_svc.quick_summary()
        except Exception:
            pass

        return {
            "total_entries": len(entries),
            "actions": dict(actions),
            "statuses": dict(statuses),
            "labeled_trades": len(labeled),
            "total_pnl": round(total_pnl, 2),
            "label_distribution": dict(labels),
            "open_positions_in_journal": len(opens),
            "brain_attribution": attribution,
        }
    except Exception as exc:
        return {"error": str(exc)}


def _probe_governance(config: Any, args: argparse.Namespace) -> dict[str, Any] | None:
    """Read governance state and summarise progress toward first promotion."""
    base_dir = getattr(config, "base_dir", "data")
    gov_path = _resolve(base_dir) / "governance_state.json"
    if not gov_path.exists():
        return None

    try:
        import json

        gov = json.loads(gov_path.read_text(encoding="utf-8"))
        brain_states = gov.get("brain_states", {})
        threshold = 10  # trades needed per brain for first promotion

        summary = []
        for bid, bs in brain_states.items():
            summary.append(
                {
                    "brain_id": bid,
                    "status": bs.get("status", "unknown"),
                    "transition_count": bs.get("transition_count", 0),
                    "exposure_limited": bs.get("exposure_limited", False),
                }
            )

        return {
            "brains_registered": len(brain_states),
            "threshold_trades_for_promotion": threshold,
            "brain_summary": summary,
            "transition_log": gov.get("transition_log", [])[-5:],
        }
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Command: train (preserved from legacy)
# ---------------------------------------------------------------------------


def cmd_train(args: argparse.Namespace) -> int:
    """Trigger CRT batch training for all lanes."""
    import subprocess

    plan_path = args.plan
    if plan_path is None:
        plan_path = str(PROJECT_ROOT / "scripts" / "training" / "generate_batch_plan.py")
    print(f"[hub] Triggering batch training plan: {plan_path}")

    plan_file = _resolve(plan_path)
    if not plan_file.exists():
        print(f"[hub] ERROR: plan generator not found: {plan_file}", file=sys.stderr)
        return 2

    batch_parent = Path(args.batch_dir) if args.batch_dir else PROJECT_ROOT / "batch_plans"
    batch_full = batch_parent / args.generation

    # Step 1: Generate the batch plan
    plan_cmd = [
        sys.executable,
        str(plan_file),
        "--generation",
        args.generation,
        "--output-dir",
        str(batch_parent),
    ]
    proc = subprocess.run(plan_cmd, capture_output=False, check=False)
    if proc.returncode != 0:
        print("[hub] ERROR: batch plan generation failed", file=sys.stderr)
        return proc.returncode

    # Step 2: Execute training (only if --execute)
    if args.execute:
        print(f"[hub] Executing training: {batch_full}")
        run_cmd = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "training" / "run_train_batch.py"),
            "--batch-dir",
            str(batch_full),
            "--execute",
        ]
        if args.lane:
            run_cmd.extend(["--lane", args.lane])
        if args.limit is not None:
            run_cmd.extend(["--limit", str(args.limit)])

        proc2 = subprocess.run(run_cmd, capture_output=False, check=False)
        if proc2.returncode != 0:
            print("[hub] ERROR: training execution failed", file=sys.stderr)
            return proc2.returncode

    return 0


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

    import MetaTrader5 as mt5

    if not mt5.initialize(path=mt5_path):
        err = mt5.last_error()
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
        skip_alpha=getattr(args, "skip_alpha", False),
        skip_online_feedback=getattr(args, "skip_online_feedback", False),
        skip_paper_simulation=getattr(args, "skip_paper_simulation", False),
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
# Command: live
# ---------------------------------------------------------------------------


def cmd_live(args: argparse.Namespace) -> int:
    """Start the full live trading pipeline (intent loop + bridge worker).

    Uses Popen (non-blocking) so the hub process stays alive and can
    restart the launcher if it exits.  Previously used subprocess.run()
    which meant a single launcher crash killed the entire system.
    """
    import subprocess
    import time as _time

    launcher = PROJECT_ROOT / "scripts" / "live_launcher.py"
    if not launcher.exists():
        print(f"[hub] ERROR: launcher not found: {launcher}", file=sys.stderr)
        return 2

    # ── No special flags needed — the hub's while-loop KeyboardInterrupt
    # handler gracefully terminates all children.  CREATE_NEW_PROCESS_GROUP
    # on Windows can cause premature termination in some terminal setups.

    # ── Multi-symbol support: auto-detect BTC config ──
    # FIX-083: if configs/live_btc.yaml exists, launch it alongside the primary
    # config.  Each launcher runs independently with its own crash monitoring.
    # Missing BTC config = zero impact on existing gold-only deployment.
    configs_to_launch: list[str] = [args.config]
    _btc_config = PROJECT_ROOT / "configs" / "live_btc.yaml"
    if _btc_config.exists() and str(_btc_config) != str(args.config):
        configs_to_launch.append(str(_btc_config))

    print("[hub] Starting live trading pipeline...")
    for _cfg in configs_to_launch:
        print(f"[hub]   Config: {_cfg}")
    print()

    RESTART_COOLDOWN = 10  # seconds between launcher restarts
    MAX_CONSECUTIVE_CRASHES = 3  # fast crashes within 60s trigger escalation
    ESCALATION_SLEEP = 300  # 5 min pause after fast-crash burst

    # Track crash times per config
    _crash_times: dict[str, list[float]] = {c: [] for c in configs_to_launch}
    _procs: dict[str, subprocess.Popen] = {}

    # Launch all configs
    for _cfg in configs_to_launch:
        _procs[_cfg] = subprocess.Popen(
            [sys.executable, str(launcher), _cfg],
            stdout=None,
            stderr=None,
        )
        print(f"[hub] Launcher[{_cfg}] started (pid={_procs[_cfg].pid})", flush=True)

    while True:
        try:
            # Monitor all processes; wait for any to exit
            for _cfg, _proc in list(_procs.items()):
                _retcode = _proc.poll()
                if _retcode is None:
                    continue  # still running

                print(
                    f"[hub] Launcher[{_cfg}] exited code={_retcode} at {_time.strftime('%H:%M:%S')}",
                    flush=True,
                )

                _now = _time.time()
                _ct = _crash_times[_cfg]
                _ct.append(_now)
                _ct[:] = [t for t in _ct if _now - t < 60]

                if len(_ct) >= MAX_CONSECUTIVE_CRASHES:
                    print(
                        f"[hub] {MAX_CONSECUTIVE_CRASHES} crashes in 60s for {_cfg} — "
                        f"cooling down {ESCALATION_SLEEP}s...",
                        flush=True,
                    )
                    _time.sleep(ESCALATION_SLEEP)
                    _ct.clear()
                else:
                    _time.sleep(RESTART_COOLDOWN)

                # Restart this config
                _procs[_cfg] = subprocess.Popen(
                    [sys.executable, str(launcher), _cfg],
                    stdout=None,
                    stderr=None,
                )
                print(f"[hub] Launcher[{_cfg}] restarted (pid={_procs[_cfg].pid})", flush=True)

            _time.sleep(1.0)  # prevent busy-wait

        except KeyboardInterrupt:
            print("[hub] KeyboardInterrupt — shutting down all launchers...", flush=True)
            for _proc in _procs.values():
                _proc.terminate()
                try:
                    _proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    _proc.kill()
                    _proc.wait()
            print("[hub] All launchers terminated.", flush=True)
            return 0


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
    train_cmd.add_argument(
        "--generation", default="g2026.1", help="Generation tag (default: g2026.1)"
    )
    train_cmd.add_argument(
        "--batch-dir",
        default=None,
        help="Override batch plan parent directory (default: batch_plans/)",
    )
    train_cmd.add_argument(
        "--execute", action="store_true", help="Execute training after plan generation"
    )
    train_cmd.add_argument(
        "--lane", default=None, help="Filter to specific lane (only with --execute)"
    )
    train_cmd.add_argument(
        "--limit", type=int, default=None, help="Limit to first N models (only with --execute)"
    )

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
    daily_cmd.add_argument("--skip-alpha", action="store_true")
    daily_cmd.add_argument("--skip-online-feedback", action="store_true")
    daily_cmd.add_argument("--skip-paper-simulation", action="store_true")
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

    # ---- live ----
    live_cmd = sub.add_parser("live", help="Start full live trading pipeline (one command)")
    live_cmd.add_argument("--config", default="configs/live.yaml", help="Path to live.yaml config")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point — dispatches to subcommand handler."""
    parser = build_parser()
    args = parser.parse_args(argv)

    commands: dict[str, Callable[[argparse.Namespace], int]] = {
        "status": cmd_status,
        "train": cmd_train,
        "auto-recover": cmd_auto_recover,
        "features-update": cmd_features_update,
        "daily-ops": cmd_daily_ops,
        "leaderboard": cmd_leaderboard,
        "dashboard": cmd_dashboard,
        "live": cmd_live,
    }

    handler = commands.get(args.command)
    if handler is None:
        print(f"[hub] Unknown command: {args.command}", file=sys.stderr)
        return 1

    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
