"""p_win computation chain — pure functions extracted under Golden Master safety net.

S3 — Functional Core路线: 将开单闸门的核心计算逻辑提取为纯函数。
纯函数 = 无I/O、无全局状态、相同输入永远产生相同输出。

Extracted functions:
  - resolve_p_win_from_brains(): 从大脑PnL历史计算动态p_win
  - adjust_p_win_for_regime(): 根据趋势方向调整OU策略的p_win

Both were previously scattered across kelly_sizer.py and strategy_line.py.
Consolidated here for:
  - Property-based testing (Hypothesis) on boundary conditions
  - Golden Master replay verification
  - Single point of change for p_win logic

Related FIXes: 018-032, 026-030, 026-031, 026-032, 026-035
Known Issue: KI-004 — 3 silent fallback paths all return 0.40
"""

from __future__ import annotations

import statistics
from typing import Any

# ── resolve_p_win_from_brains ──────────────────────────────────────────────


def resolve_p_win_from_brains(
    brains: list[Any],
    pnl_store: Any | None,
    direction: str = "long",
) -> float:
    """Resolve dynamic p_win for a strategy that does NOT use MetaFilter.

    Uses rolling 100-trade win rate from BrainPnLStore (FIX-20260526-032:
    window=100 explicitly passed to avoid all-time aggregation bias).
    Requires at least 10 settled trades before trusting the win rate.

    Falls back to 0.40 (Fail-Closed — FIX-20260526-031) when data is
    insufficient.  With min_p_win=0.45 (statarb) or 0.50 (barrier),
    0.40 < both → trades rejected when system lacks evidence.

    Trap 3 fix: Static historical win rate is a fixed multiplier in disguise.
    Rolling PnL win rate is dynamic and reflects current model performance.
    Alpha decays, regimes shift — all-time WR drags stale history into today.

    Args:
        brains: List of brain info dicts with "brain_id" key.
        pnl_store: BrainPnLStore instance or None.
        direction: "long" or "short" — for directional win rate lookup.
    """
    if pnl_store is None:
        return 0.40

    valid_rates: list[float] = []
    for b in brains:
        brain_id = b.get("brain_id") if isinstance(b, dict) else getattr(b, "brain_id", None)
        if not brain_id:
            continue
        try:
            m = pnl_store.get_metrics(str(brain_id), window=100)
        except Exception:  # noqa: BLE001
            continue
        if m is None:
            continue
        sc = getattr(m, "sample_count", 0)
        if sc < 10:
            continue
        wr = getattr(m, "win_rate", 0.0)
        if wr > 0:
            valid_rates.append(float(wr))

    if valid_rates:
        return float(statistics.median(valid_rates))

    return 0.40


# ── _adjust_p_win_for_regime ────────────────────────────────────────────────


def adjust_p_win_for_regime(
    p_win: float,
    strategy_name: str,
    regime_info: dict[str, object] | None,
    entry_z_score: float | None,
    trade_direction: str = "neutral",
) -> float:
    """Dynamically adjust p_win for OU strategies based on trend strength.

    FIX-20260526-030: Historical p_win (rolling 100-trade WR ~0.49) is a static
    average applied uniformly to all trades.  In trending regimes, high |z_score|
    means momentum ignition (price is trending away from mean), NOT a mean-reversion
    setup.  The model's "confidence" (z_score depth) is actually ANTI-informative
    in trends — the more confident the brain, the worse the outcome.

    FIX-20260526-035: Direction-aware asymmetric penalty.  With-trend pullbacks
    ("千金难买牛回头") are the highest-quality OU setups — the trend is your
    friend, not a risk factor.  Counter-trend signals receive the full penalty.

    This function inversely maps z_score → p_win discount when ADX indicates
    trending conditions.  Hard floor at 65% of original p_win prevents the
    adjustment from ever being the sole veto (that's the p_win gate's job).

    Non-OU strategies and non-trending regimes pass through unchanged.
    """
    if "statarb" not in strategy_name or not regime_info or entry_z_score is None:
        return p_win

    _rg: dict[str, Any] = {}
    if isinstance(regime_info, dict):
        _maybe_rg = regime_info.get("regime_gate")
        if isinstance(_maybe_rg, dict):
            _rg = _maybe_rg

    _h1_adx = float(_rg.get("h1_adx") or 0.0)

    if _h1_adx < 15.0:
        return p_win

    # ── Direction-aware bypass: with-trend pullbacks are Alpha, not risk ──
    _primary_dir = str(_rg.get("primary_trend") or "neutral")
    _h1_dir = str(_rg.get("h1_trend_direction") or "neutral")
    _ref_dir = _primary_dir if _primary_dir != "neutral" else _h1_dir

    if trade_direction != "neutral" and _ref_dir != "neutral":
        if trade_direction == _ref_dir:
            return p_win  # 千金难买牛回头 — no penalty for with-trend pullbacks

    abs_z = abs(entry_z_score)
    if abs_z < 0.8:
        return p_win

    # ── Penalty: higher |z_score| → stronger penalty ──
    _penalty = min((abs_z - 0.8) * 0.35, 0.40)
    _adjusted = p_win * (1.0 - _penalty)
    return max(_adjusted, p_win * 0.65)
