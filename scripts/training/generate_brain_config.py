"""Auto-generate brain registry config JSON from training output.

Produces brain_registry_entry.v1 JSON ready for configs/brains/.
Use after training to avoid manual config boilerplate.

Usage:
  # From training result (auto-detects fields)
  python scripts/training/generate_brain_config.py \
    --result-json-path data/training/batch/g2026.2/manifests/CRT.arb...s42.json \
    --model-path data/models/arb_params_v7_m15.json

  # Explicit (no result JSON available)
  python scripts/training/generate_brain_config.py \
    --model-path data/models/sur_mlp_m15_s42.onnx \
    --brain-type onnx_v9 \
    --lane sur --timeframe M15 --seed 42 \
    --training-contract label-survival-barrier-1.0.0-M15 \
    --feature-schema v9_institutional_40

  # Dry run (print to stdout, no file written)
  python scripts/training/generate_brain_config.py ... --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ── Lane → brain-type + feature-schema + role defaults ──
LANE_DEFAULTS: dict[str, dict[str, str]] = {
    "sur": {
        "brain_type": "onnx_v9",
        "feature_schema_id": "v9_institutional_40",
        "brain_role": "alpha_brain",
        "brain_id_prefix": "Survival_V9",
    },
    "mtx": {
        "brain_type": "xgboost_v4.5",
        "feature_schema_id": "v2_microstructure_288",
        "brain_role": "alpha_brain",
        "brain_id_prefix": "Microstructure",
    },
    "arb": {
        "brain_type": "ou_params_v6",
        "feature_schema_id": "v6_price_series_1",
        "brain_role": "arb_brain",
        "brain_id_prefix": "OU_Params",
    },
    "xgbinrepo": {
        "brain_type": "xgboost_v9",
        "feature_schema_id": "v9_institutional_40",
        "brain_role": "alpha_brain",
        "brain_id_prefix": "XGB_InRepo",
    },
}

# ── Lane → strategy line compatibility (fallback) ──
STRATEGY_COMPAT: dict[str, list[str]] = {
    "sur": ["barrier_12bar"],
    "mtx": ["micro_m5", "micro_m15", "micro_h1"],
    "arb": ["statarb_dynamic"],
    "xgbinrepo": ["barrier_12bar"],
}

# ── Contract → strategy line compatibility (primary, E7) ──
_CONTRACT_STRATEGY_MAP: dict[str, list[str]] = {
    "label-survival-barrier": ["barrier_12bar"],
    "label-micro-barrier": ["micro_m5", "micro_m15", "micro_h1"],
    "arb_params": ["statarb_dynamic"],
    "ou_params": ["statarb_dynamic"],
}


def _derive_strategies_from_contract(contract_id: str | None, timeframe: str) -> list[str]:
    """Derive compatible strategy lines from training contract ID.

    Uses contract ID pattern matching with per-TF filtering for
    microstructure (micro-barrier) contracts.

    Returns empty list when contract is unknown — callers should fall
    back to lane-based STRATEGY_COMPAT.
    """
    if not contract_id:
        return []
    cid = contract_id.lower()
    for pattern, strategies in _CONTRACT_STRATEGY_MAP.items():
        if pattern in cid:
            if "micro-barrier" in pattern:
                tf_map = {"M5": "micro_m5", "M15": "micro_m15", "H1": "micro_h1"}
                matched = tf_map.get(timeframe.upper())
                if matched and matched in strategies:
                    return [matched]
                return []
            return list(strategies)
    return []


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="generate_brain_config",
        description="Auto-generate brain registry config from training output",
    )
    p.add_argument("--model-path", type=Path, required=True, help="Path to model artifact")
    p.add_argument(
        "--brain-type",
        type=str,
        default=None,
        help="Brain type key (e.g. onnx_v9, xgboost_v4.5_m15)",
    )
    p.add_argument("--lane", type=str, default=None, help="Training lane (sur/mtx/arb/xgbinrepo)")
    p.add_argument("--timeframe", type=str, default="M5", help="Timeframe (M5/M15/H1/H4)")
    p.add_argument("--seed", type=int, default=None, help="Training seed")
    p.add_argument("--training-contract", type=str, default=None, help="Training contract ID")
    p.add_argument("--feature-schema", type=str, default=None, help="Feature schema ID")
    p.add_argument(
        "--brain-id", type=str, default=None, help="Custom brain_id (auto-generated if omitted)"
    )
    p.add_argument("--status", type=str, default="shadow", help="Initial status (default: shadow)")
    p.add_argument("--vote-weight", type=float, default=0.5, help="Vote weight (default: 0.5)")
    p.add_argument(
        "--result-json-path",
        type=Path,
        default=None,
        help="Path to training result.json (auto-fills fields)",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "configs" / "brains",
        help="Output directory",
    )
    p.add_argument("--dry-run", action="store_true", help="Print to stdout without writing file")
    return p


def load_result(result_path: Path) -> dict[str, Any]:
    if not result_path.exists():
        return {}
    return json.loads(result_path.read_text(encoding="utf-8"))


def make_brain_id(lane: str, timeframe: str, seed: int | None, prefix: str) -> str:
    """Generate a stable brain_id from lane/TF/seed."""
    tf_part = f"_{timeframe}" if timeframe and timeframe != "M5" else ""
    seed_part = f"_s{seed}" if seed is not None else ""
    return f"{prefix}{tf_part}{seed_part}"


def _resolve_features_for_schema(feature_schema_id: str) -> list[str] | None:
    """Resolve canonical feature name list for a schema_id."""
    try:
        from core.features.schemas.registry import SCHEMA_DIMENSIONS, get_schema_feature_names

        if feature_schema_id not in SCHEMA_DIMENSIONS:
            return None
        return get_schema_feature_names(feature_schema_id)
    except Exception:
        return None


def make_brain_config(
    model_path: Path,
    brain_type: str,
    brain_id: str,
    lane: str,
    timeframe: str,
    seed: int | None,
    training_contract: str | None,
    feature_schema_id: str,
    brain_role: str,
    status: str,
    vote_weight: float,
    hmre_layer: str | None,
) -> dict[str, Any]:
    """Build brain_registry_entry.v1 JSON."""
    config: dict[str, Any] = {
        "schema_version": "brain_registry_entry.v1",
        "brain_id": brain_id,
        "brain_type": brain_type,
        "brain_role": brain_role,
        "model_version": "auto-generated",
        "status": status,
        "vote_weight": vote_weight,
        "artifact_path": str(
            model_path.relative_to(PROJECT_ROOT)
            if model_path.is_relative_to(PROJECT_ROOT)
            else model_path
        ),
        "feature_schema_id": feature_schema_id,
        "deployment_scope": {
            "symbols": ["XAUUSDc", "XAUUSD"],
            "sessions": ["all"],
            "regimes": ["all"],
        },
    }

    # Auto-populate features from schema
    features = _resolve_features_for_schema(feature_schema_id)
    if features:
        config["features"] = features
    if training_contract:
        config["training_contract"] = training_contract
    if hmre_layer:
        config["hmre_layer"] = hmre_layer
    if seed is not None:
        config["training_seed"] = seed
    if lane:
        config["training_lane"] = lane
    if timeframe:
        config["training_timeframe"] = timeframe
    # Derive compatible strategies: contract first, lane fallback
    contract_strategies = _derive_strategies_from_contract(training_contract, timeframe)
    if contract_strategies:
        config["compatible_strategies"] = contract_strategies
    else:
        compat = STRATEGY_COMPAT.get(lane, [])
        if compat:
            config["compatible_strategies"] = compat
    return config


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Resolve model path
    model_path = args.model_path.resolve()
    if not model_path.exists():
        print(f"[ERROR] Model artifact not found: {model_path}", file=sys.stderr)
        return 2

    # Load result JSON for auto-fill
    result: dict[str, Any] = {}
    if args.result_json_path:
        result = load_result(args.result_json_path.resolve())

    # Auto-fill from result JSON
    lane = args.lane or result.get("lane")
    timeframe = args.timeframe or result.get("timeframe", "M5")
    seed = args.seed if args.seed is not None else result.get("seed") or result.get("train_seed")
    training_contract = args.training_contract or result.get("training_contract")

    # Apply lane defaults
    defaults = LANE_DEFAULTS.get(lane or "", {})
    brain_type = args.brain_type or defaults.get("brain_type", "onnx_v9")
    feature_schema_id = args.feature_schema or defaults.get(
        "feature_schema_id", "v9_institutional_40"
    )
    brain_role = defaults.get("brain_role", "alpha_brain")
    prefix = defaults.get("brain_id_prefix", lane or "Unknown")

    # Microstructure lane: per-TF brain_type suffix
    if lane == "mtx" and timeframe and timeframe != "M5":
        if "xgboost" in brain_type:
            brain_type = f"xgboost_v4.5_{timeframe.lower()}"
            feature_schema_id = "v2_microstructure_288"
        elif "transformer" in brain_type:
            brain_type = f"transformer_v5_{timeframe.lower()}"
            feature_schema_id = "v2_microstructure_9"

    # hmre_layer for microstructure brains
    hmre_layer: str | None = None
    if lane == "mtx" and feature_schema_id.startswith("v2_microstructure"):
        hmre_layer = timeframe

    # Generate brain_id
    brain_id = args.brain_id or make_brain_id(lane or "unknown", timeframe, seed, prefix)

    # Build config
    config = make_brain_config(
        model_path=model_path,
        brain_type=brain_type,
        brain_id=brain_id,
        lane=lane or "",
        timeframe=timeframe,
        seed=seed,
        training_contract=training_contract,
        feature_schema_id=feature_schema_id,
        brain_role=brain_role,
        status=args.status,
        vote_weight=args.vote_weight,
        hmre_layer=hmre_layer,
    )

    if args.dry_run:
        print(json.dumps(config, ensure_ascii=False, indent=2))
        return 0

    # Write config file
    args.output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = brain_id.lower().replace(" ", "_").replace(".", "_")
    output_path = args.output_dir / f"{safe_name}.json"
    output_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[generate_brain_config] {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
