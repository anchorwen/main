"""Live intent loop bootstrap helpers — extracted from live_intent_loop.py.

Strangler Fig #19: initialization functions extracted from main()
to keep the CLI entry point thin.  Each function is independently
testable and returns its initialized objects.

Related: Strangler Fig #9 (live_startup.py — brain/config init functions)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from core.runtime.fault_handler import fail_open_guard
from core.runtime.time_utils import _utc_iso

_logger = logging.getLogger(__name__)

# ── Initialisation ───────────────────────────────────────────────────


def init_feature_services(
    *,
    mt5: Any,
    mt5_worker: Any,
    symbol: str,
    feature_store_dir: str,
    rolling_norm: Any,
    norm_config: dict[str, Any] | None,
    project_root: Path,
    no_mt5: bool = False,
) -> dict[str, Any]:
    """Initialize V9 live feature computer, adapters, store, and daily provider.

    Returns a dict with keys: feature_computer, feature_adapter, feature_schema,
    feature_store, feature_service, micro_feature_computer, micro_feature_adapter,
    daily_feature_provider.
    """
    result: dict[str, Any] = {
        "feature_adapter": None,
        "feature_service": None,
        "feature_computer": None,
        "feature_schema": None,
        "feature_store": None,
        "micro_feature_adapter": None,
        "micro_feature_computer": None,
        "daily_feature_provider": None,
    }

    if no_mt5:
        return result

    # ── V9 Live Feature Computer ──
    from core.features.computers.v9_live_computer import V9LiveFeatureComputer

    result["feature_computer"] = V9LiveFeatureComputer(mt5, symbol, mt5_worker=mt5_worker)

    # ── V9 Feature Adapter ──
    from core.features.adapters.v9_feature_adapter import V9FeatureAdapter

    result["feature_adapter"] = V9FeatureAdapter(
        rolling_normalizer=rolling_norm,
        normalization_config=norm_config,
    )

    # ── Microstructure feature computer + adapter ──
    from core.features.adapters.microstructure_feature_adapter import MicrostructureFeatureAdapter
    from core.features.computers.microstructure_computer import MicrostructureFeatureComputer

    result["micro_feature_computer"] = MicrostructureFeatureComputer(
        mt5, symbol, mt5_worker=mt5_worker
    )
    # DQAF-054/055/058: mandatory scaler safe-loading — discover per-symbol micro scaler
    _base_dir = Path(feature_store_dir).parent
    _micro_scaler_path = MicrostructureFeatureAdapter.resolve_scaler_path(_base_dir, symbol)
    # DQAF-058: Cold-start self-healing — auto-generate identity scaler
    # when no empirical scaler exists (e.g. XAU with 0 feature-store records).
    # This closes the deployment gap between the generate_cold_start_scaler()
    # factory and the live bootstrap path.
    if _micro_scaler_path is None:
        _fallback_path = _base_dir / "models" / f"{symbol.lower()[:3]}_micro_scaler.json"
        _logger.warning(
            "No micro scaler found for %s. Auto-generating cold-start identity scaler at %s",
            symbol,
            _fallback_path,
        )
        MicrostructureFeatureAdapter.generate_cold_start_scaler(_fallback_path)
        _micro_scaler_path = _fallback_path
    result["micro_feature_adapter"] = MicrostructureFeatureAdapter(
        scaler_path=_micro_scaler_path,
        require_scaler=True,
    )

    # ── Local Feature Store ──
    _store_dir = Path(feature_store_dir)
    if not _store_dir.is_absolute():
        _store_dir = project_root / _store_dir
    from core.features.local_feature_store import LocalFeatureStore

    result["feature_store"] = LocalFeatureStore(str(_store_dir))

    # ── Schemas ──
    from core.deployment.feature_update_producer import build_v9_schema
    from core.features.schemas.microstructure_schema import build_microstructure_schema

    result["feature_schema"] = build_v9_schema(symbol=symbol)
    result["feature_store"].register_schema(result["feature_schema"])
    result["feature_store"].register_schema(build_microstructure_schema(symbol=symbol))

    # ── Feature Service ──
    from core.features.feature_service import FeatureService

    result["feature_service"] = FeatureService(
        feature_adapter=result["feature_adapter"],
        feature_computer=result["feature_computer"],
        default_venue="MT5",
        feature_store=result["feature_store"],
        default_symbol=symbol,
        store_schema_name="v9_institutional_40",
        store_timeframe="M5",
    )

    # ── Daily D1 Feature Provider ──
    try:
        from core.features.computers.live_daily_provider import LiveDailyFeatureProvider

        result["daily_feature_provider"] = LiveDailyFeatureProvider(
            mt5_module=mt5,
            mt5_worker=mt5_worker,
            symbol=symbol,
            d1_csv="data/raw/xauusdc_d1_merged.csv",
            h4_csv="data/raw/xauusdc_h4_merged.csv",
        )
        print(
            json.dumps(
                {
                    "event": "daily_feature_provider_ready",
                    "time": _utc_iso(),
                    "latest_timestamp": result["daily_feature_provider"].latest_timestamp,
                    "feature_dim": result["daily_feature_provider"].feature_dim,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    except Exception as _dfp_exc:  # BLE001:FOG (logged, Phase 3b)
        with fail_open_guard("live_bootstrap:init_feature_services"):
            print(
                json.dumps(
                    {
                        "event": "daily_feature_provider_init_failed",
                        "time": _utc_iso(),
                        "error": str(_dfp_exc),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    return result
