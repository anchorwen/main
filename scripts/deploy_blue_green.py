"""Blue-green deployment CLI.

Usage:
    python scripts/deploy_blue_green.py status                    # show topology
    python scripts/deploy_blue_green.py promote                   # cut over
    python scripts/deploy_blue_green.py promote --skip-health     # force cutover
    python scripts/deploy_blue_green.py rollback                  # revert
    python scripts/deploy_blue_green.py health                    # health check both
    python scripts/deploy_blue_green.py health --slot blue        # health check one
    python scripts/deploy_blue_green.py register blue --pid 1234  # register slot
    python scripts/deploy_blue_green.py history                   # cutover log
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.deployment.blue_green import BlueGreenManager, SlotColor


def cmd_status(mgr: BlueGreenManager) -> int:
    topo = mgr.status()
    print(json.dumps(topo, indent=2, default=str))
    return 0


def cmd_promote(mgr: BlueGreenManager, args: argparse.Namespace) -> int:
    result = mgr.promote(
        deployed_by=args.deployed_by or "cli",
        version=args.version or "",
        skip_health_check=args.skip_health,
        drain_timeout_seconds=args.drain_timeout,
    )
    print(json.dumps(result.to_dict(), indent=2, default=str))
    return 0 if result.success else 1


def cmd_rollback(mgr: BlueGreenManager) -> int:
    result = mgr.rollback()
    print(json.dumps(result.to_dict(), indent=2, default=str))
    return 0


def cmd_health(mgr: BlueGreenManager, args: argparse.Namespace) -> int:
    color = SlotColor(args.slot) if args.slot else None
    results = mgr.health_check(color)
    print(json.dumps(results, indent=2, default=str))
    if isinstance(results, dict):
        if "healthy" in results:
            return 0 if results["healthy"] else 1
        if "blue" in results:
            all_ok = results["blue"]["healthy"] and results["green"]["healthy"]
            return 0 if all_ok else 1
    return 1


def cmd_register(mgr: BlueGreenManager, args: argparse.Namespace) -> int:
    mgr.register_slot(
        color=SlotColor(args.slot),
        process_id=args.pid,
        port=args.port,
        brain_id=args.brain_id or "",
    )
    return cmd_status(mgr)


def cmd_history(mgr: BlueGreenManager, args: argparse.Namespace) -> int:
    records = mgr.cutover_history(limit=args.limit)
    print(json.dumps(records, indent=2, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Blue-green deployment manager")
    parser.add_argument(
        "--state-dir",
        default="deployments/state",
        help="Path to deployment state directory",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show current deployment topology")

    promote_p = sub.add_parser("promote", help="Promote standby to live")
    promote_p.add_argument("--deployed-by", default="", help="Who triggered the deploy")
    promote_p.add_argument("--version", default="", help="Version being deployed")
    promote_p.add_argument("--skip-health", action="store_true", help="Skip health checks")
    promote_p.add_argument("--drain-timeout", type=float, default=5.0, help="Drain wait seconds")

    sub.add_parser("rollback", help="Revert to previous live slot")

    health_p = sub.add_parser("health", help="Run health checks")
    health_p.add_argument("--slot", choices=["blue", "green"], help="Check one slot only")

    register_p = sub.add_parser("register", help="Register a slot")
    register_p.add_argument("slot", choices=["blue", "green"], help="Slot to register")
    register_p.add_argument("--pid", type=int, help="Process ID")
    register_p.add_argument("--port", type=int, default=0, help="Port number")
    register_p.add_argument("--brain-id", default="", help="Brain ID")

    history_p = sub.add_parser("history", help="Show cutover history")
    history_p.add_argument("--limit", type=int, default=20, help="Max records")

    args = parser.parse_args()

    mgr = BlueGreenManager(state_dir=args.state_dir)

    handlers = {
        "status": lambda: cmd_status(mgr),
        "promote": lambda: cmd_promote(mgr, args),
        "rollback": lambda: cmd_rollback(mgr),
        "health": lambda: cmd_health(mgr, args),
        "register": lambda: cmd_register(mgr, args),
        "history": lambda: cmd_history(mgr, args),
    }

    return handlers[args.command]()


if __name__ == "__main__":
    sys.exit(main())
