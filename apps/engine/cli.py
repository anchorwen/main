"""Unified CLI entry point for the decision system.

Global: ``--env``; ``--validation-mode``; ``--no-metrics`` or ``--force-metrics``
(mutually exclusive) override per-environment ``enable_metrics`` for all
``ServiceContainer``-backed subcommands and ``validate``.  Subcommands that do
not build a container (e.g. ``backtest``, ``alpha`` file operations, ``runtime``
over ledger files) ignore these flags.
Global options (``--base-dir``, ``--env``, metrics) may appear in any order as
long as they all come before the subcommand (e.g.
``--env production --no-metrics --base-dir ./data status``,
not ``status --base-dir``).

Usage:
    python -m apps.engine.cli run --base-dir ./data
    python -m apps.engine.cli selftest --base-dir ./data
    python -m apps.engine.cli --env test --force-metrics selftest --base-dir ./data
    python -m apps.engine.cli diagnose health --base-dir ./data
    python -m apps.engine.cli diagnose metrics --base-dir ./data
    python -m apps.engine.cli backtest --scenarios scenarios.json --base-dir ./data
"""

import argparse
import json
import sys
from json import JSONDecodeError
from pathlib import Path

from apps.engine.backtest_runner import BacktestRunner
from apps.engine.diagnostics_cli import DiagnosticsCLI
from apps.engine.system_facade import SystemFacade, SystemSelfTest
from core.alpha import (
    AlphaAllocationPolicy,
    AlphaLifecycleService,
    AlphaLifecycleState,
    AlphaPerformanceStore,
    AlphaPortfolioAllocator,
    AlphaPromotionGate,
    AlphaRecord,
    AlphaRegistry,
    AlphaRiskBudgetExporter,
)
from core.alpha.schema_versions import SCHEMA_ALPHA_RISK_BUDGET
from core.deployment.domain_keys import (
    ENGINE_CONFIG_KEY_HOT_RELOAD,
    EVIDENCE_SECTION_ENGINE_CONFIG,
    VALIDATION_MODE_DEEP,
    VALIDATION_MODE_FAST,
)
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.lifecycle_manager import LifecycleManager
from core.deployment.operational_support import ConfigValidator
from core.deployment.scheduler_service import SchedulerService
from core.deployment.schema_versions import (
    SCHEMA_ENGINE_CONFIG_RELOAD_RESULT,
    SCHEMA_ENGINE_CONFIG_STATUS,
)
from core.deployment.service_container import ServiceContainer
from core.deployment.state_persistence import StatePersistence
from core.execution import PaperExecutionGateway
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
from core.observability.alert_service import AlertService, LogAlertChannel
from core.runtime import (
    AlphaBudgetContractError,
    AlphaBudgetUsageReporter,
    AlphaBudgetUsageStore,
    AlphaRiskBudgetGate,
    ExecutionGatewayRouter,
    OrderSizingPolicy,
    RuntimeCycleReplay,
    RuntimeEvidenceReader,
    RuntimeEvidenceWriter,
    RuntimeExecutionApprovalChain,
    RuntimeExecutionPipeline,
    RuntimeGovernanceGate,
    RuntimeRiskGate,
    RuntimeSummaryService,
    SignalOrderRequestBuilder,
)
from core.runtime.schema_versions import (
    SCHEMA_ALPHA_BATCH_EVALUATION,
    SCHEMA_ALPHA_BUDGET_USAGE,
    SCHEMA_ALPHA_BUDGET_USAGE_REPORT,
    SCHEMA_ALPHA_LIVE_BRIDGE_INGESTION,
    SCHEMA_ALPHA_RUNTIME_INGESTION,
    SCHEMA_CLI_ERROR,
    SCHEMA_ENGINE_STATUS,
    SCHEMA_RUNTIME_CYCLE_LIST,
    SCHEMA_RUNTIME_RUN_PAPER,
)
from core.strategies.examples import ThresholdAlphaAgent
from core.strategies.registry import StrategyPluginRegistry, StrategyPluginRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engine", description="Decision Engine CLI")
    parser.add_argument("--base-dir", default="./data")
    parser.add_argument(
        "--env", choices=["development", "production", "test"], default="development"
    )
    parser.add_argument(
        "--live-read-only",
        action="store_true",
        help=(
            "Enable live read-only guard: block real dispatch attempts while keeping"
            " runtime visibility."
        ),
    )
    parser.add_argument(
        "--mt5-terminal-path",
        default=None,
        help="Optional MT5 terminal executable path for live integration prechecks.",
    )
    parser.add_argument(
        "--adapter-name",
        choices=["stub", "file_queue", "mt5"],
        default=None,
        help="Dispatch adapter backend. Use mt5 for MT5 bridge handoff.",
    )
    parser.add_argument(
        "--dispatch-outbox-dir",
        default=None,
        help="Outbox directory used by file_queue adapter.",
    )
    parser.add_argument(
        "--mt5-outbox-dir",
        default=None,
        help="Outbox directory used by mt5 adapter handoff.",
    )
    parser.add_argument(
        "--enable-live-dispatch",
        action="store_true",
        help=(
            "Enable real dispatch path (keep disabled by default; combine with symbol"
            " allowlist for micro rollout)."
        ),
    )
    parser.add_argument(
        "--live-allowed-symbol",
        action="append",
        default=[],
        help="Symbol allowlist entry for live dispatch. Repeat for multiple symbols.",
    )
    parser.add_argument(
        "--validation-mode",
        choices=[VALIDATION_MODE_FAST, VALIDATION_MODE_DEEP],
        default=None,
        help=(
            "Default validation depth for deployment/governance commands"
            " (can be overridden per subcommand)"
        ),
    )
    mgroup = parser.add_mutually_exclusive_group()
    mgroup.add_argument(
        "--no-metrics",
        action="store_true",
        help="Disable in-process metrics (overrides env default, e.g. production)",
    )
    mgroup.add_argument(
        "--force-metrics",
        action="store_true",
        help=(
            "Enable in-process metrics (overrides env default, e.g. test which defaults"
            " metrics off)"
        ),
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="Start the engine in foreground")
    sub.add_parser("selftest", help="Run system self-test")
    sub.add_parser("validate", help="Validate configuration")

    ecfg = sub.add_parser("config", help="Engine runtime JSON config (hot reload path and apply)")
    ecfg.add_argument("action", choices=["status", "reload"])

    diag = sub.add_parser("diagnose", help="Diagnostics subcommands")
    diag.add_argument(
        "subcommand",
        nargs="?",
        default="snapshot",
        choices=["health", "metrics", "snapshot", "brain", "audit", "positions", "orders"],
    )

    bt = sub.add_parser("backtest", help="Run backtest from scenarios file")
    bt.add_argument("--scenarios", required=True, help="Path to JSON scenarios file")
    bt.add_argument("--output", default=None, help="Path to save report")
    bt.add_argument("--equity", type=float, default=100000.0)

    sub.add_parser("status", help="Show system status")

    ready = sub.add_parser("readiness", help="Generate release readiness report")
    ready.add_argument("--output", default=None, help="Path to save readiness report JSON")
    ready.add_argument(
        "--validation-mode",
        choices=[VALIDATION_MODE_FAST, VALIDATION_MODE_DEEP],
        default=argparse.SUPPRESS,
        help="Validation depth: fast (core checks) or deep (core + compliance checks)",
    )

    rb = sub.add_parser("runbook", help="Run operational runbook")
    rb.add_argument("name", choices=["preflight", "doctor", "postmortem"])
    rb.add_argument("--output", default=None, help="Path to save runbook result JSON")
    rb.add_argument("--label", default=None, help="Label for postmortem state snapshot")
    rb.add_argument(
        "--validation-mode",
        choices=[VALIDATION_MODE_FAST, VALIDATION_MODE_DEEP],
        default=argparse.SUPPRESS,
        help="Validation depth: fast (core checks) or deep (core + compliance checks)",
    )

    slo = sub.add_parser("slo", help="Evaluate SLO and error budget")
    slo.add_argument("--output", default=None, help="Path to save SLO report JSON")

    gate = sub.add_parser("gate", help="Evaluate deployment release gate")
    gate.add_argument("--output", default=None, help="Path to save release gate report JSON")
    gate.add_argument(
        "--non-strict", action="store_true", help="Warn instead of block on warning-level signals"
    )
    gate.add_argument(
        "--validation-mode",
        choices=[VALIDATION_MODE_FAST, VALIDATION_MODE_DEEP],
        default=argparse.SUPPRESS,
        help="Validation depth: fast (core checks) or deep (core + compliance checks)",
    )
    gate.add_argument(
        "--alpha-budget-usage-report",
        default=None,
        help=f"{SCHEMA_ALPHA_BUDGET_USAGE_REPORT} JSON path for release gate input",
    )

    ev = sub.add_parser("evidence", help="Build or verify release evidence bundle")
    ev.add_argument("action", choices=["build", "verify"])
    ev.add_argument(
        "--output-dir", default="reports/evidence", help="Directory for evidence bundle output"
    )
    ev.add_argument("--label", default=None, help="Evidence bundle label")
    ev.add_argument("--manifest", default=None, help="Manifest path for verification")
    ev.add_argument(
        "--alpha-budget-usage-report",
        default=None,
        help=f"{SCHEMA_ALPHA_BUDGET_USAGE_REPORT} JSON path to include in evidence bundle",
    )
    ev.add_argument(
        "--validation-mode",
        choices=[VALIDATION_MODE_FAST, VALIDATION_MODE_DEEP],
        default=argparse.SUPPRESS,
        help="Validation depth: fast (core checks) or deep (core + compliance checks)",
    )

    dp = sub.add_parser("deploy-plan", help="Generate deployment rollout plan")
    dp.add_argument("--version", default="0.1.0")
    dp.add_argument("--strategy", choices=["standard", "canary", "shadow"], default="standard")
    dp.add_argument("--output", default=None, help="Path to save deployment plan JSON")
    dp.add_argument(
        "--evidence-dir", default=None, help="Optional evidence bundle output directory"
    )
    dp.add_argument("--non-strict", action="store_true", help="Allow warning-level gate signals")
    dp.add_argument(
        "--alpha-budget-usage-report",
        default=None,
        help=f"{SCHEMA_ALPHA_BUDGET_USAGE_REPORT} JSON path for deployment gate input",
    )
    dp.add_argument(
        "--validation-mode",
        choices=[VALIDATION_MODE_FAST, VALIDATION_MODE_DEEP],
        default=argparse.SUPPRESS,
        help="Validation depth: fast (core checks) or deep (core + compliance checks)",
    )

    de = sub.add_parser("deploy-exec", help="Execute deployment plan in dry-run mode")
    de.add_argument("--plan", default=None, help="Existing deployment plan JSON path")
    de.add_argument("--output", default=None, help="Path to save execution result JSON")
    de.add_argument("--version", default="0.1.0")
    de.add_argument("--strategy", choices=["standard", "canary", "shadow"], default="standard")
    de.add_argument(
        "--validation-mode",
        choices=[VALIDATION_MODE_FAST, VALIDATION_MODE_DEEP],
        default=argparse.SUPPRESS,
        help="Validation depth: fast (core checks) or deep (core + compliance checks)",
    )

    rd = sub.add_parser("rollback-drill", help="Run dry-run rollback drill")
    rd.add_argument("--version", default="0.1.0")
    rd.add_argument("--reason", default="manual_drill")
    rd.add_argument("--evidence-manifest", default=None)
    rd.add_argument("--output", default=None, help="Path to save rollback drill result JSON")
    rd.add_argument(
        "--validation-mode",
        choices=[VALIDATION_MODE_FAST, VALIDATION_MODE_DEEP],
        default=argparse.SUPPRESS,
        help="Validation depth: fast (core checks) or deep (core + compliance checks)",
    )

    ot = sub.add_parser(
        "ops-timeline", help="Record, list, summarize, or export operations timeline"
    )
    ot.add_argument(
        "action",
        choices=[
            "record-gate",
            "record-deploy-exec",
            "record-rollback",
            "record-evidence",
            "list",
            "summary",
            "export",
            "clear",
        ],
    )
    ot.add_argument("--input", default=None, help="Input JSON report for record actions")
    ot.add_argument("--event-type", default=None, help="Filter event type for list")
    ot.add_argument("--limit", type=int, default=None, help="Limit events for list")
    ot.add_argument("--output", default=None, help="Export output path")
    ot.add_argument("--actor", default="system")

    pm = sub.add_parser(
        "postmortem-report", help="Generate postmortem report from operations timeline"
    )
    pm.add_argument("--incident-id", required=True)
    pm.add_argument("--title", default="Operational Postmortem")
    pm.add_argument("--severity", default="informational")
    pm.add_argument("--output", default=None, help="Path to save postmortem report JSON")
    pm.add_argument(
        "--validation-mode",
        choices=[VALIDATION_MODE_FAST, VALIDATION_MODE_DEEP],
        default=argparse.SUPPRESS,
        help="Validation depth: fast (core checks) or deep (core + compliance checks)",
    )

    rp = sub.add_parser("release-pipeline", help="Run full dry-run release pipeline")
    rp.add_argument("--version", default="0.1.0")
    rp.add_argument("--strategy", choices=["standard", "canary", "shadow"], default="standard")
    rp.add_argument("--output-dir", default=None, help="Directory for pipeline artifacts")
    rp.add_argument("--output", default=None, help="Path to save pipeline summary JSON")
    rp.add_argument("--non-strict", action="store_true", help="Allow warning-level gate signals")
    rp.add_argument(
        "--alpha-budget-usage-report",
        default=None,
        help=f"{SCHEMA_ALPHA_BUDGET_USAGE_REPORT} JSON path for release pipeline gate/evidence",
    )
    rp.add_argument(
        "--validation-mode",
        choices=[VALIDATION_MODE_FAST, VALIDATION_MODE_DEEP],
        default=argparse.SUPPRESS,
        help="Validation depth: fast (core checks) or deep (core + compliance checks)",
    )
    rp.add_argument("--actor", default="system")

    rc = sub.add_parser("release-cert", help="Generate or verify release certificate")
    rc.add_argument("action", choices=["certify", "verify"])
    rc.add_argument("--pipeline", default=None, help="Release pipeline summary JSON path")
    rc.add_argument("--certificate", default=None, help="Release certificate JSON path for verify")
    rc.add_argument("--approver", default="system")
    rc.add_argument("--output", default=None, help="Path to save release certificate JSON")

    rr = sub.add_parser(
        "release-registry", help="Register, list, verify, or export release certificates"
    )
    rr.add_argument(
        "action", choices=["register", "list", "summary", "latest", "verify", "export", "clear"]
    )
    rr.add_argument("--certificate", default=None, help="Release certificate JSON path")
    rr.add_argument("--record-id", default=None, help="Registry record id for verify")
    rr.add_argument("--version", default=None, help="Filter by version")
    rr.add_argument(
        "--certified", action="store_true", help="Filter only certified records for list"
    )
    rr.add_argument("--output", default=None, help="Export output path")
    rr.add_argument("--actor", default="system")

    ca = sub.add_parser("compliance-audit", help="Generate compliance audit report")
    ca.add_argument("--output", default=None, help="Path to save compliance audit report JSON")
    ca.add_argument(
        "--validation-mode",
        choices=[VALIDATION_MODE_FAST, VALIDATION_MODE_DEEP],
        default=argparse.SUPPRESS,
        help="Validation depth: fast (core checks) or deep (core + compliance checks)",
    )

    cm = sub.add_parser("compliance-matrix", help="Generate compliance control matrix")
    cm.add_argument("--output", default=None, help="Path to save compliance control matrix JSON")
    cm.add_argument(
        "--validation-mode",
        choices=[VALIDATION_MODE_FAST, VALIDATION_MODE_DEEP],
        default=argparse.SUPPRESS,
        help="Validation depth: fast (core checks) or deep (core + compliance checks)",
    )

    fa = sub.add_parser("final-audit", help="Run consolidated pre-production final audit")
    fa.add_argument("--output", default=None, help="Path to save final audit report JSON")
    fa.add_argument(
        "--validation-mode",
        choices=[VALIDATION_MODE_FAST, VALIDATION_MODE_DEEP],
        default=argparse.SUPPRESS,
        help="Validation depth: fast (core checks) or deep (core + compliance checks)",
    )

    om = sub.add_parser(
        "ops-maturity", help="Compute operations maturity score (0–100) with Alpha budget pillar"
    )
    om.add_argument("--output", default=None, help="Path to save ops maturity report JSON")
    om.add_argument(
        "--min-score",
        type=float,
        default=None,
        help="Override EnvironmentConfig.ops_maturity_min_score for exit code and report threshold",
    )
    om.add_argument(
        "--validation-mode",
        choices=[VALIDATION_MODE_FAST, VALIDATION_MODE_DEEP],
        default=argparse.SUPPRESS,
        help="Validation depth: fast (core checks) or deep (core + compliance checks)",
    )

    rt = sub.add_parser("runtime", help="Inspect runtime cycle evidence")
    rt.add_argument("action", choices=["list-cycles", "replay", "inspect", "run-paper", "summary"])
    rt.add_argument(
        "--cycle-id", default=None, help="Runtime cycle id for replay/inspect/run-paper"
    )
    rt.add_argument(
        "--ledger-dir", default=None, help="Ledger base directory, defaults to <base-dir>/ledger"
    )
    rt.add_argument("--output", default=None, help="Path to save JSON output")
    rt.add_argument("--symbol", default="XAUUSD", help="Symbol for runtime run-paper")
    rt.add_argument(
        "--feature", action="append", default=[], help="Feature key=value for runtime run-paper"
    )
    rt.add_argument("--price", type=float, default=None, help="Market price for runtime run-paper")
    rt.add_argument("--bid", type=float, default=None, help="Bid price for runtime run-paper")
    rt.add_argument("--ask", type=float, default=None, help="Ask price for runtime run-paper")
    rt.add_argument(
        "--base-quantity", type=float, default=10.0, help="Base quantity for runtime run-paper"
    )
    rt.add_argument("--strategy-id", default="alpha1", help="Strategy id for runtime run-paper")
    rt.add_argument(
        "--feature-name",
        default="ema_bias",
        help="Feature name used by reference threshold strategy",
    )
    rt.add_argument(
        "--buy-threshold", type=float, default=1.0, help="Buy threshold for reference strategy"
    )
    rt.add_argument(
        "--sell-threshold", type=float, default=-1.0, help="Sell threshold for reference strategy"
    )
    rt.add_argument("--limit", type=int, default=None, help="Limit cycles for runtime summary")
    rt.add_argument(
        "--alpha-risk-budget",
        default=None,
        help=f"{SCHEMA_ALPHA_RISK_BUDGET} JSON path for runtime run-paper",
    )
    rt.add_argument(
        "--alpha-budget-usage",
        default=None,
        help=f"{SCHEMA_ALPHA_BUDGET_USAGE} JSON path for persistent run-paper daily counters",
    )

    alpha = sub.add_parser("alpha", help="Manage Alpha Factory registry and lifecycle")
    alpha.add_argument(
        "action",
        choices=[
            "register",
            "list",
            "transition",
            "performance",
            "evaluate",
            "ingest-runtime",
            "ingest-live-bridge",
            "batch-evaluate",
            "allocate",
            "export-risk-budget",
            "budget-usage",
            "budget-usage-reset",
        ],
    )
    alpha.add_argument("--alpha-id", default=None)
    alpha.add_argument("--name", default=None)
    alpha.add_argument("--version", default="1.0")
    alpha.add_argument("--strategy-id", default=None)
    alpha.add_argument(
        "--state", default="candidate", choices=[state.value for state in AlphaLifecycleState]
    )
    alpha.add_argument(
        "--to-state", default=None, choices=[state.value for state in AlphaLifecycleState]
    )
    alpha.add_argument("--reason", default="manual")
    alpha.add_argument(
        "--metric", action="append", default=[], help="Metric key=value for alpha performance"
    )
    alpha.add_argument(
        "--registry-file",
        default=None,
        help="Alpha registry JSON path, defaults to <base-dir>/alpha_registry.json",
    )
    alpha.add_argument(
        "--performance-file",
        default=None,
        help="Alpha performance JSON path, defaults to <base-dir>/alpha_performance.json",
    )
    alpha.add_argument(
        "--ledger-dir", default=None, help="Runtime ledger directory for alpha ingest-runtime"
    )
    alpha.add_argument(
        "--limit", type=int, default=None, help="Limit runtime cycles for alpha ingest-runtime"
    )
    alpha.add_argument(
        "--journal-path", default=None, help="Live trade journal JSONL for alpha ingest-live-bridge"
    )
    alpha.add_argument(
        "--date", default=None, help="UTC date key (YYYY-MM-DD) for alpha ingest-live-bridge"
    )
    alpha.add_argument(
        "--symbol", default=None, help="Optional journal symbol filter for alpha ingest-live-bridge"
    )
    alpha.add_argument(
        "--apply", action="store_true", help="Apply promotion gate decision to lifecycle"
    )
    alpha.add_argument(
        "--total-notional", type=float, default=100000.0, help="Total notional for alpha allocate"
    )
    alpha.add_argument("--output", default=None, help="Path to save JSON output")
    alpha.add_argument(
        "--usage-file",
        default=None,
        help="Alpha budget usage JSON path, defaults to <base-dir>/alpha_budget_usage.json",
    )
    alpha.add_argument(
        "--alpha-risk-budget",
        default=None,
        help="Alpha risk budget JSON path for alpha budget-usage report",
    )
    alpha.add_argument(
        "--non-strict",
        action="store_true",
        help="Return success for warning-level alpha budget-usage reports",
    )

    return parser


