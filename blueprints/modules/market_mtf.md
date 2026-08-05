# Market / MTF Price Service

## Purpose
Multi-timeframe price reconstruction from M5 tick history. Decoupled from live_cycle.py per architecture requirement. Provides correctly-closed M15 (and future H1, H4) bar OHLC data without future-function leakage — never exposes incomplete bars.

## Key Files
| File | Role |
|------|------|
| `core/market/mtf_price_service.py` | `MTFPriceService` — tick buffer, bar closure detection, OHLC reconstruction |

## Data Flow
```
M5 tick mid_price (every cycle)
    │
    v
MTFPriceService.feed_tick(ts, price)
    │
    ├── Buffer: (ts, price) pairs (up to 500 ticks)
    │
    ├── On M15 boundary (00/15/30/45): _close_bar("M15", boundary_ts)
    │   └── Groups ticks in [boundary-900s, boundary) → OHLC bar
    │
    v
Completed M15 bars: [{time, open, high, low, close, hl2, ohlc4}, ...]
    │
    ├── latest_m15_close  → float | None
    ├── latest_m15_hl2    → float | None
    ├── latest_m15_ohlc4  → float | None
    └── is_m15_boundary(minute) → bool
```

## Architecture Constraints
1. **No simple time slicing**: Bars are only "closed" when the boundary has passed. `latest_m15_close` returns the previous completed bar — never a partially-formed current bar.
2. **Down-sampling Alignment**: `is_m15_boundary()` provides a single source of truth for M15 alignment checks. Callers gate evaluation on this check.
3. **Compute Decoupling**: Standalone service — no dependency on live_cycle internals.

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| (stdlib only) | datetime.UTC, datetime.datetime | Timestamp handling |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| runtime/live_cycle | MTFPriceService | M15 bar reconstruction, boundary gating, M15 price routing |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
|--------|------|--------|--------|---------|------------|
| FIX-20260805-003 | 2026-08-05 | cursor-agent | — | **RBI-1 清偿: merge 脚本回迁 `scripts/_merge_aligned_multitf_data.py` (8/19 补给仪式阻断移除)**. 08-01 归档至 scripts/archive/ (gitignore 区=退役语义) 致 ROOT=parent.parent 少退一层 → DATA_RAW 指向 D:\future\scripts\data\raw (不存在) → 补给仪式首步 [FATAL] BTC backbone not found. 回迁修复 + 过时 Next 提示修正 (build_btc_expected_r_dataset→build_btc_dataset_from_ssot) + 预存 B007 清偿. 验证: ROOT->D:\future, 干跑 EXIT=0 (50,000 bars). | RC-09 — config-drift: 非退役脚本被误归档 |
| FIX-20260523-004 | 2026-05-23 | cursor-agent | — | MTFPriceService created: decoupled M15 bar reconstruction from M5 tick history. Bar-boundary gating ensures M15 brain only evaluated at 00/15/30/45. Enabled statarb_m15 with dedicated OU brain config. | RC-06 (missing infrastructure — M15 mid_price pipeline never implemented) |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `MTFPriceService.feed_tick(ts, price)` → populates internal buffer | live_cycle | Stable |
| `MTFPriceService.is_m15_boundary(minute)` → bool | live_cycle._evaluate_strategy_lines | Stable |
| `MTFPriceService.latest_m15_close` → float | None | live_cycle M15 price routing | Stable |

## Verification
```bash
python -m pytest tests/ -k "mtf or m15" -q
```
