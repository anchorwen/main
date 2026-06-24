"""Smoke test: verify every registered brain can load its artifact and run inference."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.brains.services.brain_factory import BrainFactory


def main():
    brains_dir = PROJECT_ROOT / "configs" / "brains"
    brain_files = sorted(
        f for f in brains_dir.glob("*.json") if not f.name.endswith(".normalization.json")
    )

    results = []
    for bf in brain_files:
        entry = json.loads(bf.read_text(encoding="utf-8"))
        brain_id = entry.get("brain_id", bf.stem)
        brain_type = entry.get("brain_type", "?")
        artifact = entry.get("artifact_path", "?")
        status = entry.get("status", "?")

        result = {
            "brain_id": brain_id,
            "brain_type": brain_type,
            "status": status,
            "artifact": artifact,
            "load_ok": False,
            "infer_ok": False,
            "error": None,
        }

        try:
            factory = BrainFactory()
            adapter = factory.build(entry)
            adapter.load()

            # Generate appropriate test input for each brain type
            if brain_type == "ou_params_v6":
                test_input = np.array([4700.0], dtype=np.float32)
            elif brain_type in ("transformer_v5", "transformer_v4.3"):
                # Feed 64 bars before inference (buffer must be full)
                rng = np.random.RandomState(42)
                last_raw = None
                for _ in range(64):
                    last_raw = adapter.infer(rng.randn(9).astype(np.float32))
                raw = last_raw
            elif brain_type in ("xgboost_v4.5",):
                # Microstructure 9-dim input
                test_input = np.random.RandomState(42).randn(9).astype(np.float32)
                raw = adapter.infer(test_input)
            else:
                # V9 40-dim input
                test_input = np.random.RandomState(42).randn(40).astype(np.float32)
                raw = adapter.infer(test_input)

            prop = adapter.get_signal(raw)

            result["load_ok"] = True
            result["infer_ok"] = True
            result["direction"] = prop.prediction.get("direction_bias", "?")
            result["confidence"] = round(prop.prediction.get("confidence", 0.0), 4)

        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
            result["error"] = f"{type(exc).__name__}: {exc!s}"[:150]
        results.append(result)
        status_icon = "OK" if result["infer_ok"] else "FAIL"
        direction = result.get("direction", "?")
        conf = result.get("confidence", "?")
        print(
            f"[{status_icon}] {brain_id:40s} type={brain_type:20s} dir={direction:6s} conf={conf}"
        )

    ok = sum(1 for r in results if r["infer_ok"])
    fail = len(results) - ok
    print(f"\nDone: {ok}/{len(results)} brains OK, {fail} failed")

    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
