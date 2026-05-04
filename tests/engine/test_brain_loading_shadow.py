"""Smoke-test brain loading + inference via live.yaml + ServiceContainer."""

import numpy as np
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

    # Run inference with dummy feature vector
    dummy_features = np.zeros(40, dtype=np.float64)
    proposals = brain_run_service.run_active_brains({}, {}, dummy_features)
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
