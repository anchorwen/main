#!/usr/bin/env python3
"""
check_training_triggers.py — Daily training trigger readiness audit.
Iron Law #11 compliant: all statistics from script stdout only.

Checks 5 conditions the user specified:
  1. BTC MetaFilter Path B: BTC live trades >= 200?
  2. BTC Calibrator: phase=HOT? (>= 200 samples)
  3. XAU retraining: brains with retraining_signal urgency=critical
  4. Label coverage: XAU/BTC > 90%?
  5. Feature store: last record within 30 min?

Usage:
  python scripts/check_training_triggers.py
"""

import json
import os
from datetime import datetime, UTC
from pathlib import Path


def load_json(filepath: str):
    try:
        with open(filepath, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return None


def load_jsonl_last_n(filepath: str, n: int = 5000):
    lines = []
    try:
        with open(filepath, encoding="utf-8") as f:
            all_lines = f.readlines()
            lines = all_lines[-n:]
    except (FileNotFoundError, PermissionError):
        pass
    result = []
    for line in lines:
        line = line.strip()
        if line:
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return result


def load_jsonl_recent(filepath: str, minutes: int = 60):
    """Load JSONL records from last N minutes based on timestamp fields."""
    cutoff = datetime.now(UTC).timestamp() - minutes * 60
    recent = []
    try:
        with open(filepath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Try various timestamp fields
                ts_str = rec.get("timestamp") or rec.get("ts") or rec.get("event_time") or rec.get("_timestamp") or rec.get("recorded_at") or ""
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                        if ts.timestamp() >= cutoff:
                            recent.append(rec)
                    except (ValueError, TypeError):
                        pass
    except (FileNotFoundError, PermissionError):
        pass
    return recent


# --─ 1. BTC Live Trades --------------------------------------------------─

def check_btc_trades(data_dir: str):
    events = load_jsonl_last_n(os.path.join(data_dir, "ledger_events.jsonl"), n=5000)
    tickets = set()
    for e in events:
        ticket = e.get("position_ticket") or e.get("ticket")
        if ticket:
            tickets.add(str(ticket))
    count = len(tickets)
    return count, count >= 200


# --─ 2. BTC Calibrator Phase ----------------------------------------------

def check_btc_calibrator(data_dir: str):
    """Read calibrator state from multiple possible locations."""
    candidates = [
        os.path.join(data_dir, "conformal_calibrator_state.json"),
        os.path.join(data_dir, "calibrator_feed_state.json"),
        os.path.join(data_dir, "state", "calibrator_feed_state.json"),
    ]
    state = None
    for path in candidates:
        state = load_json(path)
        if state:
            break

    if not state:
        return None, None, False

    phase = state.get("phase", "UNKNOWN")
    # Try multiple field names for sample count
    sample_count = (
        state.get("sample_count")
        or state.get("n_samples")
        or state.get("total_samples")
        or state.get("count", 0)
    )
    ready = (phase == "HOT" and sample_count >= 200)
    return phase, sample_count, ready


# --─ 3. XAU Retraining Signals --------------------------------------------

def check_xau_retraining(data_dir: str):
    perf = load_json(os.path.join(data_dir, "brain_performance.json"))
    if not perf:
        return [], [], False

    critical = []
    warning = []
    for brain_id, info in perf.items():
        if not isinstance(info, dict):
            continue
        rs = info.get("retraining_signal") or info.get("retrain_signal") or {}
        if isinstance(rs, dict):
            urgency = rs.get("urgency", "")
            reason = rs.get("reason", "")
            if urgency == "critical":
                critical.append(f"{brain_id} ({reason})" if reason else brain_id)
            elif urgency in ("warning", "high"):
                warning.append(f"{brain_id} ({reason})" if reason else brain_id)

    return critical, warning, len(critical) > 0


# --─ 4. Label Coverage ----------------------------------------------------

def check_label_coverage(data_dir: str):
    labels_path = os.path.join(data_dir, "reports", "live_labels.jsonl")
    records = load_jsonl_last_n(labels_path, n=1000)
    if not records:
        return None, None, False

    total = len(records)
    labeled = 0
    for rec in records:
        lbl = rec.get("label") or rec.get("meta_label") or rec.get("labels")
        if lbl is not None and lbl != "" and lbl != []:
            labeled += 1

    coverage = (labeled / total * 100) if total > 0 else 0.0
    return total, round(coverage, 1), coverage > 90.0


# --─ 5. Feature Store Freshness ------------------------------------------─

def check_feature_freshness(data_dir: str):
    fs_dir = Path(data_dir) / "feature_store" / "records"
    if not fs_dir.exists():
        return None, None, False

    now = datetime.now(UTC)
    latest_ts = None
    latest_info = ""

    for symbol_dir in fs_dir.iterdir():
        if not symbol_dir.is_dir():
            continue
        for tf_dir in symbol_dir.iterdir():
            if not tf_dir.is_dir():
                continue
            feat_file = tf_dir / "features.jsonl"
            if not feat_file.exists():
                continue
            last_lines = load_jsonl_last_n(str(feat_file), n=1)
            if last_lines:
                rec = last_lines[0]
                ts_str = rec.get("timestamp") or rec.get("ts") or rec.get("event_time") or rec.get("_timestamp") or ""
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=UTC)
                        if latest_ts is None or ts > latest_ts:
                            latest_ts = ts
                            latest_info = f"{symbol_dir.name}/{tf_dir.name}"
                    except (ValueError, TypeError):
                        pass

    if latest_ts is None:
        return None, None, False

    delta_min = round((now - latest_ts).total_seconds() / 60, 1)
    return latest_info, delta_min, delta_min <= 30


# --─ Main ----------------------------------------------------------------─

def main():
    data_xau = "data"
    data_btc = "data_btc"

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")

    print("=" * 72)
    print(f"  TRAINING TRIGGER READINESS — {now_str}")
    print("  Iron Law #11 Compliant — Script stdout is sole source of truth")
    print("=" * 72)

    # 1. BTC Trades
    trades, t1 = check_btc_trades(data_btc)
    print("\n-- 1. BTC MetaFilter Path B: Live Trades ≥ 200? --")
    print(f"   Distinct position_tickets: {trades}")
    print("   Threshold: 200")
    print(f"   Status: {'[READY]' if t1 else '[NOT READY]'} "
          f"{'(need ' + str(200 - trades) + ' more)' if not t1 else ''}")

    # 2. BTC Calibrator
    phase, samples, t2 = check_btc_calibrator(data_btc)
    print("\n-- 2. BTC Calibrator: phase=HOT (≥200 samples)? --")
    print(f"   Phase: {phase}, Samples: {samples}")
    print("   Threshold: phase=HOT, samples≥200")
    if phase is None:
        print("   Status: [WARN] UNKNOWN — calibrator state file not found")
    else:
        print(f"   Status: {'[READY] READY' if t2 else '[NOT READY] NOT READY'} "
              f"({'phase=' + str(phase) + ', ' + str(samples) + '/200 samples' if not t2 else ''})")

    # 3. XAU Retraining
    critical, warning, t3 = check_xau_retraining(data_xau)
    print("\n-- 3. XAU Retraining Signals: urgency=critical? --")
    print(f"   Critical brains: {len(critical)}")
    if critical:
        for c in critical:
            print(f"     [READY] {c}")
    print(f"   Warning brains: {len(warning)}")
    if warning:
        for w in warning[:5]:
            print(f"     [WARN]  {w}")
        if len(warning) > 5:
            print(f"     ... and {len(warning) - 5} more")
    print(f"   Status: {'[READY] READY — retraining needed!' if t3 else '[NOT READY] NOT READY — 0 critical signals'}")

    # 4. Label Coverage
    print("\n-- 4. Label Coverage: XAU/BTC > 90%? --")
    for label, dd in [("XAU", data_xau), ("BTC", data_btc)]:
        total, cov, ready = check_label_coverage(dd)
        print(f"   {label}: {cov}% coverage ({total} records)" if total else f"   {label}: no labels data")
        print(f"   {label} Status: {'[OK] READY' if ready else '[NOT READY] NOT READY' if cov is not None else '[WARN]  UNKNOWN'}")

    # 5. Feature Store Freshness
    print("\n-- 5. Feature Store Freshness: last record ≤ 30min? --")
    for label, dd in [("XAU", data_xau), ("BTC", data_btc)]:
        info, delta, ready = check_feature_freshness(dd)
        if info:
            print(f"   {label}: {delta}min ago ({info})")
            print(f"   {label} Status: {'[OK] READY' if ready else '[READY] NOT READY — ' + str(delta) + 'min stale'}")
        else:
            print(f"   {label}: no feature records found")
            print(f"   {label} Status: [WARN]  UNKNOWN")

    # --─ Summary --─
    print(f"\n{'=' * 72}")
    xau_cov_total, xau_cov_pct, xau_ready = check_label_coverage(data_xau)
    btc_cov_total, btc_cov_pct, btc_ready = check_label_coverage(data_btc)
    xau_fs_info, xau_fs_delta, xau_fs_ready = check_feature_freshness(data_xau)
    btc_fs_info, btc_fs_delta, btc_fs_ready = check_feature_freshness(data_btc)

    conditions = {
        "1. BTC Trades ≥ 200": t1,
        "2. BTC Calibrator HOT": t2,
        "3. XAU Retrain Critical": t3,
        f"4a. XAU Label Coverage ({xau_cov_pct}%)": xau_ready,
        f"4b. BTC Label Coverage ({btc_cov_pct}%)": btc_ready,
        f"5a. XAU Feature Freshness ({xau_fs_delta}min)": xau_fs_ready,
        f"5b. BTC Feature Freshness ({btc_fs_delta}min)": btc_fs_ready,
    }

    ready = [k for k, v in conditions.items() if v]
    not_ready = [k for k, v in conditions.items() if not v]

    print(f"  READY:     {len(ready)}/{len(conditions)}")
    for r in ready:
        print(f"    [READY] {r}")
    if not_ready:
        print(f"  NOT READY: {len(not_ready)}/{len(conditions)}")
        for nr in not_ready:
            print(f"    [NOT READY] {nr}")

    if any([t1, t2, t3]):
        print("\n  [WARN]  PIPELINE TRIGGERS ACTIVE:")
        if t1:
            print("     -> BTC MetaFilter Path B — training eligible")
        if t2:
            print("     -> BTC Calibrator HOT — calibration ready")
        if t3:
            print("     -> XAU Brain retraining — critical signal(s) pending")

    print(f"{'=' * 72}")
    print("\n[DONE] All statistics above are the sole source of truth.")


if __name__ == "__main__":
    main()
