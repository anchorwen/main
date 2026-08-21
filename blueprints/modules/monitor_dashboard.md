# Monitor / Dashboard

## Purpose
Web-based real-time monitoring dashboard for the live trading system. Single-file HTTP server with embedded HTML/CSS/JS serving 14 JSON API endpoints with auto-refresh.

## Key Files
| File | Role |
|------|------|
| `apps/monitor/live_trading_dashboard.py` | `LiveDashboardHandler` — HTTP server, 14 API endpoints, HTML template |

## Data Flow
```
┌──────────────────────────────────────────────────────────┐
│  Browser (10s polling)                                    │
│    │ fetch /api/* × 12 endpoints                          │
│    ▼                                                      │
│  LiveDashboardHandler.do_GET()                            │
│    ├─ /api/dashboard  → scripts.live_dashboard            │
│    ├─ /api/performance → BrainPnLStore + tracker + gov    │
│    ├─ /api/brains      → tracker + gov + PnL + decisions  │
│    ├─ /api/brain/{id}  → single brain detail (NEW)        │
│    ├─ /api/decisions   → shadow: decisions.jsonl          │
│    │                     live: live_trade_journal.jsonl    │
│    ├─ /api/modules     → bridge/outbox/feature store/...  │
│    ├─ /api/governance  → GovernanceService + tracker      │
│    ├─ /api/analytics   → param_suggestions + tracker      │
│    ├─ /api/positions   → MT5 live or snapshot cache       │
│    ├─ /api/journal     → live_trade_journal.jsonl         │
│    ├─ /api/slo         → SloService                       │
│    ├─ /api/alerts      → AlertService + AuditLog          │
│    ├─ /api/health      → auto_healthcheck + tracker + gov │
│    └─ /api/risk        → live_dispatch_policy             │
└──────────────────────────────────────────────────────────┘
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| feedback | BrainPnLStore, BrainPerformanceTracker | PnL and performance metrics for display |
| governance | GovernanceService | Brain lifecycle states |
| observability | SloService, AlertService, StructuredAuditLog | Health and alert data |
| scripts | live_dashboard, live_auto_healthcheck, live_dispatch_policy, mt5_positions_snapshot | Data collection |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| (none) | — | Dashboard is a leaf consumer; no other module imports it |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
| FIX-20260821-005 | 2026-08-21 | cursor-agent | — | **TECH_DEBT-018 清偿 (P7, 投委会战术推进令): META_FILTER_WIRED_STALE 假阳性根治 — The Boot-Anchored Wire**. 新 `core/observability/meta_wire_events.py` (纯 stdlib SSOT leaf): producer (`scripts/live_intent_loop.py`) wired 成功时同步 append `{base_dir}/state/meta_pipeline_wired.jsonl` (跨 stdout 路由持久, 崩溃循环期 intent log 断流免疫) + `read_last_wired_event` tail-read + `parse_intent_boot_ts` (intent 日志文件名编码 boot). `check_meta_filter_state` 决策表重构为 **boot 锚定**: SSOT 时间 ≥ 最新 intent log boot 时刻 → 当前 boot wired → PASS (MICRO_SCALER_NOT_LOADED WARN 保留); 未确认 + last wire ≤360min → mid-boot 宽限 PASS (FIX-20260610-007 防狼嚎); 未确认 + >360min → 真 STALE WARN; intent-log fallback 覆盖 rollout 空窗 (每日假 WARN 即刻消除无需重启); 8/11 崩溃循环 (每次重启均成功 wired) 亦 PASS; state-file 次级 (MISSING/ATR_FROZEN/pred_buffer) 原样保留. `wired_age` 降级为诊断指标. 回归锁 25 测试. | L3 — boot-scoped 事件按 raw age 判定 staleness + 无持久 SSOT (RC-12/RC-06) |
| FIX-20260821-001 | 2026-08-21 | cursor-agent | — | **健康检查唯一时钟 (DQAF-20260820-005, TECH_DEBT-011 清偿收尾)**: `health_checks.check_feature_store` 移除 ad-hoc `POST_OUTAGE` 1440-min 启发式 (FIX-20260629-172) — 分支收敛为 warn/fail 双 anchor (staleness_anchor × base / ×2), market_type per-symbol (XAU→forex_24_5 / else crypto_24_7); TIMESTAMP_UNREADABLE→COLD_START→FAIL→WARN→PASS 顺序. 系统不再有第二个计算时间的"钟表" (IC 裁决: "不要在系统里留下第二个计算时间的'钟表'"). 回归锁 6 测试 (test_health_checks_feature_store.py: 闭市冻结 PASS / 闭市前断裂 FAIL / crypto 中龄 WARN / 不可读 WARN / 冷启动 WARN / 新鲜 PASS — 首次为 check_feature_store 提供零覆盖率以来覆盖). | L2 — ad-hoc 停机阈值 (1440min) 与市场日历重复造钟 |
| FIX-20260806-003 | 2026-08-06 | cursor-agent | — | **A2 mypy 清零 — `live_trading_dashboard.py` `_collect_system_resources` with-return 重构**: return 移出 fail_open_guard with 块 (mypy missing-return 分析盲区) → 行为等价 (fallback 计算在 with 内, return 在 except 块末). 消除最后 1 处 --full mypy 错误. 详见 FIX_REGISTRY. | L1 — mypy with-context return 分析局限 |
| FIX-20260804-007 | 2026-08-04 | cursor-agent | — | MODULE_SOURCE_MAP: add `scripts/audits/_audit_xau_votes_today.py` to monitor_dashboard module (DQAF-20260804-006 法证工具纳入版本库 — IC 批准; XAU 退化脑投票审计, scripts/ 根 → scripts/audits/ 迁移 + 参数化 --date/--data-dir). | RC-12 — new module registration |
| FIX-20260713-004 | 2026-07-13 | cursor-agent | — | **Phase 3 Layer 4: gate activity metrics cross-check + BehavioralMetrics extension**. `_check_gate_activity_metrics`: reads golden_master.jsonl, classifies gate reasons (mirrors analyze_gate_activity.py), detects dead gates (block rate <0.05%), returns FAIL when >10% unique gates dead. `BehavioralMetrics` extended with `gate_blocks: dict[str,int]` and `gate_block_rate: dict[str,float]`. Registered in DataHealthService include_extras. Part of 4-layer config defense system Phase 3 (P2). | RC-12 |
| FIX-20260712-003 | 2026-07-12 | cursor-agent | — | **Phase 2 Layer 4: dead gate detection script**. `scripts/analyze_gate_activity.py` — Iron Law #11 compliant golden_master.jsonl gate frequency analyzer with dead gate (<0.05% block rate) detection. Outputs formatted table + dead gate detail. Part of 4-layer config defense system Phase 2. | RC-12 |
| FIX-20260712-002 | 2026-07-12 | cursor-agent | — | **Phase 1 Layer 2: 2 new cross-checks + probation coverage in data_health checks**. `_check_brain_config_governance_status_alignment` (config-as-floor violation→FAIL), `_check_live_yaml_enabled_vs_brain_status` (zombie/intent-conflict detection). Existing brain_registry_governance_alignment extended to live+probation. Registered in include_extras of DataHealthService._run(). Part of 4-layer config defense system (FIX-20260712-002). | RC-09, RC-07 |
| FIX-20260629-172 | 2026-06-29 | cursor-agent | — | **Feature Store 健康检查市场拓扑感知:** check_feature_store() 增加冷启动宽限期 (COLD_START: 系统运行 < 10 min → WARN) + 长时间停机判断 (POST_OUTAGE: feature age > 24h → WARN). 周末/维护窗口后重启不再触发 FAIL 虚警. 新增阈值: feature_store_cold_start_grace_minutes=10, feature_store_post_outage_threshold_minutes=1440. | L2 — 健康检查无市场状态感知, 15-min 固定阈值在停机和周末产生虚警 |
| FIX-20260626-140 | 2026-06-26 | cursor-agent | — | MODULE_SOURCE_MAP: add `scripts/data_integrity_check.py` to monitor_dashboard module. Resolved 13 mypy errors + 1 ruff UP038. Iron Law #11 data integrity verification script. | RC-09 |
| FIX-20260625-136 | 2026-06-25 | cursor-agent | — | MODULE_SOURCE_MAP: add `scripts/audit_entry_spread_coverage.py` to monitor_dashboard module. Iron Law #11 audit script for journal entry_spread coverage. | RC-09 |
| FIX-20260625-131 | 2026-06-25 | cursor-agent | d0513d53 | Flaky test: test_summary_emission — time.sleep(0.06) insufficient on Windows (~15.6ms timer quantum), raised to 0.15s to safely exceed 50ms storm_summary_interval | missing-null-check |
| FIX-20260624-119 | 2026-06-24 | cursor-agent | — | **BLE001:FOG over-narrowing in EventBus.publish()** — restore `except Exception` for pub/sub fire-and-forget handler dispatch. 5-type FOG tuple excluded ZeroDivisionError, crashing publisher. Same class as FIX-20260624-104. | RC-05 — one-size-fits-all narrow tuple on generic callable dispatch |
| FIX-20260624-086 | 2026-06-24 | cursor-agent | — | **UGR-A06: InvariantEngine — 15 binary invariants in shadow mode**. New `core/observability/invariant_engine.py` with 15 invariants + shadow-mode enforcement + alert hub integration. | RC-12 |
| FIX-20260624-085 | 2026-06-24 | cursor-agent | — | **UGR-A05: AlertStormDetector + self-monitoring in LiveAlertHub**. Added sliding-window storm detection with NORMAL→WARNING→STORM auto-recovery, storm summary emission, and `get_health_status()` self-monitoring. | RC-12 |
| FIX-20260623-077 | 2026-06-23 | cursor-agent | — | **DQAF-077: mypy Baseline Cleanup — health_checks.py mixin attr-defined fix**. Added class-level attribute annotations (`_base_dir`, `_symbol`, `_position_manager`, etc.) + `_t()` stub method to satisfy mypy for the HealthCheckMethods mixin pattern. 70 errors → 0. | RC-10 — mixin attr-defined false positives |
| FIX-20260623-069 | 2026-06-23 | cursor-agent | — | **DQAF-069: Audit Filter Bias Prevention — 3 false positive FAILs eliminated**. (069a) Identity check: last-5 PnL → full-record MD5 hash + record count + cumulative PnL signature. V6+V7+V8 FAIL→PASS, V11 FAIL→PASS. (069b) Label dup check: position_ticket → (ticket, brain_id) pair — 335 false dups eliminated. (069c) Timestamp compare: normalize Z-suffix before string comparison. Added AUDIT_CHECK_MANIFEST (16 checks, self-documenting) + `--validate-self` mode for filter bias prevention. 5 FAIL→2 FAIL. | L2 — audit metrics not aligned with production semantics (multi-brain labeling, brain inactivity, ISO 8601 variants) |
| FIX-20260623-067 | 2026-06-23 | cursor-agent | — | **DQAF-067: `audit_data_exhaustive.py` journal ticket filter alignment**. Replaced narrow `retcode=10009 + request.action=1` filter with label_builder-consistent `action=="open"` + `detail.order` fallback. Coverage metric now matches actual label pipeline. BTC 39%→96%, XAU 64%→99%. | L2 — audit filter measured different dimension than label pipeline |
| FIX-20260622-061 | 2026-06-22 | cursor-agent | — | **Code style: line-wrapping in `audit_state_of_system.py` + `commander_g4_g6_g7_coverage_xau.py`**. Pure formatting — long dict comprehensions and conditional expressions broken across multiple lines for readability. No behavioral change. | L1 — code style consistency |
| FIX-20260622-059 | 2026-06-22 | cursor-agent | — | **DQAF-059 Magic Drift Attribution Loss — Complete Repair**. See FIX-20260622-059 in FIX_REGISTRY.md and DQAF-20260622-059 in DQAF_DOCKET_REGISTRY. | L2 — MAGIC_TO_STRATEGY missing entries; L3 — no SSOT for magic↔strategy mapping |
| FIX-20260622-058 | 2026-06-22 | cursor-agent | — | **DQAF-058: `check_meta_filter_state` extended with `micro_scaler_loaded` tracking**. Health check now extracts `micro_scaler_loaded` from `meta_pipeline_wired` event. New `MICRO_SCALER_NOT_LOADED` warning code when scaler not loaded (raw features → PSI drift). Prevents 23-day silent false-negative like the DQAF-054/055 deployment gap. | L3 — no health check existed for scaler loading status |
| FIX-20260622-053 | 2026-06-22 | cursor-agent | — | **DQAF-053 Phase 3: audit script path + Unicode fixes**. `commander_guardrails_arch.py`: `reports/` subdirectory path fallback for leaderboard.json, state files, and schema validation — MISSING false positives 16→7. `commander_g3_alpha_vacuum.py`: `reports/` path fallback for alpha_allocation.json + execution_state.json. `monitor_pwin_fix.py`: emoji→ASCII markers + stdout UTF-8 hardening (fixes UnicodeEncodeError on Windows GBK terminals). | L1 — hardcoded root-directory paths missed `reports/` subdirectory layout; GBK codec can't encode emoji |
| FIX-20260622-052x | 2026-06-22 | cursor-agent | — | **MODULE_SOURCE_MAP: Iron Law #11 Institutional Audit Portfolio → monitor_dashboard**. 8 ad-hoc diagnostic scripts (previously untracked) registered as permanent audit assets: `audit_full_pipeline.py` (DQAF-043 Phase 3), `audit_state_of_system.py` (SSOT audit), `commander_g2_metafilter_path.py` (G2 MetaFilter), `commander_g3_alpha_vacuum.py` (G3 Alpha vacuum), `commander_g4_g6_g7_coverage_xau.py` (G4/G6/G7 XAU coverage), `commander_guardrails_arch.py` (cross-symbol arch guardrails), `monitor_pwin_fix.py` (DQAF-044-bis monitor), `verify_dqaf044_fix_effect.py` (DQAF-044 verification). All pure Iron Law #11 stdout-reporting tools. 3 mypy type annotations fixed (pre-existing). | RC-09 |
| FIX-20260620-007 | 2026-06-20 | cursor-agent | 93c938e | **Unit tests for _health_helpers.py (SF #28)**: 30 tests covering all 6 shared I/O helpers (_utc_iso, _age_minutes, _safe_json_load, _safe_jsonl_count, _safe_jsonl_last, _safe_jsonl_tail_stats). Edge cases: None/empty/invalid inputs, label distribution. | RC-08 |
| FIX-20260620-006 | 2026-06-20 | cursor-agent | 0c2d65a | **SF #28 — DataHealthService check farm extraction**: 3,117→274 lines (−91%). Created HealthCheckMethods mixin (2,731 lines, 38 @health_check methods) + _health_helpers.py (145 lines, 6 shared I/O). `DataHealthService(HealthCheckMethods)`. | RC-08 |
| FIX-20260613-088 | 2026-06-13 | cursor-agent | — | **Iron Law #13: DingTalk Institutional-Grade Structured Alert**. (D1) event_schema.py — BaseTelemetryEvent + DataHealthPayload frozen dataclasses. (D2) localization.py — SSOT RuleRegistry (17 rules + 22 keys, extracted from alert_channels.py hardcoded dicts). (D3 reserved) runbook_id for Self-Healing Engine. data_health_schema.py build_alert_context() → list[dict] (rejects \\n-string). DingTalkAlertChannel._format() renders dedicated 故障源/警告源 bullet sections + Type B runbook SOP (P0 actions + diagnostic commands + escalation path). RULE_NAME_CN +6, CONTEXT_KEY_CN +8. Fixes operator blindness in data_source_critical_failure alerts. | RC-06 |
| FIX-20260612-023 | 2026-06-11 | cursor-agent | 6753a86 | Downgrade ConformalCalibrator cold-start alert from CRITICAL to WARNING. CRITICAL on every restart was alert noise — calibrator needs 50 closes to warm up. Now only WARNING during warmup. Also diagnosed duplicate alert dispatch bug (RULE-012 fires twice in 1s despite 300s cooldown). | contract-violation |
| FIX-20260608-001 | 2026-06-08 | cursor-agent | — | **DingTalk alert pipeline P0 repair**: (1) Dedup bypass for trade_notification — _dedup_or_pass() now whitelist-passes trade_notification instead of suppressing all after 1/60s. (2) Polymorphic _format() engine — Type A direct (title+text), Type B runbook (Phase 2), Type C snapshot fallback. Fixes renderer blindness where notify_trade's title/text and runbook_bridge's SOP were discarded. (3) Skip runbook enrichment for trade_notifications. (4) Symbol instance fingerprinting injected into all alerts. (5) 4 `__import__` anti-patterns replaced with top-level imports. | RC-06 |
| FIX-20260610-007 | 2026-06-10 | cursor-agent | — | **FIX-007 收尾**: (1) tail-read优化 _safe_jsonl_last() XAU LIGHT 117ms→12ms; (2) 补齐4个缺失检查(feature_store_schemas/alert_delivery/data_health_self/alpha_allocation) 覆盖率21→24源; (3) FS schemas契约修复 fields vs dimensions; (4) golden_master 区分病理/正常阻塞; (5) leaderboard crash message未初始化; (6) journal_vs_pnl_ledger 计数错误 len(brains)→sum(len(trades)); (7) send_data_health_alert.py DingTalk适配器 双品种推送测试通过. | RC-06, RC-10 |
| FIX-20260610-005 | 2026-06-10 | cursor-agent | — | **DataHealthService — 统一数据健康监控**: `data_health_schema.py` (dataclass/enum/@health_check装饰器注册表), `data_health_service.py` (CRITICAL 6项检查 + 跨源对账 + 孤儿检测 + 原子状态持久化), `run_data_health.py` (CLI, JSON输出, 退出码0/1/2). LIGHT<50ms. data_health_monitor.py→shim. 告警规则RULE-012~016. DQAF-20260610-001 IC Mandate + Architecture Review. | RC-12 |
| FIX-20260607-146 | 2026-06-07 | cursor-agent | — | **Alert label fix + frankenstein metric**: strategy_pnl label corrected (USD→R). strategy_win_rate→最差大脑胜率. worst_brain_id added. Frankenstein metric fixed: single brain selection vs independent min(). | RC-08 |
|--------|------|--------|--------|---------|------------|
| FIX-20260606-136 | 2026-06-06 | cursor-agent | — | **Agentic DQAF v1.0**: Diagnostic Quality Assurance Framework — 3 system ledgers (DQAF_DOCKET_REGISTRY, CCT_LEDGER, ReB_PATTERN_INDEX), ECoL evidence collection script (dqaf_collect.py), Iron Law #9 (zero-hallucination dual-track diagnostic protocol). IEC 62740 / ISO 31000 / NTSB Party System. | RC-12 |
| FIX-20260604-087 | 2026-06-04 | cursor-agent | — | **Alert rule SSOT merge**: 11 declarative alert rules moved to YAML `alert_system.rules`. live_alert_hub.py (10 rules) and alert_service.py (5 rules, 4 overlapping) merged into single `build_rules_from_config()` shared builder. Eliminates hardcoded lambda duplication and rule drift between the two subsystems. Added `high_throttle_rate` (previously alert_service-only) to unified catalog. Added `rules_config` param to `LiveAlertHub.__init__` and `AlertService.with_default_rules()`. YAML schema supports `operator: gt/lt/eq` for simple rules + `type: composite` for multi-field rules. **Phase 3**: journal freeze gate (`scripts/journal_freeze_gate.py`) + `.pre-commit-config.yaml` hook + `.github/CODEOWNERS`. **Phase 2**: 2026-09-04 deadman's switch written to `deferred_evaluate_extraction.md`. | RC-09, RC-06 |
| FIX-20260529-044 | 2026-05-29 | cursor-agent | — | PR#2 CB→AlertHub cross-propagation: LiveAlertHub.send_critical(reason, detail) added — direct injection API for external infrastructure components (e.g. MT5Worker) to enqueue critical alerts outside the normal evaluate-and-dispatch cycle. Trips hub circuit breaker + enqueues alert for async delivery. | RC-06 |
| FIX-20260529-040 | 2026-05-29 | cursor-agent | — | Phase A alert infrastructure: DingTalkAlertChannel (HMAC-SHA256), CircuitBreaker.trip(), LiveAlertHub (6-layer pipeline: rules→circuit breaker→Slack/DingTalk/Log). BackgroundDeliveryWorker with per-rule dedup. live_alert_hub.py (~260行) + alert_channels.py (+100行). | RC-12 |
| FIX-20260524-033 | 2026-05-24 | cursor-agent | — | Batch mypy type safety: slo_service.py (1→0 — type: ignore[arg-type] for float cast), live_dashboard.py (1→0 — getattr fix). MODULE_SOURCE_MAP: add live_dashboard.py to monitor_dashboard. | type-confusion |
| FIX-20260522-023 | 2026-05-22 | cursor-agent | 24ff517 | Batch mypy type safety: annotation fixes, None guards, type narrowing | type-confusion |
| FIX-20260518-028 | 2026-05-18 | cursor-agent | — | Phase 3: Unified health aggregator — _build_unified_health() reads 7 data sources, /api/health/full endpoint with 10s cache, overall_status: healthy|degraded|critical, frontend single-request rendering with fallback to individual endpoints. | missing-feature, observability-gap |
| FIX-20260517-023 | 2026-05-17 | cursor-agent | — | Panel redesign: 全局汉化 + 布局重整 5行→4行+tab + 新增 /api/brain/{id} 端点 (SVG sparkline PnL 走势/方向分布/治理/训练指标) + P0 shadow/live 同文件修复 + 异常日志改进 + 新建 monitor_dashboard 蓝图。 | missing-feature, config-drift |
| FIX-20260519-002 | 2026-05-19 | cursor-agent | — | Commit catch-up: schema v2 + logging + unified health cache. Previously registered as FIX-20260517-023, FIX-20260518-028. | process-violation |
| FIX-20260602-059 | 2026-06-02 | cursor-agent | — | **Trade notifications to DingTalk**: `LiveAlertHub.notify_trade()` sends real-time open/close push. Hooked into dispatch path in live_cycle.py. Config thresholds also reloaded. | RC-12 |
| FIX-20260603-067 | 2026-06-03 | cursor-agent | — | **Gate telemetry funnel**: per-cycle gate reason counters flushed every 12 cycles to `reports/telemetry_gates.jsonl`. Enables strategy funnel analysis. | RC-12 |
| FIX-20260612-009 | 2026-06-12 | cursor-agent | — | **Partial-view warning**: system_health.py now prints explicit warning + summary footer when only one symbol is checked. Prevents "XAU is offline" false positive from incomplete health queries. | RC-07 |
| FIX-20260612-011 | 2026-06-12 | cursor-agent | — | **Bridge heartbeat key mismatch**: data_health_service checked `bridge_last_ack_utc`/`connected` but bridge writes `last_heartbeat_utc`/`mt5_connected`. Fixed key names to match actual bridge health report format. Eliminates BRIDGE_TIMESTAMP_UNREADABLE false positive. | RC-06 |
| FIX-20260612-015 | 2026-06-12 | cursor-agent | — | **Brain registry ↔ governance alignment cross-check**: new FULL-mode check detects triple-bookkeeping bugs (registry status≠governance, vote_weight=0, live.yaml disabled). Would have caught DQAF-20260612-002 before trading was blocked. 3 cross-checks now: journal-ledger, open-close, brain-gov alignment. | RC-09 |
| FIX-20260613-088r | 2026-06-13 | cursor-agent | — | **AlertService context_snapshot filter fix**: isinstance check only allowed str\ | int\ |
| FIX-20260613-089 | 2026-06-13 | cursor-agent | — | **journal_completeness dupes threshold desensitization**: raised `dupes > 0` → `dupes > 5` in `check_journal_completeness()`. Original threshold caused alert fatigue (CRITICAL alert for 2 dupes with zero PnL impact). In async retry-reentrant architecture, occasional dupes are expected until Phase 2 Event Sourcing delivers idempotent journal writes. Tech debt: `TODO-20260711-journal-idempotency`. **CLEARED by FIX-20260805-008 (2026-08-05)** — Phase 2 idempotent-key dedup, residue metrics-only. | RC-06 |
| FIX-20260805-009 | 2026-08-06 | cursor-agent | — | **L1: trade_journal trail 遥测探针契约对齐 (DQAF-20260805-003, IC 行动令 Sev 4 Lite)**: health_checks.py:101 精确键 `"trail" not in labels` 未随 FIX-20260612-003 (reconciliation 写 sl_hit_trailed) 演进 → 每轮误报. 改包含性匹配 `not any("trail" in k for k in labels)`. 回归锁 test_trade_journal_trail_probe.py 4 测试 (sl_hit_trailed 抑制 / 混合抑制 / 真缺失保留 / close_count 门槛). 实库: 契约对齐正确, 但 tail 窗口真实缺 trail label → DQAF-20260806-001 (Sev 2 OPEN) 追 trail 出场遥测缺口. ReB: SEMANTIC_DRIFT_MONITOR_PROBE. | RC-06 |
| FIX-20260805-008 | 2026-08-05 | cursor-agent | — | **L2: journal_completeness Phase 2 幂等语义升级 — The Idempotent Key (DQAF-20260805-002, IC 批准)**: 清偿 TODO-20260711-journal-idempotency (超期 25 天), FIX-20260611-005 临时补丁退役. 移除 _expiry/[EXPIRES] 框架; dedup 升级为 Phase 2 幂等事件身份 (position_identifier, deal_id) — retry residue metrics-only 不 FAIL (实库 212 残留 → 每次重启假 CRITICAL 根治), 同仓 ≥2 不同非零 deal_id → ambiguous_events FAIL (实测 0), deal_id=0 不参与歧义; 保留 close_price/trail FAIL. 回归锁 test_journal_completeness_phase2.py 10 测试. 实库实证 JOURNAL_SLA_OK. ReB: EXPIRED_TEMP_GATE_UNRETIRED. | RC-06 |
| FIX-20260625-137 | 2026-06-25 | cursor-agent | — | MODULE_SOURCE_MAP: add `scripts/diagnose_mypy_baseline.py`. Iron Law #11 diagnostic script for mypy baseline regression analysis and roadmap target gap assessment. | RC-09 |
| FIX-20260628-058b | 2026-06-28 | cursor-agent | — | DQAF-058 diagnostics: add `scripts/audit_btc_live_direction.py` (Iron Law #11 compliant BTC live brain direction audit) + `scripts/forensic_feature_analysis.py` (feature distribution forensics for direction bias root cause). Both scripts write stats to stdout only. | RC-09 |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `/api/brain/{id}` | live_dashboard frontend | Stable |
| `/api/health/full` | live_dashboard health panel | Stable |

## Verification
```bash
python apps/monitor/live_trading_dashboard.py --port 8080 &
curl -s http://127.0.0.1:8080/api/brain/OU_Params_V6_Sniper | python -m json.tool | head -20
curl -s http://127.0.0.1:8080/ | grep "量化交易"
kill %1
```
