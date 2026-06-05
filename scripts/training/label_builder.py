"""Build training labels from trade journals or price history.

Two modes:
  1. Journal mode (default): Extracts P&L labels from live_trade_journal.jsonl.
     When --label-contract is provided, enriches labels with contract-based
     SL/TP classification instead of simple win/loss.
  2. Barrier mode (--price-data): Generates barrier labels directly from OHLC
     price history using a Label Contract. Walks forward through every bar,
     simulating entries and recording which barrier (TP/SL) is hit first.

Output schema: training_label.v1 — one JSONL record per label.

Usage:
  # Journal mode (backward compatible)
  python scripts/training/label_builder.py --journal data/live_trade_journal.jsonl

  # Journal mode + contract enrichment
  python scripts/training/label_builder.py --journal data/live_trade_journal.jsonl \\
      --label-contract blueprints/contracts/label-survival-barrier-1.0.0.json

  # Barrier mode: generate labels from price history
  python scripts/training/label_builder.py \\
      --price-data data/raw/XAUUSD_M5_2024.csv \\
      --label-contract blueprints/contracts/label-survival-barrier-1.0.0.json \\
      --output data/labels/barrier_labels.jsonl
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from core.contracts.training.label_contract import BarrierResult, LabelContract

SCHEMA_VERSION = "training_label.v1"


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ═══════════════════════════════════════════════════════════════════════
# Journal mode: trade journal → labels
# ═══════════════════════════════════════════════════════════════════════


def _read_journal_entries(
    journal_path: Path, *, date_filter: str | None = None
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not journal_path.exists():
        return entries
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if date_filter:
            recorded = str(rec.get("recorded_at", ""))
            if not recorded.startswith(date_filter):
                continue
        entries.append(rec)
    return entries


def _extract_entry_price(detail: dict[str, Any] | None) -> float | None:
    if not detail or not isinstance(detail, dict):
        return None
    # Open entries: price is in detail.request.price
    req = detail.get("request")
    if isinstance(req, dict):
        price = req.get("price")
        if price is not None:
            return float(price)
    # Fallback: detail.order might be a dict (legacy format)
    order = detail.get("order")
    if isinstance(order, dict):
        price = order.get("price") or order.get("price_open")
        if price is not None:
            return float(price)
    return None


def _extract_exit_price(detail: dict[str, Any] | None) -> float | None:
    if not detail or not isinstance(detail, dict):
        return None
    # Close entries: price is in detail.close_price
    close_price = detail.get("close_price")
    if close_price is not None:
        return float(close_price)
    # Fallback: detail.order might be a dict (legacy format)
    order = detail.get("order")
    if isinstance(order, dict):
        price = order.get("price") or order.get("price_close") or order.get("price_current")
        if price is not None:
            return float(price)
    return None


def _compute_pnl(
    side: str,
    entry_price: float | None,
    exit_price: float | None,
    volume: float | None,
) -> float | None:
    if entry_price is None or exit_price is None:
        return None
    pnl = exit_price - entry_price if side == "long" else entry_price - exit_price
    if volume is not None and volume > 0:
        pnl *= volume
    return round(pnl, 6)


def _classify_label(pnl: float | None) -> str:
    if pnl is None:
        return "unlabeled"
    if pnl > 0:
        return "win"
    if pnl < 0:
        return "loss"
    return "breakeven"


def _classify_barrier_label(
    side: str,
    entry_price: float | None,
    exit_price: float | None,
    sl_atr_mult: float,
    tp_atr_mult: float,
    atr: float,
) -> str:
    """Classify a trade as tp_hit_first, sl_hit_first, or timeout based on
    whether the exit price is closer to the contract-defined SL or TP level.
    """
    if entry_price is None or exit_price is None or atr <= 0:
        return "unlabeled"

    sl_dist = sl_atr_mult * atr
    tp_dist = tp_atr_mult * atr

    if side == "long":
        sl_price = entry_price - sl_dist
        tp_price = entry_price + tp_dist
    else:
        sl_price = entry_price + sl_dist
        tp_price = entry_price - tp_dist

    # Check if exit is within 20% of SL or TP distance from the respective level
    sl_tolerance = sl_dist * 0.2
    tp_tolerance = tp_dist * 0.2

    dist_to_sl = abs(exit_price - sl_price)
    dist_to_tp = abs(exit_price - tp_price)

    if dist_to_sl <= sl_tolerance and dist_to_sl < dist_to_tp:
        return "sl_hit_first"
    if dist_to_tp <= tp_tolerance:
        return "tp_hit_first"
    return "timeout"


def build_trade_records(
    journal_path: Path,
    *,
    date_filter: str | None = None,
    contract: LabelContract | None = None,
) -> list[dict[str, Any]]:
    """Build trade records from journal entries by matching open/close pairs.

    When a LabelContract is provided, labels are classified using contract-defined
    SL/TP levels instead of simple P&L-based win/loss.
    """
    entries = _read_journal_entries(journal_path, date_filter=date_filter)
    if not entries:
        return []

    by_ticket: dict[int, list[dict[str, Any]]] = {}
    unlinked: list[dict[str, Any]] = []

    for rec in entries:
        ticket = rec.get("position_ticket")
        if ticket is not None and isinstance(ticket, int) and ticket > 0:
            by_ticket.setdefault(ticket, []).append(rec)
        else:
            unlinked.append(rec)

    trades: list[dict[str, Any]] = []

    for ticket, recs in by_ticket.items():
        opens = [r for r in recs if r.get("action") == "open"]
        closes = [r for r in recs if r.get("action") in ("close", "modify", "modify_sltp")]

        for i, open_rec in enumerate(opens):
            # FIX-20260601-046: prefer close with valid close_price.
            # Bridge close orders have detail={order,request,retcode} (no price).
            # Reconciliation confirmations have detail={close_price,reason}.
            # Taking closes[0] blindly picks the bridge order → pnl=None → unlabeled.
            close_rec = None
            for c in closes:
                if _extract_exit_price(c.get("detail")) is not None:
                    close_rec = c
                    break
            if close_rec is None and closes:
                close_rec = closes[0]  # fallback: best-effort

            side = str(open_rec.get("side", ""))
            entry_price = _extract_entry_price(open_rec.get("detail"))
            exit_price = _extract_exit_price(close_rec.get("detail")) if close_rec else None
            volume = open_rec.get("effective_volume_hint") or open_rec.get("volume")

            pnl = _compute_pnl(side, entry_price, exit_price, volume)

            # Determine label classification
            if contract is not None and entry_price is not None:
                # Use journal's ATR field if available, otherwise use training mean
                atr = open_rec.get("atr", 2.31)
                label = _classify_barrier_label(
                    side,
                    entry_price,
                    exit_price,
                    contract.sl_atr_mult,
                    contract.tp_atr_mult,
                    atr,
                )
            else:
                label = _classify_label(pnl)

            trade: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "label_id": f"label_ticket_{ticket}_{i}",
                "position_ticket": ticket,
                "symbol": open_rec.get("symbol", ""),
                "side": side,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl": pnl,
                "label": label,
                "volume": volume,
                "open_message_id": open_rec.get("message_id"),
                "open_recorded_at": open_rec.get("recorded_at"),
                "close_message_id": close_rec.get("message_id") if close_rec else None,
                "close_recorded_at": close_rec.get("recorded_at") if close_rec else None,
                "is_closed": close_rec is not None,
                "open_ack_status": open_rec.get("ack_status"),
                "sl": open_rec.get("sl"),
                "tp": open_rec.get("tp"),
            }

            # Enrich with contract metadata
            if contract is not None:
                trade["label_contract_id"] = contract.contract_id
                trade["sl_atr_mult"] = contract.sl_atr_mult
                trade["tp_atr_mult"] = contract.tp_atr_mult
                trade["horizon_bars"] = contract.horizon_bars

            trades.append(trade)

    # Add unlinked records as unlabeled
    for rec in unlinked:
        if rec.get("action") != "open":
            continue
        side = str(rec.get("side", ""))
        entry_price = _extract_entry_price(rec.get("detail"))
        volume = rec.get("effective_volume_hint") or rec.get("volume")

        unlinked_trade: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "label_id": f"label_unlinked_{rec.get('message_id', 'unknown')[:20]}",
            "position_ticket": None,
            "symbol": rec.get("symbol", ""),
            "side": side,
            "entry_price": entry_price,
            "exit_price": None,
            "pnl": None,
            "label": "unlabeled",
            "volume": volume,
            "open_message_id": rec.get("message_id"),
            "open_recorded_at": rec.get("recorded_at"),
            "close_message_id": None,
            "close_recorded_at": None,
            "is_closed": False,
            "open_ack_status": rec.get("ack_status"),
            "sl": rec.get("sl"),
            "tp": rec.get("tp"),
        }
        if contract is not None:
            unlinked_trade["label_contract_id"] = contract.contract_id
            unlinked_trade["sl_atr_mult"] = contract.sl_atr_mult
            unlinked_trade["tp_atr_mult"] = contract.tp_atr_mult
            unlinked_trade["horizon_bars"] = contract.horizon_bars
        trades.append(unlinked_trade)

    return trades


def build_basic_stats_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Build aggregate label statistics."""
    if not records:
        return {
            "schema_version": "label_stats.v1",
            "generated_at": _utc_now_iso(),
            "total_records": 0,
        }

    wins = sum(1 for r in records if r["label"] in ("win", "tp_hit_first"))
    losses = sum(1 for r in records if r["label"] in ("loss", "sl_hit_first"))
    timeouts = sum(1 for r in records if r["label"] == "timeout")
    breakeven = sum(1 for r in records if r["label"] == "breakeven")
    unlabeled = sum(1 for r in records if r["label"] == "unlabeled")
    closed = sum(1 for r in records if r.get("is_closed", True))
    pnls = [r["pnl"] for r in records if r["pnl"] is not None]

    return {
        "schema_version": "label_stats.v1",
        "generated_at": _utc_now_iso(),
        "total_records": len(records),
        "closed_trades": closed,
        "open_trades": len(records) - closed,
        "labels": {
            "win_or_tp": wins,
            "loss_or_sl": losses,
            "timeout": timeouts,
            "breakeven": breakeven,
            "unlabeled": unlabeled,
        },
        "pnl_summary": {
            "total_pnl": round(sum(pnls), 6) if pnls else 0.0,
            "avg_pnl": round(sum(pnls) / len(pnls), 6) if pnls else None,
            "max_pnl": round(max(pnls), 6) if pnls else None,
            "min_pnl": round(min(pnls), 6) if pnls else None,
        },
    }


