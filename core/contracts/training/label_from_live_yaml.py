"""Label params SSOT — label parameters come from live YAML, NOT training scripts.

Phase 2 / M1 (FIX-20260803-003, 战役二 — 标签契约的绝对独裁 / IC 最高批准):
    Training scripts are DEPRIVED of the power to set SL/TP/spread themselves.
    The ONLY source of truth for label-relevant parameters is the strategy line
    in the live YAML (``configs/live_btc.yaml`` for BTC, ``configs/live.yaml``
    for XAU):

        strategy_configs.<line>.sl.base_atr_mult
        strategy_configs.<line>.tp.base_atr_mult
        strategy_configs.<line>.spread_points
        strategy_configs.<line>.timeframe

    Alignment direction (DQAF-20260630-200 lesson): live.yaml → training.  NEVER
    reverse (aligning training to a stale brain config was the original bug).

    ``validate_label_vs_live.py`` is the hard gate: any training contract whose
    label triple (SL/TP/spread) diverges from the live strategy line is REJECTED
    (raise / non-zero exit) — no model is produced from mismatched labels.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

_log = logging.getLogger(__name__)

# Conservative slippage estimate (matches LabelContract default).  Live YAML
# carries spread_points but not slippage; this constant is the documented floor.
DEFAULT_SLIPPAGE_POINTS = 10


@dataclass(frozen=True)
class LiveLabelParams:
    """Label-relevant parameters resolved from a live strategy line (SSOT)."""

    strategy_line: str
    symbol: str
    sl_atr_mult: float
    tp_atr_mult: float
    spread_points: float
    slippage_points: float
    timeframe: str
    tick_size: float
    tick_value: float

    def to_dict(self) -> dict:
        return {
            "strategy_line": self.strategy_line,
            "symbol": self.symbol,
            "sl_atr_mult": self.sl_atr_mult,
            "tp_atr_mult": self.tp_atr_mult,
            "spread_points": self.spread_points,
            "slippage_points": self.slippage_points,
            "timeframe": self.timeframe,
            "tick_size": self.tick_size,
            "tick_value": self.tick_value,
        }


def _symbol_physics(live_yaml_path: str | Path) -> tuple[str, float, float]:
    """Resolve (symbol, tick_size, tick_value) from the live YAML filename.

    BTCUSDc: 3-digit? No — BTC pairs on MT5 retail are 0.01 tick.  XAUUSDc
    cent account: 0.001 tick (3-digit).  Convention mirrors LabelContract
    dataclass defaults + the configs/brains_btc label_contract tick values.
    """
    name = Path(live_yaml_path).name.lower()
    if "btc" in name:
        return ("BTCUSDc", 0.01, 0.01)
    return ("XAUUSDc", 0.001, 0.01)


def label_params_from_live_yaml(
    strategy_line: str,
    live_yaml_path: str | Path = "configs/live_btc.yaml",
) -> LiveLabelParams:
    """Extract label-relevant parameters from a strategy line (single SSOT).

    Raises ``KeyError`` when the strategy line is absent or lacks required
    label fields — fail-closed: a training script MUST NOT guess defaults.

    Args:
        strategy_line: Name of the strategy line (e.g. ``btc_swing_m30``).
        live_yaml_path: Live YAML (BTC → live_btc.yaml, XAU → live.yaml).

    Returns:
        LiveLabelParams frozen dataclass.
    """
    path = Path(live_yaml_path)
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    strategy_lines = data.get("strategy_lines", {})
    if strategy_line not in strategy_lines:
        raise KeyError(
            f"Strategy line '{strategy_line}' not found in {path}. "
            f"Known BTC lines: {sorted(k for k in strategy_lines if k.startswith('btc'))}"
        )
    line = strategy_lines[strategy_line]

    def _require(key: str) -> float:
        val = line.get(key)
        if val is None:
            raise KeyError(
                f"Strategy line '{strategy_line}' missing required label field "
                f"'{key}' in {path} — training cannot derive labels without it."
            )
        return float(val)

    sl = line.get("sl", {})
    tp = line.get("tp", {})
    sl_mult = sl.get("base_atr_mult")
    tp_mult = tp.get("base_atr_mult")
    if sl_mult is None or tp_mult is None:
        raise KeyError(
            f"Strategy line '{strategy_line}' missing sl.base_atr_mult / tp.base_atr_mult "
            f"in {path} — label contract cannot be validated against live."
        )
    spread = line.get("spread_points")
    if spread is None:
        raise KeyError(f"Strategy line '{strategy_line}' missing spread_points in {path}.")
    timeframe = line.get("timeframe", "M5")

    symbol, tick_size, tick_value = _symbol_physics(path)
    return LiveLabelParams(
        strategy_line=strategy_line,
        symbol=symbol,
        sl_atr_mult=float(sl_mult),
        tp_atr_mult=float(tp_mult),
        spread_points=float(spread),
        slippage_points=DEFAULT_SLIPPAGE_POINTS,
        timeframe=str(timeframe),
        tick_size=tick_size,
        tick_value=tick_value,
    )
