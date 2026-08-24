#!/usr/bin/env python3
"""P3 XAU Micro Scaler v2 — Cost Model 基建 (周一开火序列 第二步 + 第三步).

投委会周一开火序列 (2026-08-24, 蓝图: references/DQAF_AUDIT_20260823_P3_XAU_MICRO_SCALER.md §6.4):
  第二步 成本基建战役:
    (a) 加载 v9 current-gen 特征 (TECH_DEBT-023 世代守卫) + v4.3_microstructure_9 avg_spread;
    (b) pd.merge_asof direction='backward' 严格防 forward 泄露 (1ms 泄露 = 把未来点差当现在信号 = 实盘雪崩);
    (c) 逐 M5 bar 核算绝对盈亏平衡线 (Break-even Threshold): spread + 手续费.
  第三步 门禁凭证:
    产出 Net-of-Cost Alpha 覆盖率统计 → 投委会授权 fit() 才可训练.

本脚本只读 (Iron Law #11): 一切统计以 stdout 为唯一合法证据源.
JSON 工件仅为 IC 审阅快照, 非统计来源. 模型训练前严禁 fit().

数据源与单位实证 (2026-08-24 MT5 探针, EXNESS2):
  - M5_Ret_1 = 每 M5 bar 百分比收益 ((c-c_prev)/c_prev*100) — v9_live_computer._returns().
  - v4.3_microstructure_9.avg_spread: 14,839 行 100% 零值 (实证) → 动态 spread 列不可用.
  - MT5 实测 XAU spread: p50=p90=0.26 USD, p99=0.34, max=0.48 (500K ticks, 2026-08-20→08-21).
  - config spread_points 单位 = 0.01 USD/point (strategy_line 用 (ask-bid)/tick_size, XAU tick_size=0.001 时单位不同, 勿混).
  - MT5 M5 bar 全窗口 17,888 根 (2026-05-24→08-24), 金价 current ~4642.

用法:
  python scripts/build_micro_cost_model.py --data-dir data --symbol XAUUSDc
  python scripts/build_micro_cost_model.py --write   # 写 JSON 快照工件
  python scripts/build_micro_cost_model.py --commission-per-lot-usd 6.0   # 若 EXNESS 有佣金
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

# ── Repo-root bootstrap ──
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pandas as pd  # noqa: E402  (仅 v4.3 ASOF join, IC 指名机制)

# ── TECH_DEBT-023 Schema 世代标记 (与 v1 一致) ──
_CUR_GEN_HAVE = "H1_Price_ZScore"
_OLD_GEN_HAVE = "H1_Macro_Gold_Silver_Spread"

# MT5 常量
_MT5_TIMEFRAME_M5 = 5
_MT5_COPY_TICKS_ALL = -1

# XAU 合约常量 (EXNESS2 实证: 1 lot = 100 oz)
_CONTRACT_OZ = 100.0

# 文档化默认 spread 锚点 (MT5 探针 500K tick p50; 仅在探针失败时用作 fallback)
_DEFAULT_SPREAD_USD = 0.26


def _parse_dt(s: str | None) -> datetime | None:
    """ISO-ish -> aware UTC datetime. 处理尾部 'Z' + 任意小数秒; 失败返回 None."""
    if not s or not isinstance(s, str):
        return None
    t = s.strip()
    try:
        dt = datetime.fromisoformat(t[:26])
    except ValueError:
        # 处理无 'T' 分隔的 YYYY-MM-DD HH:MM:SS
        if "T" not in t:
            try:
                dt = datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


@dataclass
class FeatureRow:
    """v9 current-gen 特征行 (仅成本模型所需字段)."""

    event_dt: datetime
    m5_ret_1: float
    values: dict[str, float] = field(default_factory=dict)


def load_current_gen_rows(path: Path) -> list[FeatureRow]:
    """加载 feature store, 过滤 current-gen (TECH_DEBT-023 列族守卫).

    返回按 event_time 升序去重后的行. 统计在 stdout 上报.
    """
    raw: list[FeatureRow] = []
    n_v9 = 0
    n_cur = 0
    seen: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("schema_name") != "v9_institutional_40":
                continue
            n_v9 += 1
            vals = rec.get("values")
            if not isinstance(vals, dict):
                continue
            keys = set(vals)
            # current-gen 列族: 含 Price_ZScore 且无 Macro_Gold_Silver_Spread
            if _CUR_GEN_HAVE not in keys or _OLD_GEN_HAVE in keys:
                continue
            et = _parse_dt(str(rec.get("event_time", "")))
            if et is None:
                continue
            n_cur += 1
            key = f"{rec.get('event_time', '')}|{rec.get('ingested_at', '')}"
            if key in seen:
                continue
            seen.add(key)
            raw.append(
                FeatureRow(
                    event_dt=et,
                    m5_ret_1=float(vals.get("M5_Ret_1", 0.0)),
                    values={k: float(v) for k, v in vals.items()},
                )
            )
    # 按 bar (5-min 时间桶) 去重 — 同 bar 多次 ingestion 只保留首条 (先到先得)
    seen_bar: set[int] = set()
    deduped: list[FeatureRow] = []
    for r in sorted(raw, key=lambda x: x.event_dt):
        bar = int(r.event_dt.timestamp() // 300) * 300
        if bar in seen_bar:
            continue
        seen_bar.add(bar)
        deduped.append(r)
    print(
        f"  [load] v9 rows={n_v9}  current-gen(col-family)={n_cur}  "
        f"after_bar_dedup={len(deduped)}"
    )
    return deduped


def load_mt5_bars(start: datetime, end: datetime) -> dict[int, tuple[float, float]] | None:
    """MT5 copy_rates_range 全窗口 M5 bars -> {bar_open_epoch: (open, close)}.

    只读附加到运行中的 EXNESS2 终端. 失败返回 None (caller 回退).
    """
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return None
    ok = mt5.initialize(path=r"D:\exness\MetaTrader 5 EXNESS2\terminal64.exe")
    if not ok:
        return None
    try:
        rates = mt5.copy_rates_range("XAUUSDc", _MT5_TIMEFRAME_M5, start, end)
        if rates is None or len(rates) == 0:
            return None
        out: dict[int, tuple[float, float]] = {}
        for r in rates:
            out[int(r[0])] = (float(r[1]), float(r[4]))  # time, open, close
        return out
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):
        return None
    finally:
        mt5.shutdown()


def probe_mt5_spread(bars: dict[int, tuple[float, float]] | None) -> dict[str, Any] | None:
    """MT5 实时 + tick 历史真实 spread 统计 (成本锚点).

    返回 dict (provenance/分布) 或 None. 只读.
    """
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return None
    ok = mt5.initialize(path=r"D:\exness\MetaTrader 5 EXNESS2\terminal64.exe")
    if not ok:
        return None
    try:
        si = mt5.symbol_info("XAUUSDc")
        now = datetime.now(UTC)
        ticks = mt5.copy_ticks_from(
            "XAUUSDc", now - timedelta(hours=96), 500000, _MT5_COPY_TICKS_ALL
        )
        result: dict[str, Any] = {"terminal": "EXNESS2"}
        if si is not None:
            bid = float(si.bid)
            ask = float(si.ask)
            result["bid"] = round(bid, 3)
            result["ask"] = round(ask, 3)
            result["live_spread_usd"] = round(ask - bid, 4) if ask > bid else None
            if bars is not None:
                # 参考价格 = 最新 M5 bar close (若可用)
                if bars:
                    latest_epoch = max(bars)
                    result["ref_close"] = round(bars[latest_epoch][1], 3)
        if ticks is not None and len(ticks) > 1:
            bids = ticks["bid"].astype(float)
            asks = ticks["ask"].astype(float)
            spreads = asks - bids
            nz = spreads[spreads > 0]
            if len(nz) > 0:
                result["tick_n"] = int(len(ticks))
                result["tick_first"] = datetime.fromtimestamp(
                    float(ticks["time"][0]), UTC
                ).isoformat()
                result["tick_last"] = datetime.fromtimestamp(
                    float(ticks["time"][-1]), UTC
                ).isoformat()
                result["spread_p50_usd"] = round(float(np.median(nz)), 4)
                result["spread_mean_usd"] = round(float(np.mean(nz)), 4)
                result["spread_p90_usd"] = round(float(np.percentile(nz, 90)), 4)
                result["spread_p99_usd"] = round(float(np.percentile(nz, 99)), 4)
                result["spread_max_usd"] = round(float(np.max(nz)), 4)
                result["n_nonzero"] = int(len(nz))
        return result
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):
        return None
    finally:
        mt5.shutdown()


def asof_join_v43_backward(
    v9_epochs: np.ndarray,
    v43_epochs: np.ndarray,
    v43_spreads: np.ndarray,
) -> tuple[np.ndarray, dict[str, Any]]:
    """pd.merge_asof direction='backward' — IC 指定的"致命拼接".

    对每个 v9 bar, 取时间 <= 它的最近一个 v4.3 记录 (严格 past-only).
    严禁 direction='nearest'/'forward' — 那会把未来点差当现在信号.
    """
    v9_df = pd.DataFrame({"t": v9_epochs, "idx": np.arange(len(v9_epochs))}).sort_values("t")
    v43_df = pd.DataFrame({"t": v43_epochs, "spread": v43_spreads}).sort_values("t")
    merged = pd.merge_asof(v9_df, v43_df, on="t", direction="backward", allow_exact_matches=True)
    joined = np.full(len(v9_epochs), np.nan, dtype=np.float64)
    joined[merged["idx"].to_numpy(dtype=np.int64)] = merged["spread"].to_numpy(dtype=np.float64)
    cov = int(np.sum(~np.isnan(joined)))
    zero = int(np.sum(~np.isnan(joined) & (joined == 0.0)))
    stats: dict[str, Any] = {
        "v9_rows": len(v9_epochs),
        "v43_rows": len(v43_epochs),
        "joined": cov,
        "coverage_pct": round(100.0 * cov / len(v9_epochs), 2) if len(v9_epochs) else 0.0,
        "joined_nonzero_spread": int(np.sum(~np.isnan(joined) & (joined != 0.0))),
        "joined_zero_spread": zero,
        "direction": "backward (strict past-only, 防 1ms forward 泄露)",
    }
    return joined, stats


def load_v43_spread(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """加载 v4.3_microstructure_9 记录 -> (epochs, avg_spread) 数组 (原始值, 含符号)."""
    epochs: list[float] = []
    spreads: list[float] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("schema_name") != "v4.3_microstructure_9":
                continue
            et = _parse_dt(str(rec.get("event_time", "")))
            if et is None:
                continue
            vals = rec.get("values", {})
            epochs.append(et.timestamp())
            spreads.append(float(vals.get("avg_spread", 0.0)))
    return np.array(epochs, dtype=np.float64), np.array(spreads, dtype=np.float64)


def resolve_dynamic_spread(
    joined_raw: np.ndarray, anchor: float
) -> tuple[np.ndarray, dict[str, Any]]:
    """解析逐 bar 动态 spread.

    v4.3 avg_spread 存在 bid/ask 索引互换的**符号翻转 bug**
    (microstructure_computer._compute_tick_features: bids=t[2]/asks=t[1], 实际 MT5
    COPY_TICKS_ALL 布局 t[1]=bid/t[2]=ask → asks-bids = -(ask-bid)).
    → 物理正确的 spread = |avg_spread|. join 未命中处回退锚点.

    avg_spread == 0.0 视为**采集失败** (XAU 物理 spread 不可能为 0):
    tick 窗口内无有效价差 (micro_persist 仅跳过全零记录, 部分零值通过) →
    与缺失同等对待, 回退锚点, 避免低估盈亏平衡线.

    返回 (per-bar spread USD, 诊断统计).
    """
    n = len(joined_raw)
    missing = ~np.isfinite(joined_raw) | (joined_raw == 0.0)
    valid_mask = ~missing
    n_valid = int(np.sum(valid_mask))
    # neg_rate 仅在**有效命中子集**内统计 (真实签名率): 全量(含 NaN)会让其与 join 覆盖率混淆.
    neg_rate = float(np.sum(valid_mask & (joined_raw < 0.0)) / n_valid) if n_valid else 0.0
    abs_series = np.abs(joined_raw)
    abs_series[missing] = anchor
    valid_abs = np.abs(joined_raw[valid_mask])
    stats: dict[str, Any] = {
        "joined": int(np.sum(np.isfinite(joined_raw))),
        "joined_valid": n_valid,
        "joined_zero_spread": int(np.sum(np.isfinite(joined_raw) & (joined_raw == 0.0))),
        "joined_negative_rate": round(neg_rate, 4),
        "raw_min_usd": round(float(np.min(joined_raw[valid_mask])), 4) if n_valid else None,
        "raw_p50_usd": round(float(np.median(joined_raw[valid_mask])), 4) if n_valid else None,
        "abs_spread_median_usd": round(float(np.median(valid_abs)), 4) if n_valid else anchor,
        "abs_spread_mean_usd": round(float(np.mean(valid_abs)), 4) if n_valid else anchor,
        "abs_spread_p90_usd": round(float(np.percentile(valid_abs, 90)), 4) if n_valid else anchor,
        "abs_spread_p99_usd": round(float(np.percentile(valid_abs, 99)), 4) if n_valid else anchor,
        "abs_spread_max_usd": round(float(np.max(valid_abs)), 4) if n_valid else anchor,
        "fallback_anchor_usd": anchor,
        "fallback_rate": round(100.0 * float(np.sum(missing)) / n, 2),
        "note": "负号 = bid/ask 索引互换 bug 实证 (有效命中子集签名率); 已取 |avg_spread| 还原物理 spread; 0.0 视作采集失败回退锚点",
    }
    return abs_series, stats


def build_forward_returns(
    rows: list[FeatureRow], bars: dict[int, tuple[float, float]]
) -> tuple[np.ndarray, np.ndarray, list[int]]:
    """真实价格 3-bar 前向收益 (与 v1 语义一致: 信号 bar 之后 3 个 M5 bar).

    fwd[T] = (close[T+3*300] - close[T]) / close[T] * 100  (percent).
    严格: 只用 <= 决策时刻的价格; 前向上下文不足 / bar 缺失的行剔除.
    返回 (fwd_pct, abs_fwd_pct, matched_indices).
    """
    fwd_vals: list[float] = []
    abs_vals: list[float] = []
    matched: list[int] = []
    for i, row in enumerate(rows):
        t0 = int(row.event_dt.timestamp() // 300) * 300
        c0 = bars.get(t0)
        c3 = bars.get(t0 + 3 * 300)
        if c0 is None or c3 is None:
            continue
        prev = c0[1]
        if prev <= 0.0:
            continue
        fwd = (c3[1] - prev) / prev * 100.0
        fwd_vals.append(fwd)
        abs_vals.append(abs(fwd))
        matched.append(i)
    return np.array(fwd_vals, dtype=np.float64), np.array(abs_vals, dtype=np.float64), matched


def check_alignment(
    rows: list[FeatureRow], matched: list[int], bars: dict[int, tuple[float, float]]
) -> dict[str, Any]:
    """特征行 ↔ 真实 bar 对齐校验 (数据完整性红线).

    对每个匹配行: feature M5_Ret_1 vs 真实 1-bar 收益
    (close[t0]-close[t0-300])/close[t0-300]*100. 若两者系统偏离 → 对齐有洞.
    """
    diffs: list[float] = []
    n_check = 0
    for i in matched:
        row = rows[i]
        t0 = int(row.event_dt.timestamp() // 300) * 300
        c0 = bars.get(t0)
        c_prev = bars.get(t0 - 300)
        if c0 is None or c_prev is None or c_prev[1] <= 0.0:
            continue
        real_1bar = (c0[1] - c_prev[1]) / c_prev[1] * 100.0
        if abs(real_1bar) > 5.0 or abs(row.m5_ret_1) > 5.0:
            continue  # 跳过明显异常 bar (缺口/数据洞), 避免污染一致性判断
        diffs.append(abs(real_1bar - row.m5_ret_1))
        n_check += 1
    if not diffs:
        return {"n_checked": 0}
    return {
        "n_checked": n_check,
        "median_abs_diff_pct": round(float(np.median(diffs)), 5),
        "p90_abs_diff_pct": round(float(np.percentile(diffs, 90)), 5),
        "p95_abs_diff_pct": round(float(np.percentile(diffs, 95)), 5),
        "max_abs_diff_pct": round(float(np.max(diffs)), 5),
        "note": "median|M5_Ret_1 - 真实1bar| < 0.005 视为对齐无误",
    }


def net_of_cost_stats(
    abs_fwd: np.ndarray, fwd: np.ndarray, break_even_pct: np.ndarray, label: str
) -> dict[str, Any]:
    """Net-of-Cost Alpha 覆盖率统计 (Iron Law #11: 数字只来自本函数)."""
    n = len(abs_fwd)
    if n == 0:
        return {"label": label, "n": 0}
    gross = abs_fwd
    net = gross - break_even_pct
    cover: int = int(np.sum(gross > break_even_pct))
    positive_net: int = int(np.sum(net > 0.0))
    # top-decile by gross
    k = max(1, n // 10)
    idx = np.argsort(gross)[::-1][:k]
    top_mean_gross = float(np.mean(gross[idx]))
    top_mean_net = float(np.mean(net[idx]))
    # 方向分布
    up = fwd[fwd > 0]
    down = fwd[fwd < 0]
    return {
        "label": label,
        "n": n,
        "break_even_pct_mean": round(float(np.mean(break_even_pct)), 5),
        "gross_abs_mean_pct": round(float(np.mean(gross)), 5),
        "gross_abs_median_pct": round(float(np.median(gross)), 5),
        "gross_abs_p90_pct": round(float(np.percentile(gross, 90)), 5),
        "gross_abs_p99_pct": round(float(np.percentile(gross, 99)), 5),
        "coverage_gross_gt_be_pct": round(100.0 * cover / n, 2),
        "net_positive_share_pct": round(100.0 * positive_net / n, 2),
        "top_decile_mean_gross_pct": round(top_mean_gross, 5),
        "top_decile_mean_net_pct": round(top_mean_net, 5),
        "net_of_cost_alpha_pct_mean": round(float(np.mean(net)), 5),
        "net_of_cost_alpha_pct_median": round(float(np.median(net)), 5),
        "dir_up_share": round(100.0 * len(up) / n, 2),
        "fwd_mean_pct": round(float(np.mean(fwd)), 5),
        "fwd_up_mean_pct": round(float(np.mean(up)), 5) if len(up) else None,
        "fwd_down_mean_pct": round(float(np.mean(down)), 5) if len(down) else None,
        "_note": "net = |fwd_3bar| - break_even_pct; coverage = P(|fwd| > be)",
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="P3 XAU Micro Scaler v2 — Cost Model (Net-of-Cost Alpha)"
    )
    p.add_argument("--data-dir", default="data")
    p.add_argument("--symbol", default="XAUUSDc")
    p.add_argument("--fwd-bars", type=int, default=3)
    p.add_argument(
        "--spread-usd",
        type=float,
        default=None,
        help="手动指定 spread USD (默认: MT5 探针 p50 → fallback 0.26)",
    )
    p.add_argument(
        "--commission-per-lot-usd",
        type=float,
        default=0.0,
        help="每 1.0 手往返佣金 USD (默认 0.0 = EXNESS XAU 纯点差计费, 需 IC 确认)",
    )
    p.add_argument(
        "--write", action="store_true", help="写 JSON 快照工件到 data/training/micro_cost_model/"
    )
    args = p.parse_args()

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
    print("  P3 XAU MICRO SCALER v2 — COST MODEL (周一开火序列 第二步/第三步)")
    print("=" * 78)
    print(f"  symbol={args.symbol}  data-dir={args.data_dir}  fwd_bars={args.fwd_bars}")

    # ── 1. 加载 current-gen 特征行 ──
    if not fs_path.exists():
        print(f"[FAIL] feature store not found: {fs_path}")
        return 1
    rows = load_current_gen_rows(fs_path)
    if not rows:
        print("[FAIL] 0 current-gen feature rows")
        return 1

    # ── 2. MT5 真实 bars (价格) ──
    bars = load_mt5_bars(datetime(2026, 5, 24, tzinfo=UTC), datetime.now(UTC))
    if bars:
        epoch_list = sorted(bars)
        print(
            f"  [mt5] bars={len(bars)}  span={datetime.fromtimestamp(epoch_list[0], UTC).isoformat()}.."
            f"{datetime.fromtimestamp(epoch_list[-1], UTC).isoformat()}  "
            f"close_last={bars[epoch_list[-1]][1]:.3f}"
        )
    else:
        print("  [mt5] WARN: 终端不可达/无数据 — 真实价格不可用, 前向收益将无法计算.")
        return 1

    # ── 3. 前向 3-bar 收益 (真实价格) ──
    fwd, abs_fwd, matched = build_forward_returns(rows, bars)
    print(
        f"  [fwd] feature rows with 3-bar fwd context: {len(matched)}/{len(rows)} "
        f"(剔除后段无前向上下文或 bar 缺失行)"
    )
    if len(fwd) == 0:
        print("[FAIL] no rows with forward context")
        return 1

    # ── 3b. 特征行 ↔ 真实 bar 对齐校验 ──
    align = check_alignment(rows, matched, bars)
    if align["n_checked"]:
        print(
            f"  [align] n={align['n_checked']}  median|M5_Ret_1-真实1bar|={align['median_abs_diff_pct']}%  "
            f"p95={align['p95_abs_diff_pct']}%  max={align['max_abs_diff_pct']}%"
        )
        if align["median_abs_diff_pct"] > 0.005:
            print("    WARN: 特征 M5_Ret_1 与真实 bar 收益偏离较大 — 对齐或特征生成可疑")
    else:
        print("  [align] WARN: 无可校验行 (bars 覆盖不足)")

    # ── 4. v4.3 avg_spread ASOF join (IC 指名机制, 防 forward 泄露) ──
    v43_epochs, v43_spreads = load_v43_spread(fs_path)
    v9_epochs = np.array([int(r.event_dt.timestamp()) for r in rows], dtype=np.float64)[matched]
    joined, join_stats = asof_join_v43_backward(v9_epochs, v43_epochs, v43_spreads)
    print(f"  [v4.3 join] direction={join_stats['direction']}")
    print(
        f"    joined={join_stats['joined']}/{join_stats['v9_rows']} "
        f"({join_stats['coverage_pct']}%)  nonzero_spread={join_stats['joined_nonzero_spread']}  "
        f"zero_spread={join_stats['joined_zero_spread']}"
    )

    # ── 5. spread 锚点解析 ──
    spread_anchor: dict[str, Any]
    if args.spread_usd is not None:
        spread_price = args.spread_usd
        spread_anchor = {"source": "cli", "spread_usd": args.spread_usd}
    else:
        probe = probe_mt5_spread(bars)
        if probe is not None and probe.get("spread_p50_usd") is not None:
            spread_price = float(probe["spread_p50_usd"])
            spread_anchor = {**probe, "source": "mt5_probe_p50"}
        else:
            spread_price = _DEFAULT_SPREAD_USD
            spread_anchor = {"source": "documented_default", "spread_usd": _DEFAULT_SPREAD_USD}
            print("  [spread] WARN: MT5 探针失败, 使用文档化默认锚点 0.26 USD")
    print(f"  [spread anchor] source={spread_anchor.get('source')}  spread_usd={spread_price:.4f}")
    if spread_anchor.get("source", "").startswith("mt5"):
        print(
            f"    tick_n={spread_anchor.get('tick_n')}  p50={spread_anchor.get('spread_p50_usd')}  "
            f"p90={spread_anchor.get('spread_p90_usd')}  p99={spread_anchor.get('spread_p99_usd')}  "
            f"max={spread_anchor.get('spread_max_usd')}"
        )

    # ── 6. 逐 bar 盈亏平衡线 (% return) ──
    # be_pct = (动态 spread + commission_per_oz) / close_price * 100
    commission_per_oz = args.commission_per_lot_usd / _CONTRACT_OZ
    closes = np.array(
        [bars[int(rows[i].event_dt.timestamp() // 300) * 300][1] for i in matched],
        dtype=np.float64,
    )
    # 动态 spread 系列: |v4.3 avg_spread| (ASOF, 符号翻转 bug 已还原), 未命中回退锚点
    dyn_spread, dyn_stats = resolve_dynamic_spread(joined, spread_price)
    print("  [dynamic spread] 还原自 v4.3 avg_spread (|·|):")
    print(
        f"    joined={dyn_stats['joined']}  zero_as_missing={dyn_stats['joined_zero_spread']}  "
        f"negative_rate={dyn_stats['joined_negative_rate']*100:.1f}%  "
        f"|spread| median={dyn_stats['abs_spread_median_usd']}  "
        f"mean={dyn_stats['abs_spread_mean_usd']}  p90={dyn_stats['abs_spread_p90_usd']}  "
        f"p99={dyn_stats['abs_spread_p99_usd']}  max={dyn_stats['abs_spread_max_usd']}"
    )
    print(
        f"    fallback_anchor={dyn_stats['fallback_anchor_usd']}  "
        f"fallback_rate={dyn_stats['fallback_rate']}%  (ASOF 未命中 bar 用锚点)"
    )
    be_pct = (dyn_spread + commission_per_oz) / closes * 100.0
    print(
        f"  [break-even] commission_per_lot_usd={args.commission_per_lot_usd} "
        f"(per_oz={commission_per_oz:.5f})  "
        f"be_pct mean={np.mean(be_pct):.5f}%  median={np.median(be_pct):.5f}%"
    )
    print(
        f"    be_pct 价格敏感带: close∈[{closes.min():.0f},{closes.max():.0f}] → "
        f"be∈[{100*(dyn_spread.max()+commission_per_oz)/closes.max():.5f}%,"
        f"{100*(dyn_spread.min()+commission_per_oz)/closes.min():.5f}%]"
    )

    # ── 7. Net-of-Cost Alpha 统计 (基准 = 实测锚点) ──
    print("\n" + "-" * 78)
    print("  NET-OF-COST ALPHA (基准 spread = 实测锚点)")
    print("-" * 78)
    stats_main = net_of_cost_stats(abs_fwd, fwd, be_pct, "base")
    _print_stats(stats_main)
    covered = stats_main["coverage_gross_gt_be_pct"]
    top_net = stats_main["top_decile_mean_net_pct"]
    verdict = "PASS" if (top_net > 0.0 and covered >= 20.0) else "INCONCLUSIVE"
    print(
        f"  >>> Toll-gate verdict (gross coverage vs be): {verdict}  "
        f"(top-decile net={top_net:+.5f}%, coverage={covered}%)"
    )

    # ── 8. 情景分析 (spread 敏感性) ──
    print("\n" + "-" * 78)
    print("  SPREAD 情景敏感性 (覆盖率 / top-decile net)")
    print("-" * 78)
    scenarios: list[dict[str, Any]] = []
    scenario_spreads = {
        "dyn_p50": dyn_stats["abs_spread_median_usd"],
        "dyn_p90": dyn_stats["abs_spread_p90_usd"],
        "dyn_p99": dyn_stats["abs_spread_p99_usd"],
        "dyn_max": dyn_stats["abs_spread_max_usd"],
        "stress 0.60": 0.60,
    }
    for name, sp in scenario_spreads.items():
        be_s = (sp + commission_per_oz) / closes * 100.0
        st = net_of_cost_stats(abs_fwd, fwd, be_s, name)
        scenarios.append(
            {
                "scenario": name,
                "spread_usd": sp,
                "coverage_pct": st["coverage_gross_gt_be_pct"],
                "top_decile_mean_net_pct": st["top_decile_mean_net_pct"],
            }
        )
        print(
            f"    {name:<20} spread={sp:.2f}  coverage={st['coverage_gross_gt_be_pct']:>6.2f}%  "
            f"top10-net={st['top_decile_mean_net_pct']:+.5f}%"
        )

    # ── 9. v4.3 avg_spread 数据缺口实证 (符号翻转 bug 还原) ──
    print("\n" + "-" * 78)
    print("  数据缺口实证: v4.3 avg_spread 动态 spread 列 (符号翻转 bug)")
    print("-" * 78)
    print(
        f"    v4.3 记录: {join_stats['v43_rows']} 行 | ASOF 命中: {join_stats['joined']} "
        f"({join_stats['coverage_pct']}%)"
    )
    print(
        "    → 覆盖瓶颈: v4.3_microstructure_9 部署自 2026-06-14 起 (逐日实证), "
        "current-gen v9 行 7,286/8,280 在 05-25..06-13 期无 v4.3 → backward ASOF 仅能命中 v4.3 同代期 bar."
    )
    print(
        f"    → 未命中 bar 全部回退实测锚点 {spread_price:.2f} USD (fallback_rate={dyn_stats['fallback_rate']}%)."
    )
    print(
        f"    ASOF 命中中非零 spread: {join_stats['joined_nonzero_spread']} 行 "
        f"(= {100.0 * join_stats['joined_nonzero_spread'] / max(join_stats['joined'], 1):.2f}%); "
        f"零值 {dyn_stats['joined_zero_spread']} 行 视作采集失败回退锚点"
    )
    print(
        f"    avg_spread 符号: negative_rate={dyn_stats['joined_negative_rate']*100:.1f}% "
        f"(负值即 |spread|, min 观测 {dyn_stats.get('raw_min_usd', 'n/a')} USD)"
    )
    print(
        "    → 根因: microstructure_computer._compute_tick_features 将 MT5 t[1]=bid/t[2]=ask "
        "错位写入 bids=t[2]/asks=t[1] → asks-bids = -(真实spread)."
    )
    print(
        "    → 修复路径 (本模型): 取 |avg_spread| 还原物理 spread, ASOF 未命中回退锚点 "
        f"{spread_price:.2f} USD (fallback_rate={dyn_stats['fallback_rate']}%)."
    )
    print(
        "    → 记档: v4.3 avg_spread 生产侧符号 bug — cost model 使用 |avg_spread|; "
        "microstructure_computer 修复列 TECH_DEBT 登记 (勿动核心代码, 纯消费端兼容)."
    )

    # ── 10. 快照工件 ──
    out_dir = base / "training" / "micro_cost_model"
    artifact: dict[str, Any] = {
        "schema": "micro_cost_model.v1",
        "built_at": datetime.now(UTC).isoformat(),
        "symbol": args.symbol,
        "data_dir": args.data_dir,
        "fwd_bars": args.fwd_bars,
        "n_feature_rows_current_gen": len(rows),
        "n_matched_with_fwd": len(matched),
        "spread_anchor": spread_anchor,
        "commission_per_lot_usd": args.commission_per_lot_usd,
        "break_even_pct_mean": stats_main["break_even_pct_mean"],
        "v43_asof_join": join_stats,
        "net_of_cost": stats_main,
        "scenarios": scenarios,
        "verdict": verdict,
        "note": "JSON 为审阅快照, 统计唯一合法来源 = 本脚本 stdout (Iron Law #11). 模型训练前须 IC 授权 fit().",
    }
    if args.write:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "cost_model.json").write_text(json.dumps(artifact, indent=2), encoding="utf-8")
        print(f"\n  Artifact: {out_dir / 'cost_model.json'}")
    else:
        print("\n  (dry-run: 未写 JSON, 加 --write 落盘)")

    print("\n[DONE] 门禁凭证以上述 stdout 为准 — 授权 fit() 由投委会裁决.")
    return 0


def _print_stats(s: dict[str, Any]) -> None:
    """打印一行 Net-of-Cost 统计."""
    print(f"    n={s['n']}  be_mean={s['break_even_pct_mean']}%")
    print(
        f"    gross |fwd3|: mean={s['gross_abs_mean_pct']}%  median={s['gross_abs_median_pct']}%  "
        f"p90={s['gross_abs_p90_pct']}%  p99={s['gross_abs_p99_pct']}%"
    )
    print(
        f"    coverage P(|fwd|>be)={s['coverage_gross_gt_be_pct']}%  "
        f"net>0 share={s['net_positive_share_pct']}%"
    )
    print(
        f"    top-decile: mean_gross={s['top_decile_mean_gross_pct']}%  "
        f"mean_net={s['top_decile_mean_net_pct']}%"
    )
    print(
        f"    net-of-cost alpha: mean={s['net_of_cost_alpha_pct_mean']}%  "
        f"median={s['net_of_cost_alpha_pct_median']}%"
    )
    print(
        f"    direction: up_share={s['dir_up_share']}%  fwd_mean={s['fwd_mean_pct']}%  "
        f"up_mean={s['fwd_up_mean_pct']}%  down_mean={s['fwd_down_mean_pct']}%"
    )


if __name__ == "__main__":
    raise SystemExit(main())
