#!/usr/bin/env python
"""Detect and (append-only) correct historically-fabricated break-even closes.

DQAF-20260708-003 remediation.  Before the deal-selection SSOT fix,
``position_close_adapter._build_event`` resolved a close from the DEAL_ENTRY_IN
opening deal → ``close_price == entry_price``, ``pnl == 0``, ``label ==
"breakeven"``.  This script finds those fabricated records and, when MT5 deal
history is available, appends a broker-verified correction.

Iron Law #11 compliant — the detection pass is pure Python (no MT5, no writes)
and its stdout is the SOLE evidence source.

Event-sourcing compliant — corrections are **APPENDED**, never written in place.
``live_trade_journal.jsonl`` is an immutable SSOT (CLAUDE.md §1).  A correction
is a new ``close`` record carrying ``_source="mt5_reconciliation_backfill"`` and
``_corrects=<original message_id>``; last-close-per-ticket projections supersede
the fabricated original.  Every correction is also mirrored to
``reports/journal_pnl_corrections.jsonl`` as a standalone audit trail.

Detection signature (the fabrication fingerprint):
  action == "close"
  label == "breakeven"
  pnl in {0, 0.0, None}
  entry_price and close_price both present and equal (abs diff <= tol)
  _close_price_source != "mt5_exit_deal"   (not already SSOT-resolved)

Usage::

    # Dry-run detection only (no MT5, no writes) — quantify the scope
    python scripts/backfill_fabricated_breakeven.py --data-dir data_btc

    # Apply corrections from MT5 deal history (append-only)
    python scripts/backfill_fabricated_breakeven.py --data-dir data_btc \
        --apply --mt5-terminal-path "D:\\MetaTrader 5\\terminal64.exe"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ── Statistical口径声明 (Iron Law #11) ──
# 去重键: position_ticket. 一笔 = 一个 close 记录.
# fabricated breakeven = label==breakeven ∧ pnl∈{0,None} ∧ close_price==entry_price
#                        ∧ _close_price_source≠mt5_exit_deal.
# 校正 pnl/close_price 唯一来源: MT5 history_deals_get() 经 resolve_exit_deal() SSOT.

_PRICE_TOL = 1e-6  # exact-equality tolerance for close_price == entry_price


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _f(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def is_fabricated_breakeven(entry: dict[str, Any]) -> bool:
    """Pure predicate — the fabrication fingerprint.  No side effects."""
    if entry.get("action") != "close":
        return False
    if str(entry.get("label", "")) != "breakeven":
        return False
    if entry.get("_close_price_source") == "mt5_exit_deal":
        return False  # already SSOT-resolved
    pnl = entry.get("pnl")
    if pnl not in (0, 0.0, None):
        return False
    detail = entry.get("detail") or {}
    close_price = _f(detail.get("close_price") if isinstance(detail, dict) else None)
    if close_price is None:
        close_price = _f(entry.get("exit_price"))
    entry_price = _f(entry.get("entry_price"))
    if entry_price is None or close_price is None:
        return False
    return abs(close_price - entry_price) <= _PRICE_TOL and entry_price > 0


def detect(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return fabricated-breakeven close records (pure Python — no MT5)."""
    return [e for e in entries if is_fabricated_breakeven(e)]


def _build_correction(
    orig: dict[str, Any],
    *,
    close_price: float,
    pnl: float | None,
    close_reason: int | None,
    close_price_source: str,
    pnl_status: str,
    now_iso: str,
) -> dict[str, Any]:
    """Build an APPEND-only correction close record superseding *orig*."""
    ticket = orig.get("position_ticket")
    deal_id = (
        orig.get("detail", {}).get("deal_id") if isinstance(orig.get("detail"), dict) else None
    )
    entry_price = _f(orig.get("entry_price")) or 0.0
    side = orig.get("side", "")
    _REASON = {
        0: "client_close",
        3: "signal_close",
        4: "sl_hit",
        5: "tp_hit",
        6: "stop_out",
        7: "risk_out",
    }
    reason_str = _REASON.get(close_reason or -1, f"mt5_deal_reason_{close_reason}")
    if pnl is None:
        label = "unknown_pnl_pending"
    elif close_reason == 4:
        label = "sl_hit_first"
    elif close_reason == 5:
        label = "tp_hit_first"
    elif pnl > 0:
        label = "win"
    elif pnl < 0:
        label = "loss"
    else:
        label = "breakeven"
    return {
        "schema_version": "live_trade_journal.v2",
        "recorded_at": now_iso,
        "message_id": f"corr_{ticket}_{deal_id or 'na'}_{now_iso[:19]}",
        "target": "exec_bridge",
        "ack_status": "closed",
        "detail": {
            "reason": reason_str,
            "close_price": close_price,
            "pnl": pnl,
            "close_price_source": close_price_source,
        },
        "symbol": orig.get("symbol"),
        "action": "close",
        "side": side,
        "volume": orig.get("volume"),
        "pnl": pnl,
        "label": label,
        "position_ticket": ticket,
        "position_identifier": orig.get("position_identifier") or ticket,
        "magic": orig.get("magic"),
        "strategy": orig.get("strategy"),
        "entry_price": entry_price,
        "exit_price": close_price,
        # ── correction lineage (event sourcing) ──
        "_source": "mt5_reconciliation_backfill",
        "_corrects": orig.get("message_id"),
        "_corrected_at": now_iso,
        "_close_price_source": close_price_source,
        "_pnl_status": pnl_status,
    }