def _environment_config_for_args(args) -> EnvironmentConfig:
    """Build EnvironmentConfig for --env, honoring --no-metrics / --force-metrics
    (mutually exclusive)."""
    factory = {
        "development": EnvironmentConfig.development,
        "production": EnvironmentConfig.production,
        "test": EnvironmentConfig.test,
    }
    extra: dict = {}
    if getattr(args, "no_metrics", False):
        extra["enable_metrics"] = False
    elif getattr(args, "force_metrics", False):
        extra["enable_metrics"] = True
    if getattr(args, "validation_mode", None):
        extra["validation_mode"] = args.validation_mode
    if getattr(args, "adapter_name", None):
        extra["adapter_name"] = args.adapter_name
    if getattr(args, "live_read_only", False):
        extra["live_read_only"] = True
    if getattr(args, "enable_live_dispatch", False):
        extra["live_dispatch_enabled"] = True
    allowed_symbols = tuple(getattr(args, "live_allowed_symbol", []) or [])
    if allowed_symbols:
        extra["live_allowed_symbols"] = allowed_symbols
    mt5_terminal_path = getattr(args, "mt5_terminal_path", None)
    if mt5_terminal_path:
        mt5_terminal = Path(mt5_terminal_path)
        if not mt5_terminal.exists():
            raise FileNotFoundError(mt5_terminal_path)
        extensions = dict(extra.get("extensions", {}))
        extensions["mt5_terminal_path"] = str(mt5_terminal)
        extra["extensions"] = extensions
    dispatch_outbox_dir = getattr(args, "dispatch_outbox_dir", None)
    if dispatch_outbox_dir:
        extensions = dict(extra.get("extensions", {}))
        extensions["dispatch_outbox_dir"] = str(Path(dispatch_outbox_dir))
        extra["extensions"] = extensions
    mt5_outbox_dir = getattr(args, "mt5_outbox_dir", None)
    if mt5_outbox_dir:
        extensions = dict(extra.get("extensions", {}))
        extensions["mt5_outbox_dir"] = str(Path(mt5_outbox_dir))
        extra["extensions"] = extensions
    return factory[args.env](args.base_dir, **extra)


