# Deployment / Config

## Purpose
Environment configuration, service container (dependency injection), config hot-reload, and release pipeline orchestration.

## Key Files
| File | Role |
|------|------|
| `core/deployment/environment_config.py` | `Environment` enum, `EnvironmentConfig` dataclass |
| `core/deployment/service_container.py` | DI container wiring ~60 services |
| `core/deployment/config_hot_reload.py` | `ConfigHotReload` — watches engine_config.json |
| `core/deployment/release_pipeline.py` | `ReleasePipelineService` — end-to-end dry-run release |
| `core/deployment/release_gate.py` | `ReleaseGateService` — readiness + runbook + SLO + config |
| `core/deployment/deployment_executor.py` | `DeploymentExecutor` — safe dry-run execution |
| `core/deployment/deployment_plan.py` | `DeploymentPlanService` — phased deployment plans |
| `core/deployment/release_readiness.py` | `ReleaseReadinessService` |
| `core/deployment/release_registry.py` | `ReleaseRegistryService` |
| `core/deployment/release_certification.py` | `ReleaseCertificationService` |
| `core/deployment/final_audit.py` | `FinalAuditService` |
| `core/deployment/evidence_bundle.py` | `EvidenceBundleService` |
| `core/deployment/compliance_audit.py` | `ComplianceAuditService` |
| `core/deployment/compliance_control_matrix.py` | `ComplianceControlMatrixService` |
| `core/deployment/compliance_export.py` | Compliance export |
| `core/deployment/permission_audit.py` | Permission audit |
| `core/deployment/rollback_drill.py` | `RollbackDrillService` |
| `core/deployment/runbook_engine.py` | `RunbookEngine` |
| `core/deployment/blue_green.py` | Blue-green deployment |
| `core/deployment/capability_registry.py` | Capability registry |

