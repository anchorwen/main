# Execution / Portfolio Netting

## Purpose
Physical netting gate that sits between ParliamentConsensus and MT5 dispatch. Computes conviction-weighted net exposure across all same-symbol strategy decisions. If opposing directions exist and |net|/gross falls below the netting threshold, ALL orders are physically swallowed — zero order reaches the broker.

**Institutional mandate (DQAF-20260705-064)**: The portfolio is ONE. Zero Exposure is a position.

## Key Files
| File | Role |
|------|------|
| `core/execution/portfolio_netting.py` | PortfolioNettingGate, PortfolioNettingConfig, NettedDecision |
| `core/runtime/live_cycle.py` | Integration point — inserted between reentry_alert and exec_queue.flush() |

## Data Flow
```
strategy_line_1 → decision (LONG)  ─┐
strategy_line_2 → decision (SHORT) ─┤
                                     ↓
                            exec_queue.enqueue()
                                     ↓
                     ★ PORTFOLIO NETTING GATE ★
                     Net_Exposure = Sum(LONG_power) - Sum(SHORT_power)
                     power = vote_weight × confidence × volume
                                     ↓
                     |net|/gross < 0.20 → SWALLOW ALL
                     |net|/gross ≥ 0.20 → swallow minority, dispatch majority
                     unanimous → pass-through
                                     ↓
                            exec_queue.flush() → MT5
```

## Algorithm

1. **Classify**: Separate queued decisions into LONG, SHORT, NEUTRAL (should_trade=False or direction=neutral)
2. **Compute power**: For each directional decision, power = vote_weight × confidence × volume. Zero-power decisions (vote_weight=0 or confidence=0) are treated as neutral.
3. **Net**: `Net = LONG_power - SHORT_power`, `Gross = LONG_power + SHORT_power`
4. **Decide**:
   - Gross == 0 → pass-through (no directional signals)
   - Only one side → unanimous → pass-through
   - `|Net| / Gross < netting_threshold (0.20)` → mode-dependent:
     - `swallow`: should_trade=False for ALL (institutional default)
     - `reduce`: swallow minority, dispatch majority
     - `warn`: log only, dispatch all
   - `|Net| / Gross ≥ netting_threshold` → swallow minority, dispatch majority

## Configuration

```python
PortfolioNettingConfig(
    enabled=True,            # master kill-switch
    netting_threshold=0.20,  # |net|/gross below this → swallow
    mode="swallow",          # "swallow" | "reduce" | "warn"
)
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| — | — | Self-contained — operates on decision objects with .direction, .confidence, .volume, .vote_weight, .should_trade |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| runtime/live_cycle | PortfolioNettingGate, PortfolioNettingConfig | Pre-flush netting gate |

## Known Issues

- **Same-cycle pending orders only**: Netting swallows competing pending orders within one cycle. Protection against opening against EXISTING positions is delegated to `CrossStrategyCoordinator` (position-level, per-decision) — a layered contract, not a gap in this module.
- **`reduce` mode residue**: When `|net|/gross` is above threshold but the minority side is non-trivial, `reduce` still dispatches the majority — the netted exposure can remain material. `swallow` (institutional default) is the fail-closed mode.
- **Pre-dispatch decision**: The swallow decision is made before `exec_queue.flush()`. It does not re-validate against post-decision price movement — a gap between decision and fill is not covered by this gate.

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `PortfolioNettingGate.net(queued_decisions, symbol)` → `(decisions, NettedDecision)` | live_cycle | Stable |
| Decision protocol: `.direction`, `.confidence`, `.volume`, `.vote_weight`, `.should_trade`, `.reason` | All strategy evaluators | Stable — existing StrategyDecision interface |

## Relationship to CrossStrategyCoordinator

| Layer | Component | Scope | When |
|-------|-----------|-------|------|
| **Position-level** | `CrossStrategyCoordinator` | Pending vs EXISTING positions | Per-decision, before enqueue |
| **Order-level** | `PortfolioNettingGate` | Pending vs PENDING orders (same cycle) | Per-cycle, before MT5 dispatch |

Both are complementary — CSc prevents opening against existing positions, PNG prevents same-cycle opposing orders.

## Verification
```bash
python -m pytest tests/test_portfolio_netting.py -q
```

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
|--------|------|--------|--------|---------|------------|
| FIX-20260705-064 | 2026-07-05 | cursor-agent | — | **P0+P1: Institutional Portfolio Netting — kill switch + conviction-weighted netting gate**. P0: Disabled btc_swing (V4 OOD flip, 0 LONG since June 28) and btc_swing_h1 (V12_H1_15 structural LONG bias, 0.6% SHORT rate) in live_btc.yaml. P1: Built PortfolioNettingGate — conviction-weighted netting (power = vote_weight × confidence × volume) with 0.20 netting threshold. If |net|/gross < 0.20, swallows all orders — "Zero Exposure is a position." Inserted in live_cycle.py between reentry_alert and exec_queue.flush(). 14/14 TDD tests covering unanimous, balanced-swallow, dominant-reduce, single-strategy, neutral, empty, disabled, warn, zero-vote-weight, non-trading, and counter scenarios. | RC-12: design flaw — no cross-strategy netting layer existed |
