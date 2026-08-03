"""Feature-store schema reconciliation — re-register SSOT field lists in schemas.json.

Phase 4 / M2 (FIX-20260803-005, 战役四 — 特征仓写侧修正 / IC 最高批准):

  The write side of the local feature store validates records against the
  schemas registered in ``schemas.json`` (exact field count + name match).
  Over time the registration DRIFTS from the schema registry SSOT:

    - ``btc_macro_enhanced_41`` is registered with only 37 fields while the
      real schema is 41 → even a correct write is REJECTED by precision
      counting.  (This is the R3 evidence half: the flywheel is doubly broken.)
    - ``btc_macro_enhanced_41_v2`` / ``btc_macro_flow_46`` are not registered
      at all → persistence silently skips them.

  This script repairs the registration FROM the SSOT (``core/features/schemas/registry.py``).
  Registration is the only thing it touches — it never rewrites record data.

Usage:
  # Reconcile ALL BTC schemas
  python scripts/features/reconcile_store_schemas.py --store-dir data_btc/feature_store

  # Reconcile a single schema
  python scripts/features/reconcile_store_schemas.py \
    --store-dir data_btc/feature_store --schema btc_macro_enhanced_41_v2

  # Dry-run: report drift without writing
  python scripts/features/reconcile_store_schemas.py --store-dir data_btc/feature_store --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# All BTC schemas that persist via btc_feature_persist.py / the future 46-dim path.
BTC_SCHEMAS = ("btc_macro_enhanced_41", "btc_macro_enhanced_41_v2", "btc_macro_flow_46")
# Symbol/timeframe used by the BTC persist path.
BTC_SYMBOL = "BTCUSDc"
BTC_TIMEFRAME = "M5"
# Match the version the runtime writes with (micro_persist / btc_feature_persist).
DEFAULT_VERSION = "1.0.0"


def reconcile_store_schemas(
    store_dir: str | Path,
    *,
    schema_names: tuple[str, ...] = BTC_SCHEMAS,
    dry_run: bool = False,
) -> dict[str, list[dict]]:
    """Re-register SSOT field lists for the given schemas in schemas.json.

    Returns a report: {repaired: [...], registered: [...], ok: [...], errors: [...]}.
    """
    from core.features.local_feature_store import LocalFeatureStore
    from core.features.schemas.registry import get_schema_feature_names
    from core.features.store_contracts import FeatureSchema

    store = LocalFeatureStore(str(store_dir))
    existing = {s.name: s for s in store.list_schemas()}

    report: dict[str, list[dict]] = {
        "repaired": [],
        "registered": [],
        "ok": [],
        "errors": [],
    }

    for name in schema_names:
        try:
            fields = tuple(get_schema_feature_names(name))
        except KeyError as exc:
            report["errors"].append({"schema": name, "reason": f"unknown in SSOT: {exc}"})
            continue
        if not fields:
            report["errors"].append({"schema": name, "reason": "SSOT has no feature-name mapping"})
            continue

        schema = FeatureSchema(
            name=name,
            version=DEFAULT_VERSION,
            fields=fields,
            symbol=BTC_SYMBOL,
            timeframe=BTC_TIMEFRAME,
            description=f"BTC {len(fields)}-dim institutional features (reconciled by "
            "reconcile_store_schemas.py)",
        )

        current = existing.get(name)
        if current is None:
            if not dry_run:
                store.register_schema(schema)
            report["registered"].append({"schema": name, "fields": len(fields)})
        elif tuple(current.fields) != fields:
            if not dry_run:
                store.register_schema(schema)  # overwrite with SSOT field list
            report["repaired"].append(
                {
                    "schema": name,
                    "fields": f"{len(current.fields)} → {len(fields)}",
                }
            )
        else:
            report["ok"].append({"schema": name, "fields": len(fields)})

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile feature-store schemas vs SSOT")
    parser.add_argument("--store-dir", default="data_btc/feature_store", help="Store base dir")
    parser.add_argument(
        "--schema",
        action="append",
        default=[],
        help="Schema to reconcile (repeatable; default: all BTC schemas)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report drift without writing")
    args = parser.parse_args()

    schemas = tuple(args.schema) if args.schema else BTC_SCHEMAS
    report = reconcile_store_schemas(args.store_dir, schema_names=schemas, dry_run=args.dry_run)

    print("=" * 72)
    print(f"[reconcile] store-dir : {args.store_dir}")
    print(f"[reconcile] schemas   : {', '.join(schemas)}")
    print(f"[reconcile] dry-run   : {args.dry_run}")
    print("-" * 72)
    for key in ("ok", "registered", "repaired", "errors"):
        for entry in report[key]:
            if key == "errors":
                print(f"[reconcile] ✗ {entry['schema']}: {entry['reason']}")
            elif key == "ok":
                print(f"[reconcile] ✓ {entry['schema']}: {entry['fields']} fields (SSOT-aligned)")
            elif key == "registered":
                print(
                    f"[reconcile] + {entry['schema']}: registered {entry['fields']} fields "
                    "(was missing)"
                )
            else:
                print(
                    f"[reconcile] ~ {entry['schema']}: fields {entry['fields']} "
                    "(re-registered from SSOT)"
                )
    print("=" * 72)

    n_drift = len(report["registered"]) + len(report["repaired"])
    print(
        f"[reconcile] {len(report['ok'])} aligned / {n_drift} repaired "
        f"/ {len(report['errors'])} errors"
    )
    return 0 if not report["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
