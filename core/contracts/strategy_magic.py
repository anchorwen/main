"""Centralized magic-number → strategy-name mapping.

Single source of truth — dynamically loaded from live config YAML at bootstrap.
Previously hardcoded; now derived from ``strategy_lines.<name>.magic`` in the
live config so that adding a strategy to the YAML automatically registers its
magic mapping.  Eliminates the CONFIG_CODE_DESYNC defect identified in
DQAF-20260622-059 (MAGIC_DRIFT_ATTRIBUTION_LOSS).

Architecture (DQAF-059 / P1):
  - The YAML config is the authoritative SSOT.
  - Call ``init_magic_mappings(config_path)`` once at bootstrap (Bridge Worker
    ``main()``, live_cycle entry point, or training pipeline entry point).
  - The function is idempotent — subsequent calls are no-ops.
  - Hardcoded fallback entries exist for legacy/backtest environments where
    no YAML config is available.

Sentinel value:
  90401 → "__UNATTRIBUTED_BRIDGE_DEFAULT__" — bridge fallback magic that
  MUST NOT be attributed to any real strategy.  ML pipelines MUST filter
  entries with this strategy value.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Sentinel magic number ──
SENTINEL_UNATTRIBUTED_MAGIC: int = 90401
SENTINEL_UNATTRIBUTED_STRATEGY: str = "__UNATTRIBUTED_BRIDGE_DEFAULT__"


class UnattributedOrderRejected(RuntimeError):
    """Raised when an OPEN order is dispatched with the sentinel magic (90401).

    Per DQAF-20260622-059 / P2 (Fail-Fast): the sentinel magic MUST NOT be
    used for any real strategy order.  If it appears on an open request,
    something is broken upstream (strategy→magic resolution).  This
    exception should be caught by the strategy evaluator to trigger
    strategy-level self-protection (stop sending orders, enter
    close-only mode).
    """

    def __init__(self, magic: int, strategy_name: str = "", intent_id: str = "") -> None:
        self.magic = magic
        self.strategy_name = strategy_name
        self.intent_id = intent_id
        super().__init__(
            f"Magic {magic} is the sentinel value "
            f"'{SENTINEL_UNATTRIBUTED_STRATEGY}' and MUST NOT be used for "
            f"live orders. strategy={strategy_name or '?'}, "
            f"intent_id={intent_id or '?'}.  This is a FAIL-FAST guard — "
            f"the upstream strategy→magic resolution pipeline is broken."
        )


# ── Module-level state (lazy-initialised singleton) ──
_MAPPINGS_INITIALIZED: bool = False
MAGIC_TO_STRATEGY: dict[int, str] = {}
STRATEGY_TO_MAGIC: dict[str, int] = {}

# ── Hardcoded fallback (legacy / backtest / no-config environments) ──
_HARDCODED_FALLBACK: dict[int, str] = {
    90001: "barrier_12bar",
    90002: "micro_3bar",
    90003: "statarb_dynamic",
    90014: "barrier_12bar_meta",
    90101: "micro_m15",
    90103: "statarb_m15",
    90201: "micro_h1",
    # Swing strategies (TF-specific barrier contracts, D1 features)
    90301: "daily_swing",
    90310: "m15_swing",
    90320: "m30_swing",
    90321: "m30_reversion",
    90330: "h1_swing",
    90340: "h4_swing",
    # BTC strategies (isolated 904xx range — zero collision with gold 900xx/903xx)
    90410: "btc_swing",
    # ── DQAF-20260622-059: 90411 was MISSING from this mapping ──
    90411: "btc_swing_h1",
    # Additional strategies (synced from live.yaml via validate_magic_sync.py CI gate)
    90303: "h1_directional",
    90501: "structural_swing_v1",
    # ── FIX-20260824-004: Live Fire 敢死队 — Micro Scaler v2 真实执行 (投委会方向 B) ──
    # 不注册 strategy_line (旁路派发), 独立 magic 专属 → journal 归属可追溯, 熔断器按 magic 聚合.
    90601: "micro_scaler_v2_live_fire",
    # ── Sentinel — MUST NOT be attributed to any real strategy ──
    90401: "__UNATTRIBUTED_BRIDGE_DEFAULT__",
}


def _derive_mappings_from_yaml(config_path: str) -> dict[int, str]:
    """Parse ``strategy_lines`` from a live config YAML and return magic→strategy dict.

    Returns an empty dict if the file is missing, unparseable, or has no
    strategy_lines section.
    """
    cfg_path = Path(config_path)
    if not cfg_path.exists():
        logger.warning("strategy_magic: config not found — %s", cfg_path)
        return {}

    try:
        import yaml
    except ImportError:
        logger.warning("strategy_magic: PyYAML not available, using hardcoded fallback")
        return {}

    try:
        with open(cfg_path, encoding="utf-8") as fh:
            cfg: dict[str, Any] = yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("strategy_magic: failed to parse %s — %s", cfg_path, exc)
        return {}

    strategy_lines = cfg.get("strategy_lines")
    if not isinstance(strategy_lines, dict):
        logger.warning(
            "strategy_magic: no strategy_lines section in %s, using hardcoded fallback",
            cfg_path,
        )
        return {}

    mapping: dict[int, str] = {}
    for sname, sconf in strategy_lines.items():
        if not isinstance(sconf, dict):
            continue
        magic_val = sconf.get("magic")
        if magic_val is not None:
            try:
                mapping[int(magic_val)] = str(sname)
            except (TypeError, ValueError):
                logger.warning(
                    "strategy_magic: invalid magic %r for strategy %s — skipping",
                    magic_val,
                    sname,
                )

    # Register bridge default magic as sentinel if present
    live_trading = cfg.get("live_trading")
    if isinstance(live_trading, dict):
        bridge = live_trading.get("bridge")
        if isinstance(bridge, dict):
            bridge_magic = bridge.get("magic")
            if bridge_magic is not None:
                try:
                    bm = int(bridge_magic)
                    if bm not in mapping:
                        mapping[bm] = "__UNATTRIBUTED_BRIDGE_DEFAULT__"
                except (TypeError, ValueError):
                    pass

    return mapping


def init_magic_mappings(config_path: str | None = None) -> None:
    """Initialise MAGIC_TO_STRATEGY and STRATEGY_TO_MAGIC from a YAML config.

    Idempotent for *implicit* (auto-init) calls.  Explicit calls with a
    ``config_path`` always reload — they merge YAML entries with the
    hardcoded fallback so that strategies registered in live config YAML
    but not in the hardcoded table are properly mapped.

    Call once at bootstrap from the Bridge Worker ``main()``, the
    live-cycle entry point, and training pipeline entry points.

    Args:
        config_path: Path to a live config YAML (e.g. ``configs/live_btc.yaml``).
            If *None* or the file is unreadable, the hardcoded fallback is used.
    """
    global _MAPPINGS_INITIALIZED, MAGIC_TO_STRATEGY, STRATEGY_TO_MAGIC  # noqa: PLW0603

    # Auto-init (no config_path) is idempotent — hardcoded fallback is enough.
    # Explicit init with a config_path MUST NOT be blocked by auto-init because
    # the module-level auto-init only has hardcoded entries.  Strategies
    # registered in YAML but not in _HARDCODED_FALLBACK (e.g. btc_swing_h4,
    # btc_swing_m30) would never get mapped otherwise.
    # FIX-20260715-018: auto-init idempotency guard over-blocked YAML init.
    if _MAPPINGS_INITIALIZED and not config_path:
        return

    mapping: dict[int, str] = {}

    if config_path:
        yaml_mapping = _derive_mappings_from_yaml(config_path)
        if yaml_mapping:
            mapping.update(yaml_mapping)
            logger.info(
                "strategy_magic: loaded %d entries from %s",
                len(yaml_mapping),
                config_path,
            )

    # Merge hardcoded fallback for any entries not covered by YAML
    for magic_int, strategy_name in _HARDCODED_FALLBACK.items():
        if magic_int not in mapping:
            mapping[magic_int] = strategy_name

    MAGIC_TO_STRATEGY = mapping
    # Reverse lookup — exclude sentinel entries (those starting with __)
    STRATEGY_TO_MAGIC = {v: k for k, v in mapping.items() if not v.startswith("__")}

    _MAPPINGS_INITIALIZED = True


# ── Auto-initialise with hardcoded fallback on module import ──
# This preserves backward-compatibility: any code that does
# ``from core.contracts.strategy_magic import MAGIC_TO_STRATEGY``
# without calling init_magic_mappings() first will still get a working
# (hardcoded-fallback) mapping.
if not _MAPPINGS_INITIALIZED:
    MAGIC_TO_STRATEGY = dict(_HARDCODED_FALLBACK)
    STRATEGY_TO_MAGIC = {v: k for k, v in _HARDCODED_FALLBACK.items() if not v.startswith("__")}
    _MAPPINGS_INITIALIZED = True