# ═══════════════════════════════════════════════════════════════════════
# Barrier mode: price history → barrier labels
# ═══════════════════════════════════════════════════════════════════════


def _load_price_csv(
    csv_path: Path,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """Load OHLC data from a CSV file.

    Expects columns: time, open, high, low, close (or datetime, Open, High, Low, Close).
    Returns (opens, highs, lows, closes, timestamps) as numpy arrays.
    """
    import csv

    rows: list[dict[str, float]] = []
    timestamps: list[str] = []

    with open(csv_path, encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {csv_path}")

        # Detect column name variants
        col_map: dict[str, str] = {}
        for key in reader.fieldnames:
            kl = key.strip().lower()
            if kl in ("time", "datetime", "date", "timestamp"):
                col_map["time"] = key
            elif kl in ("open", "o"):
                col_map["open"] = key
            elif kl in ("high", "h"):
                col_map["high"] = key
            elif kl in ("low", "l"):
                col_map["low"] = key
            elif kl in ("close", "c"):
                col_map["close"] = key

        missing = {"time", "open", "high", "low", "close"} - set(col_map)
        if missing:
            raise ValueError(
                f"CSV missing required columns: {missing}. Found: {list(reader.fieldnames)}"
            )

        for row in reader:
            try:
                timestamps.append(str(row[col_map["time"]]))
                rows.append(
                    {
                        "open": float(row[col_map["open"]]),
                        "high": float(row[col_map["high"]]),
                        "low": float(row[col_map["low"]]),
                        "close": float(row[col_map["close"]]),
                    }
                )
            except (ValueError, KeyError):
                continue

    opens = np.array([r["open"] for r in rows], dtype=np.float64)
    highs = np.array([r["high"] for r in rows], dtype=np.float64)
    lows = np.array([r["low"] for r in rows], dtype=np.float64)
    closes = np.array([r["close"] for r in rows], dtype=np.float64)

    return opens, highs, lows, closes, timestamps


def generate_barrier_labels_from_prices(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    timestamps: list[str],
    contract: LabelContract,
    *,
    side: str = "long",
    min_atr: float = 0.5,
) -> list[dict[str, Any]]:
    """Generate barrier labels by walking forward through price history.

    For each bar (up to len - horizon_bars), computes ATR, sets SL/TP barriers,
    and walks forward to determine which barrier is hit first.

    Args:
        highs, lows, closes: OHLC arrays.
        timestamps: Parallel array of timestamp strings.
        contract: LabelContract defining barriers and horizon.
        side: Trade direction ("long" or "short").
        min_atr: Minimum ATR threshold to suppress entries in flat markets.

    Returns:
        List of label dicts in training_label.v1 format.
    """
    n = len(closes)
    horizon = contract.horizon_bars
    labels: list[dict[str, Any]] = []

    max_entry = n - horizon - 1
    if max_entry <= contract.atr_period:
        return labels

    for entry_idx in range(contract.atr_period, max_entry):
        result: BarrierResult = contract.build_barrier_labels(
            highs,
            lows,
            closes,
            entry_idx=entry_idx,
            side=side,
        )

        # Skip entries with zero or unreliable ATR
        if result.atr_at_entry < min_atr:
            continue

        label_int = (
            "1"
            if result.label == "tp_hit_first"
            else "-1"
            if result.label == "sl_hit_first"
            else "0"
        )

        # Compute PnL in R-units for training
        _raw_pnl = (
            (result.tp_price - result.entry_price)
            if result.label == "tp_hit_first"
            else (result.sl_price - result.entry_price)
            if result.label == "sl_hit_first"
            else 0.0
        )
        _pnl_r = (
            _raw_pnl / (result.atr_at_entry * contract.sl_atr_mult)
            if result.atr_at_entry > 0
            else 0.0
        )

        label_entry: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "label_id": f"barrier_{contract.contract_id}_{entry_idx:06d}",
            "symbol": "XAUUSD",
            "side": side,
            "entry_price": result.entry_price,
            "entry_time": timestamps[entry_idx] if entry_idx < len(timestamps) else "",
            "entry_idx": entry_idx,
            "exit_price": result.hit_price,
            "hit_bar_index": result.hit_bar_index,
            "exit_time": (
                timestamps[entry_idx + result.hit_bar_index]
                if result.hit_bar_index is not None
                and entry_idx + result.hit_bar_index < len(timestamps)
                else None
            ),
            "pnl": round(_raw_pnl, 6),
            "pnl_r": round(_pnl_r, 6),
            "label": result.label,
            "label_int": label_int,
            "sl": result.sl_price,
            "tp": result.tp_price,
            "atr_at_entry": result.atr_at_entry,
            "horizon_bars": result.horizon_bars,
            "label_contract_id": contract.contract_id,
            "sl_atr_mult": contract.sl_atr_mult,
            "tp_atr_mult": contract.tp_atr_mult,
        }
        labels.append(label_entry)

    return labels


def build_barrier_labels_from_csv(
    csv_path: Path,
    contract: LabelContract,
    *,
    sides: list[str] | None = None,
    min_atr: float = 0.5,
) -> list[dict[str, Any]]:
    """Load OHLC from CSV and generate barrier labels for specified sides.

    Args:
        csv_path: Path to OHLC CSV file.
        contract: LabelContract instance.
        sides: List of sides to generate (default: ["long", "short"]).
        min_atr: Minimum ATR to filter entries.

    Returns:
        Combined list of barrier labels.
    """
    if sides is None:
        sides = ["long", "short"]

    opens, highs, lows, closes, timestamps = _load_price_csv(csv_path)

    all_labels: list[dict[str, Any]] = []
    for side in sides:
        side_labels = generate_barrier_labels_from_prices(
            highs,
            lows,
            closes,
            timestamps,
            contract,
            side=side,
            min_atr=min_atr,
        )
        all_labels.extend(side_labels)

    return all_labels


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="label_builder",
        description="Build training labels from trade journals or price history",
    )
    # ── Input sources ──
    p.add_argument(
        "--journal",
        type=Path,
        default=None,
        help="Path to live_trade_journal.jsonl (journal mode)",
    )
    p.add_argument(
        "--price-data",
        type=Path,
        default=None,
        help="Path to OHLC CSV for barrier label generation (barrier mode). "
        "Use --timeframe for auto-resolution from data/raw/.",
    )
    # ── Timeframe convenience (auto-resolves --price-data) ──
    p.add_argument(
        "--timeframe",
        default=None,
        help="Timeframe for auto CSV resolution (M5/M15/H1/H4). "
        "Resolves --price-data to data/raw/xauusdc_{tf}_merged.csv.",
    )
    # ── Label contract (optional, enriches both modes) ──
    p.add_argument(
        "--label-contract",
        type=Path,
        default=None,
        help="Path to Label Contract JSON file",
    )
    # ── Options ──
    p.add_argument(
        "--date",
        default=None,
        help="ISO date filter for journal mode (UTC), e.g. 2026-05-04",
    )
    p.add_argument(
        "--side",
        default="long,short",
        help="Trade sides for barrier mode (default: long,short)",
    )
    p.add_argument(
        "--min-atr",
        type=float,
        default=0.5,
        help="Minimum ATR threshold for barrier mode (default: 0.5)",
    )
    p.add_argument(
        "--output",
        default=None,
        help="Write JSONL labels to file (default: stdout)",
    )
    p.add_argument(
        "--stats",
        action="store_true",
        help="Print summary statistics only",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.journal and not args.price_data:
        # Auto-resolve from --timeframe if provided
        if args.timeframe:
            tf = args.timeframe.upper()
            auto_csv = Path(f"data/raw/xauusdc_{tf.lower()}_merged.csv")
            if auto_csv.exists():
                args.price_data = str(auto_csv)
                print(
                    f"[label_builder] Auto-resolved --price-data={args.price_data} from --timeframe={tf}"
                )
            else:
                print(f"[label_builder] ERROR: Auto CSV not found: {auto_csv} (timeframe={tf})")
                return 2
        else:
            print(
                "[label_builder] ERROR: --journal, --price-data, or --timeframe is required",
                flush=True,
            )
            return 2

    # ── Load contract if provided ──
    contract: LabelContract | None = None
    if args.label_contract:
        contract = LabelContract.from_file(args.label_contract)
        issues = contract.validate()
        if issues:
            for issue in issues:
                print(f"[label_builder] WARN: Contract issue: {issue}")
        print(f"[label_builder] Loaded contract: {contract.contract_id}")

    # ── Barrier mode: price data → barrier labels ──
    if args.price_data:
        if contract is None:
            print("[label_builder] ERROR: --label-contract is required with --price-data")
            return 2

        sides = [s.strip() for s in args.side.split(",") if s.strip()]
        records = build_barrier_labels_from_csv(
            Path(args.price_data),
            contract,
            sides=sides,
            min_atr=args.min_atr,
        )
    else:
        # ── Journal mode ──
        records = build_trade_records(
            Path(args.journal),
            date_filter=args.date,
            contract=contract,
        )

    # ── Stats mode ──
    if args.stats:
        stats = build_basic_stats_report(records)
        print(json.dumps(stats, indent=2, ensure_ascii=False, default=str))
        return 0

    # ── Output ──
    lines = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in records)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(lines + "\n", encoding="utf-8")
        print(f"[label_builder] Wrote {len(records)} labels to {out}")
    else:
        print(lines)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
