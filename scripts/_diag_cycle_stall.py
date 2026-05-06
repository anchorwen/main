"""Diagnose where the intent_loop first cycle stalls."""

import json
import sys
from datetime import UTC, datetime

MT5_PATH = r"D:\MetaTrader 5\terminal64.exe"
SYMBOL = "XAUUSDc"


def _utc_iso():
    return datetime.now(UTC).isoformat()


def log(msg):
    print(json.dumps({"t": _utc_iso(), "msg": msg}), flush=True)


log("Starting cycle stall diagnosis")

# Step 1: import and init MT5
log("step1: importing MetaTrader5")
import MetaTrader5 as mt5

log("step2: initializing MT5")
init_result = mt5.initialize(path=MT5_PATH)
log(f"step2_result: {init_result}")
if not init_result:
    log(f"step2_error: {mt5.last_error()}")
    sys.exit(1)

log(f"step2_terminal_info: {mt5.terminal_info()}")

# Step 3: position count (same as intent_loop)
log("step3: testing _position_count")
import threading as th


def _position_count(mt5_obj, symbol, timeout=5.0):
    result = [None]
    exc_info = [None]

    def _target():
        try:
            pos = mt5_obj.positions_get(symbol=symbol)
            result[0] = len(pos) if pos else 0
        except Exception as e:
            exc_info[0] = e

    t = th.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        return 0
    if exc_info[0] is not None:
        raise exc_info[0]
    return result[0] if result[0] is not None else 0


pos_count = _position_count(mt5, SYMBOL)
log(f"step3_pos_count: {pos_count}")

# Step 4: copy_rates_from_pos (core of feature compute)
log("step4: testing copy_rates_from_pos for M5")
rates_m5 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M5, 0, 200)
log(f"step4_rates_m5: {len(rates_m5) if rates_m5 is not None else 'None'}")

log("step5: testing copy_rates_from_pos for M15")
rates_m15 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M15, 0, 200)
log(f"step5_rates_m15: {len(rates_m15) if rates_m15 is not None else 'None'}")

log("step6: testing copy_rates_from_pos for M30")
rates_m30 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M30, 0, 200)
log(f"step6_rates_m30: {len(rates_m30) if rates_m30 is not None else 'None'}")

log("step7: testing copy_rates_from_pos for H1")
rates_h1 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H1, 0, 200)
log(f"step7_rates_h1: {len(rates_h1) if rates_h1 is not None else 'None'}")

# Step 8: Full FeatureService construction and build
log("step8: constructing FeatureService")
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.features.adapters.v9_feature_adapter import V9FeatureAdapter
from core.features.computers.v9_live_computer import V9LiveFeatureComputer
from core.features.feature_service import FeatureService
from core.features.schemas.v9_schema import build_v9_schema
from core.features.stores.local_feature_store import LocalFeatureStore

feature_computer = V9LiveFeatureComputer(mt5, SYMBOL)
feature_adapter = V9FeatureAdapter(rolling_normalizer=None)
feature_store = LocalFeatureStore(str(PROJECT_ROOT / "data" / "feature_store"))
feature_schema = build_v9_schema(symbol=SYMBOL)
feature_store.register_schema(feature_schema)

feature_service = FeatureService(
    feature_adapter=feature_adapter,
    feature_computer=feature_computer,
    default_venue="MT5",
    feature_store=feature_store,
    default_symbol=SYMBOL,
    store_schema_name="v9_institutional_40",
    store_timeframe="M5",
)
log("step8_feature_service_constructed")

# Step 9: build_feature_vector
log("step9: calling build_feature_vector")
trigger = {"symbol": SYMBOL, "venue": "MT5"}
fv = feature_service.build_feature_vector(trigger)
log(f"step9_feature_vector_shape: {fv.shape if hasattr(fv, 'shape') else len(fv)}")

# Step 10: cleanup
log("step10: shutting down MT5")
mt5.shutdown()
log("all_steps_complete")
