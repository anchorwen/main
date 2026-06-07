"""Per-brain schema validation at startup.

Validates each brain's feature schema against registered schemas
(store-backed) and implemented schemas (live-compute).  Drops
individual mismatched brains instead of killing the entire system.
"""

import logging
from typing import Any

from core.features.feature_service import FeatureService
from core.features.local_feature_store import LocalFeatureStore


def validate_per_brain_schema(
    brain_entries: list[dict[str, Any]],
    feature_store: LocalFeatureStore | None = None,
    *,
    default_symbol: str = "XAUUSDc",
    default_timeframe: str = "M5",
) -> dict[str, Any]:
    """Validate each brain's feature schema and classify as ok/dropped.

    A brain is OK if its feature_schema_id is either:
      1. Registered in the feature store's schemas.json (Tier 1 cache), or
      2. Listed in FeatureService.available_schemas() (Tier 2 live compute).

    Brains matching neither are dropped with a reason so the surviving
    strategy lines are unaffected.

    Returns:
        {"ok": [...], "dropped": [{brain_id, reason, available_store_schemas, ...}], "errors": [...]}
    """
    registered_schemas: dict[str, dict[str, Any]] = {}
    if feature_store is not None:
        try:
            registered_schemas = feature_store._load_schemas()
        except Exception:  # noqa: BLE001
            logging.warning("startup_validator: failed to load schemas from feature store")

    implemented_schemas = FeatureService.available_schemas()

    results: dict[str, Any] = {"ok": [], "dropped": [], "errors": []}

    # Build a lookup: schema_name → list of registered keys for diagnostics
    registered_by_name: dict[str, list[str]] = {}
    for key, info in registered_schemas.items():
        name = info.get("name", key.split(":")[0])
        registered_by_name.setdefault(name, []).append(key)

    for entry in brain_entries:
        brain_id = entry.get("brain_id", "unknown")
        schema_name = entry.get("feature_schema_id") or entry.get("feature_schema", "")

        if not schema_name:
            results["ok"].append(brain_id)
            continue

        # Determine symbol: deployment_scope.symbols[0] or default
        symbols = (entry.get("deployment_scope") or {}).get("symbols", [default_symbol])
        symbol = symbols[0] if symbols else default_symbol

        # Check Tier 1: registered in feature store schemas.json
        store_ok = any(
            info.get("name") == schema_name
            and info.get("symbol") == symbol
            and info.get("timeframe", default_timeframe) == default_timeframe
            for info in registered_schemas.values()
        )

        # Check Tier 2: implemented for live compute
        live_ok = schema_name in implemented_schemas

        if store_ok or live_ok:
            results["ok"].append(brain_id)
            if not store_ok and live_ok:
                logging.info(
                    "startup_validator: brain %s schema '%s' not in feature store — "
                    "will use live compute only (no cache write-back)",
                    brain_id,
                    schema_name,
                )
        else:
            results["dropped"].append(
                {
                    "brain_id": brain_id,
                    "schema_name": schema_name,
                    "symbol": symbol,
                    "reason": (
                        f"Schema '{schema_name}' not registered in feature store "
                        f"and not in implemented schemas"
                    ),
                    "available_store_schemas": list(registered_schemas.keys()),
                    "available_live_schemas": sorted(implemented_schemas),
                }
            )
            logging.error(
                "startup_validator: DROPPING brain %s — schema '%s' not found "
                "(store keys: %s, live schemas: %s)",
                brain_id,
                schema_name,
                sorted(registered_schemas.keys()),
                sorted(implemented_schemas),
            )

    if results["dropped"]:
        logging.warning(
            "startup_validator: %d brain(s) dropped, %d OK, %d errors",
            len(results["dropped"]),
            len(results["ok"]),
            len(results["errors"]),
        )

    return results
