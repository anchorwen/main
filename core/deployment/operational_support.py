import time
from collections.abc import Callable

from core.contracts.domain_keys import (
    HEALTH_CHECK_STATUS_FAILED,
    LIFECYCLE_PHASE_STATUS_ERROR,
    PAYLOAD_KEY_BACKOFF_FACTOR,
    PAYLOAD_KEY_BASE_DELAY_SECONDS,
    PAYLOAD_KEY_DETAIL,
    PAYLOAD_KEY_ERRORS,
    PAYLOAD_KEY_MAX_DELAY_SECONDS,
    PAYLOAD_KEY_MAX_RETRIES,
    PAYLOAD_KEY_RULE,
    PAYLOAD_KEY_RULES_CHECKED,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_VALID,
    PAYLOAD_KEY_WARNINGS,
)


class RetryPolicy:
    """Configurable retry with exponential backoff and jitter.

    Wraps a callable and retries on failure up to ``max_retries``
    times with configurable backoff.
    """

    def __init__(
        self,
        max_retries: int = 3,
        base_delay_seconds: float = 0.1,
        max_delay_seconds: float = 5.0,
        backoff_factor: float = 2.0,
        retryable_exceptions: tuple = (Exception,),
    ):
        self._max_retries = max_retries
        self._base_delay = base_delay_seconds
        self._max_delay = max_delay_seconds
        self._backoff_factor = backoff_factor
        self._retryable = retryable_exceptions

    def execute(self, fn: Callable, *args, **kwargs):
        last_exception: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return fn(*args, **kwargs)
            except self._retryable as exc:
                last_exception = exc
                if attempt < self._max_retries:
                    delay = min(
                        self._base_delay * (self._backoff_factor**attempt),
                        self._max_delay,
                    )
                    time.sleep(delay)
        assert last_exception is not None
        raise last_exception

    def get_config(self) -> dict:
        return {
            PAYLOAD_KEY_MAX_RETRIES: self._max_retries,
            PAYLOAD_KEY_BASE_DELAY_SECONDS: self._base_delay,
            PAYLOAD_KEY_MAX_DELAY_SECONDS: self._max_delay,
            PAYLOAD_KEY_BACKOFF_FACTOR: self._backoff_factor,
        }


class ConfigValidator:
    """Validates EnvironmentConfig for consistency and safety."""

    def __init__(self):
        self._rules: list[tuple[str, Callable]] = []
        self._register_defaults()

    def _register_defaults(self) -> None:
        self._rules.append(("base_dir_not_empty", lambda c: bool(c.base_dir)))
        self._rules.append(("max_open_positions_positive", lambda c: c.max_open_positions > 0))
        self._rules.append(("max_drawdown_valid", lambda c: 0.0 < c.max_drawdown_pct <= 100.0))
        self._rules.append(("max_notional_positive", lambda c: c.max_notional_exposure > 0))
        self._rules.append(("max_per_symbol_positive", lambda c: c.max_per_symbol > 0))
        self._rules.append(("feedback_window_positive", lambda c: c.feedback_window_size > 0))
        self._rules.append(("idempotency_ttl_positive", lambda c: c.idempotency_ttl_hours > 0))
        self._rules.append(("producer_name_set", lambda c: bool(c.producer_name)))
        self._rules.append(("target_name_set", lambda c: bool(c.target_name)))
        self._rules.append(
            (
                "ops_maturity_min_score_in_range",
                lambda c: 0.0 <= float(getattr(c, "ops_maturity_min_score", 60.0)) <= 100.0,
            )
        )
        self._rules.append(
            (
                "engine_config_poll_interval_nonnegative",
                lambda c: float(getattr(c, "engine_config_poll_interval_seconds", 60.0)) >= 0.0,
            )
        )

    def add_rule(self, name: str, check_fn: Callable) -> None:
        self._rules.append((name, check_fn))

    def validate(self, config) -> dict:
        errors = []
        warnings = []
        for name, check_fn in self._rules:
            try:
                if not check_fn(config):
                    errors.append(
                        {PAYLOAD_KEY_RULE: name, PAYLOAD_KEY_STATUS: HEALTH_CHECK_STATUS_FAILED}
                    )
            except Exception as exc:  # BLE001:REVIEWED
                errors.append(
                    {
                        PAYLOAD_KEY_RULE: name,
                        PAYLOAD_KEY_STATUS: LIFECYCLE_PHASE_STATUS_ERROR,
                        PAYLOAD_KEY_DETAIL: str(exc),
                    }
                )

        if config.environment == "production" and not config.enable_audit_log:
            warnings.append("audit_log disabled in production")
        if config.environment == "production" and not config.enable_metrics:
            warnings.append("metrics disabled in production")
        if config.environment == "production" and not config.enable_idempotency:
            warnings.append("idempotency disabled in production")

        return {
            PAYLOAD_KEY_VALID: len(errors) == 0,
            PAYLOAD_KEY_ERRORS: errors,
            PAYLOAD_KEY_WARNINGS: warnings,
            PAYLOAD_KEY_RULES_CHECKED: len(self._rules),
        }
