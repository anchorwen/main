"""BrainAttributionService — attribute realized P&L to individual brains.

Reads the live trade journal, joins open→close pairs by message_id,
and allocates realized P&L to the brain_ids recorded on each open entry.

Produces per-brain metrics: trade count, total P&L, win rate, avg return,
and a multi-layer attribution report suitable for daily recap and dashboard.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class BrainAttribution:
    """Realized P&L breakdown for a single brain."""

    brain_id: str = ""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    avg_pnl_per_trade: float = 0.0
    win_rate: float = 0.0
    label_distribution: dict[str, int] = field(default_factory=dict)
    sponsor_count: int = 0
    dissenter_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "brain_id": self.brain_id,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "total_pnl": round(self.total_pnl, 2),
            "avg_pnl_per_trade": round(self.avg_pnl_per_trade, 4),
            "win_rate": round(self.win_rate, 4),
            "label_distribution": self.label_distribution,
            "sponsor_count": self.sponsor_count,
            "dissenter_count": self.dissenter_count,
        }


@dataclass
class AttributionReport:
    """Multi-layer attribution report."""

    layer_1_counterfactual: dict[str, Any] = field(default_factory=dict)
    layer_2_attributed: list[BrainAttribution] = field(default_factory=list)
    layer_3_realized: dict[str, Any] = field(default_factory=dict)
    total_labeled_trades: int = 0
    total_realized_pnl: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer_1_counterfactual": self.layer_1_counterfactual,
            "layer_2_attributed": [a.to_dict() for a in self.layer_2_attributed],
            "layer_3_realized": self.layer_3_realized,
            "total_labeled_trades": self.total_labeled_trades,
            "total_realized_pnl": round(self.total_realized_pnl, 2),
        }


class BrainAttributionService:
    """Attribute realized P&L from journal trades to individual brains."""

    def __init__(self, journal_path: Path, pnl_ledger_path: Path | None = None):
        self._journal_path = journal_path
        self._pnl_ledger_path = pnl_ledger_path

    def build_report(self) -> AttributionReport:
        """Produce a multi-layer attribution report."""
        report = AttributionReport()

        # Layer 1: Counterfactual P&L from BrainPnLStore
        self._load_counterfactual(report)

        # Layer 2: Attribution — which brain_ids are on each trade
        self._attribute_trades(report)

        # Layer 3: Realized summary
        self._compute_realized(report)

        return report

    @staticmethod
    def _effective_pnl(entry: dict[str, Any]) -> float:
        """Extract realized P&L, falling back to detail.pnl."""
        pnl = entry.get("pnl")
        if pnl is not None:
            return float(pnl)
        detail = entry.get("detail", {})
        if isinstance(detail, dict):
            detail_pnl = detail.get("pnl")
            if detail_pnl is not None:
                return float(detail_pnl)
        return 0.0

    def _load_counterfactual(self, report: AttributionReport) -> None:
        if not self._pnl_ledger_path or not self._pnl_ledger_path.exists():
            return
        try:
            pnl = json.loads(self._pnl_ledger_path.read_text(encoding="utf-8"))
            cf: dict[str, Any] = {}
            for bid, outcomes in pnl.get("settled", {}).items():
                wins = sum(1 for o in outcomes if o.get("pnl_per_unit", 0.0) > 0)
                total = len(outcomes)
                total_pnl = sum(o.get("pnl_per_unit", 0.0) for o in outcomes)
                cf[bid] = {
                    "signals": total,
                    "winning_signals": wins,
                    "total_pnl": round(total_pnl, 2),
                    "win_rate": round(wins / total, 4) if total > 0 else 0.0,
                }
            report.layer_1_counterfactual = cf
        except Exception:  # BLE001:REVIEWED
            pass

    def _attribute_trades(self, report: AttributionReport) -> None:
        if not self._journal_path.exists():
            return

        entries = self._load_journal()
        if not entries:
            return

        # Index close entries with real labels
        labeled_closes: list[dict[str, Any]] = []
        for e in entries:
            if (
                e.get("action") == "close"
                and e.get("label")
                and not str(e.get("label", "")).startswith("auto_orphan")
            ):
                labeled_closes.append(e)

        # Index open entries by message_id for brain_ids/votes lookup
        opens_by_id: dict[str, dict[str, Any]] = {}
        for e in entries:
            if e.get("action") == "open":
                mid = e.get("message_id", "")
                if mid:
                    opens_by_id[mid] = e

        # Attribute each labeled close to brain_ids (Track 3: confidence-weighted)
        brain_pnl: dict[str, list[float]] = defaultdict(list)
        brain_labels: dict[str, Counter] = defaultdict(Counter)
        # Track sponsor/dissenter breakdown for each brain
        brain_sponsor: dict[str, list[float]] = defaultdict(list)
        brain_dissenter: dict[str, list[float]] = defaultdict(list)

        for close in labeled_closes:
            open_msg_id = close.get("open_message_id", "")
            pnl_val = self._effective_pnl(close)
            label = close.get("label", "unknown")
            trade_side = str(close.get("side", "") or "").lower()

            # Resolve brain_ids and brain_votes from close or linked open
            open_entry = opens_by_id.get(open_msg_id, {})
            brain_ids = close.get("brain_ids") or open_entry.get("brain_ids") or []
            _cv = close.get("brain_votes")
            _ov = open_entry.get("brain_votes")
            brain_votes: list[dict[str, Any]] = (
                _cv if _cv is not None else (_ov if _ov is not None else [])
            )

            if not brain_ids:
                brain_pnl["_unknown_"].append(pnl_val)
                brain_labels["_unknown_"][label] += 1
                continue

            # If no trade_side, infer from open entry
            if not trade_side:
                trade_side = str(open_entry.get("side", "") or "").lower()

            # Track 3: Confidence-Weighted Marginal Attribution
            # Separate brains into sponsors (voted WITH trade direction) and
            # dissenters (voted AGAINST). Only sponsors bear realized P&L.
            if brain_votes and trade_side:
                sponsors, dissenters = self._split_sponsors_dissenters(brain_votes, trade_side)
                if sponsors:
                    # Weight sponsors by confidence
                    total_conf = sum(max(0.01, s.get("confidence", 0.5)) for s in sponsors)
                    for s in sponsors:
                        bid = s["brain_id"]
                        weight = max(0.01, s.get("confidence", 0.5)) / total_conf
                        weighted_pnl = pnl_val * weight
                        brain_pnl[bid].append(weighted_pnl)
                        brain_labels[bid][label] += 1
                        brain_sponsor[bid].append(weighted_pnl)
                    # Dissenters get a record but no P&L — their vote was
                    # against the trade, so they shouldn't bear its outcome
                    for d in dissenters:
                        brain_labels[d["brain_id"]][f"dissented_{label}"] += 1
                        brain_dissenter[d["brain_id"]].append(0.0)
                    # Any brain_ids not in brain_votes (legacy) get even split
                    voted_ids = {s["brain_id"] for s in sponsors} | {
                        d["brain_id"] for d in dissenters
                    }
                    unvoted_ids = [bid for bid in brain_ids if bid not in voted_ids]
                    if unvoted_ids:
                        fallback_split = pnl_val / len(unvoted_ids)
                        for bid in unvoted_ids:
                            brain_pnl[bid].append(fallback_split)
                            brain_labels[bid][label] += 1
                else:
                    # No sponsors (all dissenters or unknown) — even split fallback
                    split_pnl = pnl_val / len(brain_ids)
                    for bid in brain_ids:
                        brain_pnl[bid].append(split_pnl)
                        brain_labels[bid][label] += 1
            else:
                # Legacy path: no brain_votes data — even split
                split_pnl = pnl_val / len(brain_ids)
                for bid in brain_ids:
                    brain_pnl[bid].append(split_pnl)
                    brain_labels[bid][label] += 1

        # Build per-brain attributions
        attributions = []
        for bid, pnls in sorted(brain_pnl.items()):
            wins = sum(1 for p in pnls if p > 0)
            losses = sum(1 for p in pnls if p < 0)
            total = len(pnls)
            total_p = sum(pnls)
            n_sponsor = len(brain_sponsor.get(bid, []))
            n_dissenter = len(brain_dissenter.get(bid, []))
            attr = BrainAttribution(
                brain_id=bid,
                total_trades=total,
                winning_trades=wins,
                losing_trades=losses,
                total_pnl=total_p,
                avg_pnl_per_trade=total_p / total if total > 0 else 0.0,
                win_rate=wins / total if total > 0 else 0.0,
                label_distribution=dict(brain_labels.get(bid, Counter())),
                sponsor_count=n_sponsor,
                dissenter_count=n_dissenter,
            )
            if n_sponsor or n_dissenter:
                attr.label_distribution["_sponsor_trades"] = n_sponsor
                attr.label_distribution["_dissenter_trades"] = n_dissenter
            attributions.append(attr)

        report.layer_2_attributed = attributions

    @staticmethod
    def _split_sponsors_dissenters(
        brain_votes: list[dict[str, Any]],
        trade_side: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Separate brain votes into sponsors (voted with trade) and dissenters (against).

        A brain is a sponsor if its direction_bias matches the trade_side.
        A brain with direction_bias='neutral' is neither — it gets dropped.
        """
        trade_dir = trade_side.lower()
        sponsors: list[dict[str, Any]] = []
        dissenters: list[dict[str, Any]] = []
        for v in brain_votes:
            vb_dir = str(v.get("direction_bias", "") or v.get("direction", "")).lower()
            if vb_dir == trade_dir:
                sponsors.append(v)
            elif vb_dir in ("long", "short"):
                dissenters.append(v)
            # Neutral votes are intentionally excluded from both sponsors and
            # dissenters: a neutral brain abstains rather than contradicting
            # or endorsing, so its P&L attribution is neither rewarded nor
            # penalized for this trade.
        return sponsors, dissenters

    def _compute_realized(self, report: AttributionReport) -> None:
        total = sum(a.total_pnl for a in report.layer_2_attributed)
        trade_count = sum(a.total_trades for a in report.layer_2_attributed)
        report.total_labeled_trades = trade_count
        report.total_realized_pnl = total

        # Per-brain summary for layer 3
        summary: dict[str, Any] = {}
        for a in report.layer_2_attributed:
            summary[a.brain_id] = {
                "trades": a.total_trades,
                "total_pnl": round(a.total_pnl, 2),
                "win_rate": round(a.win_rate, 4),
            }
        report.layer_3_realized = {
            "by_brain": summary,
            "unattributed_trades": (
                next(
                    (
                        a.total_trades
                        for a in report.layer_2_attributed
                        if a.brain_id == "_unknown_"
                    ),
                    0,
                )
            ),
        }

    def _load_journal(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        try:
            for line in self._journal_path.read_text(encoding="utf-8").strip().split("\n"):
                if line:
                    entries.append(json.loads(line))
        except (json.JSONDecodeError, OSError):
            pass
        return entries

    def quick_summary(self) -> dict[str, Any]:
        """Return a compact per-brain P&L summary suitable for status/dashboard."""
        report = self.build_report()
        brains = {}
        for a in report.layer_2_attributed:
            if a.brain_id == "_unknown_":
                continue
            brains[a.brain_id] = f"{a.total_pnl:+.2f} ({a.total_trades}t, {a.win_rate:.0%} wr)"
        return {
            "brains": brains,
            "total_labeled_trades": report.total_labeled_trades,
            "total_realized_pnl": round(report.total_realized_pnl, 2),
            "unattributed_trades": report.layer_3_realized.get("unattributed_trades", 0),
        }