## Data Flow
```
environment_config.json → EnvironmentConfig → ServiceContainer
                                                    ↓
                                    All services wired with correct env settings
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| contracts | domain_keys | Evidence/pipeline stage keys |
| brains | BrainFactory, BrainRunService, etc. | Service wiring |
| execution | ExecutionManager | Service wiring |
| features | FeatureService | Service wiring |
| feedback | FeedbackLoop | Service wiring |
| governance | GovernanceService | Service wiring |
| ledger | Ledger services | Service wiring |
| observability | Observability services | Service wiring |
| parliament | ParliamentService | Service wiring |
| protocol | CommunicationDispatcher | Service wiring |
| risk | RiskEvaluationService | Service wiring |
| state | ControlSnapshotService | Service wiring |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| execution/live_order_sender | EnvironmentConfig, ServiceContainer | DI access |
| runtime/live_cycle | ServiceContainer | Service access |
| apps/engine/ | EnvironmentConfig, ServiceContainer | CLI wiring |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260629-183 | 2026-06-29 | cursor-agent | — | **Fix governance auto-promotion guard**: `scheduler_service.py:400` hardcoded `if True:` → `if _GOVERNANCE_MANUAL_MODE:`. `_GOVERNANCE_MANUAL_MODE=False` was set but overridden since FIX-20260614-B0. BrainPromotionEvaluator auto-transitions were permanently dead code. | L2 — hardcoded safety guard never removed after metrics review period expired |
| FIX-20260629-181 | 2026-06-29 | cursor-agent | — | **Demote Swing_V9_H4_V2 live→frozen** (DQAF-175). Shadow PF=0.41, PnL=-5,825R. Promoted on 11 trades (FIX-064d); degraded in 6 days. | L2 — min_trades=10 insufficient |
| FIX-20260628-160 | 2026-06-28 | cursor-agent | — | **Magic 冲突误报消除 (L1)**: BrainConfigValidator._check_magic_unique() 增加 contract_group 感知 — _build_magic_index() 存储 (brain_id, contract_group) 元组, _check_magic_unique() 仅跨合约组冲突报警。同组共享 magic 是架构设计意图 (所有 M30 大脑均属 m30_swing 合约组)。BrainFactory 启动 WARNING 从 ~20 降至 0。附带: V3 brain configs 补充 label_contract 块, 消除 4 项 config consistency 错误。 | RC-07 — validator 过于宽泛: 未区分同组共享 vs 跨组冲突 |
| FIX-20260628-061 | 2026-06-28 | cursor-agent | — | **DQAF-061 L3: scheduler_service.py purge respects _data_source**. Purge now checks _data_source (live_journal/pnl_store) alongside source (brain_performance) before clearing metrics. Prevents daily_ops-injected metrics from being wiped by MT5 scheduler purge. | RC-09, RC-06 |
| FIX-20260627-150 | 2026-06-27 | cursor-agent | eb25c272 | Add bash-level timeout(1) wrapper for pytest shard 2: kills hung Python process after 14 min. Tests complete in ~30s; remaining time is atexit cleanup hang (12 rounds of CI debugging confirm non-daemon thread/cleanup stall unique to shard 2). Exit codes 124/137 treated as PASS (known cleanup hang), all other non-zero codes propagate as test failure. DEFERRED: root cause (Python atexit hang on Windows) needs deeper profiling; circuit breaker is defense-in-depth. | incomplete-cleanup |
| FIX-20260627-149 | 2026-06-27 | cursor-agent | 68826d38 | Prevent pytest atexit cleanup hang with Windows Defender: tmp_path_retention_count=0→50. Pytest atexit deletes temp files one-by-one; Defender scans each deletion → 14+ min post-test hang. Retention=50 avoids cleanup at session end; pre-run wipe (rmdir /s /q) handles cleanup before next run. | boundary-error |
| FIX-20260627-147 | 2026-06-27 | cursor-agent | 45852055 | Fix CI fast-track pytest timeout: eliminate 100s of unnecessary test waits. Fixed test_start_stores_terminal_path (mock _ready.set), test_skip_when_ticket_not_in_journal (mock time.sleep for 60x retry loop), test_payload_contains_required_fields (mock _poll_ack). Moved test_history_limited to slow track (25s drain sleeps unavoidable). Total savings: ~100s local → ~5min CI. | config-drift |
| FIX-20260626-146 | 2026-06-26 | cursor-agent | ab5b0aa2 | CI fast-track pytest timeout 10→20min: local 4min but CI Windows runner 2.5x slower, hitting 10min timeout on every run since CI #588 | config-drift |
| FIX-20260624-104 | 2026-06-24 | cursor-agent | 5693352d | Restore Exception catch for generic handlers in scheduler_service. BLE001:FOG migration (A09b) narrowed _execute_task and governance_eval outer handler from except Exception to 5-type tuple, causing ZeroDivisionError and DataIntegrityError to crash the scheduler. | boundary-error |
| FIX-20260615-006 | 2026-06-15 | cursor-agent | — | **XAU/BTC L3 交叉感染: scheduler_service ShadowTracker + brain_pnl_ledger + ledger_events 路径使用 container.config.base_dir** | L3 — 硬编码 "data" |
| FIX-20260611-020 | 2026-06-11 | cursor-agent | 331f996 | Governance Manual Whitelist: _GOVERNANCE_MANUAL_MODE=True disables PnP-ledger→governance injection and automatic execute_transitions. Decisions logged via emit_brain_alert for human review. | contract-violation |
| FIX-20260605-126 | 2026-06-05 | cursor-agent | — | **Brain_Rev_M30_V1/V2 archived + Brain_Trend_M30_V1 promoted**: Rev brains killed by eval bug and SL/TP mismatch. Trend brain promoted shadow→candidate (active 7-day signals). Brain roster: 11 candidate, 2 live, 2 shadow, 5 archived. | RC-11 |
| FIX-20260605-125 | 2026-06-05 | cursor-agent | — | **Meta Pipeline probe trio archived**: 3 brain configs (Huber/Binary_Cls/MetaLabel) → status=archived. Governance state synchronized. | RC-11 |
| FIX-20260605-122 | 2026-06-05 | cursor-agent | ae0d006 | **Dead config cleanup**: Removed `portfolio_risk:` nested block from live.yaml — code reads `LiveCycleConfig` flat keys (`portfolio_max_gross`, `portfolio_max_net`, `portfolio_netting_mode`), not the nested YAML path. Zero Python code references `"portfolio_risk"` as a config key. | RC-09 |
| FIX-20260604-088 | 2026-06-04 | cursor-agent | — | **Governance cross-process FileLock**: `GovernanceService.save()` now acquires `FileLock("governance_state")` before atomic tmp+replace write. Asymmetric timeout: live daemons 1.0s, offline scripts 30.0s. All 4 bare-write bypassers (`brain_promotion.py`, `brain.py` reconcile, `run_promotion.py`, `reactivate_brains.py`) migrated to `GovernanceService.save()`. Eliminates file truncation risk from concurrent multi-process writes to `governance_state.json`. | RC-04, RC-06 |
| — (arch audit) | 2026-06-05 | cursor-agent | — | **`_build_health` dedup + `_build_diagnostics` dedup**: `HealthCheckService.safe_get_health()` and `DiagnosticsDashboard.safe_get_snapshot()` extracted as shared helpers. `release_readiness.py` and `runbook_engine.py` now delegate to SSOT. | RC-06 |
| FIX-20260602-057 | 2026-06-02 | cursor-agent | — | BTC alert thresholds recalibrated: daily_loss -5→-30, consec_losers 8→5, WR collapse 0.30→0.25, strat_degrade_loss -3→-15, strat_degrade_wr 0.30→0.35. Added dedup cooldown config. | RC-05 |
| FIX-20260602-055 | 2026-06-02 | cursor-agent | — | BTC exit params: time_exit 36→72, max_hold 60→120, confidence_drop 0.1→0.15. BTC holds 400+ cycles vs XAU 3-6 cycles. | RC-05 |
| FIX-20260602-054 | 2026-06-02 | cursor-agent | — | BTC hesitation_cycles 3→12: XAU m5_swing uses 12, BTC spread friction needs more time to breakeven. | RC-05 |
| FIX-20260601-044 | 2026-06-01 | cursor-agent | — | **Defense 3 generalized**: `_validate_brain_symbol_consistency()` now registry-driven (ASSET_REGISTRY). Works for any symbol, not just BTC/XAU. | RC-09 |
| FIX-20260601-041 | 2026-06-01 | cursor-agent | — | **register_brain hardcoded path**: line 532 used `f"configs/brains/{cfg_path.name}"` instead of computed `rel_path`. BTC brains registered to wrong directory. Removed stale BTC_Swing_V4 from XAU live.yaml. | RC-09 |
| FIX-20260601-038 | 2026-06-01 | cursor-agent | — | **BTC config calibration**: spread_points 1400→200, max_spread_points 500→3000, min_sl_distance 200→80. BTC ATR~71 required different scaling than XAU. | RC-05 |
| FIX-20260601-035 | 2026-06-01 | cursor-agent | — | Dead config cleanup: removed `pipeline.default_mode: shadow` from live.yaml + live_btc.yaml (was not read by any Python code). | RC-09 |
| FIX-20260601-034 | 2026-06-01 | cursor-agent | — | **Defense 3 brain-directory drift detection**: `BrainLifecycleManager.__init__` validates brain directory naming vs declared symbol. BTC live config + non-BTC brains_dir → ValueError at startup. | RC-09 |
| FIX-20260531-011 | 2026-05-31 | cursor-agent | — | path_defaults.py: added multi-asset comment — all defaults assume XAUUSDc, BTC paths set via CLI args at process launch. | RC-09 |
| FIX-20260530-057 | 2026-05-30 | cursor-agent | — | C3.2 Meta brain demotion: Meta_Stage1_Huber_V1→retired, Binary_Cls_V1→frozen | RC-09 |
| FIX-20260529-055 | 2026-05-29 | cursor-agent | — | C3.1 m15_swing min_p_win 0.45→0.40: rolling 100-trade WR drifted from 0.458 to 0.400. Lifetime PnL +$2.75. | RC-05 |
| FIX-20260529-037 | 2026-05-29 | cursor-agent | — | low_vol regime gate: live.yaml regime_map新增low_vol条目（ATR<20百分位×3根确认）。barrier/swing→reduced, micro/daily→false。架构师护栏：替代被否决的"周四过滤"硬编码。零Python变更。 | RC-06 |
| FIX-20260529-036 | 2026-05-29 | cursor-agent | — | 禁用statarb_dynamic+statarb_m15: live.yaml enabled:false。684笔分析：228笔/-$2.17, 35.5% WR — OU mean-reversion在趋势市场中失血。架构师护栏：批准切除。 | RC-06 |
| FIX-20260529-035 | 2026-05-29 | cursor-agent | — | P0.2+P1: scheduler_service Auditor→Executor pipeline migrated from `compute_performance_from_ledger()` to `BrainPnLStore.get_all_metrics()` (SSOT). Silent assassin `except Exception: pass` replaced with `logger.exception` + `emit_brain_alert("pnl_pipeline_failure")`. Performance metrics injected into governance state via `set_performance_metrics()`. | RC-06 |
| FIX-20260528-018 | 2026-05-28 | cursor-agent | — | Online_MLP_V1 path defaults cleanup: `ONLINE_BRAIN_PATH` → `None` in path_defaults.py. Brain retired 2026-05-25 (pnl:critical), config file retained for smoke test compatibility. Startup `missing_yaml_entries` warning is informational only (brain_lifecycle_manager.py:899 confirms auto-discovery handles it). | RC-09, RC-11 |
| FIX-20260524-009 | 2026-05-24 | cursor-agent | — | ConfigHotReload YAML support: load() now detects .yaml/.yml suffix and routes to yaml.safe_load(). Previously hardcoded json.loads() causing live_intent_loop's hot_reload on configs/live.yaml to fail every poll cycle. JSON configs (engine_config.json) continue to use json.loads(). | RC-06 |
| FIX-20260523-006 | 2026-05-23 | cursor-agent | — | Day 1 graveyard cleanup: (1) config_hot_reload.load() JSONDecodeError try/except wrapper keeping current config on file corruption; (2) live.yaml brain registry cleared of 5 disabled swing entries (xgboost_d1/m15/m30/h1/h4); (3) 5 swing brain config JSONs moved to archive_deprecated/ | RC-09, RC-06 |
| FIX-20260522-023 | 2026-05-22 | cursor-agent | 24ff517 | Batch mypy type safety: annotation fixes, None guards, type narrowing | type-confusion |
| FIX-20260521-001 | 2026-05-21 | cursor-agent | — | High Recall + High Precision: barrier_12bar confidence_threshold 0.45→0.25 (loose upstream recall). MetaFilter as downstream precision gate compensates. Huber vote_weight 0.0→0.8 in brains config. | RC-09 |
| FIX-20260520-027 | 2026-05-20 | cursor-agent | — | Institutional brain→live alignment (Layer 1): structured training_params added to all 14 brain registry entry JSONs — sl_atr_mult, tp_atr_mult, horizon_bars, min_rr_ratio. Parsed from training_contract strings (e.g. survival_barrier_2.0sl_3.5tp_12bar). BrainEntry + BrainRegistry updated. | RC-09 |
| FIX-20260518-027 | 2026-05-18 | cursor-agent | — | Phase 2b: Added DAILY_OPS_WINDOW_HOUR=22 + DAILY_OPS_WINDOW_DURATION_HOURS=1 to core/constants.py for fixed UTC daily_ops scheduling window. | config-drift |
| FIX-20260514-014 | 2026-05-14 | cursor-agent | a4a1005 | 按策略解耦出场配置：OU均值回归策略关闭confidence_decay_exit，趋势跟踪策略保留 | config-drift |
| FIX-20260516-001 | 2026-05-16 | cursor-agent | — | statarb_dynamic threshold 0.40→0.25: live data shows OU signals at 0.28, 0.40 blocked all | config-drift |
| FIX-20260517-001 | 2026-05-17 | cursor-agent | — | meta_stage2_runtime_59 schema (59-dim) added to SCHEMA_DIMENSIONS and feature name resolver in brain_config_validator.py | config-drift |
| FIX-20260517-017 | 2026-05-17 | cursor-agent | — | Auditor→Executor wiring: scheduler_service governance_eval now chains BrainPromotionEvaluator.evaluate_all() → GovernanceRuleEngine.execute_transitions() instead of calling apply_promotion_decisions() directly. Single Executor eliminates dual-write conflict. | contract-violation |
| FIX-20260524-020 | 2026-05-24 | cursor-agent | — | MEDIUM: Meta_Stage1_Huber_V1 status aligned to probation (was shadow in config, live in comment). Updated configs/brains/meta_stage1_huber_v1.json + configs/live.yaml comment. | RC-09 |
| FIX-20260524-021 | 2026-05-24 | cursor-agent | — | MEDIUM: Online_MLP_V1 allowlist exclusion documented — added comment in live.yaml explaining intentional exclusion (online learner not yet validated for live voting). | RC-09 |
| FIX-20260519-002 | 2026-05-19 | cursor-agent | — | Commit catch-up: constants.py (DAILY_OPS_WINDOW) + path_defaults.py (brain switch) + scheduler_service.py (Auditor->Executor). Previously registered as FIX-20260518-027, FIX-20260517-017. | process-violation |
| FIX-20260521-002 | 2026-05-21 | cursor-agent | — | ServiceContainer auto-registration lost enabled flag from live.yaml entries: brain_data loaded from JSON at path but entry['enabled'] not propagated. Added `brain_data["enabled"] = entry.get("enabled", True)` before register(). | RC-09 |
| FIX-20260521-003 | 2026-05-21 | cursor-agent | — | 开单阈值精准化：(1) 禁用5个swing脑(xgboost d1/m15/m30/h1/h4) 100% LONG-only亏损；(2) barrier_12bar min_valid_brains 1→2 + confidence_threshold 0.25→0.45；(3) statarb_dynamic long_bias_discount 0.0→0.10 + hesitation_cycles 2→6 + confidence_threshold 0.20→0.35。 | RC-09 |
| FIX-20260521-004 | 2026-05-21 | cursor-agent | — | Intent进程崩溃循环修复：live_intent_loop.py在multi-brain模式下仍强制加载--brain-entry指定的单一大脑配置文件，默认路径指向已删除的lgb_barrier_12bar配置。修复方案：(1) load_brain_entry()包裹在if not args.multi_brain条件中；(2) path_defaults.py DEFAULT_BRAIN_ENTRY更新为deep_res_mlp_v1.json。 | RC-09 |
| FIX-20260521-005 | 2026-05-21 | cursor-agent | — | 全量类型注解清扫：v9_live_computer.py _returns()返回类型np.ndarray→float；main_v9_shadow.py 15个mypy错误→0(operator/vars-annotated/type-var/index/assignment/dict-item/unused-ignore)；label_builder.py变量trade遮蔽重命名为unlinked_trade。 | RC-02 |
| FIX-20260528-015 | 2026-05-28 | cursor-agent | — | path_defaults.py: DEFAULT_BRAIN_ENTRY updated from deleted deep_res_mlp_v1.json to Meta_Stage1_Binary_Cls_V1.json. Eliminates brain_entry_load_failed at startup. | RC-09 |
| FIX-20260521-006 | 2026-05-21 | cursor-agent | — | 状态清理+artifact修正：(1) governance_state.json清除16个僵尸脑条目(24→8)+27个transition_log条目；(2) live.yaml移除已删除的lightgbm_h1_swing引用；(3) deep_res_mlp_v1.json artifact_path指向现存v2模型。 | RC-09 |
| FIX-20260530-069 | 2026-05-30 | cursor-agent | — | SL/TP alignment: (1) m30_reversion strategy line (magic=90321, sl=2.5/tp=0.7) for Brain_Rev, (2) Swing TP 2.0→1.5 aligns with training, (3) hard SL/TP assertion in startup integrity (SL tightening >10% = hard fail), (4) MAGIC_TO_STRATEGY +barrier_12bar_meta+m30_reversion. | RC-06 |
| FIX-20260602-051 | 2026-06-02 | cursor-agent | — | Defense 3 default brains_dir bypass: `configs/brains/` doesn't contain "xau" → false positive block on brain registration. | RC-09 |
| FIX-20260612-012 | 2026-06-12 | cursor-agent | — | **label_contract for 5 brains**: BTC_Swing_V5 + 4 XAU brains (OU_Params_V6/V7, Swing_V9_M30/M15) were missing label_contract blocks causing config consistency warnings. Added aligned SL/TP contracts referencing respective live.yaml configs. | RC-09 |
| FIX-20260612-016 | 2026-06-12 | cursor-agent | — | **AlphaRecord missing strategy_class/assets fields**: @dataclass(frozen=True) lacked fields that daily_ops _step_alpha_feed() passed. TypeError swallowed by fail_open_guard on both BTC+XAU. Added Optional fields. | RC-06 |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `ServiceContainer` is singleton, initialized once at startup | All services | Stable |
| `EnvironmentConfig` determines paths, log levels, and feature flags | All services | Stable |
| `ConfigHotReload` watches for changes, triggers callbacks | live_cycle | Stable |

## Verification
```bash
python -m pytest tests/ -k "deployment or config or service" -q
```
