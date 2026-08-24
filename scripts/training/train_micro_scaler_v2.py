#!/usr/bin/env python3
"""P3 XAU Micro Scaler V2 — Forward Return Track (IC 授权训练协议).

投委会周一开火序列 (2026-08-24, 蓝图 §6.4/§6.5 + 门禁凭证 commit f55a6cf3):
  门禁 PASS (Net-of-Cost Alpha coverage 95.99% / top-decile net +0.381%) → IC 解除 fit() 禁令.
  本脚本 = V2 Forward Return Track 训练进程 (LightGBM 回归 + Isotonic 校准).

IC 训练协议 (逐字执行):
  1. 数据矩阵: 严格使用 4,010 个去重且带真实 Forward 3-bar Return 的标签切片.
  2. 时序切分: 60/20/20 Time-Series Split + Purge/Embargo (±60 M5 bars = 300min, 禁 shuffle).
  3. 算法压制: LightGBM, Huber Loss (压制肥尾), depth<=3, min_child>=20, 强 L2.
  4. 校准战役: Validation Fold 上拟合 Isotonic Regression → OOS Fold 校验, 校准后斜率 ∈ [0.9, 1.1].
  5. 交付: OOS rho + 校准曲线斜率.

设计约束 (架构裁决):
  1. TECH_DEBT-023 世代守卫: 只用 current-gen 列族 (复用 build_micro_cost_model.load_current_gen_rows).
  2. 前向收益来自 MT5 真实价格 (材料发现 #2: M5_Ret_1 不可靠) — 复用 build_forward_returns.
  3. net-of-cost: 动态 |avg_spread| ASOF 还原 (符号翻转 bug 已消费端兼容), 回退锚点 0.26.
  4. 零实盘触碰 (Shadow Mandate): 只产模型工件 + 报告, 不写 brain config, 不碰 live_*.yaml.
  5. SSOT 复用: ts_purged_split 直接复用 V1 同款 (scripts/training/train_micro_scaler_v1), 不散布.
  6. IC 部署令 (2026-08-24 终局裁决): 豁免 [0.9,1.1] slope 门禁 (幅度排序器定性) →
     v2 晋升 SHADOW, 触发机制强制 Quantile Trigger (|pred| 落入 D10 才允许 Shadow
     Order, 禁 Fixed Threshold) — 规格 build_trigger_spec() 随 report 落档.

用法:
  python scripts/training/train_micro_scaler_v2.py --data-dir data --symbol XAUUSDc
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

# ── Repo-root bootstrap (scripts/training/ 直接运行) ──
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import lightgbm as lgb  # noqa: E402
from sklearn.isotonic import IsotonicRegression  # noqa: E402
from sklearn.metrics import average_precision_score, brier_score_loss  # noqa: E402

# PIT/标签 SSOT 复用 (Iron Law #1.1 — 统一扩展点, 不重写数据装配)
from scripts.build_micro_cost_model import (  # noqa: E402
    FeatureRow,
    _CONTRACT_OZ,
    _DEFAULT_SPREAD_USD,
    asof_join_v43_backward,
    build_forward_returns,
    check_alignment,
    load_current_gen_rows,
    load_mt5_bars,
    load_v43_spread,
    probe_mt5_spread,
    resolve_dynamic_spread,
)

# 切分 SSOT 复用 (V1 同款 ts_purged_split — 同一逻辑单一来源, 不散布)
from scripts.training.train_micro_scaler_v1 import ts_purged_split  # noqa: E402
from core.contracts.exceptions import DataIntegrityError  # noqa: E402
from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES  # noqa: E402
from core.training.utils import spearman_rho  # noqa: E402

# ── IC 授权的克制参数 (LightGBM 回归, Huber, depth<=3, min_child>=20) ──
LGB_REGRESS_PARAMS: dict[str, Any] = {
    "objective": "huber",  # 肥尾压制 (IC 指名)
    "alpha": 0.9,  # huber 默认分位
    "metric": "l2",
    "n_estimators": 1000,
    "learning_rate": 0.03,
    "max_depth": 3,  # IC 硬上限
    "num_leaves": 8,  # depth 3 满叶
    "min_child_samples": 20,  # IC 硬下限
    "feature_fraction": 0.6,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 2.0,  # 强 L2 (§6.3 缓解 a)
    "random_state": 42,
    "verbosity": -1,
}

# 方向诊断臂 (Blueprint §6.4) 参数 — 同纪律, 仅 objective 换 binary
LGB_DIR_PARAMS: dict[str, Any] = {
    "objective": "binary",
    "metric": "binary_logloss",
    "n_estimators": 1000,
    "learning_rate": 0.03,
    "max_depth": 3,
    "num_leaves": 8,
    "min_child_samples": 20,
    "feature_fraction": 0.6,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 2.0,
    "random_state": 42,
    "verbosity": -1,
}

# IC 规定 purge 窗口: ±60 M5 bars = 300min
_PURGE_MINUTES = 300
_SPLIT_RATIOS = (0.60, 0.20, 0.20)

# 校准斜率 IC 门禁区间
_SLOPE_GATE = (0.9, 1.1)


def build_dataset(args: argparse.Namespace) -> dict[str, Any]:
    """装配 4,010 训练池 (唯一合法证据源 stdout 在本函数逐行打印).

    返回 dict 承载 X/y/时间/spread/盈亏平衡线, 供训练使用.
    """
    base = Path(args.data_dir)
    fs_path = (
        base
        / "feature_store"
        / "records"
        / f"symbol={args.symbol}"
        / "timeframe=M5"
        / "features.jsonl"
    )
    print("=" * 78)
    print("  P3 XAU MICRO SCALER V2 — FORWARD RETURN TRACK (IC 授权 fit)")
    print("=" * 78)
    print(f"  symbol={args.symbol}  data-dir={args.data_dir}  fwd_bars={args.fwd_bars}")

    # ── 1. current-gen 特征行 (TECH_DEBT-023 守卫 + bar 去重) ──
    if not fs_path.exists():
        raise FileNotFoundError(f"feature store not found: {fs_path}")
    rows = load_current_gen_rows(fs_path)
    if not rows:
        raise RuntimeError("0 current-gen feature rows")

    # ── 2. MT5 真实 bars ──
    bars = load_mt5_bars(datetime(2026, 5, 24, tzinfo=UTC), datetime.now(UTC))
    if not bars:
        raise RuntimeError("MT5 terminal unreachable — 真实价格不可用, 训练无法进行")
    epoch_list = sorted(bars)
    print(
        f"  [mt5] bars={len(bars)}  span={datetime.fromtimestamp(epoch_list[0], UTC).isoformat()}.."
        f"{datetime.fromtimestamp(epoch_list[-1], UTC).isoformat()}  "
        f"close_last={bars[epoch_list[-1]][1]:.3f}"
    )

    # ── 3. 前向 3-bar 收益 (真实价格, 材料发现 #2 强制) ──
    fwd, abs_fwd, matched = build_forward_returns(rows, bars)
    print(
        f"  [fwd] feature rows with 3-bar fwd context: {len(matched)}/{len(rows)} "
        f"(训练池 = IC 指定的 4,010 切片)"
    )
    if len(fwd) == 0:
        raise RuntimeError("no rows with forward context")
    n_pool = len(matched)

    # ── 3b. 对齐校验 (数据完整性红线) ──
    align = check_alignment(rows, matched, bars)
    if align["n_checked"]:
        print(
            f"  [align] n={align['n_checked']}  median|M5_Ret_1-真实1bar|={align['median_abs_diff_pct']}%  "
            f"p95={align['p95_abs_diff_pct']}%  max={align['max_abs_diff_pct']}%"
        )
    else:
        print("  [align] WARN: 无可校验行 (bars 覆盖不足)")

    # ── 4. X 矩阵 (canonical 40 列, 严格全键断言) ──
    names = list(V9_INSTITUTIONAL_40_FEATURES)
    assert len(names) == 40, f"canonical 40 features expected, got {len(names)}"
    canon = set(names)
    X_rows: list[list[float]] = []
    times: list[datetime] = []
    bad: list[str] = []
    for i in matched:
        row = rows[i]
        keys = set(row.values)
        if not canon.issubset(keys):
            bad.append(f"row@{row.event_dt.isoformat()} missing={sorted(canon - keys)}")
            continue
        vec = [float(row.values[nm]) for nm in names]
        if any(v != v for v in vec):  # NaN guard
            bad.append(f"row@{row.event_dt.isoformat()} contains NaN")
            continue
        X_rows.append(vec)
        times.append(row.event_dt)
    if bad:
        # DataIntegrityError 语义: 缺失/NaN 是数据病, 严禁 dict.get 抹平
        raise RuntimeError(f"{len(bad)} rows fail canonical-40 integrity: {bad[:3]} ...")
    X = np.asarray(X_rows, dtype=np.float32)
    y = fwd.astype(np.float64)  # 签名 3-bar 收益 (%)
    print(f"  [X] {X.shape[0]}x{X.shape[1]}  (canonical 40, 零缺失零 NaN)")

    # ── 5. v4.3 avg_spread ASOF join + 动态 spread 还原 (|·| + 锚点回退) ──
    v43_epochs, v43_spreads = load_v43_spread(fs_path)
    v9_epochs = np.array([int(t.timestamp()) for t in times], dtype=np.float64)
    joined, join_stats = asof_join_v43_backward(v9_epochs, v43_epochs, v43_spreads)
    print(
        f"  [v4.3 join] direction={join_stats['direction']}  joined={join_stats['joined']}/"
        f"{join_stats['v9_rows']} ({join_stats['coverage_pct']}%)"
    )

    spread_price: float
    if args.spread_usd is not None:
        spread_price = args.spread_usd
    else:
        probe = probe_mt5_spread(bars)
        if probe is not None and probe.get("spread_p50_usd") is not None:
            spread_price = float(probe["spread_p50_usd"])
        else:
            spread_price = _DEFAULT_SPREAD_USD
            print("  [spread] WARN: MT5 探针失败, 使用文档化默认锚点 0.26 USD")
    dyn_spread, dyn_stats = resolve_dynamic_spread(joined, spread_price)
    print(
        f"  [dynamic spread] |avg_spread| median={dyn_stats['abs_spread_median_usd']} "
        f"negative_rate={dyn_stats['joined_negative_rate']*100:.1f}%  "
        f"fallback_rate={dyn_stats['fallback_rate']}%  anchor={spread_price}"
    )

    # ── 6. 逐 bar 盈亏平衡线 (% return) ──
    closes = np.array(
        [bars[int(rows[i].event_dt.timestamp() // 300) * 300][1] for i in matched],
        dtype=np.float64,
    )
    commission_per_oz = args.commission_per_lot_usd / _CONTRACT_OZ
    be_pct = (dyn_spread + commission_per_oz) / closes * 100.0
    print(
        f"  [break-even] be_pct mean={np.mean(be_pct):.5f}%  median={np.median(be_pct):.5f}%  "
        f"commission_per_lot_usd={args.commission_per_lot_usd}"
    )

    # ── 7. 时序切分 (60/20/20 + purge/embargo, 禁 shuffle) ──
    order = np.argsort([t.timestamp() for t in times])
    X_s = X[order]
    y_s = y[order]
    be_s = be_pct[order]
    times_sorted = [times[i] for i in order]
    train_idx, val_idx, test_idx, dropped = ts_purged_split(
        times_sorted, ratios=_SPLIT_RATIOS, purge_minutes=_PURGE_MINUTES
    )
    print(
        f"  [split] 60/20/20 purge±{_PURGE_MINUTES}min: train={len(train_idx)} "
        f"val={len(val_idx)} test={len(test_idx)} purged={len(dropped)}"
    )
    if len(test_idx) < 20:
        print("  WARNING: OOS test < 20 — rho/slope 置信区间极宽")
    if len(train_idx) == 0 or len(val_idx) == 0 or len(test_idx) == 0:
        raise RuntimeError("empty partition after purge")

    ds: dict[str, Any] = {
        "Xtr": X_s[train_idx],
        "ytr": y_s[train_idx],
        "betr": be_s[train_idx],
        "Xva": X_s[val_idx],
        "yva": y_s[val_idx],
        "beva": be_s[val_idx],
        "Xte": X_s[test_idx],
        "yte": y_s[test_idx],
        "bete": be_s[test_idx],
        "n_pool": n_pool,
        "names": names,
        "split": {
            "train": len(train_idx),
            "val": len(val_idx),
            "test": len(test_idx),
            "purged": len(dropped),
            "purge_minutes": _PURGE_MINUTES,
        },
    }
    return ds


def eval_regression(pred: np.ndarray, truth: np.ndarray, label: str) -> dict[str, Any]:
    """单分区回归评估: Spearman rho / 分位 IC / 校准斜率 / 均方误差."""
    n = len(truth)
    rho = float(spearman_rho(pred, truth))
    # 校准斜率: y_true ~ y_pred 回归系数 (slope=1 即完美校准)
    slope, intercept = (
        np.polyfit(pred, truth, 1) if n >= 2 and np.std(pred) > 0 else (float("nan"), float("nan"))
    )
    rmse = float(np.sqrt(np.mean((pred - truth) ** 2))) if n else float("nan")
    mae = float(np.mean(np.abs(pred - truth))) if n else float("nan")
    # 分位 IC: 按 pred 分 10 档 → 各档 truth 均值 → Spearman(档序, 档均值)
    dec_ic = float("nan")
    if n >= 20 and np.std(pred) > 0:
        order = np.argsort(pred)
        bins = np.array_split(order, 10)
        bin_means = [float(np.mean(truth[ix])) for ix in bins if len(ix) > 0]
        if len(bin_means) >= 3:
            dec_ic = float(spearman_rho(np.arange(len(bin_means)), np.array(bin_means)))
    res: dict[str, Any] = {
        "partition": label,
        "n": n,
        "spearman_rho": round(rho, 4),
        "calib_slope": round(float(slope), 4),
        "calib_intercept": round(float(intercept), 4),
        "quantile_ic": round(dec_ic, 4),
        "rmse_pct": round(rmse, 5),
        "mae_pct": round(mae, 5),
    }
    return res


def net_of_cost_gate(
    pred: np.ndarray, truth: np.ndarray, be: np.ndarray, label: str
) -> dict[str, Any]:
    """Net-of-Cost top-decile 门禁 (OOS 上按 |pred| 排名取前 10%).

    net = sign(pred)*truth - be (LONG 做多 / SHORT 做空, 扣动态 spread).
    门禁: top-decile mean net > 0 且 trigger rate ∈ [1%, 50%].
    """
    n = len(truth)
    if n == 0:
        return {"label": label, "n": 0}
    signed_net = np.sign(pred) * truth - be
    k = max(1, n // 10)
    idx = np.argsort(np.abs(pred))[::-1][:k]
    top_net = float(np.mean(signed_net[idx]))
    top_gross = float(np.mean(np.abs(truth[idx])))
    full_mean = float(np.mean(signed_net))
    trigger_rate = float(k / n)
    n_trigger_net_pos: int = int(np.sum(signed_net[idx] > 0.0))
    # 触发率锚点 (|pred| 分位, 供 trigger ∈ [1%,50%] 闸门参考)
    thr_p90 = float(np.percentile(np.abs(pred), 90)) if n else float("nan")
    thr_p75 = float(np.percentile(np.abs(pred), 75)) if n else float("nan")
    res: dict[str, Any] = {
        "label": label,
        "n": n,
        "top_decile_trigger_rate_pct": round(100.0 * trigger_rate, 2),
        "top_decile_mean_net_pct": round(top_net, 5),
        "top_decile_mean_gross_abs_pct": round(top_gross, 5),
        "top_decile_net_positive_share_pct": round(100.0 * n_trigger_net_pos / max(k, 1), 2),
        "full_oos_mean_net_pct": round(full_mean, 5),
        "|pred| p75_pct": round(thr_p75, 5),
        "|pred| p90_pct": round(thr_p90, 5),
        "_note": "net = sign(pred)*fwd3 - be_pct; 门禁 = top-decile mean net > 0",
    }
    return res


# IC 部署令 (2026-08-24 终局裁决): 豁免 [0.9,1.1] 校准斜率门禁 (幅度排序器定性,
# qIC 主闸门 + 净成本 D10 PASS), v2 晋升 SHADOW. 后续实盘执行引擎对本模型
# 必须且只能采用 Quantile Trigger (|pred| 落入历史样本 Top-decile D10 才允许
# Shadow Order) — 绝不允许固定阈值 (Fixed Threshold) 触发.
_TRIGGER_MANDATE = (
    "FIXED_THRESHOLD_FORBIDDEN: Quantile Trigger ONLY — |pred| 落入历史样本 "
    "Top-decile (D10) 才允许 Shadow Order; 绝不允许固定阈值触发 (IC 终局裁决 2026-08-24)."
)


def build_trigger_spec(report: dict[str, Any], *, raw_p90: float) -> dict[str, Any]:
    """从 reg_report 派生 Quantile Trigger 规格 (单一来源, emit 脚本复用).

    FIX-20260824-005 (IC 裁决): 触发源必须为 raw |pred|. ``raw_p90`` 必传 =
    训练池 raw |pred| p90 (由 emit 脚本用已落档 booster 重推, 零 MT5 触碰).
    Isotonic 单调 → raw/cal 排序同人口, D10 经济原样有效; 仅值域 cal→raw,
    摆脱校准平坦区台阶吸附 (实测触发率 75.6% → 设计 ~10%).

    direction 由 sign(cal) 派生 (幅度排序器; 单调 → 与 sign(raw) 一致).
    斜率门禁 FAIL_SLOPE 已被 IC 豁免 (2026-08-24 终局裁决) — 状态在此固化.

    旧 cal 系默认分支 (noc["|pred| p90_pct"] = 0.06007) 已删除 — 该阈值与 raw
    判定语义不兼容 (raw p90≈0.01867), 是陷阱: 强制 raw_p90 消灭静默错配.
    """
    if raw_p90 is None:
        raise DataIntegrityError(
            "build_trigger_spec: raw_p90 必传 (FIX-20260824-005). "
            "触发源已固化 raw |pred| — 请用 emit_micro_scaler_v2_raw_trigger.py 派生, "
            "禁止 cal 系阈值 (原 0.06007 与 raw 判定不兼容)."
        )
    noc = report["net_of_cost"]
    cal = report["isotonic_calibrated_eval"]["oos"]
    slope_gate = report["calibration_slope_gate"]
    threshold = round(float(raw_p90), 5)
    return {
        "model_id": "micro_scaler_v2",
        "mode": report.get("mode"),
        "trigger_mode": "quantile_top_decile_abs_raw_pred",
        "threshold_abs_pred_pct": threshold,
        "trigger_rate_pct_oos": noc["top_decile_trigger_rate_pct"],
        "direction_semantics": (
            "sign(cal): LONG if cal>0 else SHORT; 触发基于 raw |pred| (FIX-20260824-005)"
        ),
        "economics": {
            "d10_mean_net_pct": noc["top_decile_mean_net_pct"],
            "d10_net_positive_share_pct": noc["top_decile_net_positive_share_pct"],
            "full_oos_mean_net_pct": noc["full_oos_mean_net_pct"],
        },
        "quality": {
            "oos_rho_calibrated": cal["spearman_rho"],
            "oos_quantile_ic": cal["quantile_ic"],
            "calib_slope": slope_gate["slope"],
            "calib_slope_gate": slope_gate["gate"],
            "slope_gate_status": "EXEMPTED_BY_IC_2026-08-24",
        },
        "oos_decile_table": report["oos_decile_table"],
        "mandate": _TRIGGER_MANDATE,
        "derivation": (
            "FIX-20260824-005 IC 裁决: 触发源 cal→raw |pred|. 阈值 = 训练池 raw |pred| "
            f"p90 ({threshold}%) 重导; Isotonic 单调同人口, D10 经济不变. 原 cal 阈值 "
            f"{noc['|pred| p90_pct']}% 弃用 (校准平坦区台阶吸附)."
        ),
    }


def run_regression(ds: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """主轨道: LightGBM huber 回归 + Isotonic 校准 + 门禁."""
    print("\n" + "=" * 78)
    print("  主轨道: LIGHTGBM HUBER REGRESSION (前向 3-bar 收益)")
    print("=" * 78)
    Xtr, ytr = ds["Xtr"], ds["ytr"]
    Xva, yva = ds["Xva"], ds["yva"]
    Xte, yte = ds["Xte"], ds["yte"]
    print(
        f"  target y (fwd3%): mean={np.mean(ytr):+.4f}  std={np.std(ytr):.4f}  "
        f"min={np.min(ytr):+.4f}  max={np.max(ytr):+.4f}"
    )
    params = dict(LGB_REGRESS_PARAMS)
    params["n_estimators"] = args.n_estimators
    model = lgb.LGBMRegressor(**params)
    model.fit(
        Xtr,
        ytr,
        eval_set=[(Xva, yva)],
        eval_metric="l2",
        callbacks=[lgb.early_stopping(stopping_rounds=80, verbose=False)],
    )
    best_iter = int(
        model.best_iteration_ if model.best_iteration_ is not None else params["n_estimators"]
    )
    print(
        f"  [train] huber alpha={params['alpha']} depth={params['max_depth']} "
        f"min_child={params['min_child_samples']} leaves={params['num_leaves']} "
        f"L2={params['lambda_l2']} best_iteration={best_iter}"
    )

    pred_tr = model.predict(Xtr)
    pred_va = model.predict(Xva)
    pred_te = model.predict(Xte)

    print("\n  Raw predictions (校准前):")
    for ev in (
        eval_regression(pred_tr, ytr, "train"),
        eval_regression(pred_va, yva, "val"),
        eval_regression(pred_te, yte, "OOS"),
    ):
        _print_reg(ev)
    oos_raw = eval_regression(pred_te, yte, "OOS")

    # ── Isotonic 校准: val 拟合 → OOS 校验 (防自偏) ──
    print("\n  Isotonic 校准 (val 拟合 → OOS 校验):")
    iso = IsotonicRegression(out_of_bounds="clip")
    try:
        iso.fit(pred_va, yva)
    except ValueError as e:
        print(f"    [FAIL] Isotonic fit failed: {e}")
        return {"raw_oos": oos_raw, "iso_failed": True}
    pred_te_cal = iso.predict(pred_te)
    pred_tr_cal = iso.predict(pred_tr)
    cal_oos = eval_regression(pred_te_cal, yte, "OOS-cal")
    cal_tr = eval_regression(pred_tr_cal, ytr, "train-cal")
    _print_reg(cal_tr)
    _print_reg(cal_oos)
    slope = cal_oos["calib_slope"]
    slope_gate_ok: bool = _SLOPE_GATE[0] <= slope <= _SLOPE_GATE[1]
    print(
        f"    >>> Calibration slope gate: slope={slope}  "
        f"gate=[{_SLOPE_GATE[0]},{_SLOPE_GATE[1]}]  "
        f"{'PASS' if slope_gate_ok else 'FAIL'}"
    )

    # ── 校准曲线采样 (val 拟合的映射, 供曲线图/落档) ──
    iso_grid = np.linspace(float(np.min(pred_te)), float(np.max(pred_te)), 21)
    iso_curve = {
        "x_grid": [round(float(x), 5) for x in iso_grid],
        "y_grid": [round(float(v), 5) for v in iso.predict(iso_grid)],
        "method": "isotonic_regression",
        "fit_partition": "val",
        "eval_partition": "OOS",
    }

    # ── 分位收益表 (校准后, OOS) ──
    print("\n  OOS 分位收益表 (校准后预测):")
    dec_tbl = _decile_table(pred_te_cal, yte)

    # ── Net-of-Cost 门禁 (OOS, 校准后) ──
    print("\n  Net-of-Cost top-decile 门禁 (OOS, sign(pred)*fwd3 - be):")
    ng = net_of_cost_gate(pred_te_cal, yte, ds["bete"], "OOS")
    print(
        f"    top-decile(10%) trigger={ng['top_decile_trigger_rate_pct']}%  "
        f"mean_net={ng['top_decile_mean_net_pct']:+.5f}%  "
        f"net_pos_share={ng['top_decile_net_positive_share_pct']}%"
    )
    print(
        f"    full-OOS mean_net={ng['full_oos_mean_net_pct']:+.5f}%  "
        f"|pred| p75={ng['|pred| p75_pct']}% p90={ng['|pred| p90_pct']}%"
    )
    trigger_ok: bool = 1.0 <= ng["top_decile_trigger_rate_pct"] <= 50.0
    net_ok: bool = ng["top_decile_mean_net_pct"] > 0.0
    print(
        f"    >>> Net-of-cost gate: top-decile net={'PASS' if net_ok else 'FAIL'}  "
        f"trigger∈[1%,50%]={'PASS' if trigger_ok else 'FAIL'}"
    )

    # ── 特征重要性 (gain) ──
    imp = sorted(
        zip(ds["names"], model.feature_importances_, strict=False),
        key=lambda kv: kv[1],
        reverse=True,
    )
    print("\n  Top-15 feature importance (gain):")
    for i, (nm, gi) in enumerate(imp[:15]):
        print(f"    {i + 1:>2}. {nm:<30} gain={gi}")

    # ── 门禁汇总 ──
    rho = cal_oos["spearman_rho"]
    verdict_parts: list[str] = []
    if not slope_gate_ok:
        verdict_parts.append(f"FAIL_SLOPE: OOS calib slope {slope} not in [0.9,1.1]")
    if rho <= 0.0:
        verdict_parts.append(f"FAIL_RHO: OOS spearman rho {rho} <= 0")
    if not net_ok:
        verdict_parts.append("FAIL_NET: top-decile net <= 0")
    verdict = "PASS (Shadow 资格候选)" if not verdict_parts else "; ".join(verdict_parts)
    print(f"\n  >>> V2 Regression gate verdict: {verdict}")

    out_dir = Path(args.data_dir) / "training" / "micro_scaler_v2"
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / "micro_scaler_v2_reg.txt"
    model.booster_.save_model(str(model_path))
    report: dict[str, Any] = {
        "mode": "regression_forward3bar",
        "built_at": datetime.now(UTC).isoformat(),
        "n_pool": ds["n_pool"],
        "split": ds["split"],
        "lgb_params": {**params, "n_estimators_actual": best_iter},
        "raw_eval": {
            "train": eval_regression(pred_tr, ytr, "train"),
            "val": eval_regression(pred_va, yva, "val"),
            "oos": oos_raw,
        },
        "isotonic_calibrated_eval": {"train": cal_tr, "oos": cal_oos},
        "calibration_curve": iso_curve,
        "calibration_slope_gate": {
            "slope": slope,
            "gate": list(_SLOPE_GATE),
            "pass": slope_gate_ok,
        },
        "oos_decile_table": dec_tbl,
        "net_of_cost": ng,
        "net_of_cost_gate": {"top_decile_net_pass": net_ok, "trigger_pass": trigger_ok},
        "gate_verdict": verdict,
        "top15_features": [nm for nm, _ in imp[:15]],
        "model_path": str(model_path),
        "data_lineage": "current-gen 40col / fwd3bar real MT5 / |avg_spread| dyn + anchor",
    }
    (out_dir / "micro_scaler_v2_reg_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    # ── Quantile Trigger 落档已迁移至 emit 脚本 (FIX-20260824-005, IC 裁决) ──
    # 触发源固化 raw |pred|, 阈值 = 训练池 raw |pred| p90. raw_p90 需用已落档
    # booster 对切分池重推 (emit_micro_scaler_v2_raw_trigger.py, 零 MT5 触碰);
    # train 不再自落 trigger.json — 单一生产者, 防 cal 系阈值静默错配陷阱.
    print("\n  Artifacts (Shadow Mandate, 零实盘触碰):")
    print(f"    model  : {model_path}")
    print(f"    report : {out_dir / 'micro_scaler_v2_reg_report.json'}")
    print(
        "    trigger: 运行 emit_micro_scaler_v2_raw_trigger.py --data-dir <dir> 派生 "
        "(raw |pred| p90, FIX-20260824-005)"
    )
    return {"raw_oos": oos_raw, "cal_oos": cal_oos, "net": ng, "verdict": verdict}


def run_direction_diag(ds: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """诊断臂: 方向二分类 (涨跌阈值) — 仅诊断, 不下场 (Blueprint §6.4)."""
    print("\n" + "=" * 78)
    print("  诊断臂: 方向二分类 (sign(fwd3), 仅信息熵探针)")
    print("=" * 78)
    ytr_d = (ds["ytr"] > 0.0).astype(np.int32)
    yva_d = (ds["yva"] > 0.0).astype(np.int32)
    yte_d = (ds["yte"] > 0.0).astype(np.int32)
    base_rate = float(np.mean(yte_d))
    n_pos = int(np.sum(yte_d))
    print(f"  OOS direction base_rate={base_rate:.4f} (pos={n_pos}/{len(yte_d)})")

    n_neg_tr: int = int(np.sum(ytr_d == 0))
    n_pos_tr: int = int(np.sum(ytr_d == 1))
    params = dict(LGB_DIR_PARAMS)
    params["scale_pos_weight"] = round(n_neg_tr / max(n_pos_tr, 1), 3)
    params["n_estimators"] = args.n_estimators
    model = lgb.LGBMClassifier(**params)
    model.fit(
        ds["Xtr"],
        ytr_d,
        eval_set=[(ds["Xva"], yva_d)],
        eval_metric="binary_logloss",
        callbacks=[lgb.early_stopping(stopping_rounds=80, verbose=False)],
    )
    proba_tr = model.predict_proba(ds["Xtr"])[:, 1]
    proba_va = model.predict_proba(ds["Xva"])[:, 1]
    proba_te = model.predict_proba(ds["Xte"])[:, 1]

    def _eval_bin(proba: np.ndarray, yb: np.ndarray, label: str) -> dict[str, Any]:
        n = len(yb)
        n_p = int(np.sum(yb))
        if 0 < n_p < n:
            pr_auc = float(average_precision_score(yb, proba))
            brier = float(brier_score_loss(yb, proba))
            rho = float(spearman_rho(proba, yb))
            sign = float(np.mean((proba >= 0.5).astype(int) == yb))
        else:
            pr_auc = brier = rho = sign = float("nan")
        return {
            "partition": label,
            "n": n,
            "base_rate": round(float(np.mean(yb)), 4),
            "pr_auc": round(pr_auc, 4),
            "spearman_rho": round(rho, 4),
            "sign_match": round(sign, 4),
            "brier": round(brier, 4),
        }

    print("\n  Direction classification eval:")
    ev_tr = _eval_bin(proba_tr, ytr_d, "train")
    ev_va = _eval_bin(proba_va, yva_d, "val")
    ev_te = _eval_bin(proba_te, yte_d, "OOS")
    for ev in (ev_tr, ev_va, ev_te):
        print(
            f"    [{ev['partition']:<6}] n={ev['n']:<4} base={ev['base_rate']:.3f} "
            f"PR-AUC={ev['pr_auc']:.3f} rho={ev['spearman_rho']:.3f} "
            f"sign={ev['sign_match']:.3f} brier={ev['brier']:.3f}"
        )

    # 概率 isotonic 校准 (val → OOS)
    iso_p = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    try:
        iso_p.fit(proba_va, yva_d.astype(np.float64))
        proba_te_cal = iso_p.predict(proba_te)
    except ValueError:
        proba_te_cal = proba_te
        print("    [WARN] Isotonic fit failed for direction — using raw proba")
    slope_p, _ = (
        np.polyfit(proba_te_cal, yte_d.astype(np.float64), 1)
        if len(yte_d) >= 2 and np.std(proba_te_cal) > 0
        else (float("nan"), float("nan"))
    )
    print(f"    >>> Direction post-isotonic OOS calib slope={slope_p:.4f}")

    out_dir = Path(args.data_dir) / "training" / "micro_scaler_v2"
    out_dir.mkdir(parents=True, exist_ok=True)
    diag: dict[str, Any] = {
        "mode": "direction_diagnostic",
        "built_at": datetime.now(UTC).isoformat(),
        "eval": {"train": ev_tr, "val": ev_va, "oos": ev_te},
        "oos_post_isotonic_calib_slope": round(float(slope_p), 4),
        "note": "诊断臂仅探方向信息熵, 不下场 (Shadow Mandate).",
    }
    (out_dir / "micro_scaler_v2_direction_diag.json").write_text(
        json.dumps(diag, indent=2), encoding="utf-8"
    )
    print(f"  Diagnostic report: {out_dir / 'micro_scaler_v2_direction_diag.json'}")
    return diag


def _decile_table(pred: np.ndarray, truth: np.ndarray) -> list[dict[str, Any]]:
    """按预测分 10 档 → 每档真实收益均值 (校准单调性表)."""
    order = np.argsort(pred)
    bins = np.array_split(order, 10)
    out: list[dict[str, Any]] = []
    for d, ix in enumerate(bins):
        if len(ix) == 0:
            continue
        out.append(
            {
                "decile": d + 1,
                "pred_mean": round(float(np.mean(pred[ix])), 5),
                "truth_mean": round(float(np.mean(truth[ix])), 5),
                "n": int(len(ix)),
            }
        )
    for r in out:
        print(
            f"      D{r['decile']:<2} pred={r['pred_mean']:+.5f}%  "
            f"truth={r['truth_mean']:+.5f}%  n={r['n']}"
        )
    return out


def _print_reg(res: dict[str, Any]) -> None:
    print(
        f"    [{res['partition']:<8}] n={res['n']:<4} rho={res['spearman_rho']:.4f} "
        f"qIC={res['quantile_ic']:.4f} slope={res['calib_slope']:.4f} "
        f"inter={res['calib_intercept']:.4f} rmse={res['rmse_pct']:.5f} mae={res['mae_pct']:.5f}"
    )


def main() -> int:
    p = argparse.ArgumentParser(description="P3 XAU Micro Scaler V2 — Forward Return Track")
    p.add_argument("--data-dir", default="data", help="Base data dir (XAU live)")
    p.add_argument("--symbol", default="XAUUSDc")
    p.add_argument("--fwd-bars", type=int, default=3)
    p.add_argument(
        "--spread-usd",
        type=float,
        default=None,
        help="手动指定 spread USD (默认: MT5 探针 p50 → fallback 0.26)",
    )
    p.add_argument("--commission-per-lot-usd", type=float, default=0.0)
    p.add_argument("--n-estimators", type=int, default=1000)
    args = p.parse_args()

    ds = build_dataset(args)
    result = run_regression(ds, args)
    run_direction_diag(ds, args)

    print("\n" + "-" * 78)
    print("  V2 交付摘要 (唯一合法证据源 = 本脚本 stdout):")
    print(f"    OOS rho (校准后)      = {result['cal_oos']['spearman_rho']}")
    print(
        f"    OOS 校准斜率 (isotonic) = {result['cal_oos']['calib_slope']}  "
        f"gate [0.9,1.1]={'PASS' if _SLOPE_GATE[0] <= result['cal_oos']['calib_slope'] <= _SLOPE_GATE[1] else 'FAIL'}"
    )
    print(f"    OOS net-of-cost top-decile = {result['net']['top_decile_mean_net_pct']:+.5f}%")
    print(f"    Regression gate verdict  = {result['verdict']}")
    print("\n[DONE] fit() 完成. 训练凭证以上述 stdout 为准.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
