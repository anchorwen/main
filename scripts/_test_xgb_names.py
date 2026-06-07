"""Test various feature_names scenarios."""
import json

import numpy as np
import xgboost as xgb

from core.brains.adapters.xgboost_brain_adapter import XGBoostBrainAdapter

cfg = json.load(open("configs/brains_btc/BTC_Swing_V4.json"))
print(f"Brain features: {len(cfg['features'])}")

# Test 1: directly with booster
b = xgb.Booster()
b.load_model("data_btc/models/swing/BTC_Swing_V4_model.json")
print("\n=== Direct booster tests ===")
print(f"feature_names: {len(b.feature_names)}")
print(f"feature_names[0]: {b.feature_names[0]}")

# Test with correct 29-dim + 29 names
fv29 = np.random.randn(29).astype(np.float64)
d = xgb.DMatrix(fv29.reshape(1,-1), feature_names=b.feature_names)
try:
    pred = b.predict(d)
    print(f"Test 1 (29dim+29names): OK, pred shape={pred.shape}")
except Exception as e:  # noqa: BLE001
    print(f"Test 1 (29dim+29names): FAIL - {e}")

# Test with 24-dim + 24 names (what XGBoost seems to expect)
fv24 = np.random.randn(24).astype(np.float64)
fn24 = b.feature_names[:24]  # first 24 names
d24 = xgb.DMatrix(fv24.reshape(1,-1), feature_names=fn24)
try:
    pred = b.predict(d24)
    print(f"Test 2 (24dim+24names): OK, pred shape={pred.shape}")
except Exception as e:  # noqa: BLE001
    print(f"Test 2 (24dim+24names): FAIL - {e}")

# Test with 24-dim + 29 names
fn29 = b.feature_names  # all 29
d24_29 = xgb.DMatrix(fv24.reshape(1,-1), feature_names=fn29)
try:
    pred = b.predict(d24_29)
    print(f"Test 3 (24dim+29names): OK, pred shape={pred.shape}")
except Exception as e:  # noqa: BLE001
    print(f"Test 3 (24dim+29names): FAIL - {e}")

# Test with 29-dim + NO names
d29_none = xgb.DMatrix(fv29.reshape(1,-1))
try:
    pred = b.predict(d29_none)
    print(f"Test 4 (29dim+no names): OK, pred shape={pred.shape}")
except Exception as e:  # noqa: BLE001
    print(f"Test 4 (29dim+no names): FAIL - {e}")

# Test 5: use brain config features, 29-dim
brain_names = cfg['features']
d29_brain = xgb.DMatrix(fv29.reshape(1,-1), feature_names=brain_names)
try:
    pred = b.predict(d29_brain)
    print(f"Test 5 (29dim+brain names): OK, pred shape={pred.shape}")
except Exception as e:  # noqa: BLE001
    print(f"Test 5 (29dim+brain names): FAIL - {e}")

# Test 6: adapter directly with 29-dim (assembly output)
print("\n=== Adapter test ===")
a = XGBoostBrainAdapter(brain_entry=cfg)
a.load()
fv29 = np.random.randn(29).astype(np.float64)
try:
    result = a.infer(fv29)
    print(f"Adapter infer(29-dim): OK, raw_score={result.get('raw_score','?')}")
except Exception as e:  # noqa: BLE001
    print(f"Adapter infer(29-dim): FAIL - {e}")

# Test 7: adapter with 24-dim (the actual live scenario)
fv24 = np.random.randn(24).astype(np.float64)
try:
    result = a.infer(fv24)
    print(f"Adapter infer(24-dim): OK, raw_score={result.get('raw_score','?')}")
except Exception as e:  # noqa: BLE001
    print(f"Adapter infer(24-dim): FAIL - {e}")
