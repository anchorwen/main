#!/usr/bin/env python
"""XAU 指定日 brain_votes 审计 — Iron Law #11 (法证工具)

统计口径声明:
- 数据源: {data_dir}/brain_votes/{date}.jsonl (投票流)
- 治理源: {data_dir}/governance_state.json (运行时 brain_status/vote_weight)
- 目标: 判定驱动开单的共识脑 (默认内嵌 2026-08-04 DQAF-20260804-006 事故 3 单:
  m15_swing@10:15 / m30_swing@10:15 / h1_swing@12:10)
- 每策略线: 列出全部投票脑 + 运行时 status + 方向分布 + 置信度唯一值 (退化检测)
- 共识窗口: 提取开单时间点前后最近周期的 consensus_direction/confidence

用法:
  python scripts/audits/_audit_xau_votes_today.py --date 2026-08-04 --data-dir data
"""

import argparse
import json
from collections import defaultdict
from datetime import datetime
from typing import Any

TRADES = [  # (strategy, open_time_utc) — DQAF-20260804-006 事故三单
    ("m15_swing", "2026-08-04T10:15:02"),
    ("m30_swing", "2026-08-04T10:15:22"),
    ("h1_swing", "2026-08-04T12:10:02"),
]


def load_gov(gov_path):
    with open(gov_path, encoding="utf-8") as f:
        g = json.load(f)
    out = {}
    for bid, st in g.get("brain_states", {}).items():
        out[bid] = {
            "status": st.get("status"),
            "vote_weight": st.get("vote_weight", 0.0),
        }
    return out


def load_votes(votes_path):
    rows = []
    with open(votes_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def main(date: str = "2026-08-04", data_dir: str = "data"):
    votes_path = f"{data_dir}/brain_votes/{date}.jsonl"
    gov_path = f"{data_dir}/governance_state.json"
    gov = load_gov(gov_path)
    rows = load_votes(votes_path)
    print(f"[audit] {date} total votes ({data_dir}): {len(rows)}")

    # Per strategy-line: brain stats
    by_line: dict[str, dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        strat = r.get("strategy")
        bid = r.get("brain_id")
        by_line[strat][bid].append(r)

    for strat, brains in sorted(by_line.items()):
        print("\n" + "=" * 92)
        print(f"STRATEGY LINE: {strat}")
        # order brains by governance weight desc
        ordered = sorted(
            brains.items(),
            key=lambda kv: (-gov.get(kv[0], {}).get("vote_weight", 0.0), kv[0]),
        )
        for bid, recs in ordered:
            gs = gov.get(bid, {})
            gw = gs.get("vote_weight", 0.0)
            gst = gs.get("status", "?")
            dirs: dict[str, int] = defaultdict(int)
            confs = set()
            confs_list = []
            for r in recs:
                d = r.get("direction", "?")
                dirs[d] += 1
                c = r.get("confidence")
                if c is not None:
                    confs.add(round(c, 3))
                    confs_list.append(round(c, 3))
            total = len(recs)
            pct_short = 100.0 * dirs.get("short", 0) / total if total else 0.0
            uniq = len(confs)
            conf_range = ""
            if confs_list:
                conf_range = f"[{min(confs_list):.3f}..{max(confs_list):.3f}]"
            flag = "  <<< DEGENERATE (uniq<=3 conf)" if 0 < uniq <= 3 else ""
            print(
                f"  {bid:<28} gov={gst:>9}/{gw:<4} votes={total:>4}  "
                f"L={dirs.get('long',0)} S={dirs.get('short',0)} N={dirs.get('neutral',0)}  "
                f"S%={pct_short:5.1f}  conf_uniq={uniq}{conf_range}{flag}"
            )

    # Consensus near trade open times
    print("\n" + "=" * 92)
    print("TRADE-TIME CONSENSUS WINDOWS")
    for strat, t_open in TRADES:
        t_open_dt = datetime.fromisoformat(t_open)
        # nearest vote row at or before t_open
        best = None
        best_dt = None
        for r in rows:
            if r.get("strategy") != strat:
                continue
            rt = r.get("recorded_at")
            if not rt:
                continue
            dt = datetime.fromisoformat(rt)
            if dt <= t_open_dt:
                if best_dt is None or dt > best_dt:
                    best = r
                    best_dt = dt
        if best:
            print(
                f"  {strat}  open={t_open}  last_consensus@{best.get('recorded_at')}  "
                f"cons_dir={best.get('consensus_direction')}  cons_conf={best.get('consensus_confidence')}"
            )
            # show live/probation brains contributing
            for r in rows:
                if r.get("strategy") != strat or r.get("recorded_at") != best.get("recorded_at"):
                    continue
                gs = gov.get(r.get("brain_id"), {})
                print(
                    f"      {r.get('brain_id'):<28} runtime_status={r.get('brain_status'):>9} "
                    f"gov_w={gs.get('vote_weight', 0.0)}  dir={r.get('direction'):>7}  conf={r.get('confidence')}"
                )
        else:
            print(f"  {strat}  open={t_open}  NO consensus vote found before open")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="XAU brain_votes degeneracy audit")
    parser.add_argument("--date", default="2026-08-04", help="Vote-date to audit (YYYY-MM-DD)")
    parser.add_argument(
        "--data-dir", default="data", help="Asset data dir (data for XAU, data_btc for BTC)"
    )
    args = parser.parse_args()
    main(date=args.date, data_dir=args.data_dir)
