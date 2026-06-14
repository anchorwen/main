"""Rigorous brain fleet audit — training, deployment, and live performance.

Iron Law #11: Script stdout is the sole source of truth.
Cross-references three independent data sources to validate the brain
rotation decision:
  1. Training reports (CV metrics from data/models/*/training_summary.json)
  2. Brain configs (configs/brains_btc/*.json — deployment state)
  3. Live journal (data_btc/live_trade_journal.jsonl — actual PnL)

Output: per-brain report card with verdict on rotation correctness.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path


def main(data_dir: str) -> int:
    base = Path(data_dir)

    # ── 1. Training Reports ──
    print("=" * 80)
    print("SECTION 1: TRAINING QUALITY (Cross-Validation Reports)")
    print("=" * 80)

    training_reports: dict[str, dict] = {}
    models_root = Path("data/models")
    for summary_path in models_root.rglob("training_summary.json"):
        model_name = summary_path.parent.name
        try:
            with open(summary_path) as f:
                d = json.load(f)
        except Exception:
            continue
        cv = d.get("cv_summary", {})
        if not cv:
            continue
        report = {}
        for arch, metrics in cv.items():
            wr = metrics.get("mean_val_wr", 0)
            folds = metrics.get("folds", 0)
            report[arch] = {"val_wr": round(float(wr), 4), "folds": int(folds)}
        if report:
            training_reports[model_name] = report
            for arch, m in report.items():
                verdict = "PASS" if m["val_wr"] > 0.50 else "FAIL"
                print(f"  {model_name}/{arch}: WR={m['val_wr']:.1%} folds={m['folds']} [{verdict}]")

    # ── 2. Brain Configs (active + archived) ──
    print()
    print("=" * 80)
    print("SECTION 2: DEPLOYMENT STATE (Brain Configs)")
    print("=" * 80)

    config_status: dict[str, dict] = {}
    for cfg_dir in ["configs/brains_btc", "configs/brains_btc/archive"]:
        cfg_path = Path(cfg_dir)
        if not cfg_path.exists():
            continue
        for cfg_file in cfg_path.glob("*.json"):
            if "meta_stage" in cfg_file.name or "normalization" in cfg_file.name:
                continue
            bid = cfg_file.stem
            try:
                with open(cfg_file, encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                config_status[bid] = {"status": "CORRUPT", "path": str(cfg_file)}
                continue
            bt = d.get("brain_type", "?")
            tf = d.get("timeframe", "?")
            mp = d.get("model_path", "")
            weight_exists = False
            for root in ["data_btc", "data", "."]:
                if mp and os.path.exists(os.path.join(root, mp)):
                    weight_exists = True
                    break
            is_archived = "archive" in cfg_dir
            config_status[bid] = {
                "status": "archived" if is_archived else "active",
                "brain_type": bt,
                "timeframe": tf,
                "weight_exists": weight_exists,
                "path": str(cfg_file),
            }

    for bid in sorted(config_status.keys()):
        s = config_status[bid]
        print(f"  [{s['status'].upper()}] {bid}: type={s['brain_type']} tf={s['timeframe']} weight={'OK' if s['weight_exists'] else 'MISSING'}")

    # ── 3. Live Journal Performance ──
    print()
    print("=" * 80)
    print("SECTION 3: LIVE TRADING PERFORMANCE (Journal — deduped by ticket)")
    print("=" * 80)

    journal_path = base / "live_trade_journal.jsonl"
    if not journal_path.exists():
        print("  Journal not found")
    else:
        closes: list[dict] = []
        with open(journal_path, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                if e.get("ack_status") == "closed":
                    closes.append(e)

        # Dedup by position_ticket (keep last)
        ticket_close: dict = {}
        for c in closes:
            t = c.get("position_ticket")
            if t is None:
                continue
            t = int(t)
            if t in ticket_close:
                existing_ts = ticket_close[t].get("recorded_at", "")
                if c.get("recorded_at", "") > existing_ts:
                    ticket_close[t] = c
            else:
                ticket_close[t] = c

        # Per-brain aggregation
        brain_stats: dict[str, dict] = defaultdict(
            lambda: {"trades": 0, "wins": 0, "losses": 0, "be": 0, "pnl": 0.0}
        )

        for close in ticket_close.values():
            brain_ids = close.get("brain_ids")
            pnl = close.get("pnl")
            if pnl is None:
                continue
            pnl = float(pnl)
            is_win = pnl > 0
            is_loss = pnl < 0

            if isinstance(brain_ids, list):
                for bid in brain_ids:
                    bs = brain_stats[str(bid)]
                    bs["trades"] += 1
                    bs["pnl"] += pnl
                    if is_win:
                        bs["wins"] += 1
                    elif is_loss:
                        bs["losses"] += 1
                    else:
                        bs["be"] += 1
            else:
                bid = str(close.get("strategy", "unknown"))
                bs = brain_stats[bid]
                bs["trades"] += 1
                bs["pnl"] += pnl
                if is_win:
                    bs["wins"] += 1
                elif is_loss:
                    bs["losses"] += 1
                else:
                    bs["be"] += 1

        print(f"  {'Brain':<40} {'Trades':>7} {'Wins':>6} {'Losses':>6} {'WR':>7} {'PnL':>10} {'Verdict'}")
        print(f"  {'-'*40} {'-'*7} {'-'*6} {'-'*6} {'-'*7} {'-'*10} {'-'*10}")
        for bid in sorted(brain_stats.keys(), key=lambda b: brain_stats[b]["trades"], reverse=True):
            bs = brain_stats[bid]
            t = bs["trades"]
            if t == 0:
                continue
            w = bs["wins"]
            l = bs["losses"]
            wr = w / (w + l) * 100 if (w + l) > 0 else 0.0
            pnl = bs["pnl"]
            verdict = "KEEP" if wr >= 40 or pnl > 0 else ("OBSERVE" if t < 20 else "RETIRE")
            print(f"  {bid:<40} {t:>7} {w:>6} {l:>6} {wr:>6.1f}% {pnl:>9.2f}  {verdict}")

    # ── 4. CROSS-REFERENCE MATRIX ──
    print()
    print("=" * 80)
    print("SECTION 4: CROSS-REFERENCE — Training vs Deployment vs Live")
    print("=" * 80)
    print(f"  {'Brain':<35} {'Train':>8} {'Deploy':>8} {'LiveWR':>8} {'LivePnL':>9} {'Verdict'}")
    print(f"  {'-'*35} {'-'*8} {'-'*8} {'-'*8} {'-'*9} {'-'*10}")

    all_bids = set(list(training_reports.keys()) + list(config_status.keys()) + list(brain_stats.keys()))
    for bid in sorted(all_bids):
        # Map config name to training name
        train_key = None
        for tk in training_reports:
            if bid.lower().replace("_survival", "").replace("btc_", "") in tk.lower().replace("_survival", ""):
                train_key = tk
                break
            # Direct match
            if bid.lower() in tk.lower() or tk.lower() in bid.lower():
                train_key = tk
                break

        train_wr = None
        if train_key:
            best_wr = max(
                (m["val_wr"] for m in training_reports[train_key].values()), default=None
            )
            train_wr = best_wr

        cfg = config_status.get(bid, {})
        deploy_status = cfg.get("status", "NONE")
        bs = brain_stats.get(bid, {})
        live_trades = bs.get("trades", 0)
        live_wr = bs["wins"] / (bs["wins"] + bs["losses"]) * 100 if (bs.get("wins", 0) + bs.get("losses", 0)) > 0 else 0
        live_pnl = bs.get("pnl", 0)

        train_str = f"{train_wr:.1%}" if train_wr is not None else "N/A"
        live_wr_str = f"{live_wr:.1f}%" if live_trades > 0 else "N/A"

        # Verdict logic
        if deploy_status == "active" and live_trades > 0 and live_wr < 30:
            verdict = "⚠️ RETIRE (live <30%)"
        elif deploy_status == "active" and train_wr is not None and train_wr < 0.40:
            verdict = "⚠️ RETIRE (train <40%)"
        elif deploy_status == "active" and train_wr is not None and train_wr > 0.70:
            verdict = "✅ PROMOTE (train >70%)"
        elif deploy_status == "archived" and live_trades > 0 and live_wr >= 40:
            verdict = "⚠️ REVIEW (good live, archived)"
        elif deploy_status == "archived":
            verdict = "✅ correct (archived)"
        elif live_trades == 0 and train_wr is None:
            verdict = "UNKNOWN"
        else:
            verdict = "OK"

        print(f"  {bid:<35} {train_str:>8} {deploy_status:>8} {live_wr_str:>8} {live_pnl:>9.2f}  {verdict}")

    # ── 5. FINAL VERDICT ──
    print()
    print("=" * 80)
    print("SECTION 5: ROTATION DECISION AUDIT")
    print("=" * 80)

    issues = []
    # Check: V11s had 0 live WR → correct to retire
    for bid in ["BTC_Swing_V11_H1_Directional", "BTC_Swing_V11_M15_Directional"]:
        bs = brain_stats.get(bid, {})
        t = bs.get("trades", 0)
        w = bs.get("wins", 0)
        l = bs.get("losses", 0)
        if t > 0 and w == 0:
            issues.append(f"✅ CORRECT: {bid} retired — 0 wins in {t} trades")
        elif t == 0:
            issues.append(f"⚠️ {bid} retired — but had 0 live trades (no data)")

    # Check: V10_M15 training quality
    if "btc_v10_m15_survival" in training_reports:
        best = max(m["val_wr"] for m in training_reports["btc_v10_m15_survival"].values())
        folds = list(training_reports["btc_v10_m15_survival"].values())[0]["folds"]
        if best > 0.80:
            issues.append(f"⚠️ FLAG: btc_v10_m15 CV WR={best:.1%} with {folds} folds — verify no look-ahead bias")
        if folds < 5:
            issues.append(f"⚠️ FLAG: btc_v10_m15 only {folds} folds — low statistical confidence")

    # Check: V9_H1 training quality
    if "btc_swing_v9_h1" in training_reports:
        best = max(m["val_wr"] for m in training_reports["btc_swing_v9_h1"].values())
        folds = list(training_reports["btc_swing_v9_h1"].values())[0]["folds"]
        if best > 0.80:
            issues.append(f"⚠️ FLAG: btc_swing_v9_h1 CV WR={best:.1%} with {folds} folds — verify no look-ahead bias")

    for issue in issues:
        print(f"  {issue}")

    if not issues:
        print("  All checks passed — rotation decision validated.")

    print()
    print("[DONE] All statistics above are the sole source of truth. (Iron Law #11)")
    return 0


if __name__ == "__main__":
    data_dir = "data_btc"
    args = sys.argv[1:]
    if "--data-dir" in args:
        idx = args.index("--data-dir")
        if idx + 1 < len(args):
            data_dir = args[idx + 1]
    elif args and not args[0].startswith("--"):
        data_dir = args[0]
    sys.exit(main(data_dir))
