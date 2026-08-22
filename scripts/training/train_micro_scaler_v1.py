#!/usr/bin/env python3
"""P3 XAU Micro Scaler V1 — Option A (真实标签基线) + Option B (伪标签敏感性诊断).

IC 雷霆开火令 (2026-08-23, DQAF-AUDIT-20260823-P3): 双轨并行, 放弃 Option C.
- Option A (--mode baseline): 576 条 current-gen 真实标签 → LightGBM 纪律化训练
  (max_depth<=3 / min_child_samples>=20 / num_leaves<=8), 60/20/20 Time-Series
  Split + Purge/Embargo (±--purge-minutes 默认 300min = 60 M5 bars), 输出
  PR-AUC / MCC / OOS Spearman rho / 校准斜率评估报告.
- Option B (--mode pseudo-diagnostic): 8,572 条 current-gen 特征 → 前向收益
  (forward-return) 伪标签弱监督诊断, 验证 M5 微观特征是否具备信息熵
  (feature importance + OOS signal 存在性).

设计约束 (架构裁决):
1. **Schema 世代过滤** (TECH_DEBT-023 The Schema Mutagenesis): 只用 current-gen
   列族 (含 *_Price_ZScore, 无 *_Macro_Gold_Silver_Spread) — 与 live 特征引擎
   列对齐, 旧世代行零 impute/桥接 (宁可样本少, 不要 train/serve skew).
2. **PIT 语义 SSOT 复用** (Iron Law #1.1): ASOF join + knowledge-time 直接复用
   `build_btc_metafilter_v2_dataset.py` — 不散布, 不重写.
3. **无实盘代码触碰** (Shadow Mandate): 只产模型工件 + 报告, 不写 brain config,
   不碰 live_*.yaml / governance. 通过 Shadow 门禁后才获实盘资格.

用法:
  python scripts/training/train_micro_scaler_v1.py --mode baseline
  python scripts/training/train_micro_scaler_v1.py --mode pseudo-diagnostic
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

# sklearn 1.8 / lightgbm 4.6 ndarray-vs-names 化妆品警告 (结果零影响)
warnings.filterwarnings("ignore", message="X does not have valid feature names.*")

# ── Repo-root bootstrap (scripts/training/ 直接运行) ──
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import lightgbm as lgb  # noqa: E402
from sklearn.metrics import (  # noqa: E402
    average_precision_score,
    brier_score_loss,
    matthews_corrcoef,
)

# PIT 语义 SSOT 复用 (Iron Law #1.1 — 统一扩展点, 不散布)
from scripts.build_btc_metafilter_v2_dataset import (  # noqa: E402
    apply_labels,
    asof_join,
    load_contract_feature_names,
    load_feature_store,
)
from core.training.utils import spearman_rho  # noqa: E402

# ── TECH_DEBT-023 Schema 世代标记 ──
_CUR_GEN_HAVE = "H1_Price_ZScore"
_OLD_GEN_HAVE = "H1_Macro_Gold_Silver_Spread"

# IC 批准的克制参数 (576 x 40 微型数据集)
LGB_PARAMS: dict[str, Any] = {
    "objective": "binary",
    "n_estimators": 500,
    "learning_rate": 0.03,
    "max_depth": 3,
    "num_leaves": 8,
    "min_child_samples": 20,
    "feature_fraction": 0.6,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "random_state": 42,
    "verbosity": -1,
}


def _parse_dt(s: str | None) -> datetime | None:
    """Builder 同款解析: [:26] 截断 + naive→UTC (PIT SSOT 语义一致)."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s)[:26])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


