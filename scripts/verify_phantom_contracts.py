#!/usr/bin/env python
"""Offline Phantom Contract Verifier — UGR v3.1 §修正1.

Scans the WAL for PhantomStub records, re-executes the predicate against
the recorded input_snapshot, and reports violations where production
assumed the predicate passed but replay shows it would have failed.

UGR-B04: State-aware verification with StateProjector for state-dependent
predicates.  Supports incremental mode (since_seq) for large WALs.

Usage::

    python scripts/verify_phantom_contracts.py --wal-path data_btc/wal.jsonl
    python scripts/verify_phantom_contracts.py --wal-path data_btc/wal.jsonl --verbose
    python scripts/verify_phantom_contracts.py --wal-path data_btc/wal.jsonl --state-aware
    python scripts/verify_phantom_contracts.py --wal-path data_btc/wal.jsonl --since-seq 5000

Exit codes (per phantom_state_replay.md §4.3):
  0 — All stubs replayed, all assumed_ok confirmed (PASS)
  1 — ≥1 assumed_ok=True but predicate returned False (FAIL — violation)
  2 — WAL file missing or unreadable (SKIP)
  3 — Predicate registry incomplete (WARN)
  4 — State projection errors detected (FAIL — cannot verify)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ═══════════════════════════════════════════════════════════════════════════
# Types
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class Violation:
    """A single phantom contract violation found during verification."""

    contract_id: str
    wal_seq: int
    assumed_ok: bool
    actual_ok: bool
    input_hash: str
    timestamp_wall: str
    detail: str = ""


@dataclass
class VerificationReport:
    """Result of a phantom contract verification run."""

    total_stubs: int = 0
    deduped_stubs: int = 0
    replayed: int = 0
    violations: list[Violation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped_version_mismatch: int = 0
    skipped_unknown_contract: int = 0
    # UGR-B04: state-aware verification fields
    state_dependent_replayed: int = 0
    state_projection_errors: int = 0
    state_completeness_warnings: int = 0

    @property
    def has_violations(self) -> bool:
        return len(self.violations) > 0

    @property
    def has_projection_errors(self) -> bool:
        return self.state_projection_errors > 0

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0


# ═══════════════════════════════════════════════════════════════════════════
# Verifier
# ═══════════════════════════════════════════════════════════════════════════


def verify(
    wal_path: Path,
    *,
    verbose: bool = False,
    state_aware: bool = False,
    since_seq: int | None = None,
) -> VerificationReport:
    """Scan WAL for PhantomStub records and replay predicates.

    Args:
        wal_path: Path to WAL JSONL file.
        verbose: Print per-stub replay details.
        state_aware: If True, reconstruct state via StateProjector for
            state-dependent predicates.  Predicates that declare
            required_state_keys get _state kwarg from projected WAL state.
        since_seq: Only verify stubs with seq > N (incremental mode for
            large WALs).  None = verify all.
    """
    report = VerificationReport()

    # Import phantom contract types (may fail if module not importable)
    try:
        from core.contracts.phantom_contract import (
            PhantomSerializer,
            PhantomStub,
            PredicateRegistry,
            StateProjectionError,
            StateProjector,
        )
    except ImportError as e:
        report.warnings.append(f"Cannot import phantom_contract: {e}")
        return report

    from core.data.write_ahead_log import WALConfig, WriteAheadLog

    # Open WAL
    if not wal_path.exists():
        report.warnings.append(f"WAL file not found: {wal_path}")
        return report

    wal_config = WALConfig(path=wal_path, create_if_missing=False)
    wal = WriteAheadLog(wal_config)

    # UGR-B04: Set up StateProjector if state-aware mode is requested
    projector: StateProjector | None = None
    if state_aware:
        projector = StateProjector()
        # Register built-in handlers
        from core.contracts.phantom_contract import (
            _handle_brain_state,
            _handle_budget_update,
            _handle_position_close,
            _handle_position_open,
        )

        projector.register_handler(
            "position_open", _handle_position_open, priority=10, writes_keys={"positions"}
        )
        projector.register_handler(
            "position_close", _handle_position_close, priority=10, writes_keys={"positions"}
        )
        projector.register_handler(
            "budget_update", _handle_budget_update, priority=20, writes_keys={"risk_budget"}
        )
        projector.register_handler(
            "brain_state_change", _handle_brain_state, priority=30, writes_keys={"brain_states"}
        )
        # Sync required state keys from PredicateRegistry
        for cid in PredicateRegistry.list_contracts():
            keys = PredicateRegistry.get_required_state_keys(cid)
            if keys:
                projector.declare_required_keys(cid, keys)

    seen_hashes: set[str] = set()

    for record in wal:
        # UGR-B04: incremental mode — skip stubs before since_seq
        if since_seq is not None and record.seq <= since_seq:
            continue

        payload = record.payload
        if not isinstance(payload, dict):
            continue

        if record.type != "phantom_stub":
            continue

        report.total_stubs += 1

        stub = PhantomStub.from_payload(payload)

        # Dedup by input_hash
        if stub.input_hash in seen_hashes:
            report.deduped_stubs += 1
            if verbose:
                print(f"  [dedup] seq={record.seq} contract={stub.contract_id}")
            continue
        seen_hashes.add(stub.input_hash)

        # Version check
        current_version = PredicateRegistry.get_version(stub.contract_id)
        if stub.contract_version != current_version:
            report.skipped_version_mismatch += 1
            msg = (
                f"Version mismatch: {stub.contract_id} "
                f"v{stub.contract_version} != v{current_version}"
            )
            report.warnings.append(msg)
            if verbose:
                print(f"  [skip:version] {msg}")
            continue

        # Look up predicate
        predicate = PredicateRegistry.get(stub.contract_id)
        if predicate is None:
            report.skipped_unknown_contract += 1
            msg = f"Unknown contract_id: {stub.contract_id}"
            report.warnings.append(msg)
            if verbose:
                print(f"  [skip:unknown] {msg}")
            continue

        # UGR-B04: State-aware replay
        required_keys = PredicateRegistry.get_required_state_keys(stub.contract_id)
        state_for_predicate: dict | None = None

        if required_keys and projector is not None:
            # State-dependent predicate: project state from WAL
            try:
                state_for_predicate = projector.project_to(wal, stub.recorded_at_wal_seq)
                # Validate only this contract's required keys
                state_for_predicate = projector.snapshot_for(stub.contract_id)
                report.state_dependent_replayed += 1
            except StateProjectionError as e:
                report.state_projection_errors += 1
                msg = f"State projection failed for {stub.contract_id} " f"at seq={record.seq}: {e}"
                report.warnings.append(msg)
                if verbose:
                    print(f"  [state_error] {msg}")
                # Conservative: cannot verify → treat as violation
                violation = Violation(
                    contract_id=stub.contract_id,
                    wal_seq=record.seq,
                    assumed_ok=stub.assumed_ok,
                    actual_ok=False,
                    input_hash=stub.input_hash,
                    timestamp_wall=stub.timestamp_wall,
                    detail=f"State projection error: {e}",
                )
                report.violations.append(violation)
                continue
            except Exception as e:  # noqa: BLE001 (state projection can raise anything from handlers)
                report.state_projection_errors += 1
                msg = f"Unexpected error during state projection: {e}"
                report.warnings.append(msg)
                continue
        elif required_keys and projector is None:
            # State-dependent predicate but no projector configured
            report.state_completeness_warnings += 1
            if verbose:
                print(
                    f"  [state_warn] {stub.contract_id} requires state keys "
                    f"{required_keys} but --state-aware not set"
                )

        # Replay predicate
        args, kwargs = PhantomSerializer.deserialize_args(stub.input_snapshot)
        try:
            if state_for_predicate is not None:
                actual_ok = predicate(*args, _state=state_for_predicate, **kwargs)
            else:
                actual_ok = predicate(*args, **kwargs)
        except (TypeError, ValueError, RuntimeError, KeyError) as e:
            actual_ok = False
            report.warnings.append(f"Predicate {stub.contract_id} raised: {e} (seq={record.seq})")

        report.replayed += 1

        if verbose:
            status = "✓" if actual_ok == stub.assumed_ok else "✗ VIOLATION"
            state_tag = " [state-aware]" if state_for_predicate is not None else ""
            print(
                f"  seq={record.seq} contract={stub.contract_id} "
                f"assumed={stub.assumed_ok} actual={actual_ok} {status}{state_tag}"
            )

        # Check for violation
        if actual_ok != stub.assumed_ok:
            violation = Violation(
                contract_id=stub.contract_id,
                wal_seq=record.seq,
                assumed_ok=stub.assumed_ok,
                actual_ok=actual_ok,
                input_hash=stub.input_hash,
                timestamp_wall=stub.timestamp_wall,
                detail=(
                    f"Production assumed predicate '{stub.contract_id}' "
                    f"would return {stub.assumed_ok}, "
                    f"but replay returned {actual_ok}"
                ),
            )
            report.violations.append(violation)

    return report


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Offline Phantom Contract Verifier — UGR v3.1",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--wal-path",
        required=True,
        type=Path,
        help="Path to the WAL JSONL file",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print per-stub replay details",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output report as JSON (default: human-readable)",
    )
    # UGR-B04: state-aware and incremental mode
    parser.add_argument(
        "--state-aware",
        action="store_true",
        help="Enable state reconstruction via StateProjector for state-dependent predicates",
    )
    parser.add_argument(
        "--since-seq",
        type=int,
        default=None,
        help="Only verify stubs with WAL seq > N (incremental mode for large WALs)",
    )
    args = parser.parse_args()

    if not args.wal_path.exists():
        print(f"WAL file not found: {args.wal_path}", file=sys.stderr)
        return 2  # SKIP

    report = verify(
        args.wal_path,
        verbose=args.verbose,
        state_aware=args.state_aware,
        since_seq=args.since_seq,
    )

    if args.json_output:
        print(json.dumps(_report_to_dict(report), indent=2, ensure_ascii=False))
    else:
        _print_report(report)

    # UGR-B04: exit code 4 for state projection errors
    if report.has_projection_errors:
        return 4  # FAIL — cannot verify
    if report.has_violations:
        return 1  # FAIL — contract violation in production
    if report.skipped_unknown_contract > 0:
        return 3  # WARN — configuration gap

    return 0  # PASS


def _print_report(report: VerificationReport) -> None:
    """Print a human-readable verification report."""
    print("=" * 60)
    print("Phantom Contract Verification Report")
    print("=" * 60)
    print(f"  Total stubs found:       {report.total_stubs}")
    print(f"  Deduplicated:             {report.deduped_stubs}")
    print(f"  Replayed:                 {report.replayed}")
    print(f"  State-dependent replayed: {report.state_dependent_replayed}")
    print(f"  State projection errors:  {report.state_projection_errors}")
    print(f"  State completeness warns: {report.state_completeness_warnings}")
    print(f"  Skipped (version):        {report.skipped_version_mismatch}")
    print(f"  Skipped (unknown):        {report.skipped_unknown_contract}")
    print(f"  Violations:               {len(report.violations)}")
    print(f"  Warnings:                 {len(report.warnings)}")

    if report.violations:
        print()
        print("─" * 60)
        print("VIOLATIONS (production assumed OK but predicate returned False):")
        print("─" * 60)
        for v in report.violations:
            print(f"  [{v.contract_id}] seq={v.wal_seq}")
            print(f"    Assumed: {v.assumed_ok}  Actual: {v.actual_ok}")
            print(f"    Hash: {v.input_hash[:16]}...")
            print(f"    Time: {v.timestamp_wall}")
            print(f"    Detail: {v.detail}")
            print()

    if report.warnings:
        print("─" * 60)
        print("WARNINGS:")
        print("─" * 60)
        for w in report.warnings:
            print(f"  ⚠ {w}")

    print()
    if report.has_projection_errors:
        print("RESULT: FAIL — state projection errors prevent verification")
    elif report.has_violations:
        print("RESULT: FAIL — contract violations detected in production")
    elif report.has_warnings:
        print("RESULT: PASS with warnings")
    else:
        print("RESULT: PASS — all phantom contracts verified")


def _report_to_dict(report: VerificationReport) -> dict[str, Any]:
    """Serialize report to JSON."""
    return {
        "total_stubs": report.total_stubs,
        "deduped_stubs": report.deduped_stubs,
        "replayed": report.replayed,
        "state_dependent_replayed": report.state_dependent_replayed,
        "state_projection_errors": report.state_projection_errors,
        "state_completeness_warnings": report.state_completeness_warnings,
        "skipped_version_mismatch": report.skipped_version_mismatch,
        "skipped_unknown_contract": report.skipped_unknown_contract,
        "violations_count": len(report.violations),
        "warnings_count": len(report.warnings),
        "violations": [
            {
                "contract_id": v.contract_id,
                "wal_seq": v.wal_seq,
                "assumed_ok": v.assumed_ok,
                "actual_ok": v.actual_ok,
                "input_hash": v.input_hash,
            }
            for v in report.violations
        ],
        "warnings": report.warnings,
    }


if __name__ == "__main__":
    sys.exit(main())
