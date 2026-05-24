"""Generate a daily recap report: journal quality, P&L snapshot, cross-verify, and auto-fill evolution plan.

Run from repo root:
  python scripts/live_daily_recap.py --base-dir data --symbol XAUUSDc
  python scripts/live_daily_recap.py --base-dir data --symbol XAUUSDc --date 2026-04-29 --evolution-plan EVOLUTION_PLAN.md

Produces: data/reports/live_daily_recap_YYYY-MM-DD.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Fix garbled Chinese output on Windows (QO-0015)
if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

SCHEMA_VERSION = "live_daily_recap.v1"


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _today_utc_key() -> str:
    return datetime.now(UTC).replace(tzinfo=None).date().isoformat()


def _lookback_date_key(hours: int = 24) -> str:
    """Return the ISO date key for `hours` ago, so recaps catch all recent trades."""
    from datetime import timedelta

    return (datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=hours)).date().isoformat()


def _run_quality_report(base_dir: Path, date_key: str) -> dict[str, Any]:
    """Call live_data_quality_report.build_report in-process."""
    try:
        from scripts.live_data_quality_report import build_report as build_dq
    except Exception:
        return {"error": "import_dq_failed"}
    try:
        return build_dq(base_dir, date_filter=date_key)
    except Exception as exc:
        return {"error": str(exc)}


def _run_trade_quality_report(journal_path: Path, date_key: str) -> dict[str, Any]:
    """Call trade_quality_report.build_report in-process."""
    try:
        from scripts.trade_quality_report import build_report as build_tq
    except Exception:
        return {"error": "import_tq_failed"}
    try:
        return build_tq(journal_path=str(journal_path), date_key=date_key)
    except Exception as exc:
        return {"error": str(exc)}


def _run_mt5_pnl_snapshot(
    base_dir: Path,
    symbol: str,
    *,
    mt5_terminal_path: str | None = None,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Run mt5_positions_snapshot as a subprocess, return parsed JSON."""
    repo = Path(repo_root or ".")
    terminal = mt5_terminal_path or ""
    if not terminal:
        return {"error": "mt5_terminal_path_not_provided"}
    try:
        outpath = str(base_dir / "reports" / "live_daily_recap_pnl_snapshot.json")
        cmd = [
            sys.executable,
            str(repo / "scripts/mt5_positions_snapshot.py"),
            "--mt5-terminal-path",
            terminal,
            "--symbol",
            symbol,
            "--output",
            outpath,
        ]
        cp = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, timeout=60)
        if cp.returncode == 0 and Path(outpath).exists():
            return json.loads(Path(outpath).read_text(encoding="utf-8"))
        return {"error": f"subprocess_exit_{cp.returncode}", "stderr": cp.stderr[:500]}
    except subprocess.TimeoutExpired:
        return {"error": "pnl_snapshot_timeout"}
    except Exception as exc:
        return {"error": str(exc)}


def _effective_pnl(entry: dict[str, Any]) -> float:
    """Extract realized P&L from a journal entry, falling back to detail.pnl."""
    pnl = entry.get("pnl")
    if pnl is not None:
        return float(pnl)
    detail = entry.get("detail", {})
    if isinstance(detail, dict):
        detail_pnl = detail.get("pnl")
        if detail_pnl is not None:
            return float(detail_pnl)
    return 0.0


def _count_labeled_trades(journal_path: Path) -> dict[str, Any]:
    """Count close entries with real labels (excluding auto-orphan cleanup)."""
    if not journal_path.exists():
        return {"labeled_trades": 0, "label_distribution": {}, "total_pnl": 0.0}

    from collections import Counter

    labeled = []
    for line in journal_path.read_text(encoding="utf-8").strip().split("\n"):
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            e.get("action") == "close"
            and e.get("label")
            and not str(e.get("label", "")).startswith("auto_orphan")
        ):
            labeled.append(e)

    labels = Counter(e.get("label") for e in labeled)
    total_pnl = sum(_effective_pnl(e) for e in labeled)

    return {
        "labeled_trades": len(labeled),
        "label_distribution": dict(labels),
        "total_pnl": round(total_pnl, 2),
    }


