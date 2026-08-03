"""Breakeven analysis — the mathematical minimum win rate a model must beat.

Phase 3 / M2 (FIX-20260803-004, 战役三 — 自动 OOS / 盈亏平衡门槛 / IC 最高批准):

    Institutional mandate: a model whose expected win rate cannot cover the
    PHYSICAL wear of spread & slippage is hard-vetoed at training time.  The
    gate lives in ``check_quality_gates()`` / ``oos_blind_test.py`` and raises
    ``ModelQualityException`` before any registry write or brain config is
    produced.  No human may waive it.

    Friction accounting is aligned with ``core.contracts.training.label_contract``
    (MT5-native: cost_in_price = points * tick_size):

      barrier convention (conservative / physical-wear):
        win leg  = TP_dist - spread_cost   (TP must clear the full spread)
        loss leg = SL_dist + slippage_cost (slippage makes the SL worse)
        RR = (TP_dist - spread_cost) / (SL_dist + slippage_cost)

      expected_r convention (FIX-20260803-003 canon):
        costs are baked into the entry (open ± half_spread + slippage), so the
        realized R-multiple ratio is RR = tp_atr_mult / sl_atr_mult.  The gate
        still reports the barrier-convention RR alongside so a physical-wear
        verdict can be issued when requested.

    Breakeven win rate (Klein / standard):  WR * RR = (1 - WR)  ⇒  WR = 1/(1+RR).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# This module must remain dependency-free (no numpy, no pandas) so it can be
# imported by the training gate and the blind-test CLI without pulling the
# feature stack.  ``Any`` is used for the LabelContract duck-type.
from typing import Any


@dataclass(frozen=True)
class BreakevenResult:
    """The RR and breakeven win rate for a label contract / friction model."""

    rr: float
    breakeven_win_rate: float
    sl_dist: float
    tp_dist: float
    spread_cost: float
    slippage_cost: float
    friction_model: str  # "barrier_net_spread_gross_slippage" | "expected_r_entry_costed"

    def to_dict(self) -> dict[str, float | str]:
        return {
            "rr": round(self.rr, 6),
            "breakeven_win_rate": round(self.breakeven_win_rate, 6),
            "sl_dist": round(self.sl_dist, 6),
            "tp_dist": round(self.tp_dist, 6),
            "spread_cost": round(self.spread_cost, 6),
            "slippage_cost": round(self.slippage_cost, 6),
            "friction_model": self.friction_model,
        }


def compute_friction_costs(
    spread_points: float,
    slippage_points: float,
    tick_size: float,
) -> tuple[float, float]:
    """MT5-native cost = points * tick_size (matches label_contract physics).

    Returns (spread_cost, slippage_cost) in price units.
    """
    if tick_size <= 0:
        raise ValueError(f"tick_size must be > 0, got {tick_size}")
    if spread_points < 0 or slippage_points < 0:
        raise ValueError(
            f"friction points must be >= 0 (spread={spread_points}, slippage={slippage_points})"
        )
    return spread_points * tick_size, slippage_points * tick_size


def compute_rr(
    sl_dist: float,
    tp_dist: float,
    *,
    spread_cost: float = 0.0,
    slippage_cost: float = 0.0,
) -> float:
    """Friction-adjusted reward:risk (barrier convention).

    - Win leg: TP distance net of spread (TP must clear the spread).
    - Loss leg: SL distance gross of slippage (slippage makes SL worse).

    Raises ``ValueError`` when friction consumes the entire win distance or
    the loss leg is non-positive — such a trade is untradeable and the
    breakeven win rate is > 100% (cannot be covered).
    """
    effective_win = tp_dist - spread_cost
    effective_loss = sl_dist + slippage_cost
    if effective_win <= 0 or effective_loss <= 0:
        raise ValueError(
            f"Untradeable friction: win leg {effective_win:.6f} (TP {tp_dist} - "
            f"spread {spread_cost}) or loss leg {effective_loss:.6f} "
            f"(SL {sl_dist} + slippage {slippage_cost}) <= 0. "
            f"Spread/slippage exceeds the reward structure."
        )
    return effective_win / effective_loss


def compute_breakeven(rr: float) -> float:
    """Breakeven win rate from reward:risk.  WR * RR = (1-WR) ⇒ WR = 1/(1+RR)."""
    if not math.isfinite(rr) or rr <= 0:
        raise ValueError(f"RR must be a positive finite number, got {rr!r}")
    return 1.0 / (1.0 + rr)


def compute_breakeven_from_params(
    sl_atr_mult: float,
    tp_atr_mult: float,
    *,
    spread_points: float,
    slippage_points: float,
    tick_size: float,
    friction_model: str = "barrier",
) -> BreakevenResult:
    """Derive RR + breakeven win rate from raw ATR multiples + friction.

    ATR multiples are the barrier distances at ATR = 1.0 (unitless); friction
    costs are in the same price units as the ATR-scaled distances, so the ratio
    is unit-consistent.

    ``friction_model`` selects the accounting convention:
      - ``"barrier"`` (default, conservative physical-wear): win = TP − spread,
        loss = SL + slippage.
      - ``"expected_r"`` (FIX-20260803-003 canon): entry already carries
        half-spread + slippage → realized R ratio RR = tp/sl.
    """
    if sl_atr_mult <= 0 or tp_atr_mult <= 0:
        raise ValueError(f"ATR multiples must be positive (sl={sl_atr_mult}, tp={tp_atr_mult})")
    spread_cost, slippage_cost = compute_friction_costs(spread_points, slippage_points, tick_size)

    if friction_model == "expected_r":
        # FIX-20260803-003 canon: entry = open ± (half_spread + slippage).
        # Win = +tp_dist, loss = -1.0R → RR = tp/sl.
        rr = tp_atr_mult / sl_atr_mult
        friction_model_label = "expected_r_entry_costed"
    elif friction_model == "barrier":
        # barrier convention — the conservative physical-wear model.
        rr = compute_rr(
            sl_atr_mult,
            tp_atr_mult,
            spread_cost=spread_cost,
            slippage_cost=slippage_cost,
        )
        friction_model_label = "barrier_net_spread_gross_slippage"
    else:
        raise ValueError(
            f"Unknown friction_model '{friction_model}'. " f"Use 'barrier' or 'expected_r'."
        )

    return BreakevenResult(
        rr=rr,
        breakeven_win_rate=compute_breakeven(rr),
        sl_dist=sl_atr_mult,
        tp_dist=tp_atr_mult,
        spread_cost=spread_cost,
        slippage_cost=slippage_cost,
        friction_model=friction_model_label,
    )


def breakeven_from_contract(contract: Any) -> BreakevenResult:
    """Derive RR + breakeven win rate from a LabelContract (ATR-normalized).

    Delegates to :func:`compute_breakeven_from_params` — the friction_model is
    chosen by the contract's ``type`` (``expected_r`` vs everything else).
    """
    from core.contracts.training.label_contract import LabelContract

    if not isinstance(contract, LabelContract):
        raise TypeError(f"Expected LabelContract, got {type(contract).__name__}")

    friction_model = "expected_r" if contract.type == "expected_r" else "barrier"
    return compute_breakeven_from_params(
        contract.sl_atr_mult,
        contract.tp_atr_mult,
        spread_points=contract.spread_points,
        slippage_points=contract.slippage_points,
        tick_size=contract.tick_size,
        friction_model=friction_model,
    )
