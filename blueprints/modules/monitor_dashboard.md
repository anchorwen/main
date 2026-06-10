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
| FIX-20260608-001 | 2026-06-08 | cursor-agent | — | **DingTalk alert pipeline P0 repair**: (1) Dedup bypass for trade_notification — _dedup_or_pass() now whitelist-passes trade_notification instead of suppressing all after 1/60s. (2) Polymorphic _format() engine — Type A direct (title+text), Type B runbook (Phase 2), Type C snapshot fallback. Fixes renderer blindness where notify_trade's title/text and runbook_bridge's SOP were discarded. (3) Skip runbook enrichment for trade_notifications. (4) Symbol instance fingerprinting injected into all alerts. (5) 4 `__import__` anti-patterns replaced with top-level imports. | RC-06 |
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