def filter_current_gen(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """TECH_DEBT-023 规避: 仅保留与 live 列对齐的 current-gen 特征行."""
    out: list[dict[str, Any]] = []
    for f in features:
        keys = set(f.get("values", {}).keys())
        if _CUR_GEN_HAVE in keys and _OLD_GEN_HAVE not in keys:
            out.append(f)
    return out


def ts_purged_split(
    times: list[datetime],
    ratios: tuple[float, float, float] = (0.60, 0.20, 0.20),
    purge_minutes: int = 300,
) -> tuple[list[int], list[int], list[int], list[int]]:
    """60/20/20 Time-Series Split + Purge/Embargo (时序泄漏零容忍).

    按 open_time 升序切分; 每个分割边界两侧 ±purge_minutes 内的样本从相邻
    分区剔除 (IC 规定 ±60 M5 bars = 300min). 返回 (train_idx, val_idx,
    test_idx, dropped_idx). 不 shuffle.
    """
    n = len(times)
    if n == 0:
        return [], [], [], []
    n_train = int(n * ratios[0])
    n_val = int(n * ratios[1])
    if n_train <= 0 or n_val <= 0 or n - n_train - n_val <= 0:
        raise ValueError(f"split ratios produce empty partition (n={n})")
    b1 = times[n_train - 1]
    b2 = times[n_train + n_val - 1]
    win = timedelta(minutes=purge_minutes)

    def _near(t: datetime, boundary: datetime) -> bool:
        return abs((t - boundary).total_seconds()) <= win.total_seconds()

    train = list(range(0, n_train))
    val = list(range(n_train, n_train + n_val))
    test = list(range(n_train + n_val, n))

    dropped: list[int] = []
    for i in list(train):
        if _near(times[i], b1):
            train.remove(i)
            dropped.append(i)
    for i in list(val):
        if _near(times[i], b1) or _near(times[i], b2):
            val.remove(i)
            dropped.append(i)
    for i in list(test):
        if _near(times[i], b2):
            test.remove(i)
            dropped.append(i)
    return train, val, test, dropped


def evaluate(y_true: np.ndarray, proba: np.ndarray, label: str) -> dict[str, Any]:
    """单分区评估: PR-AUC / MCC / OOS Spearman rho / 校准斜率 / Brier."""
    n = len(y_true)
    n_pos = int(np.sum(y_true))
    base_rate = n_pos / max(n, 1) if n else float("nan")
    if n_pos > 0 and n_pos < n:
        pr_auc = float(average_precision_score(y_true, proba))
        mcc = float(matthews_corrcoef(y_true, (proba >= 0.5).astype(int)))
        brier = float(brier_score_loss(y_true, proba))
        rho = float(spearman_rho(proba, y_true))
    else:
        pr_auc = mcc = brier = rho = float("nan")
    fit = np.polyfit(proba, y_true, 1) if n >= 2 else np.array([float("nan"), float("nan")])
    slope = float(fit[0])
    sign_match = float(np.mean(((proba >= 0.5).astype(int)) == y_true))
    pred_pos_mean = float(proba[y_true == 1].mean()) if n_pos else float("nan")
    pred_neg_mean = float(proba[y_true == 0].mean()) if n - n_pos else float("nan")
    res: dict[str, Any] = {
        "partition": label,
        "n": n,
        "n_pos": n_pos,
        "base_rate": round(base_rate, 4),
        "pr_auc": round(pr_auc, 4),
        "mcc": round(mcc, 4),
        "spearman_rho": round(rho, 4),
        "sign_match_rate": round(sign_match, 4),
        "brier": round(brier, 4),
        "calib_slope": round(slope, 4),
        "pred_mean_pos": round(pred_pos_mean, 4),
        "pred_mean_neg": round(pred_neg_mean, 4),
    }
    return res


def _print_eval(res: dict[str, Any]) -> None:
    print(
        f"    [{res['partition']:<6}] n={res['n']:<4} pos={res['n_pos']:<4} "
        f"base={res['base_rate']:.3f} | PR-AUC={res['pr_auc']:.3f} "
        f"MCC={res['mcc']:.3f} rho={res['spearman_rho']:.3f} "
        f"sign={res['sign_match_rate']:.3f} brier={res['brier']:.3f} "
        f"slope={res['calib_slope']:.3f}"
    )


def build_xy(
    data_dir: str, symbol: str, max_lookback_minutes: int
) -> tuple[np.ndarray, np.ndarray, list[dict[str, Any]], list[str], list[datetime]]:
    """Option A 数据装配: current-gen 过滤 → ASOF join → 二分类标签.

    复用权威 builder (PIT SSOT). 返回 (X, y, meta, feature_names, open_dts).
    """
    from scripts.build_btc_metafilter_v2_dataset import load_journal_opens

    trades = load_journal_opens(data_dir)
    if not trades:
        raise RuntimeError(f"no open journal entries in {data_dir}/live_trade_journal.jsonl")
    feats = load_feature_store(data_dir, symbol, feature_contract="v9_institutional_40")
    cur = filter_current_gen(feats)
    names = load_contract_feature_names(data_dir, feature_contract="v9_institutional_40")
    if not names:
        raise RuntimeError("no contract feature names available")
    X, y_pnl, meta, join_stats = asof_join(
        trades, cur, names, max_lookback_seconds=max_lookback_minutes * 60
    )
    if len(X) == 0:
        raise RuntimeError("ASOF join produced 0 current-gen samples")
    y = apply_labels(y_pnl, spread_cost=0.0, threshold_mult=1.5)
    open_dts: list[datetime] = []
    for m in meta:
        dt = _parse_dt(m.get("open_time"))
        open_dts.append(dt if dt is not None else datetime(2000, 1, 1, tzinfo=UTC))
    print(f"  [build_xy] ASOF join stats: {join_stats}")
    return X.astype(np.float32), y.astype(np.int32), meta, names, open_dts


def run_baseline(args: argparse.Namespace) -> int:
    """Option A — 真实标签 LightGBM 基线 (主轨道)."""
    print("=" * 78)
    print("  P3 XAU MICRO SCALER V1 — Option A (真实标签 LightGBM 基线)")
    print("=" * 78)
    X, y, meta, names, open_dts = build_xy(args.data_dir, args.symbol, args.max_lookback_minutes)
    n_total = len(y)
    n_win = int(np.sum(y))
    print(
        f"  current-gen labeled samples : {n_total} (win={n_win}, WR={n_win / n_total * 100:.1f}%)"
    )
    n_long = sum(1 for m in meta if m.get("side") == "long")
    n_short = sum(1 for m in meta if m.get("side") == "short")
    print(f"  direction balance          : LONG={n_long} SHORT={n_short}")

    # ── Time-Series Split + Purge/Embargo ──
    order = np.argsort([t.timestamp() for t in open_dts])
    X_s = X[order]
    y_s = y[order]
    times_sorted = [open_dts[i] for i in order]
    train_idx, val_idx, test_idx, dropped = ts_purged_split(
        times_sorted, ratios=(0.60, 0.20, 0.20), purge_minutes=args.purge_minutes
    )
    print(
        f"  TS split (60/20/20, purge±{args.purge_minutes}min): "
        f"train={len(train_idx)} val={len(val_idx)} test={len(test_idx)} "
        f"purged={len(dropped)}"
    )
    if len(test_idx) < 20:
        print("  WARNING: OOS test set < 20 samples — rho/PR-AUC 置信区间极宽, 结论仅供方向参考")

    Xtr, ytr = X_s[train_idx], y_s[train_idx]
    Xva, yva = X_s[val_idx], y_s[val_idx]
    Xte, yte = X_s[test_idx], y_s[test_idx]

    # ── LightGBM 纪律化训练 (scale_pos_weight = train 类别比) ──
    n_neg_tr = int(np.sum(ytr == 0))
    n_pos_tr = int(np.sum(ytr == 1))
    params = dict(LGB_PARAMS)
    params["scale_pos_weight"] = round(n_neg_tr / max(n_pos_tr, 1), 3)
    print(
        f"  LGB params: depth={params['max_depth']} min_child={params['min_child_samples']} "
        f"leaves={params['num_leaves']} lr={params['learning_rate']} "
        f"scale_pos_weight={params['scale_pos_weight']}"
    )
    model = lgb.LGBMClassifier(**params)
    model.fit(
        Xtr,
        ytr,
        eval_set=[(Xva, yva)],
        eval_metric="binary_logloss",
        callbacks=[lgb.early_stopping(stopping_rounds=50, verbose=False)],
    )
    print(f"  best_iteration = {model.best_iteration_}")

    # ── 三区评估 ──
    proba_tr = model.predict_proba(Xtr)[:, 1]
    proba_va = model.predict_proba(Xva)[:, 1]
    proba_te = model.predict_proba(Xte)[:, 1]
    evals = [
        evaluate(ytr, proba_tr, "train"),
        evaluate(yva, proba_va, "val"),
        evaluate(yte, proba_te, "OOS-test"),
    ]
    print("\n  Evaluation (唯一合法证据源 = 本脚本 stdout):")
    for e in evals:
        _print_eval(e)
    oos = evals[2]

    # ── 特征重要性 (gain) ──
    imp = sorted(
        zip(names, model.feature_importances_, strict=False),
        key=lambda kv: kv[1],
        reverse=True,
    )
    print("\n  Top-15 feature importance (gain):")
    for i, (nm, gi) in enumerate(imp[:15]):
        print(f"    {i + 1:>2}. {nm:<30} gain={gi}")

    # ── 机构级门禁判定 (Shadow Mandate — 只判资格, 不触实盘) ──
    verdict: list[str] = []
    if oos["n_pos"] == 0 or oos["n_pos"] == oos["n"]:
        verdict.append("INSUFFICIENT: OOS 无类别变异")
    else:
        if oos["spearman_rho"] is not None and oos["spearman_rho"] <= 0.05:
            verdict.append("FAIL_RHO: OOS spearman rho <= 0.05 (Flow46 门禁)")
        if oos["pr_auc"] is not None and oos["pr_auc"] <= oos["base_rate"] + 0.03:
            verdict.append("FAIL_PR_AUC: OOS PR-AUC 未超过基线 +0.03")
        if oos["calib_slope"] is not None and not (0.5 <= oos["calib_slope"] <= 1.5):
            verdict.append("FAIL_CALIB: OOS 校准斜率超出 [0.5, 1.5]")
    status = "PASS (Shadow 资格)" if not verdict else "; ".join(verdict)
    print(f"\n  >>> Gate verdict: {status}")

    # ── 工件落盘 (零实盘触碰; data/training 为 gitignore 域) ──
    out_dir = Path(args.data_dir) / "training" / "micro_scaler_v1"
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / "micro_scaler_v1_model.txt"
    model.booster_.save_model(str(model_path))
    report = {
        "mode": "baseline",
        "built_at": datetime.now(UTC).isoformat(),
        "n_samples": n_total,
        "n_win": n_win,
        "win_rate": round(n_win / max(n_total, 1), 4),
        "n_long": n_long,
        "n_short": n_short,
        "feature_contract": "v9_institutional_40",
        "schema_generation": "current (Price_ZScore family, TECH_DEBT-023 规避)",
        "lgb_params": params,
        "best_iteration": int(model.best_iteration_ if model.best_iteration_ is not None else 0),
        "split": {
            "ratios": [0.60, 0.20, 0.20],
            "purge_minutes": args.purge_minutes,
            "train": len(train_idx),
            "val": len(val_idx),
            "test": len(test_idx),
            "purged": len(dropped),
        },
        "metrics": evals,
        "gate_verdict": status,
        "top15_features": [nm for nm, _ in imp[:15]],
        "model_path": str(model_path),
    }
    report_path = out_dir / "micro_scaler_v1_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("\n  Artifacts (零实盘触碰, Shadow 门禁后另议):")
    print(f"    model : {model_path}")
    print(f"    report: {report_path}")
    return 0


def build_pseudo_dataset(
    feats: list[dict[str, Any]],
    names: list[str],
    fwd_bars: int = 3,
    max_gap_minutes: int = 30,
) -> tuple[np.ndarray, np.ndarray, list[datetime]]:
    """前向收益伪标签: y=sign(未来 fwd_bars 个 M5 bar 收益之和) ∈ {0,1}.

    弱监督诊断用 — 标签合成, 绝不下场. 不足 fwd_bars 前向上下文的行剔除.
    """
    rows = sorted(feats, key=lambda f: f.get("event_time", ""))
    X_rows: list[list[float]] = []
    y_rows: list[float] = []
    ts: list[datetime] = []
    max_gap = timedelta(minutes=max_gap_minutes)
    for i, f in enumerate(rows):
        et = _parse_dt(f.get("event_time"))
        if et is None:
            continue
        vals = f.get("values", {})
        vec = [float(vals.get(fn, 0.0)) for fn in names]
        fwd = 0.0
        cnt = 0
        for j in range(i + 1, min(i + 1 + fwd_bars * 3, len(rows))):
            nxt_et = _parse_dt(rows[j].get("event_time"))
            if nxt_et is None or (nxt_et - et) > max_gap:
                break
            fwd += float(rows[j].get("values", {}).get("M5_Ret_1", 0.0))
            cnt += 1
            if cnt >= fwd_bars:
                break
        if cnt < fwd_bars:
            continue
        X_rows.append(vec)
        y_rows.append(1.0 if fwd > 0 else 0.0)
        ts.append(et)
    return np.array(X_rows, dtype=np.float32), np.array(y_rows), ts


def run_pseudo_diagnostic(args: argparse.Namespace) -> int:
    """Option B — 前向收益伪标签特征重要性诊断 (不下场, 仅探信号存在性)."""
    print("=" * 78)
    print("  P3 XAU MICRO SCALER V1 — Option B (伪标签敏感性诊断)")
    print("=" * 78)
    feats = load_feature_store(args.data_dir, args.symbol, feature_contract="v9_institutional_40")
    cur = filter_current_gen(feats)
    names = load_contract_feature_names(args.data_dir, feature_contract="v9_institutional_40")
    X, y, ts = build_pseudo_dataset(cur, names, fwd_bars=args.fwd_bars)
    n = len(y)
    n_pos = int(np.sum(y))
    print(f"  current-gen rows with forward context : {n}")
    print(
        f"  pseudo-label (sign 3-bar fwd return)   : pos={n_pos} neg={n - n_pos} "
        f"base_rate={n_pos / max(n, 1):.3f}"
    )

    # 70/30 时序划分 (walk-forward 单折, 禁 shuffle)
    order = np.argsort([t.timestamp() for t in ts])
    X_s, y_s = X[order], y[order]
    cut = int(n * 0.70)
    Xtr, Xte = X_s[:cut], X_s[cut:]
    ytr, yte = y_s[:cut], y_s[cut:]
    n_neg_tr = int(np.sum(ytr == 0))
    n_pos_tr = int(np.sum(ytr == 1))
    params = dict(LGB_PARAMS)
    params["scale_pos_weight"] = round(n_neg_tr / max(n_pos_tr, 1), 3)
    model = lgb.LGBMClassifier(**params)
    model.fit(
        Xtr,
        ytr,
        eval_set=[(Xte, yte)],
        eval_metric="binary_logloss",
        callbacks=[lgb.early_stopping(stopping_rounds=50, verbose=False)],
    )
    proba_te = model.predict_proba(Xte)[:, 1]
    print(f"\n  OOS eval (后 30% 时序, n={len(yte)}):")
    _print_eval(evaluate(yte, proba_te, "OOS"))

    imp = sorted(
        zip(names, model.feature_importances_, strict=False),
        key=lambda kv: kv[1],
        reverse=True,
    )
    print("\n  Top-20 feature importance (gain) — M5 微观特征信息熵探针:")
    for i, (nm, gi) in enumerate(imp[:20]):
        print(f"    {i + 1:>2}. {nm:<30} gain={gi}")

    out_dir = Path(args.data_dir) / "training" / "micro_scaler_v1"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "mode": "pseudo_diagnostic",
        "built_at": datetime.now(UTC).isoformat(),
        "n_pseudo_samples": n,
        "base_rate": round(n_pos / max(n, 1), 4),
        "fwd_bars": args.fwd_bars,
        "oos_eval": evaluate(yte, proba_te, "OOS"),
        "top20_features": [nm for nm, _ in imp[:20]],
    }
    (out_dir / "micro_scaler_v1_pseudo_diag.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(f"\n  Diagnostic report: {out_dir / 'micro_scaler_v1_pseudo_diag.json'}")
    print("  NOTE: 伪标签为合成信号, 仅作信息熵探针, 绝不下场 (Shadow Mandate).")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="P3 XAU Micro Scaler V1 (Option A/B)")
    p.add_argument("--mode", choices=["baseline", "pseudo-diagnostic"], default="baseline")
    p.add_argument("--data-dir", default="data", help="Base data dir (XAU live)")
    p.add_argument("--symbol", default="XAUUSDc")
    p.add_argument("--max-lookback-minutes", type=int, default=15)
    p.add_argument(
        "--purge-minutes",
        type=int,
        default=300,
        help="Purge/Embargo 窗口 (默认 300min = 60 M5 bars, IC 规定)",
    )
    p.add_argument("--fwd-bars", type=int, default=3, help="Option B 前向收益 bar 数")
    args = p.parse_args()
    if args.mode == "baseline":
        return run_baseline(args)
    return run_pseudo_diagnostic(args)


if __name__ == "__main__":
    raise SystemExit(main())
