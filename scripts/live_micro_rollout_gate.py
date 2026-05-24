"""Evaluate whether a micro live rollout configuration is allowed.

This script does not place orders. It validates configuration gates for a
single-symbol, limited live rollout and emits a JSON Go/No-Go report.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_repo_on_path() -> None:
    repo_root = _repo_root()
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="live_micro_rollout_gate")
    parser.add_argument("--base-dir", default="data")
    parser.add_argument("--mt5-terminal-path", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--max-open-positions", type=int, default=1)
    parser.add_argument("--max-notional-exposure", type=float, default=10_000.0)
    parser.add_argument("--output", default=None)
    return parser


def build_report(
    *,
    base_dir: str,
    mt5_terminal_path: str,
    symbol: str,
    max_open_positions: int,
    max_notional_exposure: float,
) -> dict:
    _ensure_repo_on_path()

    from core.contracts.domain.communication_envelope import CommunicationEnvelope
    from core.contracts.enums import CommunicationMessageType, CommunicationPriority, DispatchStatus
    from core.deployment.environment_config import EnvironmentConfig
    from core.deployment.service_container import ServiceContainer
    from core.protocol.schema_versions import SCHEMA_COMMUNICATION_ENVELOPE

    terminal_path = Path(mt5_terminal_path)
    if not terminal_path.exists():
        raise FileNotFoundError(str(terminal_path))

    cfg = EnvironmentConfig.production(
        base_dir=base_dir,
        adapter_name="mt5",
        live_read_only=False,
        live_dispatch_enabled=True,
        live_allowed_symbols=(symbol,),
        max_open_positions=max_open_positions,
        max_notional_exposure=max_notional_exposure,
        extensions={"mt5_terminal_path": str(terminal_path)},
    )
    container = ServiceContainer(cfg).build()
    assert container.dispatcher is not None
    assert container.health_check is not None
    envelope = CommunicationEnvelope(
        schema_version=SCHEMA_COMMUNICATION_ENVELOPE,
        message_id="micro_live_probe_001",
        correlation_id="micro_live_probe_corr",
        causation_id=None,
        event_time=datetime.now(UTC).replace(tzinfo=None),
        producer="decision_engine",
        target="exec_bridge",
        message_type=CommunicationMessageType.DECISION_INTENT,
        priority=CommunicationPriority.NORMAL,
        payload={"intent_id": "micro_live_probe_001", "symbol": symbol},
        deadline_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=30),
    )
    dispatch_result = container.dispatcher.dispatch(envelope)

    checks = {
        "environment_is_production": cfg.is_live(),
        "live_read_only_disabled": cfg.live_read_only is False,
        "live_dispatch_enabled": cfg.live_dispatch_enabled is True,
        "single_symbol_allowlist": cfg.live_allowed_symbols == (symbol,),
        "max_open_positions_limited": cfg.max_open_positions <= 1,
        "max_notional_exposure_limited": cfg.max_notional_exposure <= 10_000.0,
        "mt5_terminal_exists": terminal_path.exists(),
        "dispatcher_healthy": container.health_check.readiness().get("status") == "ready",
        "dispatch_probe_routed_to_mt5_adapter": dispatch_result.adapter_name == "mt5_adapter",
        "dispatch_probe_delivered": dispatch_result.status == DispatchStatus.TRANSPORT_DELIVERED,
    }
    ready = all(checks.values())
    return {
        "schema_version": "live_micro_rollout_gate.v1",
        "generated_at": datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds") + "Z",
        "go_for_micro_live": ready,
        "symbol": symbol,
        "checks": checks,
        "effective": {
            "live_dispatch_enabled": cfg.live_dispatch_enabled,
            "live_allowed_symbols": list(cfg.live_allowed_symbols),
            "max_open_positions": cfg.max_open_positions,
            "max_notional_exposure": cfg.max_notional_exposure,
            "mt5_terminal_path": cfg.extensions.get("mt5_terminal_path"),
        },
        "dispatch_probe": {
            "adapter_name": dispatch_result.adapter_name,
            "status": str(dispatch_result.status),
            "transport_metadata": dispatch_result.transport_metadata,
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_report(
        base_dir=args.base_dir,
        mt5_terminal_path=args.mt5_terminal_path,
        symbol=args.symbol,
        max_open_positions=args.max_open_positions,
        max_notional_exposure=args.max_notional_exposure,
    )
    rendered = json.dumps(report, indent=2)
    print(rendered)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    return 0 if report["go_for_micro_live"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