def _build_container(args) -> ServiceContainer:
    return ServiceContainer(_environment_config_for_args(args)).build()


def cmd_run(args) -> int:
    container = _build_container(args)
    orch = container.build_orchestrator()

    sp = StatePersistence(str(Path(args.base_dir) / "state"))
    lm = LifecycleManager(container, sp)

    channels = []
    if container.audit_log:
        channels.append(LogAlertChannel(container.audit_log))
    alerts = AlertService.with_default_rules(channels=channels)
    sched = SchedulerService.for_container(container, persistence=sp, alert_service=alerts)

    SystemFacade(container, orchestrator=orch, lifecycle=lm, scheduler=sched, alert_service=alerts)

    startup = lm.startup(restore_state=True)
    sched.start()

    print(
        json.dumps(
            {
                "status": "running",
                "base_dir": args.base_dir,
                "environment": args.env,
                "startup": startup,
            },
            indent=2,
            default=str,
        )
    )

    return 0


def cmd_selftest(args) -> int:
    container = _build_container(args)
    result = SystemSelfTest(container).run()
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["all_passed"] else 1


def cmd_validate(args) -> int:
    cfg = _environment_config_for_args(args)
    result = ConfigValidator().validate(cfg)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["valid"] else 1


def cmd_config(args) -> int:
    container = _build_container(args)
    hr = container.config_hot_reload
    if args.action == "status":
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_ENGINE_CONFIG_STATUS,
                    ENGINE_CONFIG_KEY_HOT_RELOAD: hr.get_status(),  # type: ignore[reportOptionalMemberAccess]
                    "effective": {
                        "ops_maturity_min_score": container.config.ops_maturity_min_score,
                        "max_open_positions": container.config.max_open_positions,
                        "max_drawdown_pct": container.config.max_drawdown_pct,
                        "max_notional_exposure": container.config.max_notional_exposure,
                        "system_mode": container.config.system_mode,
                    },
                },
                indent=2,
                default=str,
            )
        )
        return 0
    changes = hr.check_and_reload()  # type: ignore[reportOptionalMemberAccess]
    out = {
        "schema_version": SCHEMA_ENGINE_CONFIG_RELOAD_RESULT,
        "reloaded": changes is not None,
        "changes": changes,
        ENGINE_CONFIG_KEY_HOT_RELOAD: hr.get_status(),  # type: ignore[reportOptionalMemberAccess]
        "effective": {
            "ops_maturity_min_score": container.config.ops_maturity_min_score,
            "max_open_positions": container.config.max_open_positions,
            "max_drawdown_pct": container.config.max_drawdown_pct,
            "max_notional_exposure": container.config.max_notional_exposure,
            "system_mode": container.config.system_mode,
        },
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_diagnose(args) -> int:
    container = _build_container(args)
    cli = DiagnosticsCLI(container)
    output = cli.run([args.subcommand])
    print(output)
    return 0


def cmd_backtest(args) -> int:
    scenarios_path = Path(args.scenarios)
    if not scenarios_path.exists():
        print(json.dumps({"error": f"Scenarios file not found: {args.scenarios}"}))
        return 1

    scenarios = json.loads(scenarios_path.read_text(encoding="utf-8"))
    runner = BacktestRunner(initial_equity=args.equity)
    result = runner.run(scenarios, base_dir=args.base_dir)

    summary = result.summary()
    print(json.dumps(summary, indent=2, default=str))

    if args.output:
        result.save(args.output)
        print(f"\nFull report saved to: {args.output}")

    return 0


def cmd_status(args) -> int:
    container = _build_container(args)
    health = container.health_check.readiness()  # type: ignore[reportOptionalMemberAccess]
    brains = container.governance_service.get_all_states()  # type: ignore[reportOptionalMemberAccess]
    metrics_snap = container.metrics.snapshot() if container.metrics else {}

    print(
        json.dumps(
            {
                "schema_version": SCHEMA_ENGINE_STATUS,
                "health": health,
                "brains": {"count": len(brains), "states": brains},
                "metrics": {k: v for k, v in metrics_snap.get("counters", {}).items()},
                EVIDENCE_SECTION_ENGINE_CONFIG: container.evidence_bundle.engine_config_snapshot(),  # type: ignore[reportOptionalMemberAccess]
            },
            indent=2,
            default=str,
        )
    )
    return 0


def cmd_readiness(args) -> int:
    container = _build_container(args)
    report = container.release_readiness.build_report(validation_mode=args.validation_mode)  # type: ignore[reportOptionalMemberAccess]
    print(json.dumps(report, indent=2, default=str))
    if args.output:
        container.release_readiness.save_report(args.output, validation_mode=args.validation_mode)  # type: ignore[reportOptionalMemberAccess]
        print(f"\nReadiness report saved to: {args.output}")
    return 0 if report["ready"] else 1


def cmd_runbook(args) -> int:
    container = _build_container(args)
    kwargs = {"validation_mode": args.validation_mode}
    if args.name == "postmortem":
        kwargs.update({"label": args.label, "output": args.output})
    result = container.runbook_engine.run(args.name, **kwargs)  # type: ignore[reportOptionalMemberAccess]
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("passed") else 1


def cmd_slo(args) -> int:
    container = _build_container(args)
    report = container.slo_service.evaluate()  # type: ignore[reportOptionalMemberAccess]
    print(json.dumps(report, indent=2, default=str))
    if args.output:
        container.slo_service.save_report(args.output)  # type: ignore[reportOptionalMemberAccess]
        print(f"\nSLO report saved to: {args.output}")
    return 0 if report["status"] == "healthy" else 1


def cmd_gate(args) -> int:
    container = _build_container(args)
    strict = not args.non_strict
    alpha_report = (
        json.loads(Path(args.alpha_budget_usage_report).read_text(encoding="utf-8"))
        if args.alpha_budget_usage_report
        else None
    )
    report = container.release_gate.evaluate(  # type: ignore[reportOptionalMemberAccess]
        strict=strict,
        alpha_budget_usage_report=alpha_report,
        validation_mode=args.validation_mode,
    )
    print(json.dumps(report, indent=2, default=str))
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"\nRelease gate report saved to: {args.output}")
    return 0 if report["decision"] in {"allow", "warn"} else 1