def _connect_mt5(mt5_terminal_path: str | None) -> Any:
    import MetaTrader5 as mt5  # noqa: N813

    ok = mt5.initialize(path=mt5_terminal_path) if mt5_terminal_path else mt5.initialize()
    if not ok:
        raise RuntimeError(f"mt5_init_failed: {mt5.last_error()}")
    return mt5


def apply_corrections(
    fabricated: list[dict[str, Any]],
    *,
    mt5: Any,
    now_iso: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Query MT5 via the SSOT and build corrections.  Returns (corrections, unresolved)."""
    from core.runtime.deal_selection import resolve_exit_deal

    corrections: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for orig in fabricated:
        ticket = orig.get("position_ticket")
        if ticket is None:
            unresolved.append({"ticket": ticket, "reason": "no_ticket"})
            continue
        deals = mt5.history_deals_get(position=int(ticket))
        res = resolve_exit_deal(deals) if deals else None
        if res is None or not res.has_exit or res.close_price is None:
            unresolved.append(
                {"ticket": ticket, "reason": "no_exit_deal", "n_deals": len(deals) if deals else 0}
            )
            continue
        if res.close_pnl is not None:
            pnl, pnl_status = float(res.close_pnl), "verified_from_mt5_deal"
        else:
            pnl, pnl_status = None, "pending_mt5_confirmation"
        corrections.append(
            _build_correction(
                orig,
                close_price=float(res.close_price),
                pnl=pnl,
                close_reason=res.close_reason,
                close_price_source=res.close_price_source,
                pnl_status=pnl_status,
                now_iso=now_iso,
            )
        )
    return corrections, unresolved


def _append_lines(path: Path, records: list[dict[str, Any]], *, lock_dir: Path | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    if lock_dir is not None:
        from core.infrastructure.distributed_lock import FileLock

        lock = FileLock(path.stem, lock_dir=str(lock_dir), ttl_seconds=10)
        acq = lock.acquire(blocking=True, timeout_seconds=5)
        if not acq.acquired:
            raise RuntimeError(f"lock_failed: {acq.error}")
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(payload)
        finally:
            lock.release()
    else:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(payload)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default="data_btc")
    ap.add_argument("--apply", action="store_true", help="Append corrections (requires MT5)")
    ap.add_argument("--mt5-terminal-path", default=r"D:\MetaTrader 5\terminal64.exe")
    ap.add_argument("--now", default=None, help="Override timestamp (tests/determinism)")
    args = ap.parse_args(argv)

    dd = Path(args.data_dir)
    journal = dd / "live_trade_journal.jsonl"
    entries = _load_jsonl(journal)
    fabricated = detect(entries)

    print(f"=== 伪造 breakeven 检测: {args.data_dir} (Iron Law #11) ===")
    print(f"journal 记录数={len(entries)}  伪造 breakeven={len(fabricated)}")
    for e in fabricated[:30]:
        _d = e.get("detail") or {}
        print(
            f"  ticket={e.get('position_ticket')} entry={e.get('entry_price')} "
            f"close={_d.get('close_price') if isinstance(_d, dict) else None} "
            f"pnl={e.get('pnl')} label={e.get('label')} "
            f"src={e.get('_close_price_source', 'legacy')} msg={e.get('message_id')}"
        )
    if len(fabricated) > 30:
        print(f"  ... 及另外 {len(fabricated) - 30} 条")

    if not args.apply:
        print("\n[DRY-RUN] 未连接 MT5, 未写入. 以上为唯一合法证据源.")
        print("  → 加 --apply 从 MT5 deal 历史追加 broker-verified 校正 (append-only).")
        return 0

    if not fabricated:
        print("\n[APPLY] 无候选, 无需校正.")
        return 0

    now_iso = args.now or datetime.now(UTC).replace(tzinfo=None).isoformat()
    try:
        mt5 = _connect_mt5(args.mt5_terminal_path)
    except (RuntimeError, ImportError) as exc:
        print(f"\n[FAIL] MT5 不可用: {exc}")
        return 1
    try:
        corrections, unresolved = apply_corrections(fabricated, mt5=mt5, now_iso=now_iso)
    finally:
        mt5.shutdown()

    print(f"\n[APPLY] 可校正={len(corrections)}  无法解析={len(unresolved)}")
    if corrections:
        lock_dir = dd / "locks"
        # 1) audit trail (standalone)
        _append_lines(dd / "reports" / "journal_pnl_corrections.jsonl", corrections, lock_dir=None)
        # 2) append superseding closes to the immutable journal (never rewrite)
        _append_lines(journal, corrections, lock_dir=lock_dir)
        _pos = sum(1 for c in corrections if (c.get("pnl") or 0) > 0)
        _neg = sum(1 for c in corrections if (c.get("pnl") or 0) < 0)
        _recovered = round(sum(c.get("pnl") or 0.0 for c in corrections), 2)
        print(f"  已追加 {len(corrections)} 条校正 (win={_pos} loss={_neg} net_pnl={_recovered})")
        print(f"  审计: {dd / 'reports' / 'journal_pnl_corrections.jsonl'}")
    for u in unresolved[:20]:
        print(f"  UNRESOLVED ticket={u.get('ticket')} reason={u.get('reason')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
