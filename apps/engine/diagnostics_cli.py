import argparse
import json

from core.contracts.domain_keys import EVIDENCE_SECTION_ENGINE_CONFIG


class DiagnosticsCLI:
    """CLI interface for operational diagnostics.

    Provides subcommands for health checks, metrics inspection,
    brain status, audit log queries, and system snapshots.
    """

    def __init__(self, service_container):
        self._container = service_container

    def run(self, args: list[str] | None = None) -> str:
        parser = argparse.ArgumentParser(prog="diagnostics", description="System diagnostics")
        sub = parser.add_subparsers(dest="command")

        sub.add_parser("health", help="Liveness and readiness check")
        sub.add_parser("ready", help="Readiness check only")
        sub.add_parser("metrics", help="Current metrics snapshot")
        sub.add_parser("snapshot", help="Full diagnostic snapshot")

        brain_parser = sub.add_parser("brain", help="Brain status")
        brain_parser.add_argument("--brain-id", default=None)

        audit_parser = sub.add_parser("audit", help="Audit log entries")
        audit_parser.add_argument("--date", default=None)
        audit_parser.add_argument("--severity", default=None)
        audit_parser.add_argument("--limit", type=int, default=50)

        sub.add_parser("positions", help="Open positions")
        sub.add_parser("orders", help="Active orders")

        parsed = parser.parse_args(args)
        if not parsed.command:
            parser.print_help()
            return ""

        handler = getattr(self, f"_cmd_{parsed.command}", None)
        if handler is None:
            return json.dumps({"error": f"unknown command: {parsed.command}"})
        return handler(parsed)

    def _cmd_health(self, args) -> str:
        hc = self._container.health_check
        result = {
            "liveness": hc.liveness(),
            "readiness": hc.readiness(),
        }
        return json.dumps(result, indent=2, default=str)

    def _cmd_ready(self, args) -> str:
        return json.dumps(self._container.health_check.readiness(), indent=2, default=str)

    def _cmd_metrics(self, args) -> str:
        if self._container.metrics is None:
            return json.dumps({"error": "metrics not enabled"})
        return json.dumps(self._container.metrics.snapshot(), indent=2, default=str)

    def _cmd_snapshot(self, args) -> str:
        snap = self._container.diagnostics.build_snapshot()
        if not isinstance(snap, dict):
            snap = {}
        else:
            snap = dict(snap)
        snap[EVIDENCE_SECTION_ENGINE_CONFIG] = (
            self._container.evidence_bundle.engine_config_snapshot()
        )
        return json.dumps(snap, indent=2, default=str)

    def _cmd_brain(self, args) -> str:
        if args.brain_id:
            state = self._container.governance_service.get_brain_state(args.brain_id)
            tracker_summary = None
            if self._container.brain_tracker:
                tracker_summary = self._container.brain_tracker.get_brain_summary(args.brain_id)
            return json.dumps(
                {
                    "governance_state": state,
                    "performance": tracker_summary,
                },
                indent=2,
                default=str,
            )

        states = self._container.governance_service.get_all_states()
        summaries = (
            self._container.brain_tracker.get_all_summaries()
            if self._container.brain_tracker
            else []
        )
        perf_map = {s["brain_id"]: s for s in summaries}
        result = []
        for bid, state in states.items():
            result.append(
                {
                    "brain_id": bid,
                    "status": state["status"],
                    "health": perf_map.get(bid, {}).get("health_signal", "unknown"),
                    "recommendation": perf_map.get(bid, {}).get("recommendation", "unknown"),
                    "composite_mean": perf_map.get(bid, {}).get("composite_mean", 0),
                }
            )
        return json.dumps({"brains": result, "count": len(result)}, indent=2, default=str)

    def _cmd_audit(self, args) -> str:
        if self._container.audit_log is None:
            return json.dumps({"error": "audit log not enabled"})
        entries = self._container.audit_log.read_entries(date_key=args.date)
        if args.severity:
            entries = [e for e in entries if e.get("severity") == args.severity]
        entries = entries[-args.limit :]
        return json.dumps({"entries": entries, "count": len(entries)}, indent=2, default=str)

    def _cmd_positions(self, args) -> str:
        if self._container.position_tracker is None:
            return json.dumps({"error": "position tracker not available"})
        positions = self._container.position_tracker.list_open()
        ctx = self._container.position_tracker.get_risk_context()
        return json.dumps({"positions": positions, "risk_context": ctx}, indent=2, default=str)

    def _cmd_orders(self, args) -> str:
        if self._container.execution_manager is None:
            return json.dumps({"error": "execution manager not available"})
        orders = self._container.execution_manager.list_orders()
        return json.dumps({"orders": orders, "count": len(orders)}, indent=2, default=str)
