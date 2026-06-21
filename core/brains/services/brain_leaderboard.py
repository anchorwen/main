"""Brain leaderboard — unified ranking across all brains.

Combines PnL metrics (Sharpe, win rate, profit factor) with governance
status and vote weight to produce a single composite score per brain.

Usage (XAU)::

    pnl_store = BrainPnLStore.load("data/brain_pnl_ledger.json")
    governance = GovernanceService.load("data/governance_state.json")

Usage (BTC)::

    pnl_store = BrainPnLStore.load("data_btc/brain_pnl_ledger.json")
    governance = GovernanceService.load("data_btc/governance_state.json")

    leaderboard = BrainLeaderboard()
    rankings = leaderboard.rank(pnl_store.get_all_metrics(), governance.get_all_states())
    for r in rankings:
        print(f"{r.rank}. {r.brain_id} — score={r.score:.1f} Sharpe={r.sharpe:.1f}")
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from core.runtime.fault_handler import fail_open_guard


@dataclass
class BrainRanking:
    """Single brain's leaderboard entry."""

    rank: int
    brain_id: str
    score: float  # composite score 0-100
    sharpe: float
    win_rate: float
    profit_factor: float
    cum_pnl: float
    max_drawdown: float
    trade_count: int
    health_signal: str
    governance_status: str
    vote_weight: float
    recommendation: str  # strong_buy / hold / reduce / retire
    # Friction costs
    total_spread_cost: float = 0.0
    total_slippage_cost: float = 0.0
    # Directional breakdown
    long_win_rate: float = 0.0
    short_win_rate: float = 0.0
    long_count: int = 0
    short_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "brain_id": self.brain_id,
            "score": self.score,
            "sharpe": self.sharpe,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "cum_pnl": self.cum_pnl,
            "max_drawdown": self.max_drawdown,
            "trade_count": self.trade_count,
            "health_signal": self.health_signal,
            "governance_status": self.governance_status,
            "vote_weight": self.vote_weight,
            "recommendation": self.recommendation,
            "total_spread_cost": self.total_spread_cost,
            "total_slippage_cost": self.total_slippage_cost,
            "long_win_rate": self.long_win_rate,
            "short_win_rate": self.short_win_rate,
            "long_count": self.long_count,
            "short_count": self.short_count,
        }


