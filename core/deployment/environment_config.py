from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.deployment.domain_keys import VALIDATION_MODE_DEEP


class Environment(str, Enum):
    PRODUCTION = "production"
    SIMULATION = "simulation"
    REPLAY = "replay"
    DEVELOPMENT = "development"
    TEST = "test"


@dataclass
class EnvironmentConfig:
    """Centralised configuration for a specific deployment environment.

    All environment-specific knobs are gathered here so that the
    service container can wire the right adapters and policies.
    """

    environment: Environment
    base_dir: str
    receipt_dir: str | None = None
    audit_dir: str | None = None

    system_mode: str = "normal"
    max_open_positions: int = 10
    max_drawdown_pct: float = 5.0
    max_notional_exposure: float = 1_000_000.0
    max_per_symbol: int = 3
    idempotency_ttl_hours: int = 48

    brain_registry_path: str | None = None
    #: If set, JSON hot-reload watches this file; else `<base_dir>/engine_config.json` when it exists.
    hot_reload_path: str | None = None
    adapter_name: str = "stub"
    producer_name: str = "decision_engine"
    target_name: str = "exec_bridge"

    enable_feedback_loop: bool = False
    enable_audit_log: bool = True
    enable_metrics: bool = True
    enable_idempotency: bool = True
    live_read_only: bool = False

    risk_policy_window_size: int = 100
    feedback_window_size: int = 100

    #: Minimum `ops_maturity` score for governance checks and CLI exit 0.
    ops_maturity_min_score: float = 60.0

    #: Interval for background `check_and_reload` on `engine_config.json` (0 = disabled).
    engine_config_poll_interval_seconds: float = 60.0

    #: Default validation depth for deployment/governance services.
    validation_mode: str = VALIDATION_MODE_DEEP

    extensions: dict[str, Any] = field(default_factory=dict)

    def is_live(self) -> bool:
        return self.environment == Environment.PRODUCTION

    def is_simulation(self) -> bool:
        return self.environment == Environment.SIMULATION

    def is_replay(self) -> bool:
        return self.environment == Environment.REPLAY

    def allows_real_dispatch(self) -> bool:
        return self.environment in {Environment.PRODUCTION, Environment.SIMULATION}

    @classmethod
    def development(cls, base_dir: str, **overrides) -> "EnvironmentConfig":
        defaults = {
            "environment": Environment.DEVELOPMENT,
            "base_dir": base_dir,
            "enable_feedback_loop": True,
            "adapter_name": "stub",
        }
        defaults.update(overrides)
        return cls(**defaults)

    @classmethod
    def test(cls, base_dir: str, **overrides) -> "EnvironmentConfig":
        defaults = {
            "environment": Environment.TEST,
            "base_dir": base_dir,
            "enable_audit_log": False,
            "enable_metrics": False,
            "enable_idempotency": False,
            "adapter_name": "stub",
        }
        defaults.update(overrides)
        return cls(**defaults)

    @classmethod
    def production(cls, base_dir: str, **overrides) -> "EnvironmentConfig":
        defaults = {
            "environment": Environment.PRODUCTION,
            "base_dir": base_dir,
            "enable_feedback_loop": True,
            "enable_audit_log": True,
            "enable_metrics": True,
            "enable_idempotency": True,
            "max_drawdown_pct": 3.0,
            "max_open_positions": 5,
        }
        defaults.update(overrides)
        return cls(**defaults)
