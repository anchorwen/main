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
| `core/protocol/services/mt5_communication_adapter.py` | `MT5CommunicationAdapter` — file-based MT5 handoff (生产) |
| `core/protocol/services/zmq_communication_adapter.py` | `ZMQCommunicationAdapter` — ZeroMQ PUSH adapter (<1ms, Phase 1) |
| `core/protocol/services/zmq_receipt_listener.py` | `ZMQReceiptListener` + `resolve_ack()` — ZMQ PUB/SUB ACK (ZMQ fast path + file fallback) |
| `scripts/mt5_bridge_worker.py` | MT5 bridge daemon — `--file` (file polling) or `--zmq` (PULL+PUB) mode |
| `scripts/benchmark_zmq_latency.py` | ZMQ vs File IPC latency benchmark |

## Data Flow
```
DecisionIntent → DecisionCompiler → IntentMessageBuilder → CommunicationEnvelope
                                                                  ↓
                                                      CommunicationDispatcher
                                                                  ↓
                                              ┌───────────────────┴───────────────────┐
                                              ↓                                       ↓
                                    MT5CommunicationAdapter               ZMQCommunicationAdapter
                                    (file IPC: .mt5.json outbox)          (ZMQ PUSH tcp://127.0.0.1:5556)
                                              ↓                                       ↓
                                    mt5_bridge_worker --file              mt5_bridge_worker --zmq
                                    (poll 1s → read → MT5 API)            (PULL recv → MT5 API)
                                              ↓                                       ↓
                                    write .ack.json to receipts/          PUB push ACK to tcp://127.0.0.1:5557
                                              ↓                                       ↓
                                    ACK consumers poll files              ZMQReceiptListener (SUB)
                                    (200ms interval)                      resolve_ack() → instant
                                                                              ↓ (fallback)
                                                                         file polling
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
| FIX-20260613-036 | 2026-06-13 | cursor-agent | — | ZMQ worker health heartbeat: blocking recv prevented periodic 30s health writes when idle. Added _write_zmq_health() at startup + per-order update. DQAF-004. | RC-06 |
| FIX-20260613-035 | 2026-06-13 | cursor-agent | — | live_launcher config scope truncation: load_live_config() returned only live_trading subsection, adapter.name invisible. Forwarded adapter+zmq from top-level config. DQAF-003. | RC-06 |
| FIX-20260613-034 | 2026-06-13 | cursor-agent | — | ZMQ worker MT5 null guard: added `if mt5 is None` check before _send_to_mt5() with clear rejection ACK. DQAF-002. | RC-06 |
| FIX-20260613-032 | 2026-06-13 | cursor-agent | — | **ZeroMQ Socket Bridge Phase 1 (12,500x latency reduction)**: (1) ZMQCommunicationAdapter — PUSH socket replaces file outbox write. (2) ZMQReceiptListener + resolve_ack() — PUB/SUB replaces file ACK polling with ZMQ fast path + file fallback. (3) mt5_bridge_worker.py --zmq mode — PULL recv + PUB ACK broadcast. (4) service_container.py — adapter_name="mt5_zmq" routing. (5) All 3 ACK consumers (live_order_sender, execution_queue, exit_watchdog) migrated to resolve_ack(). (6) benchmark_zmq_latency.py: P50=72us, P99=148us vs file IPC ~1,000,000us. Backward-compat: adapter_name="mt5" preserves file IPC. | RC-05, RC-09 |
| FIX-20260605-122 | 2026-06-05 | cursor-agent | ae0d006 | **BarSyncPoller strict_mode**: New `strict_mode` parameter — when True (production), RuntimeError if MT5Worker unavailable instead of silent fallback to direct `mt5.initialize()`. Wired `strict_mode=True` in live_intent_loop.py. Defense-in-depth against accidental non-worker MT5 access. Note: BarSyncPoller already had correct Worker-first routing; strict_mode adds a hard guardrail for misconfiguration. | RC-09 |
| FIX-20260531-003 | 2026-05-31 | cursor-agent | — | DistributedLock .tmp file cleanup: `FileLock.acquire()` left stale `.tmp` staging file after failed acquire (rename failed → OSError). Added `tmp.unlink(missing_ok=True)` in OSError handler. Prevents lock directory clutter and potential confusion in stale lock detection. | RC-06 (resource-leak) |
| FIX-20260529-040 | 2026-05-29 | cursor-agent | — | CircuitBreaker.trip(reason) + _last_trip_reason field: enables instant OPEN on CRITICAL alert | RC-12 |
| FIX-20260601-045 | 2026-06-01 | cursor-agent | — | **bar_sync_state version field**: added `schema_version: bar_sync_state.v1`. v2→v3 migration complete. | RC-09 |
| FIX-20260601-043 | 2026-06-01 | cursor-agent | — | **Journal lock gap**: `journal_cleanup.py` writers now use FileLock. `_load_journal()` logs parse errors. | RC-04 |
| FIX-20260601-042 | 2026-06-01 | cursor-agent | — | **bar_sync fragility root fix**: 8 silent except:pass → logged events. Session-aware (market_type param). Real-time lag via current_lag_bars(). Degraded sentinel marked _data_incomplete. | RC-07 |
| FIX-20260525-011 | 2026-05-25 | cursor-agent | — | BarSyncPoller timeout/timeframe decoupling: DEFAULT_TIMEOUT_SECONDS was hardcoded 360s safe for M5 but insufficient for H1+ (5400s bar period). Dynamic floor: `max(360, int(bar_seconds × 1.5))` — M5=450s, M15=1350s, H1=5400s, H4=21600s. Formula enforced in __init__ using existing `_bar_seconds_for()`. | RC-05 (boundary-error: timeout-timeframe coupling) |
| FIX-20260525-009 | 2026-05-25 | cursor-agent | — | MT5 worker refactoring: event_bar_sync.py — BarSyncPoller accepts optional mt5_worker, hardcoded TF constants, worker-aware error recovery (reconnect instead of shutdown+init). | RC-04, RC-06 |
| FIX-20260524-014 | 2026-05-24 | cursor-agent | — | MODULE_SOURCE_MAP: add scripts/validators/journal_validator.py. Mypy fixes: journal_validator (1→0 — getattr for tuple.__name__), communication_operations_service (1→0 — assert posture is not None). | RC-02 |
| FIX-20260519-019 | 2026-05-19 | cursor-agent | — | BarSyncPoller 92.8% timeout rate fix: added `fetch_synthetic_bar()` — when M5 bar hasn't formed, aggregates last 6 M1 bars into synthetic M5 OHLC(V) instead of blind `time.sleep()`. Eliminates 120s data misalignment window where stale features were used against real-time prices. Caller in live_intent_loop.py uses synthetic bar on timeout instead of falling back to interval sleep. | RC-06 (data-misalignment, sampling-blind-spot) |
| FIX-20260522-006 | 2026-05-22 | cursor-agent | — | BarSyncPoller MT5 transient error retry: copy_rates_from_pos() fails after ~104s of polling despite successful initialize(). Added MAX_MT5_ERROR_RETRIES=3 with re-init+retry loop before degrading to fallback_poll + synthetic bar. Resets error count on successful poll or new bar detection. | RC-05 (transient-error) |
| FIX-20260522-010 | 2026-05-22 | cursor-agent | — | BarSyncPoller timeout 120s→360s: DEFAULT_TIMEOUT_SECONDS was shorter than M5 bar period (300s). Every polling window expired before next bar formed, forcing all cycles into fallback sleep mode. Now 360s = 300s bar period + 60s buffer. Also updated live.yaml, live_intent_loop.py, live_launcher.py defaults. | RC-05 (boundary-error) |
| FIX-20260522-028 | 2026-05-22 | cursor-agent | — | BarSyncPoller silent-failure recovery: `copy_rates_from_pos()` returning None (not exception) after MT5 re-init caused infinite silent spin → perpetual `bar_sync_degraded_wakeup`. Added `BAR_EMPTY_POLLS_REINIT` — after 5 consecutive empty polls, re-inits MT5 and logs event. Empty counter resets on successful poll. Fixes engine stuck in management-only mode. | RC-05 (missing-recovery-path) |
| FIX-20260522-011 | 2026-05-22 | cursor-agent | — | BarSyncPoller graceful degradation: added degraded deadline at `bar_period` (300s M5) that returns a truthy sentinel instead of blocking to full 360s timeout. Prevents the "360s block + 30s caller sleep = 390s dead window" cascade when MT5 is flaky. Also reduced poll_interval 2s→1s, added mt5.shutdown() before re-init for cleaner IPC recovery, and added BAR_DEGRADED_WAKEUP event logging. Caller now logs bar_sync_degraded_wakeup when sentinel received. | RC-05 (architectural: BarSyncPoller introduced a single point of failure that didn't exist with blind time.sleep — the polling loop required MT5 to be continuously available across the entire window, but MT5 IPC fails periodically) |
| FIX-20260522-029 | 2026-05-22 | cursor-agent | — | **TRUE ROOT CAUSE** of all perpetual degradation: `mt5.copy_rates_from_pos()` returns numpy structured array → rows are `numpy.void` (not dict) → `.get("tick_volume", 0)` threw `AttributeError` at new-bar return-dict construction. State was updated BEFORE the fallible return-dict build, so after crash → re-init → last_bar_time already advanced → "new bar" never detected → degraded forever. All 6 `.get()` calls replaced with `[]` (works for both numpy.void and dict). Defense-in-depth: bar_data dict built BEFORE state mutation. | RC-02 (type-confusion: numpy.void ≠ dict) + RC-03 (state-leak: state mutation before fallible operation) |
| FIX-20260523-003 | 2026-05-23 | cursor-agent | — | Meta Filter (Track 4d) threshold fix at 0.65: conformal prediction was computing `max(80th_pctile, 0.50, 0.65) = ~0.679` at runtime, rejecting 83% of proposals. Architecture decision: disable conformal prediction (`enabled: false` in meta_stage2_filter_v3.json) to fix effective threshold at base 0.65. This increases pass rate from 17% to ~28%, accelerating sample collection for precision-curve calibration. Conformal re-enabled once sufficient P(win)-vs-outcome data is accumulated. | RC-09 (config-drift: conformal percentile drifting threshold above intended base) |

## Known Issues

### KI-001: bar_sync timeout MUST exceed bar period (`2026-05-22`) — RESOLVED by FIX-029 / FIX-20260525-011

**Original diagnosis (WRONG)**: FIX-006 fixed MT5 transient errors, but this exposed that `DEFAULT_TIMEOUT_SECONDS` (120s) must exceed bar period (300s). The causal chain was:
1. Pre-FIX-006: MT5 threw at ~104s → old code fell back → ~224s effective window → occasionally got bars
2. Post-FIX-006: retry logic swallowed errors → 120s hard timeout → 100% timeout rate (< 300s bar period)
3. FIX-010: timeout 120→360s, FIX-011: degraded deadline at 310s

**Corrected diagnosis (2026-05-22, FIX-029)**: The "MT5 transient error at ~104s" was NEVER an MT5 IPC error. It was always `AttributeError: 'numpy.void' object has no attribute 'get'` thrown at new-bar detection. The ~104s timing was the first M5 bar boundary after bar_sync start; the exact 300s spacing between all subsequent errors was the M5 bar period. The error count of 1 per cycle was because only ONE new bar forms per cycle. State corruption (update-before-return) made the crash self-perpetuating — after re-init, last_bar_time matched the current bar, so no "new" bar was ever found.

**Final resolution (FIX-20260525-011)**: FIX-010 fixed M5 specifically (120→360s) but the timeout was still hardcoded — H1+ strategies would face the same issue. Dynamic floor: `max(360, int(bar_seconds × 1.5))` enforced in `BarSyncPoller.__init__` using existing `_bar_seconds_for()`. M5=450s, H1=5400s, H4=21600s. Timeout is now timeframe-decoupled by construction.

**The real causal chain**:
1. `copy_rates_from_pos()` returned numpy structured array (correct MT5 behavior)
2. Row iteration yielded numpy.void (correct numpy behavior)
3. `.get()` called on numpy.void → `AttributeError` (type confusion RC-02)
4. State was updated BEFORE return-dict construction → last_bar_time already advanced (state leak RC-03)
5. Exception handler caught it, re-inited MT5, continued polling
6. After re-init, no bar newer than (already-updated) last_bar_time → degraded deadline fired
7. FIX-006/010/011/028 were all treating symptoms of this single type-confusion bug

**Prevention**: (1) Never use `.get()` on MT5 rate data — always `[]`. (2) Mutate state AFTER all fallible data construction. (3) Suspicious patterns: identical error spacing (= bar period), error count always 1 per cycle, "transient error" that always fires at the bar boundary.

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