def cmd_evidence(args) -> int:
    container = _build_container(args)
    if args.action == "build":
        alpha_report = (
            json.loads(Path(args.alpha_budget_usage_report).read_text(encoding="utf-8"))
            if args.alpha_budget_usage_report
            else None
        )
        result = container.evidence_bundle.build_bundle(  # type: ignore[reportOptionalMemberAccess]
            args.output_dir,
            label=args.label,
            alpha_budget_usage_report=alpha_report,
            validation_mode=args.validation_mode,
        )
    else:
        if not args.manifest:
            print(json.dumps({"error": "--manifest is required for evidence verify"}, indent=2))
            return 1
        result = container.evidence_bundle.verify_bundle(args.manifest)  # type: ignore[reportOptionalMemberAccess]
    print(json.dumps(result, indent=2, default=str))
    if args.action == "verify":
        return 0 if result.get("verified") else 1
    return 0


def cmd_deploy_plan(args) -> int:
    container = _build_container(args)
    alpha_report = (
        json.loads(Path(args.alpha_budget_usage_report).read_text(encoding="utf-8"))
        if args.alpha_budget_usage_report
        else None
    )
    kwargs = {
        "version": args.version,
        "strategy": args.strategy,
        "evidence_dir": args.evidence_dir,
        "strict_gate": not args.non_strict,
        "alpha_budget_usage_report": alpha_report,
        "validation_mode": args.validation_mode,
    }
    plan = container.deployment_plan.build_plan(**kwargs)  # type: ignore[reportOptionalMemberAccess]
    print(json.dumps(plan, indent=2, default=str))
    if args.output:
        container.deployment_plan.save_plan(args.output, **kwargs)  # type: ignore[reportOptionalMemberAccess]
        print(f"\nDeployment plan saved to: {args.output}")
    return 0 if plan.get("executable") else 1


