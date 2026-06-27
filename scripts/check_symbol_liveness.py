#!/usr/bin/env python
"""Symbol liveness probe — institutional preflight gate for multi-symbol conditions.

Design contract (Architecture Committee 2026-06-28):
  - Standalone CLI: ``python scripts/check_symbol_liveness.py --all``
  - Importable gate: ``from scripts.check_symbol_liveness import probe_symbol_liveness``
  - daily_ops Step 0: call ``probe_all()`` → skip INACTIVE symbols downstream

Why:  MetaExit condition #1 "XAU ≥500" was reviewed 9 times over 20 days without
       anyone verifying that XAUUSDc had never been launched.  This script makes
       liveness a machine-checkable assertion, not a human memory exercise.

Output modes:
  --json     Machine-readable JSON to stdout
  --summary  Human-readable table (default)
  --exit-code Exit 0=all ACTIVE, 1=some INACTIVE, 2=all INACTIVE (implies --summary)

Examples:
  python scripts/check_symbol_liveness.py --all
  python scripts/check_symbol_liveness.py --symbol BTCUSDc --json
  python scripts/check_symbol_liveness.py --all --exit-code  # CI gate
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ── Symbol → data_dir mapping (single source of truth) ──
# KEEP THIS IN SYNC with live_launcher.py base_dir config.
SYMBOL_DATA_DIR_MAP: dict[str, str] = {
    "BTCUSDc": "data_btc",
    "XAUUSDc": "data",
}

# Minimum criteria for ACTIVE (any one suffices, all checked for evidence quality)
MIN_JOURNAL_ENTRIES = 10  # fewer than 10 live journal lines → likely never started
MIN_STATE_FILES = 3  # fewer than 3 state/*.json files → never bootstrapped
MIN_RECENT_JOURNAL_HOURS = 72  # no journal entry in 72h → stalled


@dataclass
class LivenessVerdict:
    """Structured verdict for one symbol."""

    symbol: str
    data_dir: str
    status: str  # ACTIVE | INACTIVE | STALLED | UNKNOWN
    evidence: dict[str, Any] = field(default_factory=dict)
    recommendations: list[str] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.status == "ACTIVE"

    @property
    def is_blocking(self) -> bool:
        """True if this symbol's state blocks conditions that depend on it."""
        return self.status in ("INACTIVE", "STALLED")


def _resolve_data_dir(symbol: str, project_root: Path | None = None) -> Path:
    """Resolve a symbol to its data directory.

    Falls back to ``data_<symbol_lower>`` for symbols not in the static map.
    """
    root = project_root or Path(__file__).resolve().parent.parent
    dir_name = SYMBOL_DATA_DIR_MAP.get(symbol)
    if dir_name is None:
        # Fallback: data_btc, data_xau, etc.
        dir_name = f"data_{symbol.lower().replace('usdc', '').replace('usd', '')}"
    return root / dir_name


def _count_journal_entries(journal_path: Path) -> dict[str, int]:
    """Count journal entries by action type (streaming, memory-safe)."""
    counts: dict[str, int] = {"total": 0, "open": 0, "close": 0}
    if not journal_path.exists():
        return counts
    try:
        with open(journal_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                counts["total"] += 1
                # Fast substring check — faster than json.loads per line
                if '"action": "open"' in line or "'action': 'open'" in line:
                    counts["open"] += 1
                elif '"action": "close"' in line or "'action': 'close'" in line:
                    counts["close"] += 1
    except (OSError, RuntimeError):
        pass
    return counts


def _latest_journal_timestamp(journal_path: Path) -> datetime | None:
    """Get the timestamp of the most recent journal entry."""
    if not journal_path.exists():
        return None
    try:
        # Read last ~4KB for the last entry (avoids loading whole file)
        fsize = journal_path.stat().st_size
        with open(journal_path, encoding="utf-8") as f:
            if fsize > 4096:
                f.seek(max(0, fsize - 4096))
            tail = f.read()
        # Parse last JSON line
        lines = [l for l in tail.strip().split("\n") if l.strip()]
        if not lines:
            return None
        last = json.loads(lines[-1])
        ts = last.get("recorded_at") or last.get("timestamp_utc")
        if ts:
            if isinstance(ts, int | float):
                return datetime.fromtimestamp(ts, tz=UTC)
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return None
            # Ensure timezone-aware for comparison
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError):
        pass
    return None


