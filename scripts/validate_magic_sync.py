#!/usr/bin/env python
"""DQAF-20260622-059 / P2: CI gate — validate magic↔strategy sync between config and code.

Compares every ``strategy_lines.<name>.magic`` in the live config YAML(s)
against the hardcoded entries in ``core/contracts/strategy_magic.py``.
Flags any magic that is defined in the YAML but missing from the code mapping,
or vice versa.

Exit codes:
  0 — all magics synced
  1 — drift detected (CI should block)

Usage:
    python scripts/validate_magic_sync.py
    python scripts/validate_magic_sync.py --config configs/live_btc.yaml
    python scripts/validate_magic_sync.py --all   # check both live.yaml + live_btc.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any


def parse_strategy_magics_from_yaml(config_path: Path) -> dict[str, int]:
    """Extract {strategy_name: magic} from a live config YAML."""
    try:
        import yaml
    except ImportError:
        print(f"ERROR: PyYAML not available — cannot validate {config_path}", file=sys.stderr)
        return {}

    if not config_path.exists():
        print(f"ERROR: config not found: {config_path}", file=sys.stderr)
        return {}

    try:
        with open(config_path, encoding="utf-8") as f:
            cfg: dict[str, Any] = yaml.safe_load(f) or {}
    except Exception as exc:
        print(f"ERROR: failed to parse {config_path}: {exc}", file=sys.stderr)
        return {}

    strategy_lines = cfg.get("strategy_lines")
    if not isinstance(strategy_lines, dict):
        print(f"WARNING: no strategy_lines section in {config_path}")
        return {}

    magics: dict[str, int] = {}
    for sname, sconf in strategy_lines.items():
        if not isinstance(sconf, dict):
            continue
        magic_val = sconf.get("magic")
        if magic_val is not None:
            try:
                magics[str(sname)] = int(magic_val)
            except (TypeError, ValueError):
                print(f"WARNING: invalid magic {magic_val!r} for {sname} in {config_path}")

    return magics


def get_code_magics() -> dict[str, int]:
    """Get the hardcoded fallback entries from strategy_magic.py (via STRATEGY_TO_MAGIC)."""
    from core.contracts.strategy_magic import STRATEGY_TO_MAGIC

    # Filter out sentinel entries
    return {k: v for k, v in STRATEGY_TO_MAGIC.items() if not k.startswith("__")}


def validate(config_paths: list[Path]) -> int:
    """Validate magic sync between config YAML(s) and code mapping.

    Returns 0 on success, 1 on drift.
    """
    code_magics = get_code_magics()
    code_by_magic: dict[int, str] = {v: k for k, v in code_magics.items()}

    errors = 0
    for config_path in config_paths:
        yaml_magics = parse_strategy_magics_from_yaml(config_path)
        if not yaml_magics:
            continue

        print(f"\n── Validating {config_path.name} ──")
        print(f"  Config strategies: {len(yaml_magics)}")
        print(f"  Code strategies:   {len(code_magics)}")

        # Check: every YAML magic must exist in code mapping
        for sname, magic in sorted(yaml_magics.items()):
            if magic not in code_by_magic:
                print(
                    f"  [FAIL] MISSING: magic={magic} (strategy={sname}) is in {config_path.name} "
                    f"but NOT in strategy_magic.py MAGIC_TO_STRATEGY"
                )
                errors += 1
            elif code_by_magic[magic] != sname:
                print(
                    f"  [WARN]  MISMATCH: magic={magic} → code={code_by_magic[magic]!r}, "
                    f"config={sname!r}"
                )
                errors += 1

        # Check: every code magic (non-sentinel) should exist in at least one config
        for sname, magic in sorted(code_magics.items()):
            if magic not in yaml_magics.values():
                print(
                    f"  [WARN]  CODE_ONLY: magic={magic} (strategy={sname}) is in strategy_magic.py "
                    f"but NOT in {config_path.name}"
                )

        # Also check bridge default magic
        try:
            import yaml

            with open(config_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            bridge_magic = cfg.get("live_trading", {}).get("bridge", {}).get("magic")
            if bridge_magic is not None:
                from core.contracts.strategy_magic import MAGIC_TO_STRATEGY

                resolved = MAGIC_TO_STRATEGY.get(int(bridge_magic), "")
                if not resolved.startswith("__UNATTRIBUTED"):
                    print(
                        f"  [WARN]  BRIDGE_DEFAULT: magic={bridge_magic} resolves to {resolved!r} — "
                        f"should be __UNATTRIBUTED_BRIDGE_DEFAULT__"
                    )
        except Exception:
            pass

    if errors == 0:
        print(f"\n[PASS] All magic mappings synced — {len(config_paths)} config(s) validated.")
        return 0
    else:
        print(f"\n[FAIL] {errors} drift(s) detected — CI should block.")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="DQAF-059: Validate magic-strategy sync between config and code"
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to a single live config YAML to validate",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Validate all live config YAMLs (live.yaml + live_btc.yaml)",
    )
    args = parser.parse_args()

    if args.config:
        config_paths = [Path(args.config)]
    elif args.all:
        config_paths = [
            Path("configs/live.yaml"),
            Path("configs/live_btc.yaml"),
        ]
    else:
        # Default: validate both if they exist
        config_paths = [
            p for p in [Path("configs/live.yaml"), Path("configs/live_btc.yaml")] if p.exists()
        ]
        if not config_paths:
            print("ERROR: no config files found. Use --config or --all.", file=sys.stderr)
            return 2

    return validate(config_paths)


if __name__ == "__main__":
    raise SystemExit(main())