def cmd_deploy_exec(args) -> int:
    container = _build_container(args)
    if args.plan:
        result = container.deployment_executor.execute_from_file(  # type: ignore[reportOptionalMemberAccess]
            args.plan,
            dry_run=True,
            validation_mode=args.validation_mode,
        )
    else:
        plan = container.deployment_plan.build_plan(  # type: ignore[reportOptionalMemberAccess]
            version=args.version,
            strategy=args.strategy,
            validation_mode=args.validation_mode,
        )
        result = container.deployment_executor.execute(  # type: ignore[reportOptionalMemberAccess]
            plan, dry_run=True, validation_mode=args.validation_mode
        )
    print(json.dumps(result, indent=2, default=str))
    if args.output:
        container.deployment_executor.save_result(result, args.output)  # type: ignore[reportOptionalMemberAccess]
        print(f"\nDeployment execution result saved to: {args.output}")
    return 0 if result.get("passed") else 1


def cmd_rollback_drill(args) -> int:
    container = _build_container(args)
    result = container.rollback_drill.run(  # type: ignore[reportOptionalMemberAccess]
        version=args.version,
        reason=args.reason,
        evidence_manifest=args.evidence_manifest,
        output=args.output,
        validation_mode=args.validation_mode,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("passed") else 1


def cmd_ops_timeline(args) -> int:
    container = _build_container(args)
    tl = container.operations_timeline
    if args.action.startswith("record"):
        if not args.input:
            print(json.dumps({"error": "--input is required for record actions"}, indent=2))
            return 1
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
        if args.action == "record-gate":
            result = tl.record_release_gate(payload, actor=args.actor)  # type: ignore[reportOptionalMemberAccess]
        elif args.action == "record-deploy-exec":
            result = tl.record_deployment_execution(payload, actor=args.actor)  # type: ignore[reportOptionalMemberAccess]
        elif args.action == "record-rollback":
            result = tl.record_rollback_drill(payload, actor=args.actor)  # type: ignore[reportOptionalMemberAccess]
        else:
            result = tl.record_evidence_bundle(payload, actor=args.actor)  # type: ignore[reportOptionalMemberAccess]
    elif args.action == "list":
        result = {"events": tl.list_events(event_type=args.event_type, limit=args.limit)}  # type: ignore[reportOptionalMemberAccess]
    elif args.action == "summary":
        result = tl.summarize()  # type: ignore[reportOptionalMemberAccess]
    elif args.action == "export":
        if not args.output:
            print(json.dumps({"error": "--output is required for export"}, indent=2))
            return 1
        result = {"output": tl.export(args.output)}  # type: ignore[reportOptionalMemberAccess]
    else:
        result = tl.clear()  # type: ignore[reportOptionalMemberAccess]
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_postmortem_report(args) -> int:
    container = _build_container(args)
    report = container.postmortem_report.generate(  # type: ignore[reportOptionalMemberAccess]
        incident_id=args.incident_id,
        title=args.title,
        severity=args.severity,
        output=args.output,
        validation_mode=args.validation_mode,
    )
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["incident"]["status"] != "critical" else 1


def cmd_release_pipeline(args) -> int:
    container = _build_container(args)
    alpha_report = (
        json.loads(Path(args.alpha_budget_usage_report).read_text(encoding="utf-8"))
        if args.alpha_budget_usage_report
        else None
    )
    result = container.release_pipeline.run(  # type: ignore[reportOptionalMemberAccess]
        version=args.version,
        strategy=args.strategy,
        output_dir=args.output_dir,
        strict_gate=not args.non_strict,
        actor=args.actor,
        alpha_budget_usage_report=alpha_report,
        validation_mode=args.validation_mode,
    )
    if args.output:
        container.release_pipeline.save_result(result, args.output)  # type: ignore[reportOptionalMemberAccess]
        result["artifacts"]["pipeline_summary_output"] = args.output
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("passed") else 1


def cmd_release_cert(args) -> int:
    container = _build_container(args)
    if args.action == "certify":
        if not args.pipeline:
            print(
                json.dumps({"error": "--pipeline is required for release-cert certify"}, indent=2)
            )
            return 1
        result = container.release_certification.certify(  # type: ignore[reportOptionalMemberAccess]
            pipeline_summary=args.pipeline,
            approver=args.approver,
            output=args.output,
        )
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("certified") else 1
    if not args.certificate:
        print(json.dumps({"error": "--certificate is required for release-cert verify"}, indent=2))
        return 1
    result = container.release_certification.verify_certificate(args.certificate)  # type: ignore[reportOptionalMemberAccess]
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("verified") else 1


def cmd_release_registry(args) -> int:
    container = _build_container(args)
    registry = container.release_registry
    if args.action == "register":
        if not args.certificate:
            print(json.dumps({"error": "--certificate is required for register"}, indent=2))
            return 1
        result = registry.register(args.certificate, actor=args.actor)  # type: ignore[reportOptionalMemberAccess]
    elif args.action == "list":
        result = {
            "records": registry.list_records(  # type: ignore[reportOptionalMemberAccess]
                version=args.version, certified=True if args.certified else None
            )
        }
    elif args.action == "summary":
        result = registry.summarize()  # type: ignore[reportOptionalMemberAccess]
    elif args.action == "latest":
        result = registry.latest(version=args.version) or {}  # type: ignore[reportOptionalMemberAccess]
    elif args.action == "verify":
        if not args.record_id or not args.certificate:
            print(
                json.dumps(
                    {"error": "--record-id and --certificate are required for verify"}, indent=2
                )
            )
            return 1
        result = registry.verify_record(args.record_id, args.certificate)  # type: ignore[reportOptionalMemberAccess]
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("verified") else 1
    elif args.action == "export":
        if not args.output:
            print(json.dumps({"error": "--output is required for export"}, indent=2))
            return 1
        result = {"output": registry.export(args.output)}  # type: ignore[reportOptionalMemberAccess]
    else:
        result = registry.clear()  # type: ignore[reportOptionalMemberAccess]
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_compliance_audit(args) -> int:
    container = _build_container(args)
    report = container.compliance_audit.generate(  # type: ignore[reportOptionalMemberAccess]
        output=args.output, validation_mode=args.validation_mode
    )
    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get("status") in {"pass", "warn"} else 1


def cmd_compliance_matrix(args) -> int:
    container = _build_container(args)
    report = container.compliance_control_matrix.generate(  # type: ignore[reportOptionalMemberAccess]
        output=args.output, validation_mode=args.validation_mode
    )
    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get("status") in {"pass", "warn"} else 1


def cmd_final_audit(args) -> int:
    container = _build_container(args)
    report = container.final_audit.build_report(validation_mode=args.validation_mode)  # type: ignore[reportOptionalMemberAccess]
    print(json.dumps(report, indent=2, default=str))
    if args.output:
        container.final_audit.save_report(args.output, report=report)  # type: ignore[reportOptionalMemberAccess]
        print(f"\nFinal audit report saved to: {args.output}")
    return 0 if report.get("ready_for_production") else 1


def cmd_ops_maturity(args) -> int:
    container = _build_container(args)
    if getattr(args, "min_score", None) is not None:
        container.config.ops_maturity_min_score = float(args.min_score)
    report = container.ops_maturity.evaluate(validation_mode=args.validation_mode)  # type: ignore[reportOptionalMemberAccess]
    print(json.dumps(report, indent=2, default=str))
    if args.output:
        container.ops_maturity.save_report(args.output, validation_mode=args.validation_mode)  # type: ignore[reportOptionalMemberAccess]
        print(f"\nOps maturity report saved to: {args.output}")
    min_s = float(getattr(container.config, "ops_maturity_min_score", 60.0))
    return 0 if report.get("maturity_score", 0) >= min_s else 1


def cmd_runtime(args) -> int:
    ledger_dir = Path(args.ledger_dir) if args.ledger_dir else Path(args.base_dir) / "ledger"
    reader = RuntimeEvidenceReader(str(ledger_dir))
    if args.action == "list-cycles":
        result = {
            "schema_version": SCHEMA_RUNTIME_CYCLE_LIST,
            "ledger_dir": str(ledger_dir),
            "cycle_ids": reader.list_cycle_ids(),
        }
    elif args.action == "replay":
        if not args.cycle_id:
            print(json.dumps({"error": "--cycle-id is required for runtime replay"}, indent=2))
            return 1
        result = RuntimeCycleReplay(reader).replay(args.cycle_id).to_dict()
    elif args.action == "inspect":
        if not args.cycle_id:
            print(json.dumps({"error": "--cycle-id is required for runtime inspect"}, indent=2))
            return 1
        record = reader.latest_cycle(args.cycle_id)
        if record is None:
            print(
                json.dumps(
                    {"error": "runtime cycle evidence not found", "cycle_id": args.cycle_id},
                    indent=2,
                )
            )
            return 1
        result = record
    elif args.action == "summary":
        result = RuntimeSummaryService(reader).summarize(limit=args.limit)
    else:
        result = _run_runtime_paper(args, ledger_dir)
    print(json.dumps(result, indent=2, default=str))
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    if args.action == "replay":
        return 0 if result.get("replayable") else 1
    if args.action == "run-paper":
        return 0 if result.get("completed") else 1
    return 0


def _run_runtime_paper(args, ledger_dir: Path) -> dict:
    market = _parse_runtime_market(args)
    if "price" not in market and not {"bid", "ask"}.issubset(market):
        return {
            "schema_version": SCHEMA_RUNTIME_RUN_PAPER,
            "completed": False,
            "error": "--price or both --bid/--ask are required",
        }
    features = _parse_runtime_features(args.feature)
    features.setdefault(args.feature_name, 0.0)
    evidence_writer = RuntimeEvidenceWriter(JsonlLedgerStore(str(ledger_dir)))
    registry = StrategyPluginRegistry()
    agent = ThresholdAlphaAgent(
        strategy_id=args.strategy_id,
        feature_name=args.feature_name,
        buy_threshold=args.buy_threshold,
        sell_threshold=args.sell_threshold,
        symbol=args.symbol,
    )
    registry.register(agent)
    runner = StrategyPluginRunner(registry)
    runner.warmup_all({})
    router = ExecutionGatewayRouter()
    router.register("PAPER", PaperExecutionGateway())
    gates = []
    if args.alpha_risk_budget:
        usage_store = (
            AlphaBudgetUsageStore(args.alpha_budget_usage) if args.alpha_budget_usage else None
        )
        gates.append(
            AlphaRiskBudgetGate(
                json.loads(Path(args.alpha_risk_budget).read_text(encoding="utf-8")),
                usage_store=usage_store,
            )
        )
    gates.extend(
        [
            RuntimeRiskGate(
                max_quantity=max(args.base_quantity, 1.0) * 2,
                allowed_symbols={args.symbol},
                max_notional=1_000_000,
            ),
            RuntimeGovernanceGate(
                allowed_strategy_ids={args.strategy_id}, allowed_venues={"PAPER"}
            ),
        ]
    )
    chain = RuntimeExecutionApprovalChain(gates)
    pipeline = RuntimeExecutionPipeline(
        strategy_runner=runner,
        order_builder=SignalOrderRequestBuilder(
            OrderSizingPolicy(base_quantity=args.base_quantity), default_venue="PAPER"
        ),
        gateway_router=router,
        approval_chain=chain,
        evidence_writer=evidence_writer,
    )
    context = {"runtime_cycle_id": args.cycle_id} if args.cycle_id else {}
    result = pipeline.run(features, market, context)
    return {
        "schema_version": SCHEMA_RUNTIME_RUN_PAPER,
        "completed": True,
        "runtime_cycle_id": result.runtime_cycle_id,
        "signal_count": len(result.signals),
        "order_count": len(result.orders),
        "approval_count": len(result.approvals),
        "skipped_count": len(result.skipped_signals),
        "quality_summary": result.quality_report.to_dict(),
        "ledger_dir": str(ledger_dir),
    }


def _parse_runtime_market(args) -> dict:
    market = {}
    if args.price is not None:
        market["price"] = args.price
    if args.bid is not None:
        market["bid"] = args.bid
    if args.ask is not None:
        market["ask"] = args.ask
    return market


def _parse_runtime_features(items: list[str]) -> dict:
    features = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Invalid feature format, expected key=value: {item}")
        key, value = item.split("=", 1)
        features[key] = float(value)
    return features


def cmd_alpha(args) -> int:
    registry_path = (
        Path(args.registry_file)
        if args.registry_file
        else Path(args.base_dir) / "alpha_registry.json"
    )
    performance_path = (
        Path(args.performance_file)
        if args.performance_file
        else Path(args.base_dir) / "alpha_performance.json"
    )
    usage_path = (
        Path(args.usage_file)
        if args.usage_file
        else Path(args.base_dir) / "alpha_budget_usage.json"
    )
    registry = _load_alpha_registry(registry_path)
    store = _load_alpha_performance(performance_path)
    if args.action == "budget-usage":
        usage = AlphaBudgetUsageStore(usage_path).to_dict()
        if args.alpha_risk_budget:
            result = AlphaBudgetUsageReporter().build(
                usage,
                json.loads(Path(args.alpha_risk_budget).read_text(encoding="utf-8")),
            )
        else:
            result = usage
    elif args.action == "budget-usage-reset":
        usage = AlphaBudgetUsageStore(usage_path)
        usage.reset()
        result = usage.to_dict()
    elif args.action == "register":
        if not args.alpha_id or not args.name:
            print(
                json.dumps(
                    {"error": "--alpha-id and --name are required for alpha register"}, indent=2
                )
            )
            return 1
        record = AlphaRecord(
            alpha_id=args.alpha_id,
            name=args.name,
            version=args.version,
            state=args.state,
            strategy_id=args.strategy_id or args.alpha_id,
        )
        registry.register(record)
        _save_alpha_registry(registry_path, registry)
        result = record.to_dict()
    elif args.action == "list":
        result = registry.to_dict()
    elif args.action == "transition":
        if not args.alpha_id or not args.to_state:
            print(
                json.dumps(
                    {"error": "--alpha-id and --to-state are required for alpha transition"},
                    indent=2,
                )
            )
            return 1
        lifecycle = AlphaLifecycleService(registry)
        record = lifecycle.transition(args.alpha_id, args.to_state, reason=args.reason)
        _save_alpha_registry(registry_path, registry)
        result = {
            "record": record.to_dict(),
            "transitions": [t.to_dict() for t in lifecycle.transitions(args.alpha_id)],
        }
    elif args.action == "performance":
        if not args.alpha_id:
            print(json.dumps({"error": "--alpha-id is required for alpha performance"}, indent=2))
            return 1
        if args.metric:
            store.record_snapshot(
                args.alpha_id, _parse_alpha_metrics(args.metric), source="cli", window="manual"
            )
            _save_alpha_performance(performance_path, store)
        result = store.summarize(args.alpha_id)
    elif args.action == "ingest-runtime":
        if args.strategy_id and not args.alpha_id:
            print(
                json.dumps(
                    {
                        "error": (
                            "--alpha-id is required when --strategy-id is provided"
                            " for alpha ingest-runtime"
                        )
                    },
                    indent=2,
                )
            )
            return 1
        result = _alpha_ingest_runtime(args, store)
        _save_alpha_performance(performance_path, store)
    elif args.action == "ingest-live-bridge":
        if not args.alpha_id:
            print(
                json.dumps(
                    {"error": "--alpha-id is required for alpha ingest-live-bridge"}, indent=2
                )
            )
            return 1
        result = _alpha_ingest_live_bridge(args, store)
        _save_alpha_performance(performance_path, store)
    elif args.action == "batch-evaluate":
        result = _alpha_batch_evaluate(args, registry, store)
        if args.apply:
            _save_alpha_registry(registry_path, registry)
    elif args.action == "allocate":
        result = AlphaPortfolioAllocator(
            registry,
            store,
            AlphaAllocationPolicy(total_notional=args.total_notional),
        ).allocate()
    elif args.action == "export-risk-budget":
        allocation = AlphaPortfolioAllocator(
            registry,
            store,
            AlphaAllocationPolicy(total_notional=args.total_notional),
        ).allocate()
        result = AlphaRiskBudgetExporter().export(allocation)
    else:
        if not args.alpha_id:
            print(json.dumps({"error": "--alpha-id is required for alpha evaluate"}, indent=2))
            return 1
        lifecycle = AlphaLifecycleService(registry)
        record = registry.require(args.alpha_id)
        decision = (
            AlphaPromotionGate(store).apply(record, lifecycle)
            if args.apply
            else AlphaPromotionGate(store).evaluate(record)
        )
        if args.apply and decision.approved:
            _save_alpha_registry(registry_path, registry)
        result = decision.to_dict()
    print(json.dumps(result, indent=2, default=str))
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    if (
        args.action == "budget-usage"
        and args.alpha_risk_budget
        and result.get("warning_count", 0) > 0
        and not args.non_strict
    ):
        return 1
    return 0


def _alpha_batch_evaluate(args, registry: AlphaRegistry, store: AlphaPerformanceStore) -> dict:
    lifecycle = AlphaLifecycleService(registry)
    gate = AlphaPromotionGate(store)
    decisions = []
    applied = 0
    for record in registry.list_records():
        decision = gate.apply(record, lifecycle) if args.apply else gate.evaluate(record)
        if args.apply and decision.approved and decision.target_state:
            applied += 1
        decisions.append(decision.to_dict())
    return {
        "schema_version": SCHEMA_ALPHA_BATCH_EVALUATION,
        "alpha_count": len(decisions),
        "applied_count": applied,
        "decisions": decisions,
    }


def _alpha_ingest_live_bridge(args, store: AlphaPerformanceStore) -> dict:
    from scripts.trade_quality_report import build_report

    base = Path(args.base_dir)
    journal_path = (
        args.journal_path if args.journal_path else str(base / "live_trade_journal.jsonl")
    )
    report = build_report(journal_path=journal_path, date_key=args.date, symbol=args.symbol)
    snapshot = store.ingest_live_bridge_report(
        args.alpha_id,
        report,
        journal_source_path=str(Path(journal_path).resolve()),
        symbol_filter=args.symbol,
    )
    return {
        "schema_version": SCHEMA_ALPHA_LIVE_BRIDGE_INGESTION,
        "journal_path": journal_path,
        "date_key": report.get("date_key"),
        "snapshot_count": 1,
        "snapshots": [snapshot.to_dict()],
    }


def _alpha_ingest_runtime(args, store: AlphaPerformanceStore) -> dict:
    ledger_dir = Path(args.ledger_dir) if args.ledger_dir else Path(args.base_dir) / "ledger"
    runtime_summary = RuntimeSummaryService(RuntimeEvidenceReader(str(ledger_dir))).summarize(
        limit=args.limit
    )
    mapping = {args.strategy_id: args.alpha_id} if args.strategy_id and args.alpha_id else None
    snapshots = store.ingest_runtime_summary(runtime_summary, mapping)
    return {
        "schema_version": SCHEMA_ALPHA_RUNTIME_INGESTION,
        "ledger_dir": str(ledger_dir),
        "runtime_cycle_count": runtime_summary.get("cycle_count", 0),
        "snapshot_count": len(snapshots),
        "snapshots": [snapshot.to_dict() for snapshot in snapshots],
    }


def _load_alpha_registry(path: Path) -> AlphaRegistry:
    registry = AlphaRegistry()
    if not path.exists():
        return registry
    payload = json.loads(path.read_text(encoding="utf-8"))
    for item in payload.get("records", []):
        registry.upsert(
            AlphaRecord(
                alpha_id=item["alpha_id"],
                name=item["name"],
                version=item["version"],
                state=item.get("state", "candidate"),
                strategy_id=item.get("strategy_id"),
                tags=tuple(item.get("tags", [])),
                metadata=item.get("metadata", {}),
                performance=item.get("performance", {}),
                risk_profile=item.get("risk_profile", {}),
            )
        )
    return registry


def _save_alpha_registry(path: Path, registry: AlphaRegistry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry.to_dict(), indent=2, default=str), encoding="utf-8")