def probe_symbol_liveness(
    symbol: str,
    *,
    project_root: Path | None = None,
) -> LivenessVerdict:
    """Probe whether a symbol is actively trading.

    Returns a structured LivenessVerdict with status and evidence.
    Designed for both CLI use and import into daily_ops Step 0.

    Status definitions:
      - ACTIVE:   journal has entries + recent activity + data dir populated
      - INACTIVE: data dir empty or never started (< MIN_JOURNAL_ENTRIES)
      - STALLED:  data dir populated but no recent journal activity (>72h)
      - UNKNOWN:  cannot determine (missing both journal and state)
    """
    root = project_root or Path(__file__).resolve().parent.parent
    data_dir = _resolve_data_dir(symbol, root)

    evidence: dict[str, Any] = {
        "data_dir": str(data_dir.relative_to(root)),
        "data_dir_exists": data_dir.exists(),
    }

    # ── Check 1: Data directory population ──
    if not data_dir.exists():
        return LivenessVerdict(
            symbol=symbol,
            data_dir=str(data_dir.relative_to(root)),
            status="INACTIVE",
            evidence=evidence,
            recommendations=[
                f"Create {data_dir.relative_to(root)} and launch live_launcher.py --symbol {symbol}"
            ],
        )

    # Count non-model files/dirs (empty dir with only models/ = never bootstrapped)
    top_level = list(data_dir.iterdir())
    evidence["top_level_entries"] = len(top_level)
    non_model_entries = [p for p in top_level if p.name not in ("models", "__pycache__")]

    # ── Check 2: Journal presence and activity ──
    journal_path = data_dir / "live_trade_journal.jsonl"
    counts = _count_journal_entries(journal_path)
    evidence["journal"] = {
        "exists": journal_path.exists(),
        "total_entries": counts["total"],
        "open_entries": counts["open"],
        "close_entries": counts["close"],
    }

    latest_ts = _latest_journal_timestamp(journal_path)
    if latest_ts:
        age_hours = (datetime.now(UTC) - latest_ts).total_seconds() / 3600
        evidence["journal"]["latest_entry_utc"] = latest_ts.isoformat()
        evidence["journal"]["age_hours"] = round(age_hours, 1)
    else:
        evidence["journal"]["latest_entry_utc"] = None
        evidence["journal"]["age_hours"] = None

    # ── Check 3: State file presence ──
    state_dir = data_dir / "state"
    state_files = list(state_dir.glob("*.json")) if state_dir.exists() else []
    evidence["state"] = {
        "dir_exists": state_dir.exists(),
        "json_file_count": len(state_files),
    }

    # ── Check 4: Feature store presence ──
    feature_store = data_dir / "feature_store"
    feature_records = list(feature_store.rglob("features.jsonl")) if feature_store.exists() else []
    evidence["feature_store"] = {
        "dir_exists": feature_store.exists(),
        "record_files": len(feature_records),
    }

    # ── Verdict logic ──
    recommendations: list[str] = []

    if counts["total"] < MIN_JOURNAL_ENTRIES and len(state_files) < MIN_STATE_FILES:
        # Never started
        if len(non_model_entries) <= 1:
            return LivenessVerdict(
                symbol=symbol,
                data_dir=str(data_dir.relative_to(root)),
                status="INACTIVE",
                evidence=evidence,
                recommendations=[
                    f"Symbol {symbol} has never been launched.",
                    f"To activate: python main.py live --symbol {symbol}",
                    f"Data dir {data_dir.relative_to(root)} has {len(non_model_entries)} non-model entries.",
                ],
            )
        # Data exists but no journal — corrupted or pre-bootstrap
        return LivenessVerdict(
            symbol=symbol,
            data_dir=str(data_dir.relative_to(root)),
            status="INACTIVE",
            evidence=evidence,
            recommendations=[
                "Data dir exists but journal is empty — possible failed bootstrap.",
                f"Check logs in {data_dir.relative_to(root)}/logs/",
            ],
        )

    # Has journal entries — check recency
    if latest_ts:
        age_hours = (datetime.now(UTC) - latest_ts).total_seconds() / 3600
        if age_hours > MIN_RECENT_JOURNAL_HOURS:
            recommendations.append(f"Last journal entry {age_hours:.0f}h ago — may be stalled.")
            return LivenessVerdict(
                symbol=symbol,
                data_dir=str(data_dir.relative_to(root)),
                status="STALLED",
                evidence=evidence,
                recommendations=recommendations,
            )

    return LivenessVerdict(
        symbol=symbol,
        data_dir=str(data_dir.relative_to(root)),
        status="ACTIVE",
        evidence=evidence,
        recommendations=recommendations,
    )


