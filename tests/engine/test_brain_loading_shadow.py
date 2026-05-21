"""Smoke-test brain loading + inference via live.yaml + ServiceContainer."""

import yaml

from core.deployment.environment_config import EnvironmentConfig
from core.deployment.service_container import ServiceContainer


def main() -> None:
    config = yaml.safe_load(open("configs/live.yaml", encoding="utf-8"))

    extensions: dict = {}
    brains_cfg = config.get("brains", {})
    extensions["brain_registry_entries"] = brains_cfg.get("registry_entries", [])
    extensions["mt5_terminal_path"] = None

    env = EnvironmentConfig.development("data", extensions=extensions)
    c = ServiceContainer(env)
    c.build()

    brain_run_service = c.brain_run_service
    assert brain_run_service is not None, "brain_run_service not built"

    # Run ensure_loaded first (the previous test confirmed all 3 loaded)
    loaded = brain_run_service.ensure_loaded()
    print(f"Loaded brains ({len(loaded)}):", loaded)

    # Run inference with a V9 feature dict on the blackboard.
    # Brain configs now have a "features" field; the adapter extracts
    # values by name.  Missing keys default to 0.0.
    feature_blackboard = {
        "v9_institutional_40": {
            # Provide a realistic feature dict for V9 institutional brains.
            # All values at 0.0 is valid — the model will produce a
            # prediction (not frozen, just based on zero-feature input).
            "M5_Ret_1": 0.0,
            "M5_Body_Ratio": 0.5,
            "M5_ATR_14": 2.0,
            "M5_RSI_14": 50.0,
            "M5_MACD": 0.0,
            "M5_Vol_ZScore": 0.0,
            "M5_Macro1_Corr": 0.3,
            "M5_Price_ZScore": 4.0,
        }
    }
    proposals = brain_run_service.run_active_brains({}, {}, feature_blackboard)
    print(f"\nInference proposals ({len(proposals)}):")
    for p in proposals:
        pred = p.prediction
        print(
            f"  {p.brain_id:30s} dir={pred['direction_bias']:8s} "
            f"up={pred['up_probability']:.3f} down={pred['down_probability']:.3f} "
            f"conf={pred['confidence']:.3f}"
        )


if __name__ == "__main__":
    main()