class BrainLeaderboard:
    """Compute composite rankings across all brains.

    When constructed with a BrainQualityEngine, delegates all quality assessment
    to the engine (single source of truth).  Falls back to legacy static methods
    when no engine is provided (backward compat).

    Score formula (0-100 scale):
      score = 40 * tanh(sharpe / 3.0)                # Sharpe contribution
            + 25 * clamp((wr - 0.35) / 0.55, 0, 1)    # win rate (35% baseline)
            + 15 * min(pf / 3.0, 1.0)                  # profit factor (capped at 3.0)
            + 10 * tanh(cum_pnl / 50.0)                # cumulative PnL
            + 10 * (1.0 - dd_ratio)                    # drawdown penalty
    """

    def __init__(self, quality_engine: Any | None = None) -> None:
        self._engine = quality_engine
        # Auto-wire singleton when no engine provided (production path)
        if self._engine is None:
            try:
                from core.feedback.brain_quality_engine import BrainQualityEngine

                self._engine = BrainQualityEngine.instance()
            except Exception:  # BLE001:FOG
                with fail_open_guard("brain_leaderboard:__init__"):
                    pass
    # ── DQAF-20260621-042: Required fields for schema validation ──
    _REQUIRED_METRIC_FIELDS: tuple[str, ...] = (
        "sharpe_ratio", "win_rate", "profit_factor",
        "cumulative_pnl", "max_drawdown",
    )
    _REQUIRED_COUNT_FIELDS: tuple[str, ...] = ("sample_count",)

    def _validate_metrics(
        self, metrics_map: dict[str, Any]
    ) -> dict[str, Any]:
        """Schema-validate all brain metrics before ranking.

        Raises DataIntegrityError if any brain is missing required fields.
        This is the POISON PILL: bad data → crash, don't produce broken output.
        """
        for brain_id, m in metrics_map.items():
            if isinstance(m, dict):
                for field in self._REQUIRED_METRIC_FIELDS:
                    if field not in m or m[field] is None:
                        from core.contracts.exceptions import DataIntegrityError
                        raise DataIntegrityError(
                            f"Brain '{brain_id}' missing required metric field "
                            f"'{field}' in leaderboard input. "
                            f"Available fields: {sorted(m.keys())}. "
                            f"Source: live_journal_metrics or BrainPnLStore. "
                            f"Fix: update the source to populate this field."
                        )
                for field in self._REQUIRED_COUNT_FIELDS:
                    if field not in m or m[field] is None:
                        from core.contracts.exceptions import DataIntegrityError
                        raise DataIntegrityError(
                            f"Brain '{brain_id}' missing required count field "
                            f"'{field}' in leaderboard input."
                        )
            # Object types (BrainPnLMetrics) are trusted — they carry their
            # own schema enforcement via Pydantic/dataclass.
        return metrics_map

    @staticmethod
    def _normalize_metrics_map(
        raw: dict[str, Any] | list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Accept both dict and list, normalize to dict keyed by brain_id.

        DQAF-20260621-042: daily_ops was passing list(metrics.values())
        instead of the dict itself. This normalizer provides backward
        compatibility while logging the caller error for repair.
        """
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, list):
            # Rebuild dict from list items
            result: dict[str, Any] = {}
            for item in raw:
                if isinstance(item, dict):
                    bid = item.get("brain_id", "")
                elif hasattr(item, "brain_id"):
                    bid = item.brain_id
                else:
                    continue
                if bid:
                    result[bid] = item
            return result
        return {}

    def rank(
        self,
        metrics_map: dict[str, Any] | list[dict[str, Any]],
        governance_states: dict[str, dict[str, Any]] | None = None,
        vote_weights: dict[str, float] | None = None,
    ) -> list[BrainRanking]:
        """Produce ranked leaderboard from PnL metrics and governance state.

        Args:
            metrics_map: {brain_id: BrainPnLMetrics} or list of metrics dicts.
            governance_states: {brain_id: state_dict} from GovernanceService.
            vote_weights: {brain_id: weight} from DynamicBrainWeighter (optional).

        Returns:
            List of BrainRanking sorted by score descending.

        Raises:
            DataIntegrityError: if required metric fields are missing (POISON PILL).
        """
        if governance_states is None:
            governance_states = {}
        if vote_weights is None:
            vote_weights = {}

        # ── DQAF-20260621-042: Normalize input (accept both dict and list) ──
        metrics_map = self._normalize_metrics_map(metrics_map)

        # ── DQAF-20260621-042: POISON PILL — validate before ranking ──
        if metrics_map:
            self._validate_metrics(metrics_map)

        # FIX-20260610-007: Equal-weight fallback — prevent global
        # vote_weight=0 collapse when caller doesn't pass weights.
        # All-zero weights (or empty dict) → every brain gets 1/N.
        if not vote_weights or all(v == 0.0 for v in vote_weights.values()):
            n = max(len(metrics_map), 1)
            vote_weights = {bid: 1.0 / n for bid in metrics_map}

        rankings: list[BrainRanking] = []
        for brain_id, m in metrics_map.items():
            # Extract fields — support both BrainPnLMetrics objects and dicts
            if hasattr(m, "sharpe_ratio"):
                sharpe = float(m.sharpe_ratio)
                win_rate = float(m.win_rate)
                profit_factor = float(m.profit_factor)
                cum_pnl = float(m.cumulative_pnl)
                max_dd = float(m.max_drawdown)
                trade_count = int(m.sample_count)
                long_wr = float(m.long_win_rate)
                short_wr = float(m.short_win_rate)
                long_n = int(m.long_count)
                short_n = int(m.short_count)
                spread_cost = float(getattr(m, "total_spread_cost", 0) or 0)
                slippage_cost = float(getattr(m, "total_slippage_cost", 0) or 0)
            else:
                sharpe = float(m.get("sharpe_ratio", 0))
                win_rate = float(m.get("win_rate", 0))
                profit_factor = float(m.get("profit_factor", 0))
                cum_pnl = float(m.get("cumulative_pnl", 0))
                max_dd = float(m.get("max_drawdown", 0))
                trade_count = int(m.get("sample_count", 0))
                long_wr = float(m.get("long_win_rate", 0))
                short_wr = float(m.get("short_win_rate", 0))
                long_n = int(m.get("long_count", 0))
                short_n = int(m.get("short_count", 0))
                spread_cost = float(m.get("total_spread_cost", 0) or 0)
                slippage_cost = float(m.get("total_slippage_cost", 0) or 0)

            gov_state = governance_states.get(brain_id, {})
            governance_status = gov_state.get("status", "candidate") if gov_state else "candidate"
            vote_weight = float(vote_weights.get(brain_id, 0.0))

            if self._engine is not None:
                # ── Single source of truth: BrainQualityEngine ──
                verdict = self._engine.assess(brain_id, m, governance_status=governance_status)
                score = verdict.score
                recommendation = verdict.governance_rec
                health_signal = verdict.quality_tier
            else:
                # Legacy path (kept for backward compat)
                score = self._compute_score(sharpe, win_rate, profit_factor, cum_pnl, max_dd)
                recommendation = self._recommend(score, sharpe, trade_count, governance_status)
                health_signal = self._health_label(sharpe, win_rate)

            rankings.append(
                BrainRanking(
                    rank=0,  # filled after sort
                    brain_id=brain_id,
                    score=round(score, 1),
                    sharpe=round(sharpe, 2),
                    win_rate=round(win_rate, 4),
                    profit_factor=round(profit_factor, 2)
                    if profit_factor != float("inf")
                    else 999.0,
                    cum_pnl=round(cum_pnl, 4),
                    max_drawdown=round(max_dd, 4),
                    trade_count=trade_count,
                    health_signal=health_signal,
                    governance_status=governance_status,
                    vote_weight=round(vote_weight, 4),
                    recommendation=recommendation,
                    total_spread_cost=round(spread_cost, 4),
                    total_slippage_cost=round(slippage_cost, 4),
                    long_win_rate=round(long_wr, 4),
                    short_win_rate=round(short_wr, 4),
                    long_count=long_n,
                    short_count=short_n,
                )
            )

        # Sort by score descending and assign ranks
        rankings.sort(key=lambda r: r.score, reverse=True)
        for i, r in enumerate(rankings):
            r.rank = i + 1

        return rankings

    @staticmethod
    def _compute_score(
        sharpe: float,
        win_rate: float,
        profit_factor: float,
        cum_pnl: float,
        max_dd: float,
    ) -> float:
        """Composite score on 0-100 scale."""
        # Sharpe: core signal (40% weight)
        sharpe_component = 40.0 * math.tanh(sharpe / 3.0)

        # Win rate: baseline at 40% (25% weight)
        wr_component = 25.0 * max(0.0, (win_rate - 0.40) * 2.0)

        # Profit factor: cap at 3.0 (15% weight)
        pf = profit_factor if profit_factor != float("inf") else 3.0
        pf_component = 15.0 * min(pf / 3.0, 1.0)

        # Cumulative PnL (10% weight)
        pnl_component = 10.0 * math.tanh(cum_pnl / 50.0)

        # Drawdown penalty (10% weight): lower dd → higher score
        dd_ratio = min(max_dd / max(abs(cum_pnl) + 0.01, 1.0), 1.0) if cum_pnl > 0 else 1.0
        dd_component = 10.0 * (1.0 - dd_ratio)

        return max(
            0.0, sharpe_component + wr_component + pf_component + pnl_component + dd_component
        )

    @staticmethod
    def _health_label(sharpe: float, win_rate: float) -> str:
        if sharpe >= 1.0 and win_rate >= 0.55:
            return "healthy"
        if sharpe >= 0.0 and win_rate >= 0.48:
            return "stable"
        if sharpe >= -1.0 and win_rate >= 0.40:
            return "degraded"
        if sharpe >= -2.0 and win_rate >= 0.30:
            return "warning"
        return "critical"

    @staticmethod
    def _recommend(score: float, sharpe: float, trade_count: int, governance_status: str) -> str:
        """Produce actionable recommendation."""
        if trade_count < 5:
            return "insufficient_data"
        if governance_status == "retired":
            return "retired"
        if score >= 70:
            return "strong_buy"
        if score >= 50:
            return "buy"
        if score >= 35:
            return "hold"
        if score >= 20:
            return "reduce"
        return "retire"

    def format_table(self, rankings: list[BrainRanking], top_n: int = 20) -> str:
        """Format leaderboard as Markdown table."""
        lines = [
            "| # | Brain | Score | Sharpe | WR | PF | Cum PnL | Max DD | Trades | Status | Rec |",
            "|---|-------|-------|--------|----|----|---------|--------|--------|--------|-----|",
        ]
        for r in rankings[:top_n]:
            lines.append(
                f"| {r.rank} | {r.brain_id} | {r.score:.1f} | {r.sharpe:+.1f} | "
                f"{r.win_rate:.1%} | {r.profit_factor:.2f} | {r.cum_pnl:+.2f} | "
                f"{r.max_drawdown:.2f} | {r.trade_count} | {r.governance_status} | "
                f"{r.recommendation} |"
            )
        return "\n".join(lines)

    def to_records(self, rankings: list[BrainRanking]) -> list[dict[str, Any]]:
        """Convert to list-of-dicts for JSON serialization."""
        return [r.to_dict() for r in rankings]
