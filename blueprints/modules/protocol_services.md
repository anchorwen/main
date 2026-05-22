# Protocol / Services

## Purpose
Communication layer: message dispatch, adapter registry, intent building, decision compilation, venue routing, circuit breaking, and idempotency tracking.

## Key Files
| File | Role |
|------|------|
| `core/protocol/services/communication_dispatcher.py` | `CommunicationDispatcher` — main dispatch entry point |
| `core/protocol/services/communication_adapter_registry.py` | `CommunicationAdapterRegistry` — adapter resolution |
| `core/protocol/services/communication_adapter.py` | `CommunicationAdapter(Protocol)` — adapter interface |
| `core/protocol/services/intent_message_builder.py` | `IntentMessageBuilder` — DecisionIntent → CommunicationEnvelope |
| `core/protocol/services/decision_compiler.py` | `DecisionCompiler` — DecisionCandidate → DecisionIntent |
| `core/protocol/services/venue_router.py` | `VenueAdapter`, `StubVenueAdapter` — venue dispatch |
| `core/protocol/services/resilience.py` | `CircuitBreaker` — CLOSED/OPEN/HALF_OPEN circuit breaker |
| `core/protocol/services/idempotency.py` | `IdempotencyStore` — file-backed idempotency keys |
| `core/protocol/services/override_resolver.py` | `OverrideResolver` — active override resolution |
| `core/protocol/event_bar_sync.py` | `BarSyncPoller` — event-driven M5 bar synchronization with MT5 |

## Data Flow
```
DecisionIntent → DecisionCompiler → IntentMessageBuilder → CommunicationEnvelope
                                                                  ↓
                                                      CommunicationDispatcher
                                                                  ↓
                                                    AdapterRegistry → VenueAdapter
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| contracts/domain | DecisionIntent, DecisionCandidate, CommunicationEnvelope, DispatchRequest, DispatchResult | Domain types |
| contracts/enums | DecisionAction, DecisionSide, SystemMode, DispatchStatus, CommunicationMessageType | Enum values |
| contracts/ids | new_intent_id, new_message_id, new_dispatch_id | ID generation |
| execution | gateway_contracts | Order state tracking |
| observability | metric_names | Dispatch metrics |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| execution/live_order_sender | CommunicationEnvelope, CommunicationDispatcher | Order dispatch |
| deployment/lifecycle | CommunicationDispatcher | Service wiring |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260519-019 | 2026-05-19 | cursor-agent | — | BarSyncPoller 92.8% timeout rate fix: added `fetch_synthetic_bar()` — when M5 bar hasn't formed, aggregates last 6 M1 bars into synthetic M5 OHLC(V) instead of blind `time.sleep()`. Eliminates 120s data misalignment window where stale features were used against real-time prices. Caller in live_intent_loop.py uses synthetic bar on timeout instead of falling back to interval sleep. | RC-06 (data-misalignment, sampling-blind-spot) |
| FIX-20260522-006 | 2026-05-22 | cursor-agent | — | BarSyncPoller MT5 transient error retry: copy_rates_from_pos() fails after ~104s of polling despite successful initialize(). Added MAX_MT5_ERROR_RETRIES=3 with re-init+retry loop before degrading to fallback_poll + synthetic bar. Resets error count on successful poll or new bar detection. | RC-05 (transient-error) |
| FIX-20260522-010 | 2026-05-22 | cursor-agent | — | BarSyncPoller timeout 120s→360s: DEFAULT_TIMEOUT_SECONDS was shorter than M5 bar period (300s). Every polling window expired before next bar formed, forcing all cycles into fallback sleep mode. Now 360s = 300s bar period + 60s buffer. Also updated live.yaml, live_intent_loop.py, live_launcher.py defaults. | RC-05 (boundary-error) |
| FIX-20260522-028 | 2026-05-22 | cursor-agent | — | BarSyncPoller silent-failure recovery: `copy_rates_from_pos()` returning None (not exception) after MT5 re-init caused infinite silent spin → perpetual `bar_sync_degraded_wakeup`. Added `BAR_EMPTY_POLLS_REINIT` — after 5 consecutive empty polls, re-inits MT5 and logs event. Empty counter resets on successful poll. Fixes engine stuck in management-only mode. | RC-05 (missing-recovery-path) |
| FIX-20260522-011 | 2026-05-22 | cursor-agent | — | BarSyncPoller graceful degradation: added degraded deadline at `bar_period` (300s M5) that returns a truthy sentinel instead of blocking to full 360s timeout. Prevents the "360s block + 30s caller sleep = 390s dead window" cascade when MT5 is flaky. Also reduced poll_interval 2s→1s, added mt5.shutdown() before re-init for cleaner IPC recovery, and added BAR_DEGRADED_WAKEUP event logging. Caller now logs bar_sync_degraded_wakeup when sentinel received. | RC-05 (architectural: BarSyncPoller introduced a single point of failure that didn't exist with blind time.sleep — the polling loop required MT5 to be continuously available across the entire window, but MT5 IPC fails periodically) |

## Known Issues

### KI-001: bar_sync timeout MUST exceed bar period (`2026-05-22`)
**Discovery**: FIX-20260522-006（MT5 瞬时错误重试）修复后，bar_sync 轮询不再因 MT5 异常提前退出。这暴露了一个隐藏的前提条件：`DEFAULT_TIMEOUT_SECONDS`（原值 120s）必须大于目标 K 线周期（M5=300s）。

**因果链**:
1. 修复前：MT5 `copy_rates_from_pos()` 在约 104s 后持续抛异常 → 旧代码立即 `fallback_to_poll` 返回 None → 实际上轮询存活窗口被异常截断在 ~104s
2. 修复前：异常截断 + 60s 回退睡眠 + 60s 间隔睡眠 = 隐式的 ~224s 窗口 → 偶尔能等到下一根 K 线（取决于 bar_sync 启动时的 K 线内偏移量）
3. FIX-006 修复后：异常被重试逻辑吞没 → 轮询完整存活 120s → 严格在 120s 截止时间超时
4. 120s < 300s → 若 bar_sync 在 K 线形成 30s 后启动，下一根 K 线需 270s → 永远在截止前超时 → 100% 超时率
5. FIX-010 将超时延长至 360s（300s + 60s 缓冲）→ 恢复事件驱动检测

**教训**: 任何影响外部 API 轮询循环退出行为的修复，必须验证轮询窗口是否仍能达成目标事件检测。当前超时计算是硬编码的——应转为 `_bar_seconds() * 1.2` 以适配不同时间周期。

**影响范围**: `event_bar_sync.py`, `live_intent_loop.py`, `live_launcher.py`, `live.yaml`

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `CommunicationDispatcher.dispatch(envelope)` → `DispatchResult` | live_order_sender | Stable |
| `DecisionCompiler.compile(candidate, policies)` → `DecisionIntent` | live_cycle | Stable |
| `CircuitBreaker` states: CLOSED → OPEN (5 failures) → HALF_OPEN (30s) → CLOSED | CommunicationDispatcher | Stable |

## Verification
```bash
python -m pytest tests/ -k "protocol or dispatch or circuit" -q
```