def _load_alpha_performance(path: Path) -> AlphaPerformanceStore:
    store = AlphaPerformanceStore()
    if not path.exists():
        return store
    payload = json.loads(path.read_text(encoding="utf-8"))
    for summary in payload.get("summaries", []):
        for snapshot in summary.get("history", []):
            store.record_snapshot(
                snapshot["alpha_id"],
                snapshot.get("metrics", {}),
                source=snapshot.get("source", "file"),
                window=snapshot.get("window", "latest"),
            )
    return store


def _save_alpha_performance(path: Path, store: AlphaPerformanceStore) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = store.to_dict()
    payload["summaries"] = [
        {
            **store.summarize(alpha_id),
            "history": [snapshot.to_dict() for snapshot in store.history(alpha_id)],
        }
        for alpha_id in sorted(store._snapshots)
    ]
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _parse_alpha_metrics(items: list[str]) -> dict:
    metrics = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Invalid metric format, expected key=value: {item}")
        key, value = item.split("=", 1)
        try:
            metrics[key] = float(value)
        except ValueError:
            metrics[key] = value
    return metrics


def _cli_error(error: str, message: str, path: str | None = None) -> dict:
    payload = {
        "schema_version": SCHEMA_CLI_ERROR,
        "error": error,
        "message": message,
    }
    if path:
        payload["path"] = path
    return payload


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    handlers = {
        "run": cmd_run,
        "selftest": cmd_selftest,
        "validate": cmd_validate,
        "config": cmd_config,
        "diagnose": cmd_diagnose,
        "backtest": cmd_backtest,
        "status": cmd_status,
        "readiness": cmd_readiness,
        "runbook": cmd_runbook,
        "slo": cmd_slo,
        "gate": cmd_gate,
        "evidence": cmd_evidence,
        "deploy-plan": cmd_deploy_plan,
        "deploy-exec": cmd_deploy_exec,
        "rollback-drill": cmd_rollback_drill,
        "ops-timeline": cmd_ops_timeline,
        "postmortem-report": cmd_postmortem_report,
        "release-pipeline": cmd_release_pipeline,
        "release-cert": cmd_release_cert,
        "release-registry": cmd_release_registry,
        "compliance-audit": cmd_compliance_audit,
        "compliance-matrix": cmd_compliance_matrix,
        "final-audit": cmd_final_audit,
        "ops-maturity": cmd_ops_maturity,
        "runtime": cmd_runtime,
        "alpha": cmd_alpha,
    }
    try:
        return handlers[args.command](args)
    except AlphaBudgetContractError as exc:
        path = (
            getattr(args, "alpha_risk_budget", None)
            or getattr(args, "alpha_budget_usage", None)
            or getattr(args, "usage_file", None)
        )
        print(json.dumps(_cli_error("alpha_budget_contract_error", str(exc), path), indent=2))
        return 1
    except JSONDecodeError as exc:
        path = (
            getattr(args, "alpha_risk_budget", None)
            or getattr(args, "alpha_budget_usage", None)
            or getattr(args, "usage_file", None)
        )
        print(json.dumps(_cli_error("json_decode_error", str(exc), path), indent=2))
        return 1
    except FileNotFoundError as exc:
        print(
            json.dumps(
                _cli_error("file_not_found", str(exc), getattr(exc, "filename", None)), indent=2
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
