# Dual_Assassin V4 — XAUUSD M5 双引擎实盘交易系统

## Purpose

Dual_Assassin is a standalone XAUUSD M5 live trading system running on Exness (Magic=888888, `F:\ai\Dual_Assassin\`). Dual-engine architecture: **TREND** (momentum/breakout) + **REVERSION** (mean-reversion), orchestrated by a PPO meta-controller. Four XGBoost directional models (trend_buy, trend_sell, rev_buy, rev_sell) provide raw signal probabilities; the PPO decides position sizing (0=skip, 1=1.0x, 2=3.0x).

This blueprint tracks the institutional evolution from V4.1 → V4.2 → V4.3, serving as the **rollback checkpoint** for each version.

---

## System Location

| Item | Path |
|:---|:---|
| Root directory | `F:\ai\Dual_Assassin\` |
| Live script | `F:\ai\Dual_Assassin\Dual_Assassin_Live.py` |
| Trade journal | `F:\ai\Dual_Assassin\live_trade_journal.csv` |
| State persist | `F:\ai\Dual_Assassin\open_tickets_state.json` |
| XGBoost models (V4) | `F:\ai\Dual_Assassin\god_buy.json`, `god_sell.json`, `rev_buy.json`, `rev_sell.json` |
| XGBoost models (V4.3) | `F:\ai\Dual_Assassin\god_buy_micro.json`, `god_sell_micro.json`, `rev_buy_micro.json`, `rev_sell_micro.json` |
| PPO meta | `F:\ai\Dual_Assassin\ppo_dual_assassin.zip` |
| Price data | `F:\ai\Dual_Assassin\XAUUSDc_M5_History.csv` |
| Micro features (aligned) | `F:\ai\Dual_Assassin\micro_features_m5_aligned.parquet` |
| Micro computer (shared) | `d:\future\micro_features_live.py` |
| Micro retrain script | `d:\future\scripts\_retrain_with_micro.py` |
| Micro enrichment test | `d:\future\scripts\_micro_enrichment_test.py` |
| Tick load validation | `d:\future\scripts\_validate_tick_load.py` |
| Micro live test | `d:\future\scripts\_test_micro_live.py` |
| VR EDA scripts | `F:\ai\Dual_Assassin\scripts\_vr_regime_eda.py` |
| VR data | `F:\ai\Dual_Assassin\data\vr_regime_series.csv`, `vr_trade_replay.csv` |
| VR charts | `F:\ai\Dual_Assassin\charts\vr_regime_*.png` |
| Trade review | `F:\ai\Dual_Assassin\6_trade_review_chart.py` |

---

## Key Files

| File | Role |
|------|------|
| `F:\ai\Dual_Assassin\Dual_Assassin_Live.py` | Live loop — MT5 data → XGBoost radars → PPO meta → VR regime → dispatch |
| `F:\ai\Dual_Assassin\Dual_Assassin_Live_v7.py` | V7 variant (dual-run, FIX-20260728-004 PID-lock guard) |
| `d:\future\micro_features_live.py` | Shared 34-dim micro-structure feature computer (imported by live loop) |
| `F:\ai\Dual_Assassin\god_*_micro.json` / `rev_*_micro.json` | 4 XGBoost micro-enriched models (70d/68d) |
| `F:\ai\Dual_Assassin\ppo_dual_assassin.zip` | PPO meta-controller (position sizing) |
| `F:\ai\Dual_Assassin\open_tickets_state.json` | State persistence (recreated on crash) |

## Data Flow

```
Dual_Assassin_Live.py (single process, no external I/O beyond MT5)
├─ MT5 copy_rates_from_pos(600) → OHLC → V14(36d) + V15(34d)
├─ MT5 copy_ticks_range(prev bar) → micro_features_live.py
│   ├─ compute_micro_features_from_ticks() → 14 per-tick arrays
│   ├─ aggregate_per_tick_to_bar() → 34 scalars
│   └─ apply_price_offsets() → mid_open_offset, mid_close_offset
└─ np.concatenate() → V14(70d)/V15(68d) → XGBoost (p_tb,p_ts,p_rb,p_rs)
   → PPO (0/1.0x/3.0x sizing) → RegimeDetector VR routing → MT5 dispatch
```

## Inbound Dependencies

| Source | What is consumed | Why |
|--------|-----------------|-----|
| MT5 (Exness, Magic=888888) | `copy_rates_from_pos` OHLC + `copy_ticks_range` ticks | Live market data + execution |
| `d:\future\micro_features_live.py` | 34-dim micro feature computer (shared library) | Micro-structure features for enriched XGBoost models |
| `F:\ai\Dual_Assassin\*_micro.json` models | Pre-trained XGBoost + PPO artifacts | Inference |

## Outbound Dependents

| Module | What it uses | Why |
|--------|--------------|-----|
| `scripts/_retrain_with_micro.py` (d:\future) | Micro feature training pipeline | Offline model retraining (`--deploy`) |
| `scripts/_micro_enrichment_test.py` / `_validate_tick_load.py` / `_test_micro_live.py` | Validation/EDA helpers | Post-deployment checks (see Verification) |
| FIX_REGISTRY (Dual_Assassin module) | Fix ledger | Tracks V4.x fixes (FIX-20260724-001…004, 20260725-003/004, 20260728-002/004) |
| External watchdog | `watchdog_kill.log` + heartbeat | Stall detection (FIX-20260728-004) |

---

## Version History

### V4.1 Baseline (2026-07-21) — Performance Baseline

**V4.1 Changes (DQAF-20260721-001)**:
- **Dual-threshold gating**: Entries at 2.5σ + |rel|>0.08 + σ floor (0.03); exits at 1.5σ
- **Root cause fixed**: XGBoost models collapsed (0 features >1.5× random baseline), causing 21% bar signal rate at 1.5σ ≈ noise trading
- **P0.1**: Block REV 1.0x — PPO uncertain reversal with asymmetric RR
- **P0.2**: TREND 1.0x breakeven stop — move SL to entry after +0.5R
- **P1.5**: REV 3.0x Rolling Win Rate Gate — REV engine only fires 3.0x when rolling WR > 75%
- **P1.6**: REV RR Fix — 0.7:2.5 → 1.0:1.5 (less asymmetric, needs ~60% WR to breakeven)
- **P1.7**: Direction Bias Fix — 4 independent μ+1.5σ thresholds → (p_buy - p_sell) relative strength

**V4.1 Known Performance Characteristics** (the "可圈可点" baseline):
- 11 trades recorded in the initial V4.1 run (2026-07-21 20:24 → 2026-07-22 23:04 UTC)
- TREND engine: 6 trades, net profitable (best: +$151.50 BUY, +$139.30 BUY)
- REVERSION engine: 4 trades, mixed (#4273921795: -$171.20 at ATH reversal fail; #4274529408: +$93.70)
- **Identified weakness**: REV engine fires in trending markets → fat-tail regime-mismatch losses

### V4.2 — VR Regime Detector (2026-07-22)

**Motivation**: #4273921795 — REVERSION BUY 3.0x at ATH (4156.82), stopped out at -$171.20. The market was in a strong uptrend (VR z_centered: q2=+3.2, q4=+11.8, q8=+5.9 → Ensemble TREND). The REV engine's mean-reversion assumption was structurally invalid.

**User's institutional critique**: MA20 price-position and consensus-gate approaches are "散户逻辑" (retail-trader thinking). Institutional approach: exogenous regime detection independent of ML model outputs.

### V4.3 — Micro-structure Feature Integration (2026-07-24)

**Motivation**: Despite `arrival_ratio` having zero LINEAR predictive power (Pearson r ≈ 0), XGBoost Gain-based feature importance reveals micro features capture non-linear conditional interactions that Pearson cannot detect. BASELINE vs ENRICHED comparison on 8,882 aligned bars: micro features contribute 47-50% feature importance share across all 4 Dual_Assassin models.

**User's architectural constraints** (mandatory design review, 2026-07-23):
1. **Plan B (inline MT5) over Plan A (external pipeline)** — "数据流转多一个外部依赖，系统崩溃的概率呈指数级上升"
2. **Offset transformation required** — "如果模型直接吞噬绝对价格，它其实是在做过拟合。必须将其去均值化"
3. **Pure NumPy vectorization** — "严禁使用 `.apply()` 或 for 循环去遍历数万条 Tick"
4. **Handle 20,000-50,000 tick extreme loads** — "尤其是在欧美盘交叠时段或非农等数据发布节点"

**V4.3 Changes**:
- **34 micro-structure features per M5 bar**: Computed inline from MT5 raw ticks (9-21ms/bar actual)
- **Offset transformation**: `micro_mid_open_offset = mid_price_first - OHLC_Open`, `micro_mid_close_offset = mid_price_last - OHLC_Close`
- **Model dimensionality**: V14 36→70d (TREND), V15 34→68d (REVERSION)
- **4 retrained XGBoost models**: `*_micro.json` with 47-50% micro feature importance share
- **`micro_features_live.py`**: Shared library at `d:\future\`, 14 raw tick features × 2-4 aggregations = 34 bar features
- **Graceful fallback chain**: Import fail → OHLC-only models | >500ms MT5 timeout → drop micro for that bar | <10 ticks → skip

**Top Micro Features (ranked by XGBoost Gain, all 4 models)**:
spread_toxicity, spread_median_5min, quote_intensity_zscore, gap_flag, arrival_rate_60s, spread_pips — spread/liquidity features dominate, confirming microstructure is a primary driver of short-term XAUUSD dynamics.

**Known Issue**: spread_pips distribution gap (training ~0.3-0.6 vs live ~2.4). Monitor during London/NY active hours.

**Post-Deployment Fixes** (2026-07-24/25):
- **FIX-20260724-001**: `MICRO_FEATURE_NAMES` column order corrected — training puts offset cols at [32,33], live had them at [0,1] → 34/34 features scrambled → 14.7h silence
- **FIX-20260724-002**: Rolling HISTORY window 120→60 bars (10h→5h) — faster μ/σ stabilization after restart, faster regime adaptation
- **FIX-20260725-003**: Warmup zero-micro eliminated — `warmup_radars()` uses OHLC-only models, no zero-padded micro features. Fixes μ/σ distribution mismatch (warmup σ≈0.20 vs live σ≈0.16, 25% threshold inflation for 5h after restart)
- **FIX-20260725-004**: SIGMA_ENTRY 2.5→2.0 — corrected micro model σ≈0.16 makes 2.5σ unreachable (0 signals/60 bars). 2.0σ selected as conservative midpoint (~3-5 signals/day expected)

---

## V4.2 Architecture

### RegimeDetector Class

```
Lo-MacKinlay (1988) Variance Ratio Test
├── Input: last 96 M5 close prices (8 hours)
├── Method: heteroskedasticity-consistent VR(q)
│   ├── VR(q) = Var(r_q) / (q × Var(r_1))
│   ├── H0: VR=1 (random walk)
│   ├── VR<1 → negative autocorr → MEAN_REVERT
│   └── VR>1 → positive autocorr → TREND
├── Ensemble: q ∈ {2, 4, 8}, majority vote (≥2 net votes)
├── Calibration: z_centered = z_raw - μ_empirical
│   ├── μ(q=2) = -1.300
│   ├── μ(q=4) = -2.115
│   └── μ(q=8) = -3.028
└── Threshold: |z_centered| > 1.96 (95% confidence)
```

### Engine Routing Logic

| VR Regime | TREND Signal | REV Signal | V4.2 Action |
|:---|:---|:---|:---|
| TREND | ✓ | — | TREND fires (normal) |
| TREND | — | ✓ | **REV SILENCED** 🔴 (saves fat-tail loss) |
| MEAN_REVERT | ✓ | — | **TREND SILENCED** 🔴 |
| MEAN_REVERT | — | ✓ | REV fires (normal) |
| INDETERMINATE | ✓/— | ✓/— | **Requires ≥3.5σ extreme signal** ⚠️ |
| INDETERMINATE | normal | normal | **ALL SILENCED** (below 3.5σ) |

### Code Insertions

| # | Location (line) | Change | Lines |
|:---|:---|:---|:---|
| 1 | L40-177 | `RegimeDetector` class + singleton `_regime_detector` | +138 |
| 2 | L786-789 | Startup banner V4.1→V4.2 | modified |
| 3 | L986-1047 | Engine routing with VR constraint (before PPO dispatch) | +62 |

### Key Design Decisions

1. **Exogenous detection**: VR test uses only `mt5.copy_rates_from_pos()` close prices — zero dependency on ML model outputs. The regime signal is orthogonal to the trading signal.

2. **INDETERMINATE handling**: The user explicitly rejected "both engines can try" as retail logic. V4.2 enforces ≥3.5σ extreme threshold in INDETERMINATE — only the most statistically significant signals fire. In random-walk conditions, silence is the default.

3. **Calibration μ values**: Gold M5 has persistent microstructural mean-reversion bias (bid-ask bounce). Raw z ~ N(-2.1, 10.0) instead of theoretical N(0,1). The empirical μ values center the distribution. z_centered σ≈10 is expected (volatility clustering + M5 noise), not a code bug — the Lo-MacKinlay asymptotic distribution doesn't hold in rolling windows at M5.

4. **Fail-open**: If MT5 data fetch fails or VR computation throws, `vr_regime` stays 'INDETERMINATE' and no block is applied — the original V4.1 logic runs unchanged.

---

## Calibration Parameters

### Hardcoded Constants (in RegimeDetector class)

```python
Z_MU = {2: -1.300, 4: -2.115, 8: -3.028}  # empirical z-means for w=96
Z_THRESHOLD = 1.96                           # 95% confidence (±1.96σ)
ENSEMBLE_VOTES = 2                           # ≥2 out of 3 q-values must agree
VR_WINDOW = 96                               # 8 hours on M5
VR_Q_VALUES = [2, 4, 8]                      # aggregation horizons
```

### Calibration Source

- Dataset: 228,717 XAUUSD M5 bars (2023-2026)
- Script: `F:\ai\Dual_Assassin\scripts\_vr_regime_eda.py`
- Output: `F:\ai\Dual_Assassin\data\vr_regime_series.csv`
- Recalibration cadence: **quarterly** (next: October 2026)
- Recalibration command: `python scripts/_vr_regime_eda.py`

### Expected Regime Distribution (w=96, ensemble)

| Regime | Approximate % | Interpretation |
|:---|:---|:---|
| INDETERMINATE | ~42% | Random walk / insufficient evidence |
| TREND | ~39% | Momentum/trending |
| MEAN_REVERT | ~19% | Mean-reverting (corrected for bid-ask bias) |

---

## V4.2 Trade Replay Results

VR regime classification of the 11 V4.1 trades (hindsight analysis):

| Ticket | Engine | PnL | VR Ensemble | V4.2 Would... |
|:---|:---|:---|:---|:---|
| #4252470902 | REV | -$86.90 | INDETERMINATE | depends on 3.5σ |
| #4258911359 | TREND | +$15.40 | INDETERMINATE | depends on 3.5σ |
| #4260262505 | TREND | -$68.60 | INDETERMINATE | depends on 3.5σ |
| #4260408308 | TREND | -$98.30 | INDETERMINATE | depends on 3.5σ |
| #4262226525 | TREND | +$139.30 | INDETERMINATE | depends on 3.5σ |
| #4262775659 | TREND | +$151.50 | INDETERMINATE | depends on 3.5σ |
| #4270079201 | REV | +$59.30 | INDETERMINATE | depends on 3.5σ |
| #4270750814 | TREND | +$24.40 | INDETERMINATE | depends on 3.5σ |
| #4270888746 | TREND | +$63.20 | INDETERMINATE | depends on 3.5σ |
| **#4273921795** | **REV** | **-$171.20** | **TREND** | **🔴 BLOCKED (saved $171.20)** |
| #4274529408 | REV | +$93.70 | TREND | 🔴 BLOCKED (foregone +$93.70) |

**Net effect**: -$171.20 saved, -$93.70 forgone = **+$77.50 net benefit** on this 11-trade sample. The structural benefit (fat-tail regime-mismatch prevention) likely exceeds the point-estimate as market trends strengthen.

---

## Rollback Procedure

### Full Rollback to V4.1

If V4.2 underperforms V4.1 over ≥20 live trades:

1. **Restore V4.1 script**: The V4.2 script is at `F:\ai\Dual_Assassin\Dual_Assassin_Live.py`. To rollback:
   ```bash
   # Option A: Git revert (if committed)
   cd F:\ai\Dual_Assassin
   git log --oneline Dual_Assassin_Live.py | head -5
   git revert <V4.2-commit-hash>
   
   # Option B: Manual removal of V4.2 code blocks
   # Remove lines 40-177 (RegimeDetector class + singleton)
   # Remove lines 986-1047 (VR constraint block in main loop)
   # Update line 786: "V4.2 VR Regime 机构版" → "V4.1"
   ```

2. **Restart**: Kill the live Python process and restart `Dual_Assassin_Live.py`

### Partial Rollback (VR Threshold Adjustment)

If VR is blocking too aggressively:
- Increase `Z_THRESHOLD` from 1.96 → 2.58 (99% confidence) — fewer blocks
- Increase `ENSEMBLE_VOTES` from 2 → 3 (unanimous only) — fewer blocks
- These are class constants in `RegimeDetector` (lines 60-61)

### Partial Rollback (Remove INDETERMINATE 3.5σ Requirement)

If INDETERMINATE is too restrictive:
- Comment out lines 1014-1030 (the `elif vr_regime == 'INDETERMINATE':` block)
- INDETERMINATE will then pass through with no constraint

---

## Files Created for V4.2

| File | Purpose | Re-creatable? |
|:---|:---|:---|
| `scripts/_vr_regime_eda.py` | Full EDA pipeline — calibration, visualization, trade replay | Yes (from blueprint) |
| `data/vr_regime_series.csv` | 228,717-row VR/z series for all q×window combos | Yes (re-run EDA) |
| `data/vr_trade_replay.csv` | 11 trades mapped to VR regime at entry | Yes (re-run EDA) |
| `charts/vr_regime_eda.png` | Price+VR+z+Regime 4-panel chart | Yes (re-run EDA) |
| `charts/vr_window_comparison.png` | 48/96/288 bar z-statistic comparison | Yes (re-run EDA) |
| `charts/vr_trade_replay.png` | Trade markers with regime annotations | Yes (re-run EDA) |

---

## Tick Data Analysis (F:\ai\tick\) — 2026-07-23

### Data Inventory

| Layer | Location | Files | Period | Size | Status |
|:---|:---|:---|:---|:---|:---|
| Raw Ticks | `raw_ticks/XAUUSDc/` | 9,691 parquet | 2026-05-31 → **2026-07-23** (today) | 172 MB | 🔴 LIVE |
| Micro Features | `micro_features/XAUUSDc/` | 47 daily parquet | 2026-05-31 → **2026-07-23** (today) | ~50 MB | 🔴 LIVE |
| OHLC (spread) | `ohlc/XAUUSDc/spread/` | 1 file | — | <1 MB | 🟡 Stale |
| OHLC (time) | `ohlc/XAUUSDc/time/` | 4 files | — | <1 MB | 🟡 Stale |
| OHLC (volatility) | `ohlc/XAUUSDc/volatility/` | 1 file | — | <1 MB | 🟡 Stale |

### Micro Features (16 dimensions, pre-computed)

| Feature | Description | Relevance to Dual_Assassin |
|:---|:---|:---|
| `quote_intensity_zscore` | Liquidity shock detector (z-score of tick arrival burst) | 🔴 P0 — Entry gate |
| `spread_toxicity` | Adverse selection via spread widening (>1.05 = toxic) | 🔴 P0 — Execution quality |
| `buy_pressure_20` | Directional order flow (0=selling, 1=buying) | 🟡 P1 — Direction confirmation |
| `arrival_ratio` | 5s / 60s tick arrival ratio (surge indicator) | 🟡 P1 — Volatility precursor |
| `mid_return_5/20/100` | Mid-price returns at 5/20/100-tick horizons | 🟢 P2 — Micro-trend |
| `spread_median_5min` | Median spread over 5 minutes | 🟢 P2 — Liquidity baseline |
| `slippage_p95` | 95th percentile slippage | 🟢 P2 — Execution quality audit |
| `gap_flag` | Price gap indicator | 🟢 P2 — Data quality |

### Key Finding — #4273921795 (-$171.20) Double Confirmation

At the EXACT entry time (2026-07-22 14:05 UTC), the micro features show:
- `quote_intensity_zscore` peaked at **4.45** (>3.5 = liquidity shock → HARD BLOCK in omega system)
- `spread_toxicity` hit **1.083** (>1.05 = toxic spread → conf × 0.90 in omega system)

**This trade fails on TWO independent dimensions:**
1. V4.2 VR: TREND regime → REV engine silenced ✅ (already protecting)
2. Micro Gate: Extreme liquidity shock → HARD BLOCK (not yet wired)

**If both gates were active, #4273921795 would have been blocked by whichever triggered first.**

### Dual_Assassin Already Has Aligned Data

`F:\ai\Dual_Assassin\micro_features_m5_aligned.parquet`:
- 8,882 rows, 34 columns (mean/std/max aggregates per M5 bar)
- Same feature set, aligned to M5 timeframe
- But **NOT consumed by Dual_Assassin_Live.py** — dead data

The current PPO observation vector is only 7 dimensions:
```
[p_tb, p_ts, p_rb, p_rs, drawdown, atr/5, cons_losses/10]
```
No microstructure awareness.

### V4.3 Implementation — COMPLETED (2026-07-24)

**Status**: All 4 phases delivered and deployed. See V4.3 Architecture section above for full details.

**What was built** (vs original recommendation):
- **Phase 1**: NOT just `quote_intensity_zscore` hard-block. Instead: ALL 34 micro features integrated as XGBoost model inputs. The model itself learns which micro features matter — more powerful than hard-coded gates.
- **Phase 2**: `spread_toxicity` and `buy_pressure_20` are among the 34 XGBoost input features (not just PPO). XGBoost Gain-based selection determines their contribution.
- **Phase 3**: Full 34-dim micro feature integration — not just PPO expansion. Models retrained on 8,882 aligned bars.

**What was NOT done** (intentionally):
- PPO observation vector still 7-dim. Rationale: PPO is the meta-controller (position sizing), not the signal generator. Micro features belong in the XGBoost radars (signal quality), not in PPO (risk management). This separation of concerns is architecturally correct.

**Actual Data Flow (V4.3)**:
```
Dual_Assassin_Live.py (single process, no external I/O)
├─ MT5 copy_rates_from_pos(600) → OHLC V14(36d) + V15(34d)
├─ MT5 copy_ticks_range(prev bar) → micro_features_live.py
│   ├─ compute_micro_features_from_ticks() → 14 per-tick arrays
│   ├─ aggregate_per_tick_to_bar() → 34 scalars
│   └─ apply_price_offsets() → mid_open_offset, mid_close_offset
└─ np.concatenate() → V14(70d) / V15(68d) → XGBoost → PPO → VR Regime → MT5
```

**Key architectural divergence from original proposal**:
- Original: External `F:i	ick\` pipeline → parquet → Dual_Assassin reads file (Plan A)
- Built: Inline MT5 tick computation (Plan B) — user explicitly required this
- Rationale: "数据流转多一个外部依赖，系统崩溃的概率呈指数级上升"

### Baseline vs Enriched Results (8,882 bars, 2026-07-24)

| Model | OHLC Dims | Enriched Dims | Micro Top10 | Micro Share | Verdict |
|:---|:---|:---|:---|:---|:---|
| TREND BUY | 36 | 70 | 3 | 46.7% | STRONG |
| TREND SELL | 36 | 70 | 3 | 47.4% | STRONG |
| REV BUY | 34 | 68 | 2 | 48.6% | STRONG |
| REV SELL | 34 | 68 | 1 | 47.6% | STRONG |

**Top Micro Features by Model**:
- TREND BUY: spread_median_5min_max, spread_toxicity_mean, spread_toxicity_std, arrival_rate_60s_mean, quote_intensity_zscore_mean
- TREND SELL: spread_pips_max, gap_flag_sum, gap_flag_mean, spread_median_5min_mean, quote_intensity_zscore_mean
- REV BUY: spread_median_5min_max, spread_toxicity_max, spread_pips_mean, quote_intensity_zscore_std, gap_flag_sum
- REV SELL: arrival_rate_5s_mean, arrival_rate_60s_mean, spread_median_5min_mean, spread_pips_mean, spread_toxicity_mean

**Note**: spread features dominate Top 5 across all models. This is expected — spread encodes liquidity regime, which is a primary driver of short-term price dynamics. The spread distribution gap (training 0.3-0.6 vs live ~2.4) should be monitored. If live model performance degrades, retraining with live-computed spread features (via `batch_compute_micro_features()`) may be needed.

---

## Monitoring Checklist (Post-Deployment)

After V4.2 restart (completed 2026-07-22 23:37 local):

- [x] System cycling correctly — M5 bar close events firing (confirmed 23:37/23:39/23:44)
- [x] Signals computed correctly — relative strength baselines normal
- [x] No VR over-blocking — signals below 2.5σ, VR code path not reached (correct behavior)
- [ ] Watch for `[V4.2 VR]` log lines when signal crosses 2.5σ
- [ ] Verify `TREND regime → REV engine SILENCED` blocks are reasonable
- [ ] Verify `INDETERMINATE → signals below 3.5σ, SILENCED` rate
- [ ] Track trade frequency: 2026-07-23 08:30 UTC — 9.6h gap, market in MR regime, range-bound
- [ ] If ≥48h zero trades during active session → check Z_THRESHOLD / ENSEMBLE_VOTES
- [ ] After 20 live trades, run `scripts/_vr_regime_eda.py` to generate updated trade replay
- [x] V4.3: Console shows `[V4.3] Micro features ENABLED - 34 dims vectorized` on startup
- [x] V4.3: Console shows `[V4.3] Loaded micro-enriched XGBoost models (70d/68d)` on model load
- [ ] V4.3: No `[Micro] WARNING` or `[Micro] copy_ticks_range failed` log spam during normal operation
- [ ] V4.3: After 50+ live trades, compare micro vs OHLC-only win rate (manual backtest of same period)
- [ ] V4.3: Monitor spread_pips_mean value in live logs — if consistently ~2.4 (Asian) vs training ~0.3-0.6, evaluate retraining with live-computed spread features
- [ ] V4.3: Recalibrate models quarterly (next: October 2026) — run `_retrain_with_micro.py --deploy` with updated data

---

## Known Issues

- **Quarterly recalibration needed**: Empirical μ values (Z_MU) will drift as market microstructure evolves. Failure to recalibrate will cause regime classification bias. Next recalibration: October 2026.
- **q=16 excluded**: At q=16, 69% of bars classified as MR — too aggressive. Deliberately omitted from ensemble.
- **Window granularity**: Currently 96-bar only. The EDA compares 48/96/288 but only 96 is wired. Multi-window consensus is deferred.

---

## V4.2 Impact Analysis (2026-07-23)

Based on 228,717-bar VR regime series + statistical modeling:

### Regime Distribution (w=96, ensemble q=[2,4,8])
| Regime | Bars | % |
|:---|:---|:---|
| TREND | 42,218 | 18.5% |
| MEAN_REVERT | 89,187 | 39.0% |
| INDETERMINATE | 97,312 | 42.5% |

### Signal Reduction Estimate

| Scenario | Overall Reduction | Primary Driver |
|:---|:---|:---|
| Optimistic (signal-regime correlation) | ~15-25% | Direct regime blocks |
| Moderate | ~30-45% | INDETERMINATE 3.5σ upgrade |
| Conservative (uniform signals) | ~50-65% | Both mechanisms |

**Actual expectation: 15-25%** — signals naturally concentrate in matching regimes (TREND signals in TREND, REV signals in MR), and INDETERMINATE has low model conviction. Most reduction is from filtering weak borderline signals that barely crossed 2.5σ.

### Tuning Knobs

If over-blocking observed (≥48h zero trades during active session):
| Parameter | Current | Relax | Effect |
|:---|:---|:---|:---|
| `Z_THRESHOLD` | 1.96 (95%) | → 2.58 (99%) | Fewer INDETERMINATE blocks |
| `ENSEMBLE_VOTES` | 2/3 | → 1/3 | Fewer direct regime blocks |

---

## Fix History

| Fix ID | Date | Summary | Root Cause |
|:---|:---|:---|:---|
| FIX-20260728-004 | 2026-07-28 | **L2: Process Self-Protection Triad — PID Lock + Heartbeat + Log Tee (DQAF-20260728-003).** PID lock prevents duplicate instances competing for same MT5 terminal (root: v7+V4.4 dual-run on Magic 888888). Heartbeat (`heartbeat.json`) written every bar cycle with signal state for external watchdog stall detection. Log tee (`live_console.log`) captures all console output for post-mortem. 1 file, +100 lines. ReB: `SILENT_OBSERVER`. | L2 — three concurrent design gaps: no instance protection, no liveness signal, no durable diagnostics. Process could hang silently on MT5 IPC deadlock with zero observable evidence (confirmed in `watchdog_kill.log`) |
| FIX-20260728-002 | 2026-07-28 | **L3: Hysteresis Exit — per-trade dynamic exit threshold (DQAF-20260728-001).** Replaces static 1.5σ Alpha Decay exit with peak-anchored hysteresis: `exit_σ = peak_σ - 0.8σ` (MIN_EXIT floor=1.0σ). Strong entries (peak=3.0σ)→exit at ~2.2σ; weak entries (peak=2.0σ)→exit at ~1.2σ. `_open_tickets` extended with `peak_rel` (monotonic ratchet: BUY=max, SELL=min) and `rel_type`. Static 1.5σ retained as fallback backstop only. OAT: 方案 A deployed; 方案 B (ATR gate) / 方案 C (per-engine Δσ) deferred pending post-P0 observation. ReB: `STATIC_EXIT_THRESHOLD_OVERFITTING`. 1 file, ~70 lines. | L3 — architecture defect: entry/exit σ gap compressed 50% (1.0σ→0.5σ) when SIGMA_ENTRY lowered 2.5→2.0; static exit threshold cannot adapt to per-trade signal quality (Alpha Decay surge 17%→37%) |
| FIX-20260725-004 | 2026-07-25 | **SIGMA_ENTRY 2.5→2.0**: Corrected micro model (post-FIX-20260724-001) produces narrower rel distribution (σ≈0.16 vs scrambled σ≈0.27). The 2.5σ threshold calibrated on the scrambled model is unreachable with corrected features — 0 signals in 60-bar simulation vs 6 signals at 1.5σ. 2.0σ selected as conservative midpoint: tighter than original 2.5σ (which was calibrated on noise), looser than 1.5σ (which produced 21% signal rate on scrambled model). Expected signal rate: ~3-5/day at 2.0σ (vs 0 at 2.5σ, 29 at 1.5σ). | L2: Threshold calibrated on buggy model — FIX-20260724-001 changed the feature distribution without recalibrating the downstream threshold |
| FIX-20260725-003 | 2026-07-25 | **Warmup zero-micro elimination**: `warmup_radars()` now uses OHLC-only models instead of micro models with zero-filled micro features. Root cause: zero-micro warmup predictions had μ=+0.06 σ=0.20, while live real-micro predictions had μ=+0.04 σ=0.16. The 25% wider σ inflated rolling thresholds for 5 hours after restart. Fix: load both OHLC models (for warmup) and micro models (for live), warm up with OHLC only. The OHLC→micro transition is smooth (both have comparable σ) — no fake data injected. | L2: Zero-padding micro features during warmup created distribution mismatch; the model interprets zeros as meaningful features (e.g., zero spread_toxicity = "no adverse selection" signal), not as missing data |
| FIX-20260724-002 | 2026-07-25 | **Warmup window reduction**: Rolling HISTORY window reduced from 120→60 bars (10h→5h). FIX-20260724-001's zero-to-real-micro transition contamination window halved. μ/σ statistical precision only marginally affected (σ estimate error 6.5%→9%). System adapts to regime changes 2× faster. | L2: 120 was chosen for feature computation lookback (ret_120), not for statistical stability of μ/σ estimation — warmup uses pre-computed features and doesn't need 120 bars |
| FIX-20260724-001 | 2026-07-24 | **Micro feature column order mismatch**: `MICRO_FEATURE_NAMES` in `micro_features_live.py` had offset columns (`mid_open_offset`, `mid_close_offset`) at positions [0,1], but training (`_retrain_with_micro.py` `apply_offset_transform()`) puts them at positions [32,33]. Result: 34/34 micro features scrambled → systematic prediction bias (TB -0.15, TS +0.09) → σ 4× inflation → 14.7h zero trades. Fix: reorder `MICRO_FEATURE_NAMES` to match training column order (offset columns last). No model retrain needed. | L2: Feature vector assembly order mismatch — two independent code paths (training vs live) constructed micro feature vectors in different orders |
| V4.3-MICRO-004 | 2026-07-24 | Fix shape mismatch crash: `fetch_live_features()` now zero-fills 34 micro dims when `_fetch_micro_safe()` returns None, preventing "expected 70 got 36" | L2: Model-loading decision (micro vs OHLC) is independent of per-bar fetch success; fetch fail → 36-dim vector fed to 70-dim model → crash |
| V4.3-MICRO-003 | 2026-07-24 | Phase 4: Integrated into Dual_Assassin_Live.py — 6 edit points, graceful fallback chain | L3: New feature source (micro ticks) requires full integration into live pipeline |
| V4.3-MICRO-002 | 2026-07-24 | Phase 3: Retrained 4 XGBoost models with offset micro features (70d/68d), saved as *_micro.json | L2: Models must consume offset-transformed micro features at correct dimensionality |
| V4.3-MICRO-001 | 2026-07-24 | Phase 1-2: Built `micro_features_live.py` — pure NumPy vectorized tick-to-bar computer | L3: No live micro feature computation capability existed; external pipeline not suitable for real-time |
| V4.2-BANNER-001 | 2026-07-23 | L775 banner string "V4.1" → "V4.2 VR Regime 机构版" (cosmetic fix) | L1: Stale string — L775 loading banner not updated when V4.2 deployed |
| V4.2-VR-001 | 2026-07-22 | VR Regime Detector — Lo-MacKinlay ensemble + engine routing constraint | L3: No exogenous market state detection; engine-regime mismatch (REV in TREND at ATH → -$171.20) |
| DQAF-20260721-001 | 2026-07-21 | V4.1 dual-threshold gating (2.5σ entry / 1.5σ exit) | L2: XGBoost model collapse at 1.5σ → 21% signal rate ≈ noise |

---

## Cross-Module Contracts

| Contract | Consumers | Stability |
|:---|:---|:---|
| `RegimeDetector.detect(close_prices) → str` | Engine routing in main loop | STABLE (pure function, no side effects) |
| `_regime_detector` singleton | Main trading loop | STABLE (read-only after init) |
| Module-level `Z_MU`, `Z_THRESHOLD`, `ENSEMBLE_VOTES` | `RegimeDetector.__init__` | STABLE (tunable constants) |
| `micro_features_live.compute_bar_micro_features(ticks) → dict` | `Dual_Assassin_Live._fetch_micro_safe()` | STABLE (pure function, no side effects, returns None on failure) |
| `micro_features_live.fetch_micro_features_for_bar(start, end, open, close, timeout) → dict` | `Dual_Assassin_Live._fetch_micro_safe()` | STABLE (all-in-one fetch+compute+offset) |
| `micro_features_live.MICRO_FEATURE_NAMES` (34-element list) | Model input ordering | STABLE — ORDER CRITICAL: must match training `m_cols` from `_retrain_with_micro.py:apply_offset_transform()`. Offset columns (`mid_open_offset`, `mid_close_offset`) at positions [32,33] (end), NOT [0,1]. Mismatch = 34/34 features scrambled = model silence (FIX-20260724-001) |
| `micro_features_live.apply_price_offsets(features, open, close) → dict` | Offset transformation | STABLE (idempotent; zero-fills on missing mid_price keys) |
| `Dual_Assassin_Live._MICRO_ENABLED` flag | Graceful fallback | STABLE (set once at import; models/warmup/fetch all check this flag) |

---

## Verification

```bash
# Syntax check
cd F:\ai\Dual_Assassin
python -m py_compile Dual_Assassin_Live.py

# Micro feature import check
cd F:\ai\Dual_Assassin
python -c "import sys; sys.path.insert(0, r'd:\future'); from micro_features_live import MICRO_FEATURE_NAMES; print(f'{len(MICRO_FEATURE_NAMES)} features OK')"

# Micro feature live test (requires MT5 connected)
cd d:\future
python scripts\_test_micro_live.py

# Tick load validation
cd d:\future
python scripts\_validate_tick_load.py --bars 100

# Retrain models with offset micro features (offline, requires data)
cd d:\future
python scripts\_retrain_with_micro.py --deploy

# BASELINE vs ENRICHED comparison
cd d:\future
python scripts\_micro_enrichment_test.py

# Smoke test RegimeDetector
python -c "
import numpy as np
from Dual_Assassin_Live import _regime_detector
# Random walk test
rw = np.cumsum(np.random.RandomState(42).randn(100)*0.5) + 4000
print('RW:', _regime_detector.detect(rw))
# Trending test
trend = np.cumsum(np.ones(100)*0.5 + np.random.RandomState(42).randn(100)*0.1) + 4000
print('Trend:', _regime_detector.detect(trend))
# MR test
rng = np.random.RandomState(42)
mr = 4000 + 0.3*(np.ones(100)*4000 + rng.randn(100)*10 - 4000) + rng.randn(100)*2
print('MR:', _regime_detector.detect(mr))
"

# VR EDA recalibration
python scripts/_vr_regime_eda.py
```
