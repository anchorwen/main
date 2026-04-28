"""Run a live read-only preflight and emit a JSON report.

Validates three things under a production-shaped configuration:
1. `status`-equivalent container health
2. `selftest` passes
3. dispatch is blocked by the live read-only guard
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_repo_on_path() -> None:
    repo_root = _repo_root()
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="live_read_only_preflight")
    parser.add_argument("--base-dir", default="data")
    parser.add_argument("--mt5-terminal-path", required=True)
    parser.add_argument("--output", default=None)
    return parser


def build_report(*, base_dir: str, mt5_terminal_path: str) -> dict:
    _ensure_repo_on_path()

    from apps.engine.system_facade import SystemSelfTest
    from core.contracts.domain.communication_envelope import CommunicationEnvelope
    from core.contracts.enums import CommunicationMessageType, CommunicationPriority
    from core.deployment.environment_config import EnvironmentConfig
    from core.deployment.service_container import ServiceContainer
    from core.protocol.schema_versions import SCHEMA_COMMUNICATION_ENVELOPE

    terminal_path = Path(mt5_terminal_path)
    if not terminal_path.exists():
        raise FileNotFoundError(str(terminal_path))

    cfg = EnvironmentConfig.production(
        base_dir=base_dir,
        live_read_only=True,
        extensions={"mt5_terminal_path": str(terminal_path)},
    )
    container = ServiceContainer(cfg).build()

    status = {
        "health": container.health_check.readiness(),
        "brain_state_count": len(container.governance_service.get_all_states()),
        "live_read_only": container.config.live_read_only,
        "mt5_terminal_path": container.config.extensions.get("mt5_terminal_path"),
    }

    selftest = SystemSelfTest(container).run()

    envelope = CommunicationEnvelope(
        schema_version=SCHEMA_COMMUNICATION_ENVELOPE,
        message_id="live_read_only_probe_001",
        correlation_id="live_read_only_probe_corr",
        causation_id=None,
        event_time=datetime.utcnow(),
        producer="decision_engine",
        target="exec_bridge",
        message_type=CommunicationMessageType.DECISION_INTENT,
        priority=CommunicationPriority.NORMAL,
        payload={"intent_id": "live_read_only_probe_001"},
        deadline_at=datetime.utcnow() + timedelta(seconds=30),
    )
    dispatch_result = container.dispatcher.dispatch(envelope)
    guard = {
        "status": str(dispatch_result.status),
        "adapter_name": dispatch_result.adapter_name,
        "failure_reason": dispatch_result.failure_reason,
        "attempts": dispatch_result.attempts,
        "trace": dispatch_result.trace,
        "blocked": (
            dispatch_result.adapter_name == "live_read_only_guard"
            and dispatch_result.failure_reason == "live_read_only_enabled"
        ),
    }

    ready = (
        status["health"].get("status") == "ready"
        and bool(selftest.get("all_passed"))
        and guard["blocked"]
    )
    return {
        "schema_version": "live_read_only_preflight.v1",
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "ready_for_observation": ready,
        "status_check": status,
        "selftest": selftest,
        "dispatch_guard": guard,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_report(
        base_dir=args.base_dir,
        mt5_terminal_path=args.mt5_terminal_path,
    )
    rendered = json.dumps(report, indent=2, default=str)
    print(rendered)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    return 0 if report["ready_for_observation"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