def probe_all(project_root: Path | None = None) -> dict[str, LivenessVerdict]:
    """Probe all known symbols.  Returns {symbol: LivenessVerdict}."""
    root = project_root or Path(__file__).resolve().parent.parent
    results: dict[str, LivenessVerdict] = {}
    for symbol in SYMBOL_DATA_DIR_MAP:
        results[symbol] = probe_symbol_liveness(symbol, project_root=root)
    return results


# ── CLI ──


def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Symbol liveness probe — institutional preflight gate"
    )
    parser.add_argument(
        "--symbol",
        help="Check a specific symbol (e.g. BTCUSDc, XAUUSDc)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Check all known symbols",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON",
    )
    parser.add_argument(
        "--exit-code",
        action="store_true",
        help="Exit 0=all ACTIVE, 1=some INACTIVE, 2=all INACTIVE",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        default=True,
        help="Human-readable table (default)",
    )

    args = parser.parse_args()

    if not args.symbol and not args.all:
        parser.error("Either --symbol or --all is required")

    root = Path(__file__).resolve().parent.parent

    if args.symbol:
        verdicts = {args.symbol: probe_symbol_liveness(args.symbol, project_root=root)}
    else:
        verdicts = probe_all(project_root=root)

    if args.json:
        output = {
            sym: {
                "status": v.status,
                "data_dir": v.data_dir,
                "evidence": v.evidence,
                "recommendations": v.recommendations,
            }
            for sym, v in verdicts.items()
        }
        print(json.dumps(output, indent=2, default=str))
    else:
        # Summary table
        print(f"{'Symbol':<12} {'Status':<10} {'Journal':>8} {'State':>7} {'FS':>5}  Data Dir")
        print("-" * 80)
        for sym, v in verdicts.items():
            j_count = str(v.evidence.get("journal", {}).get("total_entries", "?"))
            s_count = str(v.evidence.get("state", {}).get("json_file_count", "?"))
            fs_count = str(v.evidence.get("feature_store", {}).get("record_files", "?"))
            # Status color indicator
            if v.status == "ACTIVE":
                icon = "🟢"
            elif v.status == "STALLED":
                icon = "🟡"
            else:
                icon = "🔴"
            print(
                f"{icon} {sym:<10} {v.status:<9} {j_count:>8} {s_count:>6} {fs_count:>5}  {v.data_dir}"
            )
            if v.recommendations:
                for rec in v.recommendations:
                    print(f"   → {rec}")
        print()

    # Exit code logic
    if args.exit_code:
        statuses = {v.status for v in verdicts.values()}
        if all(s == "ACTIVE" for s in statuses):
            return 0
        if all(s in ("INACTIVE", "UNKNOWN") for s in statuses):
            return 2
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(_main())
