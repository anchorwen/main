"""Repair brain config files — fill missing ``features`` fields from schema definitions.

Usage:
    python scripts/repair_brain_configs.py --validate-only   # report issues, don't modify
    python scripts/repair_brain_configs.py --write            # fill missing fields + save
    python scripts/repair_brain_configs.py --check            # exit 1 if any issues found
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Schema name → canonical feature name list
SCHEMA_FEATURE_NAMES: dict[str, list[str]] = {}

# Schema aliases
SCHEMA_ALIASES: dict[str, str] = {
    "swing_24": "daily_swing_24",
    "v2_microstructure_9": "v4.3_microstructure_9",
    "v4.5_microstructure_9": "v4.3_microstructure_9",
}

# Schema name → expected dimension
SCHEMA_DIMENSIONS: dict[str, int] = {
    "v9_institutional_40": 40,
    "v4.5_microstructure_9": 9,
    "v2_microstructure_9": 9,
    "v2_microstructure_288": 288,
    "v4.3_microstructure_9": 9,
    "daily_swing_24": 24,
    "swing_24": 24,
    "v6_price_series_1": 1,
}


def _load_schemas() -> None:
    """Lazy-load feature schemas from core."""
    if SCHEMA_FEATURE_NAMES:
        return
    from core.features.schemas.daily_swing_schema import DAILY_SWING_24_FEATURES
    from core.features.schemas.microstructure_schema import MICROSTRUCTURE_9_FEATURES
    from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES

    SCHEMA_FEATURE_NAMES["v9_institutional_40"] = list(V9_INSTITUTIONAL_40_FEATURES)
    SCHEMA_FEATURE_NAMES["daily_swing_24"] = list(DAILY_SWING_24_FEATURES)
    SCHEMA_FEATURE_NAMES["swing_24"] = list(DAILY_SWING_24_FEATURES)
    SCHEMA_FEATURE_NAMES["v4.3_microstructure_9"] = list(MICROSTRUCTURE_9_FEATURES)
    SCHEMA_FEATURE_NAMES["v4.5_microstructure_9"] = list(MICROSTRUCTURE_9_FEATURES)
    SCHEMA_FEATURE_NAMES["v2_microstructure_9"] = list(MICROSTRUCTURE_9_FEATURES)
    SCHEMA_FEATURE_NAMES["v2_microstructure_288"] = list(MICROSTRUCTURE_9_FEATURES) * 32
    SCHEMA_FEATURE_NAMES["v6_price_series_1"] = ["price_return"]


def _resolve_schema(schema_id: str) -> str:
    """Resolve alias to canonical schema name."""
    return SCHEMA_ALIASES.get(schema_id, schema_id)


def _get_features(schema_id: str) -> list[str] | None:
    """Get canonical feature list for a schema, or None if unknown."""
    canonical = _resolve_schema(schema_id)
    return SCHEMA_FEATURE_NAMES.get(canonical)


def repair_config(config_path: str, write: bool = False) -> dict:
    """Analyze and optionally repair a single brain config.

    Returns a dict with findings.
    """
    path = Path(config_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    brain_id = data.get("brain_id", path.stem)
    schema_id = data.get("feature_schema_id", "")

    result = {
        "path": str(path),
        "brain_id": brain_id,
        "schema_id": schema_id,
        "has_features": "features" in data and bool(data.get("features")),
        "issues": [],
        "fixed": False,
    }

    # Check schema is known
    canonical = _resolve_schema(schema_id) if schema_id else ""
    if schema_id and canonical not in SCHEMA_FEATURE_NAMES:
        result["issues"].append(f"unknown feature_schema_id: {schema_id}")
        return result

    expected = _get_features(schema_id) if schema_id else None

    if "features" not in data or not data.get("features"):
        if expected:
            result["issues"].append(f"missing features field (expected {len(expected)} names)")
            if write:
                data["features"] = expected
                result["fixed"] = True
        else:
            result["issues"].append("missing features field (unknown schema, cannot auto-fill)")
    else:
        current = data["features"]
        if expected and len(current) != len(expected):
            result["issues"].append(
                f"features length mismatch: got {len(current)}, expected {len(expected)}"
            )
            if write:
                data["features"] = expected
                result["fixed"] = True
        elif expected:
            # Check individual names
            mismatched = []
            for i, name in enumerate(current):
                if i < len(expected) and name != expected[i]:
                    mismatched.append(f"[{i}] '{name}' != '{expected[i]}'")
            if mismatched:
                result["issues"].append(f"feature name mismatches: {'; '.join(mismatched[:5])}")
                if write:
                    data["features"] = expected
                    result["fixed"] = True

    if write and result["fixed"]:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        result["issues"].append("FIXED: features field populated from schema")

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair brain config metadata")
    parser.add_argument(
        "--validate-only", action="store_true", help="Report issues only, no writes"
    )
    parser.add_argument("--write", action="store_true", help="Fill missing features fields + save")
    parser.add_argument("--check", action="store_true", help="Exit 1 if any issues found (CI mode)")
    args = parser.parse_args()

    _load_schemas()

    config_dir = Path("configs/brains")
    configs = sorted(f for f in config_dir.glob("*.json") if ".normalization." not in f.name)

    results = []
    for cfg in configs:
        r = repair_config(str(cfg), write=args.write)
        results.append(r)

    # Report
    missing = [r for r in results if not r["has_features"]]
    mismatched = [r for r in results if r["issues"] and not any("FIXED" in i for i in r["issues"])]
    fixed = [r for r in results if r["fixed"]]

    print(f"Total configs: {len(results)}")
    print(f"  OK:              {sum(1 for r in results if r['has_features'] and not r['issues'])}")
    print(f"  Missing features: {len(missing)}")
    print(f"  Other issues:     {len(mismatched)}")
    if args.write:
        print(f"  Fixed:            {len(fixed)}")

    for r in results:
        if r["issues"]:
            print(f"\n  {Path(r['path']).name} ({r['schema_id']}):")
            for issue in r["issues"]:
                print(f"    - {issue}")

    if args.check and (missing or mismatched):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
