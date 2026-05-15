"""Canonical system-wide window sizes and buffer limits.

Every bounded buffer in the system MUST be documented here with its
location, default value, and the reason for the chosen size.  This
prevents magical "why 64?" questions six months later and makes it
easy to audit all memory-growth-prevention mechanisms in one place.

When adding a new deque / list truncation / rolling window, add an
entry to the relevant section below.  New code should import from
here; existing code may continue using local constants but should
eventually be migrated.
"""

# ═══════════════════════════════════════════════════════════════════════
# Inference / Feature sequence windows
# ═══════════════════════════════════════════════════════════════════════

# Transformer sequence length (M5 bars in rolling buffer).
# 64 bars × 5min = 5h20min context window — long enough to capture
# intraday regime shifts without accumulating overnight noise.
# Used by: TransformerBrainAdapter._seq_len
TRANSFORMER_SEQ_LEN = 64
TRANSFORMER_NUM_FEATURES = 9

# Rolling normalizer warmup — bars to accumulate before switching
# from full-sample to EWMA statistics.
# Used by: core.features.rolling_normalizer.RollingNormalizer
WARMUP_BARS = 100

# EWMA halflife for rolling normalizer (in M5 bars).
# 18,144 bars ≈ 63 trading days = one quarter.
# Used by: core.features.rolling_normalizer.RollingNormalizer
EWMA_HALFLIFE_BARS = 18144

# Feature freshness SLA — max age (seconds) of cached features before
# falling through to live computation.
# Used by: core.features.feature_service.FeatureService
FEATURE_FRESHNESS_SLA_SECONDS = 300

# ═══════════════════════════════════════════════════════════════════════
# Feedback / Performance tracking windows
# ═══════════════════════════════════════════════════════════════════════

# Rolling performance window — most recent N trade outcomes used for
# brain quality scoring (Sharpe, win rate, profit factor).
# 100 trades at ~5 trades/day ≈ 20 trading days.
# Used by: BrainPerformanceTracker, BrainPnLStore
PERFORMANCE_WINDOW = 100

# Online learner recent-update history — kept for diagnostics only.
# Used by: OnlineLearnerAdapter._max_recent
ONLINE_LEARNER_UPDATE_HISTORY = 50

# Online learner recent validation samples — feature+label pairs held
# for drift loss computation.
# Used by: OnlineLearnerAdapter._RECENT_SAMPLES
ONLINE_LEARNER_VALIDATION_SAMPLES = 30

# Drift protection: weight-delta tracking window.
# Used by: OnlineLearnerAdapter._DRIFT_WINDOW
DRIFT_WEIGHT_DELTA_WINDOW = 20

# Drift protection: snapshot interval (in partial_fit updates).
# Used by: OnlineLearnerAdapter._SNAPSHOT_INTERVAL
DRIFT_SNAPSHOT_INTERVAL = 10

# Max drift events before freezing online learner permanently.
# Used by: OnlineLearnerAdapter._MAX_DRIFT_EVENTS
MAX_DRIFT_EVENTS = 3

# ═══════════════════════════════════════════════════════════════════════
# Signal health / monitoring windows
# ═══════════════════════════════════════════════════════════════════════

# Prediction history for drift detection — up/down/confidence distributions.
# 500 cycles × ~20s cycle = ~2h46min of prediction history.
# Used by: SignalHealthMonitor
PREDICTION_HISTORY_WINDOW = 500

# ATR anomaly IQR multiplier for outlier detection.
# Used by: SignalHealthMonitor
ATR_IQR_MULTIPLIER = 3.0

# Spread anomaly IQR multiplier for outlier detection.
# Used by: SignalHealthMonitor
SPREAD_IQR_MULTIPLIER = 3.0

# Prediction drift threshold — |Δmean| > 0.30 triggers alert.
# Used by: SignalHealthMonitor
PREDICTION_DRIFT_THRESHOLD = 0.30

# Confidence collapse threshold — mean confidence < 0.35 triggers alert.
# Used by: SignalHealthMonitor
CONFIDENCE_COLLAPSE_THRESHOLD = 0.35

