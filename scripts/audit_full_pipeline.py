#!/usr/bin/env python
"""Full Pipeline Data Health Audit — DQAF-20260621-043 Phase 3
==============================================================
Checks ALL data paths needed for:
  A. Training pipeline (golden_master, feature_store, labels)
  B. Gate/Barrier pipeline (calibrator, MetaFilter, regime)
  C. Alpha signal pipeline (registry, performance, feed, allocation)
  D. Execution/Position pipeline (snapshots, bridge, execution_state)
  E. Brain data pipeline (brain_performance, PnP ledger, configs)
  F. Data health self-report (internal consistency)

Usage:
  python scripts/audit_full_pipeline.py [--data-dir data_btc]
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    if not path.exists():
        return records
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            with __import__("contextlib").suppress(json.JSONDecodeError):
                records.append(json.loads(line))
    return records


def load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def health_icon(ok: bool) -> str:
    return "[OK]" if ok else "[GAP]"


def check(label: str, ok: bool, detail: str = "") -> bool:
    icon = health_icon(ok)
    line = f"  {icon} {label}"
    if detail:
        line += f": {detail}"
    print(line)
    return ok


def section(title: str):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


# ═══════════════════════════════════════════════════════════════════════════
# A. Training Pipeline
# ═══════════════════════════════════════════════════════════════════════════


def audit_training_pipeline(data_dir: Path, label: str) -> dict:
    section(f"A. TRAINING PIPELINE — {label}")

    gaps = []
    ok_count = 0

    # A1. Golden Master
    gm = load_jsonl(data_dir / "golden_master.jsonl")
    gm_has_data = len(gm) > 0
    ok_count += check("A1. Golden Master records", gm_has_data, f"{len(gm):,} records")

    if gm_has_data:
        # Check label diversity
        labels = []
        for r in gm:
            outputs = r.get("outputs", {})
            if isinstance(outputs, dict):
                for strategy, sdata in outputs.items():
                    if isinstance(sdata, dict):
                        direction = sdata.get("direction", "neutral")
                        labels.append(direction)
        if labels:
            label_dist = Counter(labels)
            for d, c in label_dist.most_common():
                pct = c / len(labels) * 100
                ok_count += check(f"  Golden Master label '{d}'", pct > 3, f"{c:,} ({pct:.1f}%)")
        else:
            gaps.append("A1.1: No labels extractable from golden_master outputs")

        # Check chronological sorting
        timestamps = []
        for r in gm:
            ts = r.get("timestamp_utc", r.get("timestamp", ""))
            if ts:
                timestamps.append(ts)
        if len(timestamps) > 1:
            sorted_ok = all(timestamps[i] <= timestamps[i + 1] for i in range(len(timestamps) - 1))
            ok_count += check("A1.2 Golden Master sorted", sorted_ok)
            if not sorted_ok:
                gaps.append("A1.2: Golden Master not chronologically sorted")
        else:
            gaps.append("A1.2: Golden Master has insufficient timestamps for sorting check")

        # Check feature dimensions
        first = gm[0]
        inputs = first.get("inputs", {})
        if isinstance(inputs, dict):
            n_features = len(inputs)
            ok_count += check("A1.3 Feature dimensions", n_features >= 10, f"{n_features} features")
            if n_features < 10:
                gaps.append(f"A1.3: Only {n_features} features in golden_master")
        else:
            gaps.append("A1.3: No 'inputs' field in golden_master records")

        # Time range
        if timestamps:
            timestamps.sort()
            days_span = "N/A"
            try:
                t0 = datetime.fromisoformat(timestamps[0].replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(timestamps[-1].replace("Z", "+00:00"))
                days_span = f"{(t1 - t0).days} days"
            except Exception:  # noqa: BLE001
                pass
            print(
                f"  [INFO] Golden Master time range: {timestamps[0][:19]} → {timestamps[-1][:19]} ({days_span})"
            )

    # A2. Feature Store
    fs_dir = data_dir / "feature_store" / "records"
    if fs_dir.exists():
        fs_symbols = list(fs_dir.glob("symbol=*"))
        total_fs = 0
        for sym_dir in fs_symbols:
            for tf_dir in sym_dir.glob("timeframe=*"):
                f_path = tf_dir / "features.jsonl"
                if f_path.exists():
                    fc = sum(1 for _ in open(f_path, encoding="utf-8"))
                    total_fs += fc
                    symbol = sym_dir.name.replace("symbol=", "")
                    tf = tf_dir.name.replace("timeframe=", "")
                    check(f"A2. Feature Store {symbol}/{tf}", fc > 0, f"{fc:,} records")
        ok_count += check(
            "A2.1 Feature Store total", total_fs > 0, f"{total_fs:,} records across all symbols/TFs"
        )
        if total_fs == 0:
            gaps.append("A2: Feature store has zero records")
    else:
        gaps.append("A2: Feature store directory not found")
        check("A2. Feature Store", False, "directory missing")

    # A3. Training readiness
    tr_path = data_dir / "reports" / "training_readiness.json"
    tr = load_json(tr_path)
    if tr:
        ok_count += check("A3. Training readiness report", True, "present")
    else:
        check("A3. Training readiness report", False, "missing — training may not have guidance")
        gaps.append("A3: training_readiness.json missing")

    return {"section": "training", "gaps": gaps, "ok": ok_count}


# ═══════════════════════════════════════════════════════════════════════════
# B. Gate / Barrier Pipeline
# ═══════════════════════════════════════════════════════════════════════════


def audit_gate_pipeline(data_dir: Path, label: str) -> dict:
    section(f"B. GATE / BARRIER PIPELINE — {label}")

    gaps = []
    ok_count = 0

    # B1. Calibrator
    cal = load_json(data_dir / "conformal_calibrator_state.json")
    cal_feed = load_json(data_dir / "calibrator_feed_state.json")

    if cal:
        if isinstance(cal, dict):
            history = cal.get("history", [])
            n_samples = len(history) if isinstance(history, list) else 0
            total_computations = cal.get("total_computations", 0)
            cold_started = cal.get("cold_started", True)
            # Check for p_win collapse (G1): if recent history entries all have p_win≈0.5
            p_win_stuck = False
            if n_samples >= 10 and isinstance(history, list):
                recent = history[-50:] if n_samples >= 50 else history
                p_win_values = [h.get("p_win", 0.5) for h in recent if isinstance(h, dict)]
                if p_win_values:
                    unique_vals = set(round(v, 3) for v in p_win_values)
                    p_win_stuck = len(unique_vals) <= 2  # collapsed to few distinct values
            ok_count += check(
                "B1. Calibrator state",
                True,
                f"history={n_samples}, computations={total_computations}, cold_started={cold_started}",
            )
            if cold_started:
                gaps.append("B1: Calibrator still cold_started — may need warmup")
            if p_win_stuck and not cold_started and total_computations > 100:
                gaps.append("B1: Calibrator p_win collapsed — model output may be degraded (G1)")
        else:
            ok_count += check("B1. Calibrator state", True, "present")
    else:
        gaps.append("B1: conformal_calibrator_state.json not found")

    if cal_feed:
        last_ts = (
            cal_feed.get("last_recorded_at", cal_feed.get("updated_utc", ""))
            if isinstance(cal_feed, dict)
            else ""
        )
        ok_count += check("B1.1 Calibrator feed", bool(last_ts), f"last={str(last_ts)[:30]}")
        if not last_ts:
            gaps.append("B1.1: Calibrator feed has no timestamp — feed pipeline may be broken")
    else:
        gaps.append("B1.1: calibrator_feed_state.json missing")

    # B2. MetaFilter
    mf = load_json(data_dir / "meta_filter_state.json")
    if mf:
        if isinstance(mf, dict):
            mf_model = mf.get("model_path", mf.get("current_model", ""))
            mf_wr = mf.get("win_rate", mf.get("rolling_wr", None))
            ok_count += check(
                "B2. MetaFilter state", True, f"model={str(mf_model)[:40]}, wr={mf_wr}"
            )
            if not mf_model:
                gaps.append("B2: MetaFilter has no model path — may be using rolling_wr fallback")
        else:
            ok_count += check("B2. MetaFilter state", True, "present")
    else:
        check("B2. MetaFilter state", False, "missing — barrier may use fallback")
        gaps.append("B2: meta_filter_state.json missing")

    # B3. Regime Detector
    rd = load_json(data_dir / "regime_detector_state.json")
    if rd:
        if isinstance(rd, dict):
            regime = rd.get("current_regime", rd.get("regime", "unknown"))
            ok_count += check("B3. Regime detector", True, f"regime={regime}")
        else:
            ok_count += check("B3. Regime detector", True, "present")
    else:
        check("B3. Regime detector", False, "missing")
        gaps.append("B3: regime_detector_state.json missing")

    # B4. Gate audit data
    gate_audit_dir = data_dir / "gate_audit"
    if gate_audit_dir.exists():
        gate_files = list(gate_audit_dir.glob("*.jsonl")) + list(gate_audit_dir.glob("*.json"))
        ok_count += check("B4. Gate audit records", len(gate_files) > 0, f"{len(gate_files)} files")
    else:
        check("B4. Gate audit directory", False, "missing")

    return {"section": "gate", "gaps": gaps, "ok": ok_count}


# ═══════════════════════════════════════════════════════════════════════════
# C. Alpha Signal Pipeline
# ═══════════════════════════════════════════════════════════════════════════


def audit_alpha_pipeline(data_dir: Path, label: str) -> dict:
    section(f"C. ALPHA SIGNAL PIPELINE — {label}")

    gaps = []
    ok_count = 0

    # C1. Alpha Registry
    reg = load_json(data_dir / "alpha_registry.json")
    perf = load_json(data_dir / "alpha_performance.json")
    feed = load_json(data_dir / "alpha_feed_state.json")
    alloc = load_json(data_dir / "reports" / "alpha_allocation.json")

    if reg:
        n_alphas = 0
        n_active = 0
        if isinstance(reg, dict):
            alphas = reg.get("alphas", reg.get("entries", []))
            if isinstance(alphas, dict):
                alphas = list(alphas.values())
            elif isinstance(alphas, list):
                pass
            n_alphas = len(alphas) if isinstance(alphas, list) else 0
            n_active = sum(
                1
                for a in alphas
                if isinstance(a, dict) and a.get("status") in ("active", "live", True)
            )
        ok_count += check(
            "C1. Alpha registry", n_alphas > 0, f"{n_alphas} registered, {n_active} active"
        )
        if n_alphas == 0:
            gaps.append("C1: Zero alphas registered — no alpha signal pipeline")
        if n_active == 0 and n_alphas > 0:
            gaps.append(f"C1: {n_alphas} registered but 0 active")
    else:
        gaps.append("C1: alpha_registry.json missing")

    if perf:
        n_entries = 0
        if isinstance(perf, dict):
            entries = perf.get("entries", perf.get("performance", []))
            if isinstance(entries, dict):
                entries = list(entries.values())
            n_entries = len(entries) if isinstance(entries, list) else 0
        ok_count += check("C2. Alpha performance", n_entries > 0, f"{n_entries} entries")
    else:
        check("C2. Alpha performance", False, "missing")

    if feed:
        last_line = (
            feed.get("last_line", feed.get("last_recorded_at", 0)) if isinstance(feed, dict) else 0
        )
        ok_count += check("C3. Alpha feed state", last_line > 0, f"last_line={last_line}")
        if last_line == 0:
            gaps.append("C3: Alpha feed may be stalled")
    else:
        gaps.append("C3: alpha_feed_state.json missing")

    if alloc:
        allocations = alloc if isinstance(alloc, dict) else {}
        n_alloc = len(allocations.get("allocations", allocations.get("entries", [])))
        ok_count += check(
            "C4. Alpha allocation",
            n_alloc > 0,
            f"{n_alloc} allocations" if n_alloc else "present but empty",
        )
    else:
        check("C4. Alpha allocation report", False, "missing")

    return {"section": "alpha", "gaps": gaps, "ok": ok_count}


# ═══════════════════════════════════════════════════════════════════════════
# D. Execution / Position Pipeline
# ═══════════════════════════════════════════════════════════════════════════


def audit_execution_pipeline(data_dir: Path, label: str) -> dict:
    section(f"D. EXECUTION / POSITION PIPELINE — {label}")

    gaps = []
    ok_count = 0

    # D1. Position Snapshots
    snaps = load_jsonl(data_dir / "position_snapshots.jsonl")
    has_snaps = len(snaps) > 0
    ok_count += check("D1. Position snapshots", has_snaps, f"{len(snaps):,} records")

    if has_snaps:
        # Check coverage vs journal
        journal = load_jsonl(data_dir / "live_trade_journal.jsonl")
        jt_tickets = set()
        for r in journal:
            if r.get("action") == "close":
                ticket = r.get("position_ticket")
                if ticket:
                    jt_tickets.add(ticket)

        snap_tickets = set()
        for s in snaps:
            ticket = s.get("ticket") or s.get("position_ticket")
            if ticket:
                snap_tickets.add(ticket)

        matched = jt_tickets & snap_tickets
        cov_pct = len(matched) / max(len(jt_tickets), 1) * 100
        ok_count += check(
            "D1.1 Snapshot vs journal coverage",
            cov_pct > 70,
            f"{cov_pct:.1f}% ({len(matched)}/{len(jt_tickets)})",
        )
        if cov_pct < 70:
            gaps.append(f"D1.1: Only {cov_pct:.1f}% of trades have position snapshots")

        # Check if snapshots have sl/tp fields
        first = snaps[0]
        has_sl = "sl_price" in first or "trailing_sl_distance" in first
        has_tp = "tp_price" in first
        ok_count += check(
            "D1.2 Snapshot SL/TP fields",
            has_sl,
            f"sl={'YES' if has_sl else 'NO'}, tp={'YES' if has_tp else 'NO'}",
        )

    # D2. Execution State
    exec_state = load_json(data_dir / "execution_state.json")
    if exec_state is None:
        exec_state = load_json(data_dir / "state" / "execution_state.json")
    if exec_state:
        ok_count += check("D2. Execution state", True, "present")
    else:
        check("D2. Execution state", False, "missing")
        gaps.append("D2: execution_state.json missing")

    # D3. Bridge Health
    bridge = load_json(data_dir / "reports" / "mt5_bridge_health.json")
    if bridge:
        if isinstance(bridge, dict):
            conn = bridge.get("connected", bridge.get("connection_ok", None))
            ok_count += check("D3. MT5 Bridge health", True, f"connected={conn}")
        else:
            ok_count += check("D3. MT5 Bridge health", True, "present")
    else:
        check("D3. MT5 Bridge health", False, "missing")

    # D4. Ledger Events
    ledger = load_jsonl(data_dir / "ledger_events.jsonl")
    has_ledger = len(ledger) > 0
    ok_count += check("D4. Ledger events", has_ledger, f"{len(ledger):,} records")
    if has_ledger:
        event_types = Counter(e.get("event_type", e.get("type", "?")) for e in ledger)
        for etype, cnt in event_types.most_common(3):
            print(f"       {etype}: {cnt:,}")

    # D5. OFI Snapshot
    ofi = load_json(data_dir / "reports" / "ofi_snapshot.json")
    if ofi:
        ok_count += check("D5. OFI snapshot", True, "present")
    else:
        check("D5. OFI snapshot", False, "missing (order flow imbalance not tracked)")

    return {"section": "execution", "gaps": gaps, "ok": ok_count}


# ═══════════════════════════════════════════════════════════════════════════
# E. Brain Data Pipeline
# ═══════════════════════════════════════════════════════════════════════════


def audit_brain_pipeline(data_dir: Path, label: str) -> dict:
    section(f"E. BRAIN DATA PIPELINE — {label}")

    gaps = []
    ok_count = 0

    # E1. Brain Performance
    bp = load_json(data_dir / "brain_performance.json")
    if bp:
        if isinstance(bp, dict):
            n_brains = len(bp.get("records", bp.get("brain_ids", {})))
            ok_count += check("E1. Brain performance", n_brains > 0, f"{n_brains} brains tracked")
            if n_brains == 0:
                gaps.append("E1: Brain performance has 0 tracked brains")
        else:
            ok_count += check("E1. Brain performance", True, "present")
    else:
        gaps.append("E1: brain_performance.json missing")

    # E2. Brain PnL Ledger
    bp_ledger = load_json(data_dir / "brain_pnl_ledger.json")
    if bp_ledger is None:
        bp_ledger = load_json(data_dir / "brain_pnl_ledger.json")
    if bp_ledger:
        ok_count += check("E2. Brain PnL Ledger", True, "present")
    else:
        check("E2. Brain PnL Ledger", False, "missing")

    # E3. Governance State (already fixed by DQAF-043)
    gov = load_json(data_dir / "governance_state.json")
    if isinstance(gov, dict):
        brains = gov.get("brain_states", gov.get("brains", {}))
        if isinstance(brains, dict):
            b_list = list(brains.values())
        elif isinstance(brains, list):
            b_list = brains
        else:
            b_list = []
        live_count = sum(1 for b in b_list if isinstance(b, dict) and b.get("status") == "live")
        total = len(b_list)
        ok_count += check("E3. Governance state", total > 0, f"{total} brains, {live_count} live")
        if live_count == 0:
            gaps.append("E3: 0 live brains in governance")

        # Check for remaining backtest contamination
        contaminated = []
        for b in b_list:
            if not isinstance(b, dict):
                continue
            perf = b.get("performance_metrics", {})
            if isinstance(perf, dict):
                trades = perf.get("total_trades", 0) or 0
                if trades > 1000:
                    contaminated.append(b.get("brain_id", "?"))
        if contaminated:
            gaps.append(f"E3.1: {len(contaminated)} brains still contaminated: {contaminated}")
        else:
            check("E3.1 No backtest contamination", True, "all brains clean")
            ok_count += 1

    # E4. Leaderboard
    lb = load_json(data_dir / "reports" / "leaderboard.json")
    if lb:
        if isinstance(lb, dict):
            has_brains = len(lb.get("brains", lb.get("entries", []))) > 0
            ok_count += check(
                "E4. Leaderboard", has_brains, f"{len(lb.get('brains', []))} brains ranked"
            )
            if not has_brains:
                gaps.append("E4: Leaderboard has 0 ranked brains")
        else:
            ok_count += check("E4. Leaderboard", True, "present")
    else:
        gaps.append("E4: leaderboard.json missing")

    # E5. Brain configs
    # Determine config directory
    if "btc" in str(data_dir).lower():
        config_dir = data_dir.parent / "configs" / "brains_btc"
    else:
        config_dir = data_dir.parent / "configs" / "brains"
    if config_dir.exists():
        configs = list(config_dir.glob("*.json"))
        ok_count += check("E5. Brain config files", len(configs) > 0, f"{len(configs)} configs")
    else:
        check("E5. Brain config files", False, f"directory not found: {config_dir}")

    return {"section": "brain", "gaps": gaps, "ok": ok_count}


# ═══════════════════════════════════════════════════════════════════════════
# F. Data Health Self-Report
# ═══════════════════════════════════════════════════════════════════════════


def audit_data_health(data_dir: Path, label: str) -> dict:
    section(f"F. DATA HEALTH SELF-REPORT — {label}")

    gaps = []
    ok_count = 0

    dh = load_json(data_dir / "state" / "data_health_state.json")
    if dh is None:
        dh = load_json(data_dir / "data_health_state.json")

    if dh:
        if isinstance(dh, dict):
            # schema: data_health_state.v2 — overall_status + sources (per-source health records)
            overall = dh.get("overall_status", dh.get("overall", "unknown"))
            sources = dh.get("sources", dh.get("checks", {}))
            n_checks = len(sources) if isinstance(sources, dict) else 0
            last_run = dh.get("last_full_run_utc", dh.get("updated_at", ""))
            ok_count += check(
                "F1. Data health report",
                n_checks > 0,
                f"{n_checks} checks, overall={overall}, last_full={str(last_run)[:19]}",
            )
            if n_checks == 0:
                gaps.append("F1: Data health report has 0 checks — self-diagnostic is blind")
            # Track individual failing sources
            if isinstance(sources, dict):
                failing = [
                    src
                    for src, rec in sources.items()
                    if isinstance(rec, dict)
                    and rec.get("last_status") in ("fail", "warn", "missing")
                ]
                if failing:
                    print(f"  [INFO] Non-pass sources: {', '.join(failing[:8])}")
        else:
            ok_count += check("F1. Data health report", True, "present")
    else:
        gaps.append("F1: data_health_state.json missing — no self-diagnostic")
        check("F1. Data health report", False, "missing")

    return {"section": "data_health", "gaps": gaps, "ok": ok_count}


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="Full pipeline data health audit")
    parser.add_argument("--data-dir", default="data_btc")
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()

    targets = []
    if args.full:
        targets = [(Path("data_btc"), "BTC"), (Path("data"), "XAU")]
    else:
        targets = [(Path(args.data_dir), "BTC" if "btc" in args.data_dir else "XAU")]

    all_gaps: dict[str, list] = {}

    for data_dir, label in targets:
        print(f"\n{'='*60}")
        print(f"  FULL PIPELINE DATA HEALTH AUDIT: {label}")
        print(f"  {data_dir}")
        print(f"{'='*60}")

        results = [
            audit_training_pipeline(data_dir, label),
            audit_gate_pipeline(data_dir, label),
            audit_alpha_pipeline(data_dir, label),
            audit_execution_pipeline(data_dir, label),
            audit_brain_pipeline(data_dir, label),
            audit_data_health(data_dir, label),
        ]

        # Summary
        print(f"\n{'='*60}")
        print(f"  SUMMARY: {label}")
        print(f"{'='*60}")

        total_gaps = []
        total_ok = 0
        for r in results:
            sec = r["section"]
            total_gaps.extend([f"[{sec}] {g}" for g in r.get("gaps", [])])
            total_ok += r.get("ok", 0)
            gap_count = len(r.get("gaps", []))
            status = "[OK]" if gap_count == 0 else f"[GAP] {gap_count} gaps"
            print(f"  {sec:20s}: {r['ok']} checks passed, {status}")

        print(f"\n  TOTAL: {total_ok} checks passed, {len(total_gaps)} gaps found")
        if total_gaps:
            print("\n  --- GAP DETAILS ---")
            for g in total_gaps:
                print(f"  {g}")
        else:
            print("\n  [OK] ALL PIPELINES HEALTHY — No gaps detected")

        all_gaps[label] = total_gaps

    print(f"\n{'='*60}")
    print("  AUDIT COMPLETE")
    print(f"{'='*60}")

    return all_gaps


if __name__ == "__main__":
    main()