def _read_governance_progress(base_dir: Path, threshold: int = 10) -> dict[str, Any]:
    """Read governance state and brain_pnl_ledger, produce per-brain progress toward promotion."""
    result: dict[str, Any] = {
        "brains": [],
        "threshold": threshold,
    }

    # Governance state
    gov_path = base_dir / "governance_state.json"
    gov = {}
    if gov_path.exists():
        try:
            gov = json.loads(gov_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    # Brain P&L ledger (for per-brain trade counts)
    pnl_path = base_dir / "brain_pnl_ledger.json"
    pnl_ledger: dict[str, int] = {}
    if pnl_path.exists():
        try:
            pnl = json.loads(pnl_path.read_text(encoding="utf-8"))
            for bid, outcomes in pnl.get("settled", {}).items():
                pnl_ledger[bid] = len(outcomes)
        except (json.JSONDecodeError, OSError):
            pass

    brain_states = gov.get("brain_states", {})
    if not brain_states:
        return result

    for bid, bs in brain_states.items():
        trade_count = pnl_ledger.get(bid, 0)
        status = bs.get("status", "unknown")
        result["brains"].append(
            {
                "brain_id": bid,
                "status": status,
                "trade_count": trade_count,
                "remaining": max(0, threshold - trade_count),
                "fraction": min(1.0, trade_count / threshold) if threshold > 0 else 1.0,
            }
        )

    return result


def _read_contract_group_summary(base_dir: Path) -> dict[str, Any]:
    """Aggregate brain P&L metrics by contract group.

    Maps each brain_id to its contract group (barrier_12bar / micro_3bar /
    statarb_dynamic) via contract_groups.py, then computes per-group:
    brain_count, total_signals, avg_pnl_per_unit, win_rate, sharpe estimate.
    """
    result: dict[str, Any] = {
        "groups": {},
        "total_brains": 0,
        "total_signals": 0,
    }

    from core.parliament.contract_groups import get_group_for_brain_type

    # Load brain registry entries to get brain_type → brain_id mapping
    brains_dir = Path("configs/brains")
    brain_id_to_type: dict[str, str] = {}
    if brains_dir.exists():
        for f in brains_dir.glob("*.json"):
            if "normalization" in f.name:
                continue
            try:
                entry = json.loads(f.read_text(encoding="utf-8"))
                brain_id_to_type[entry["brain_id"]] = entry.get("brain_type", "")
            except (json.JSONDecodeError, OSError, KeyError):
                pass

    # Load P&L ledger
    pnl_path = base_dir / "brain_pnl_ledger.json"
    if not pnl_path.exists():
        return result

    try:
        pnl = json.loads(pnl_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return result

    settled = pnl.get("settled", {})

    # Aggregate per group
    groups: dict[str, dict[str, Any]] = {}
    for bid, outcomes in settled.items():
        bt = brain_id_to_type.get(bid, "")
        group = get_group_for_brain_type(bt) if bt else None
        gname = group["name"] if group else "unassigned"

        if gname not in groups:
            groups[gname] = {
                "group_name": gname,
                "contract": group["contract"] if group else "",
                "brain_count": 0,
                "total_signals": 0,
                "total_pnl": 0.0,
                "win_count": 0,
                "loss_count": 0,
                "brain_ids": [],
            }

        g = groups[gname]
        g["brain_count"] += 1
        g["brain_ids"].append(bid)
        pnls = [o.get("pnl_per_unit", 0.0) for o in outcomes]
        g["total_signals"] += len(pnls)
        g["total_pnl"] += sum(pnls)
        g["win_count"] += sum(1 for p in pnls if p > 0)
        g["loss_count"] += sum(1 for p in pnls if p < 0)

    # Compute derived metrics
    for _gname, g in groups.items():
        n = g["total_signals"]
        g["avg_pnl"] = round(g["total_pnl"] / n, 6) if n > 0 else 0.0
        g["win_rate"] = round(g["win_count"] / n, 4) if n > 0 else 0.0
        # Simple Sharpe estimate (annualised)
        if n > 1:
            import math

            avg = g["avg_pnl"]
            variance = sum(
                (o.get("pnl_per_unit", 0.0) - avg) ** 2
                for bid in g["brain_ids"]
                for o in settled.get(bid, [])
            ) / (n - 1)
            std = math.sqrt(variance) if variance > 1e-12 else 1e-12
            g["sharpe_estimate"] = round((avg / std) * math.sqrt(288 * 252), 2)
        else:
            g["sharpe_estimate"] = 0.0
        # Directional agreement: fraction of signals in dominant direction
        long_count = sum(
            1
            for bid in g["brain_ids"]
            for o in settled.get(bid, [])
            if o.get("direction") == "long"
        )
        short_count = sum(
            1
            for bid in g["brain_ids"]
            for o in settled.get(bid, [])
            if o.get("direction") == "short"
        )
        g["long_pct"] = round(long_count / n, 3) if n > 0 else 0.0
        g["short_pct"] = round(short_count / n, 3) if n > 0 else 0.0
        result["total_brains"] += g["brain_count"]
        result["total_signals"] += n

    result["groups"] = groups
    return result


def _format_progress_bar(fraction: float, width: int = 10) -> str:
    """Render a single progress bar segment: ████░░░░░░."""
    filled = round(fraction * width)
    return "█" * filled + "░" * (width - filled)


def _derive_run_state(
    trade_quality: dict[str, Any],
    data_quality: dict[str, Any],
    flag_present: bool,
) -> str:
    """Determine run state from the day's numbers."""
    tq_total = trade_quality.get("total", 0)
    tq_accepted = trade_quality.get("counts", {}).get("accepted", 0)
    tq_rejected = trade_quality.get("counts", {}).get("rejected", 0)
    tq_rejection_rate = trade_quality.get("rejection_rate", 0.0)
    dq_issues = data_quality.get("summary", {}).get("issues_count", 0)

    if flag_present:
        return "阻断（保护旗标存在）"
    if tq_total == 0:
        return "静默（当日无交易记录）"
    if tq_rejected > 0 and tq_accepted == 0 and tq_total >= 2:
        return "告警（当日全部拒绝）"
    if tq_rejection_rate >= 0.5 and tq_total >= 3:
        return "告警（拒单率 >= 50%）"
    if dq_issues > 10:
        return "需关注（数据质量异常较多）"
    if tq_accepted > 0:
        return "活跃（有成交）"
    return "待定"


def _generate_evolution_block(
    date_key: str,
    run_state: str,
    trade_quality: dict[str, Any],
    data_quality: dict[str, Any],
    flag_present: bool,
    *,
    shadow_ensemble: dict[str, Any] | None = None,
    eval_alignment: dict[str, Any] | None = None,
    brain_leaderboard: dict[str, Any] | None = None,
    feature_quality: dict[str, Any] | None = None,
    governance: dict[str, Any] | None = None,
    champion_challenger: dict[str, Any] | None = None,
    labeled_trades: dict[str, Any] | None = None,
    governance_progress: dict[str, Any] | None = None,
    brain_attribution: dict[str, Any] | None = None,
    contract_group_summary: dict[str, Any] | None = None,
) -> str:
    """Produce the Markdown block to append to EVOLUTION_PLAN.md."""
    counts = trade_quality.get("counts", {})
    dq_issues = data_quality.get("summary", {}).get("issues_count", 0)
    outbox_stale = data_quality.get("outbox_staleness", {}).get("stale_count", 0)

    ensemble_lines = ""
    if shadow_ensemble and shadow_ensemble.get("total_brains", 0) > 0:
        comparison = shadow_ensemble.get("comparison", {})
        consensus = comparison.get("consensus", "unknown")
        agreement = comparison.get("agreement_score", 0.0)
        n_brains = comparison.get("total_brains", 0)
        ensemble_lines = f"\n- 多模型共识: {consensus} (一致性={agreement:.0%}, 参与={n_brains})"

    align_lines = ""
    if eval_alignment and "error" not in eval_alignment:
        live_m = eval_alignment.get("live_metrics", {})
        bt_m = eval_alignment.get("backtest_metrics", {})
        a = eval_alignment.get("alignment", {})
        align_lines = (
            f"\n- 线上线下对齐: {a.get('severity', '?')}"
            f" | 实盘胜率={live_m.get('win_rate', '?')}"
            f" 回测胜率={bt_m.get('win_rate', '?')}"
            f" | 问题={a.get('issues', [])}"
        )

    leaderboard_lines = ""
    if brain_leaderboard and "error" not in brain_leaderboard:
        lb = brain_leaderboard.get("leaderboard", [])
        total_brains = brain_leaderboard.get("total_brains", 0)
        if lb:
            top = lb[0]
            leaderboard_lines = (
                f"\n- Brain 排行: 共{total_brains}个 | "
                f"Top1={top['brain_id']}(信号={top['signal_count']})"
            )
            if len(lb) > 1:
                leaderboard_lines += f" | Top2={lb[1]['brain_id']}(信号={lb[1]['signal_count']})"

    feature_quality_lines = ""
    if feature_quality and "error" not in feature_quality:
        shift = feature_quality.get("distribution_shift", {})
        shifted = shift.get("shifted_count", 0)
        if shifted > 0:
            feature_quality_lines = f"\n- 特征偏移: {shifted}个特征偏离基线 >2σ"

    governance_lines = ""
    if governance and governance.get("status") == "ok":
        applied = governance.get("actions_applied", 0)
        flagged = governance.get("actions_flagged", 0)
        if applied > 0 or flagged > 0:
            governance_lines = f"\n- 治理动作: 自动={applied} 待确认={flagged}"

    champion_lines = ""
    if champion_challenger and champion_challenger.get("status") == "ok":
        promotions = champion_challenger.get("promotions", 0)
        eligible = champion_challenger.get("eligible", 0)
        if promotions > 0 or eligible > 0:
            champion_lines = f"\n- 晋升评估: 已晋升={promotions} 符合条件={eligible}"

    labeled_lines = ""
    if labeled_trades:
        lt_count = labeled_trades.get("labeled_trades", 0)
        lt_pnl = labeled_trades.get("total_pnl", 0.0)
        lt_dist = labeled_trades.get("label_distribution", {})
        labeled_lines = f"\n- 已标注交易: {lt_count}笔 | 总P&L={lt_pnl:+.2f} | 分布={lt_dist}"

    gov_progress_lines = ""
    if governance_progress and governance_progress.get("brains"):
        bar_parts = []
        threshold = governance_progress.get("threshold", 10)
        for b in governance_progress["brains"]:
            bar = _format_progress_bar(b["fraction"])
            bar_parts.append(
                f"{b['brain_id'][:15]}: {bar} {b['trade_count']}/{threshold}  ({b['status']})"
            )
        gov_progress_lines = "\n- 治理晋升进度:\n  " + "\n  ".join(bar_parts)

    contract_group_lines = ""
    if contract_group_summary and contract_group_summary.get("groups"):
        cg_parts = []
        for gname, g in sorted(contract_group_summary["groups"].items()):
            direction_hint = (
                f"做多{g['long_pct']:.0%}/做空{g['short_pct']:.0%}"
                if g["total_signals"] > 0
                else "无信号"
            )
            cg_parts.append(
                f"{gname}: {g['brain_count']}脑 {g['total_signals']}信号 "
                f"胜率={g['win_rate']:.1%} 均值={g['avg_pnl']:.4f} "
                f"Sharpe≈{g['sharpe_estimate']:.1f} [{direction_hint}]"
            )
        contract_group_lines = "\n- 合同组表现:\n  " + "\n  ".join(cg_parts)

    attr_lines = ""
    if brain_attribution and brain_attribution.get("brains"):
        parts = [f"{bid}: {info}" for bid, info in brain_attribution["brains"].items()]
        attr_lines = (
            f"\n- 大脑归因P&L: {brain_attribution.get('total_realized_pnl', 0):+.2f} | "
            + " | ".join(parts)
        )
        unatt = brain_attribution.get("unattributed_trades", 0)
        if unatt > 0:
            attr_lines += f" | 未归因交易: {unatt}笔"

    return f"""

### Daily Update - {_utc_now_iso()}（自动生成）

- 日期键(UTC): {date_key}
- 运行状态: {run_state}
- 核心统计: 接受={counts.get('accepted', 0)} 拒绝={counts.get('rejected', 0)} 确认={counts.get('acknowledged', 0)} 其他={counts.get('other', 0)} 合计={trade_quality.get('total', 0)} 拒单率={trade_quality.get('rejection_rate', 0.0)}
- 数据质量: 交叉校验问题={dq_issues} outbox超时={outbox_stale}
- live_dispatch_block.flag: {"存在" if flag_present else "不存在"}{labeled_lines}{attr_lines}{gov_progress_lines}{contract_group_lines}{ensemble_lines}{align_lines}{leaderboard_lines}{feature_quality_lines}{governance_lines}{champion_lines}
- 关键事件: <手动最多 3 条>
- 根因与修复: <手动最多 3 条>
- 阶段进度: <Phase A/B/C 到达位置>
- 明日唯一优先事项: <1-3 条>
"""


def _run_shadow_compare(
    base_dir: Path,
    symbol: str,
    date_key: str,
    journal_path: Path,
) -> dict[str, Any]:
    """Call shadow_live_compare_report.build_report_payload in-process."""
    try:
        from scripts.shadow_live_compare_report import build_report_payload as build_shadow
    except Exception:
        return {"error": "import_shadow_compare_failed"}
    try:
        return build_shadow(
            date_key=date_key,
            symbol=symbol,
            journal_path=str(journal_path),
            shadow_baseline_json=None,
            base_dir=base_dir,
        )
    except Exception as exc:
        return {"error": str(exc)}


def _run_shadow_ensemble(
    brains_dir: Path,
    *,
    brain_ids: list[str] | None = None,
    feature_dim: int = 40,
) -> dict[str, Any]:
    """Call live_shadow_ensemble.build_report in-process (parallel multi-model inference)."""
    try:
        from scripts.live_shadow_ensemble import build_report as build_ensemble
    except Exception:
        return {"error": "import_shadow_ensemble_failed"}
    try:
        return build_ensemble(
            brains_dir=brains_dir,
            brain_ids=brain_ids,
            feature_dim=feature_dim,
            parallel=True,
        )
    except Exception as exc:
        return {"error": str(exc)}


def _run_feature_quality(
    store_dir: Path,
    norm_config_path: Path,
    *,
    symbol: str = "XAUUSD",
    date_filter: str | None = None,
) -> dict[str, Any]:
    """Call live_feature_quality_report.build_report in-process."""
    try:
        from scripts.live_feature_quality_report import build_report as build_fq
    except Exception:
        return {"error": "import_feature_quality_failed"}
    try:
        return build_fq(
            store_dir=store_dir,
            norm_config_path=norm_config_path,
            symbol=symbol,
            date_filter=date_filter,
        )
    except Exception as exc:
        return {"error": str(exc)}


def _run_eval_alignment(
    labels_path: Path,
    backtest_path: Path,
) -> dict[str, Any]:
    """Call eval_alignment.build_report in-process (live P&L vs backtest)."""
    try:
        from scripts.training.eval_alignment import build_report as build_align
    except Exception:
        return {"error": "import_eval_alignment_failed"}
    try:
        return build_align(labels_path, backtest_path)
    except Exception as exc:
        return {"error": str(exc)}


def _run_brain_leaderboard(
    decisions_dir: Path,
    *,
    date_filter: str | None = None,
    labels_path: Path | None = None,
) -> dict[str, Any]:
    """Call brain_leaderboard.build_report in-process."""
    try:
        from scripts.training.brain_leaderboard import build_report as build_bl
    except Exception:
        return {"error": "import_brain_leaderboard_failed"}
    try:
        return build_bl(decisions_dir, date_filter=date_filter, labels_path=labels_path)
    except Exception as exc:
        return {"error": str(exc)}


def _run_pnl_leaderboard(base_dir: Path) -> dict[str, Any]:
    """Generate PnL-based leaderboard using BrainPnLStore + GovernanceService."""
    try:
        from core.brains.services.brain_leaderboard import BrainLeaderboard
        from core.feedback.brain_pnl_ledger import BrainPnLStore
        from core.governance.governance_service import GovernanceService

        pnl_path = base_dir / "brain_pnl_ledger.json"
        gov_path = base_dir / "governance_state.json"

        if not pnl_path.exists():
            return {"error": "no_pnl_ledger", "path": str(pnl_path)}

        pnl_store = BrainPnLStore.load(pnl_path)
        governance = GovernanceService.load(gov_path) if gov_path.exists() else GovernanceService()

        lb = BrainLeaderboard()
        rankings = lb.rank(
            pnl_store.get_all_metrics(),
            governance_states=governance.get_all_states(),
        )
        return {
            "schema_version": "pnl_leaderboard.v1",
            "generated_at": _utc_now_iso(),
            "total_brains": len(rankings),
            "rankings": lb.to_records(rankings),
            "table": lb.format_table(rankings),
        }
    except Exception as exc:
        return {"error": str(exc)[:500]}


def _run_governance_snapshot(base_dir: Path, *, dry_run: bool = True) -> dict[str, Any]:
    """Run governance cycle from persisted tracker data and return summary."""
    try:
        from scripts.daily_ops import _step_governance

        return _step_governance(str(base_dir), dry_run=dry_run)
    except Exception as exc:
        return {"step": "governance", "status": "error", "error": str(exc)[:500]}


def _run_champion_challenger_snapshot(base_dir: Path, *, dry_run: bool = True) -> dict[str, Any]:
    """Run champion/challenger from persisted tracker data and return summary."""
    try:
        from scripts.daily_ops import _step_champion_challenger

        return _step_champion_challenger(str(base_dir), dry_run=dry_run)
    except Exception as exc:
        return {"step": "champion_challenger", "status": "error", "error": str(exc)[:500]}


def _write_evolution_plan_update(
    plan_path: Path,
    block_content: str,
    backup_threshold_hours: int = 24,
) -> dict[str, Any]:
    """Append daily update block to EVOLUTION_PLAN.md, with optional backup."""
    result: dict[str, Any] = {
        "plan_exists": plan_path.exists(),
        "backup_created": False,
        "backup_path": None,
        "block_appended": False,
    }
    if not plan_path.exists():
        result["note"] = "plan_file_not_found"
        return result

    try:
        # Optional backup
        mtime = plan_path.stat().st_mtime
        age_hours = (datetime.now(UTC).timestamp() - mtime) / 3600
        if age_hours > backup_threshold_hours:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = plan_path.parent / f"EVOLUTION_PLAN.backup.{stamp}.md"
            backup_path.write_text(plan_path.read_text(encoding="utf-8"), encoding="utf-8")
            result["backup_created"] = True
            result["backup_path"] = str(backup_path)

        # Append block
        with plan_path.open("a", encoding="utf-8") as f:
            f.write(block_content)
        result["block_appended"] = True
    except OSError as exc:
        result["error"] = str(exc)
    return result


def build_report(
    base_dir: Path,
    symbol: str,
    *,
    date_key: str | None = None,
    mt5_terminal_path: str | None = None,
    repo_root: str | None = None,
    evolution_plan_path: str | None = None,
    backup_threshold_hours: int = 24,
    brains_dir: Path | None = None,
    feature_store_dir: Path | None = None,
    norm_config_path: Path | None = None,
    labels_path: Path | None = None,
    backtest_path: Path | None = None,
    decisions_dir: Path | None = None,
    build_dataset_flag: bool = False,
    run_governance: bool = False,
    run_champion: bool = False,
) -> dict[str, Any]:
    date = date_key or _lookback_date_key(hours=24)
    journal_path = base_dir / "live_trade_journal.jsonl"
    flag_path = base_dir / "live_dispatch_block.flag"

    flag_present = flag_path.exists()
    flag_payload: dict[str, Any] = {}
    if flag_present:
        try:
            flag_payload = json.loads(flag_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    data_quality = _run_quality_report(base_dir, date_key=date)
    trade_quality = _run_trade_quality_report(journal_path, date_key=date)
    pnl = _run_mt5_pnl_snapshot(
        base_dir,
        symbol,
        mt5_terminal_path=mt5_terminal_path,
        repo_root=repo_root,
    )
    shadow_compare = _run_shadow_compare(base_dir, symbol, date, journal_path)

    shadow_ensemble: dict[str, Any] = {}
    if brains_dir:
        shadow_ensemble = _run_shadow_ensemble(brains_dir)

    feature_quality: dict[str, Any] = {}
    if feature_store_dir and norm_config_path:
        feature_quality = _run_feature_quality(
            feature_store_dir, norm_config_path, symbol=symbol, date_filter=date
        )

    eval_alignment: dict[str, Any] = {}
    if labels_path and backtest_path:
        eval_alignment = _run_eval_alignment(labels_path, backtest_path)

    brain_leaderboard: dict[str, Any] = {}
    if decisions_dir:
        brain_leaderboard = _run_brain_leaderboard(
            decisions_dir, date_filter=date, labels_path=labels_path
        )

    # ── PnL-based leaderboard (from BrainPnLStore) ──
    pnl_leaderboard = _run_pnl_leaderboard(base_dir)

    # ── Governance & Champion/Challenger snapshots (Phase C) ──
    governance: dict[str, Any] = {}
    if run_governance:
        governance = _run_governance_snapshot(base_dir, dry_run=True)

    champion: dict[str, Any] = {}
    if run_champion:
        champion = _run_champion_challenger_snapshot(base_dir, dry_run=True)

    # ── Labeled trades & governance progress ──
    labeled_trades = _count_labeled_trades(journal_path)
    governance_progress = _read_governance_progress(base_dir, threshold=10)

    # ── Contract group summary ──
    contract_group_summary = _read_contract_group_summary(base_dir)

    # ── Brain attribution ──
    brain_attribution: dict[str, Any] = {}
    try:
        from core.brains.services.brain_attribution_service import BrainAttributionService

        pnl_ledger_path = base_dir / "brain_pnl_ledger.json"
        attr_svc = BrainAttributionService(
            journal_path=journal_path,
            pnl_ledger_path=pnl_ledger_path if pnl_ledger_path.exists() else None,
        )
        brain_attribution = attr_svc.quick_summary()
    except Exception:
        pass

    # ── Build training dataset (Phase B) ──
    training_dataset: dict[str, Any] = {}
    if build_dataset_flag:
        try:
            from scripts.training.dataset_builder import build_dataset

            labels_path_for_dataset = base_dir / "reports" / "live_labels.jsonl"
            training_dataset = build_dataset(
                labels_path=labels_path_for_dataset,
                feature_store_dir=base_dir / "feature_store",
                output_dir=base_dir / "training",
                symbol=symbol.rstrip("c"),
            )
        except Exception as exc:
            training_dataset = {"error": str(exc)[:500]}

    run_state = _derive_run_state(trade_quality, data_quality, flag_present)

    evolution_result: dict[str, Any] = {}
    if evolution_plan_path:
        block = _generate_evolution_block(
            date,
            run_state,
            trade_quality,
            data_quality,
            flag_present,
            shadow_ensemble=shadow_ensemble if shadow_ensemble else None,
            eval_alignment=eval_alignment if eval_alignment else None,
            brain_leaderboard=brain_leaderboard if brain_leaderboard else None,
            feature_quality=feature_quality if feature_quality else None,
            governance=governance if governance else None,
            champion_challenger=champion if champion else None,
            labeled_trades=labeled_trades,
            governance_progress=governance_progress,
            brain_attribution=brain_attribution if brain_attribution else None,
            contract_group_summary=contract_group_summary if contract_group_summary else None,
        )
        evolution_result = _write_evolution_plan_update(
            Path(evolution_plan_path),
            block,
            backup_threshold_hours=backup_threshold_hours,
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "date_key": date,
        "base_dir": str(base_dir.resolve()),
        "symbol": symbol,
        "run_state": run_state,
        "flag_present": flag_present,
        "flag_payload": flag_payload,
        "trade_quality": trade_quality,
        "data_quality": data_quality,
        "pnl_snapshot": pnl,
        "shadow_compare": shadow_compare,
        "shadow_ensemble": shadow_ensemble,
        "feature_quality": feature_quality,
        "eval_alignment": eval_alignment,
        "brain_leaderboard": brain_leaderboard,
        "pnl_leaderboard": pnl_leaderboard,
        "governance": governance,
        "champion_challenger": champion,
        "labeled_trades": labeled_trades,
        "governance_progress": governance_progress,
        "brain_attribution": brain_attribution,
        "contract_group_summary": contract_group_summary,
        "evolution_plan_update": evolution_result,
        "training_dataset": training_dataset,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="live_daily_recap")
    p.add_argument("--base-dir", default="data")
    p.add_argument("--symbol", default="XAUUSDc")
    p.add_argument("--date", default=None, help="UTC date key; default = today")
    p.add_argument("--mt5-terminal-path", default=None)
    p.add_argument("--repo-root", default=None)
    p.add_argument("--evolution-plan", default=None, help="Path to EVOLUTION_PLAN.md")
    p.add_argument("--backup-threshold-hours", type=int, default=24)
    p.add_argument(
        "--brains-dir", type=Path, default=None, help="Brains config dir for shadow ensemble"
    )
    p.add_argument(
        "--feature-store-dir", type=Path, default=None, help="Feature store dir for quality report"
    )
    p.add_argument(
        "--norm-config",
        type=Path,
        default=Path("configs/brains/v9_institutional_01.normalization.json"),
        help="Normalization config for feature quality",
    )
    p.add_argument(
        "--labels-path",
        type=Path,
        default=None,
        help="Training labels JSONL for eval alignment",
    )
    p.add_argument(
        "--backtest-path",
        type=Path,
        default=None,
        help="Backtest result.json for eval alignment",
    )
    p.add_argument(
        "--decisions-dir",
        type=Path,
        default=None,
        help="Decisions dir for brain leaderboard",
    )
    p.add_argument("--output", default=None, help="Write JSON report to file")
    p.add_argument(
        "--build-dataset",
        action="store_true",
        help="Build training dataset from labels and feature store after recap",
    )
    p.add_argument(
        "--run-governance",
        action="store_true",
        help="Run governance snapshot (loads persisted tracker state)",
    )
    p.add_argument(
        "--run-champion",
        action="store_true",
        help="Run champion/challenger snapshot (loads persisted tracker state)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = Path(args.base_dir)
    report = build_report(
        base_dir=base,
        symbol=args.symbol,
        date_key=args.date,
        mt5_terminal_path=args.mt5_terminal_path,
        repo_root=args.repo_root,
        evolution_plan_path=args.evolution_plan,
        backup_threshold_hours=args.backup_threshold_hours,
        brains_dir=args.brains_dir,
        feature_store_dir=args.feature_store_dir,
        norm_config_path=args.norm_config,
        labels_path=args.labels_path,
        backtest_path=args.backtest_path,
        decisions_dir=args.decisions_dir,
        build_dataset_flag=args.build_dataset,
        run_governance=args.run_governance,
        run_champion=args.run_champion,
    )
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(text)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