# ═══════════════════════════════════════════════════════════════════════
# Risk / Guard windows
# ═══════════════════════════════════════════════════════════════════════

# Regime detector rolling lookback (M5 bars) for volatility percentile.
# 500 bars ≈ 1.7 trading days.
# Used by: core.risk.regime_detector.RegimeDetector
REGIME_LOOKBACK_BARS = 500

# Consecutive bars to confirm a new regime (hysteresis).
# Used by: core.risk.regime_detector.RegimeDetector
REGIME_CONFIRM_BARS = 3

# Consecutive bars to exit a regime.
# Used by: core.risk.regime_detector.RegimeDetector
REGIME_EXIT_BARS = 2

# Minimum cycles between regime changes (rate limit).
# Used by: core.risk.regime_detector.RegimeDetector
REGIME_RATE_LIMIT_CYCLES = 10

# Circuit breaker — consecutive failures before opening.
# Used by: core.protocol.services.resilience.CircuitBreaker
CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5

# Circuit breaker — cooldown seconds in OPEN before HALF_OPEN probe.
# Used by: core.protocol.services.resilience.CircuitBreaker
CIRCUIT_BREAKER_COOLDOWN_SECONDS = 30

# Intraday drawdown kill threshold (% of account equity).
# Used by: core.execution.pre_trade_guards.IntradayDrawdownKill
INTRADAY_DD_KILL_PCT = 2.0

# Intraday drawdown force-close threshold (% of account equity).
# Used by: core.execution.pre_trade_guards.IntradayDrawdownKill
INTRADAY_DD_FORCE_CLOSE_PCT = 3.0

# Max drawdown policy threshold (% of account equity).
# Used by: core.risk.risk_policies.DrawdownPolicy
MAX_DRAWDOWN_PCT = 5.0

# ═══════════════════════════════════════════════════════════════════════
# Re-entry guard windows
# ═══════════════════════════════════════════════════════════════════════

# SL-hit re-entry cooldown (seconds).
# Used by: core.execution.reentry_guard.ReentryGuard
SL_REENTRY_COOLDOWN_SECONDS = 180

# SL re-entry confidence improvement requirement.
# Used by: core.execution.reentry_guard.ReentryGuard
SL_REENTRY_CONFIDENCE_IMPROVEMENT = 0.10

# SL streak breaker — consecutive losses before strategy block.
# Used by: core.runtime.live_cycle (SL streak tracking)
SL_STREAK_BREAK_COUNT = 3

# SL streak block duration (seconds).
# Used by: core.runtime.live_cycle (SL streak tracking)
SL_STREAK_BLOCK_SECONDS = 1800

# ═══════════════════════════════════════════════════════════════════════
# Messaging / Infrastructure
# ═══════════════════════════════════════════════════════════════════════

# Redis stream max length — prevents unbounded stream growth.
# Used by: core.observability.message_broker.MessageBroker
REDIS_STREAM_MAXLEN = 10000

# Feature store retention (days) for JSONL compaction.
# Used by: LocalFeatureStore.compact()
FEATURE_STORE_RETENTION_DAYS = 7

# Inference guard timeout for subprocess inference (seconds).
# Used by: core.brains.services.inference_guard.InferenceGuard
INFERENCE_GUARD_TIMEOUT_SECONDS = 5.0

# Inference guard max restarts before giving up.
# Used by: core.brains.services.inference_guard.InferenceGuard
INFERENCE_GUARD_MAX_RESTARTS = 3

# Inference guard restart cooldown (seconds).
# Used by: core.brains.services.inference_guard.InferenceGuard
INFERENCE_GUARD_RESTART_COOLDOWN_SECONDS = 3

# Daily ops run interval (seconds) — 23h to allow slack.
DAILY_OPS_INTERVAL_SECONDS = 82800

# System mode stale threshold (seconds) — reset to NORMAL after 24h.
# Used by: core.state.stores.system_mode_store.SystemModeStore
MODE_STALE_SECONDS = 86400

# Verification stamp expiry (seconds).
# Used by: scripts.verify.check_stamp()
VERIFY_STAMP_EXPIRY_SECONDS = 1800
