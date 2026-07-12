"""Config Semantic Validation — cross-file consistency checks.

FIX-20260712-002 (Phase 1 Layer 1): Provides reusable config consistency
validation that can be called at startup (brain_lifecycle_manager) or
on-demand (daily_ops / health checks).

Prevents:
  - Magic number collisions across different contract groups
  - Strategy family members spread across terminals (BTC↔XAU pollution)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ConfigConsistencyError(ValueError):
    """Cross-file config inconsistency detected."""


@dataclass
class ConsistencyReport:
    """Result of a cross-file consistency scan."""

    valid: bool = True
    magic_collisions: list[str] = field(default_factory=list)
    family_cross_terminal: list[str] = field(default_factory=list)
    total_brains_scanned: int = 0


def check_magic_uniqueness(
    brains: list[dict[str, Any]],
    *,
    allow_same_contract_group: bool = True,
) -> list[str]:
    """Detect duplicate magic numbers across brain configs."""
    collisions: list[str] = []
    magic_map: dict[int, list[tuple[str, str]]] = {}
    for b in brains:
        magic = b.get("magic")
        if magic is None or magic == 0:
            continue
        bid = b.get("brain_id", "?")
        cg = b.get("contract_group", "?")
        magic_map.setdefault(int(magic), []).append((bid, cg))

    for magic, entries in magic_map.items():
        if len(entries) <= 1:
            continue
        if allow_same_contract_group:
            groups = {cg for _, cg in entries}
            if len(groups) == 1:
                continue
        bids = [bid for bid, _ in entries]
        collisions.append(
            f"Magic {magic} collision: {', '.join(bids)} "
            f"(groups: {', '.join(set(cg for _, cg in entries))})"
        )
    return collisions


def check_family_cross_terminal(
    brains: list[dict[str, Any]],
    terminal_map: dict[str, str] | None = None,
) -> list[str]:
    """Detect strategy family members spread across BTC/XAU terminals."""
    if terminal_map is None:
        terminal_map = {"brains_btc": "BTC", "brains_xau": "XAU", "brains": "XAU"}

    family_terminals: dict[str, dict[str, list[str]]] = {}
    for b in brains:
        cg = b.get("contract_group", "?")
        bid = b.get("brain_id", "?")
        src_dir = b.get("_source_dir", "")
        terminal = "?"
        for key, label in terminal_map.items():
            if key in src_dir:
                terminal = label
                break
        family_terminals.setdefault(cg, {}).setdefault(terminal, []).append(bid)

    issues: list[str] = []
    for cg, terminals in family_terminals.items():
        if len(terminals) > 1:
            members = []
            for term, bids in terminals.items():
                members.append(f"{term}: [{', '.join(bids)}]")
            issues.append(f"Cross-terminal family '{cg}': {', '.join(members)}")
    return issues


def scan_brains_dir(brains_dir: str | Path) -> list[dict[str, Any]]:
    """Load all brain_registry_entry.v1 configs from a directory."""
    brains: list[dict[str, Any]] = []
    path = Path(brains_dir)
    if not path.is_dir():
        return brains
    for fp in sorted(path.glob("*.json")):
        if ".normalization." in fp.name:
            continue
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
            continue
        if raw.get("schema_version") != "brain_registry_entry.v1":
            continue
        raw["_source_dir"] = path.name
        raw["_source_path"] = str(fp)
        brains.append(raw)
    return brains


def run_consistency_check(
    brains_dirs: list[str] | None = None,
) -> ConsistencyReport:
    """Run all cross-file consistency checks across brain config directories."""
    if brains_dirs is None:
        brains_dirs = ["configs/brains_btc", "configs/brains_xau", "configs/brains"]

    all_brains: list[dict[str, Any]] = []
    for d in brains_dirs:
        all_brains.extend(scan_brains_dir(d))

    report = ConsistencyReport(total_brains_scanned=len(all_brains))
    report.magic_collisions = check_magic_uniqueness(all_brains)
    report.family_cross_terminal = check_family_cross_terminal(all_brains)

    report.valid = not (report.magic_collisions or report.family_cross_terminal)
    if not report.valid:
        logger.warning(
            "Config consistency check found %d issues: magic=%d, cross-terminal=%d",
            len(report.magic_collisions) + len(report.family_cross_terminal),
            len(report.magic_collisions),
            len(report.family_cross_terminal),
        )
    return report
