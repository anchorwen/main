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

    return f"""

### Daily Update - {_utc_now_iso()}（自动生成）

- 日期键(UTC): {date_key}
- 运行状态: {run_state}
- 核心统计: 接受={counts.get('accepted', 0)} 拒绝={counts.get('rejected', 0)} 确认={counts.get('acknowledged', 0)} 其他={counts.get('other', 0)} 合计={trade_quality.get('total', 0)} 拒单率={trade_quality.get('rejection_rate', 0.0)}
- 数据质量: 交叉校验问题={dq_issues} outbox超时={outbox_stale}
- live_dispatch_block.flag: {"存在" if flag_present else "不存在"}{ensemble_lines}{align_lines}
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
        age_hours = (datetime.now(UTC).replace(tzinfo=None).timestamp() - mtime) / 3600
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
) -> dict[str, Any]:
    date = date_key or _today_utc_key()
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
        "evolution_plan_update": evolution_result,
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
    p.add_argument("--output", default=None, help="Write JSON report to file")
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
