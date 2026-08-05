# Monitoring Subsystem

> **Module**: monitoring | **Owner**: infrastructure | **Status**: active
> **Created**: 2026-06-17 | **Dependencies**: data/, data_btc/, MT5 terminals

## Purpose

Automated silent monitoring for data integrity and feature distribution drift.
Replaces ad-hoc manual checks with scheduled hourly audit + PSI-based early
warning for model decay.

## Key Files
See [Sub-Components](#sub-components) below for the full list of monitoring scripts and their functions.

## Sub-Components

| Component | File | Function |
|-----------|------|----------|
| Data Integrity Auditor | `scripts/audit_data_integrity.py` | 9-dimension institutional audit |
| Feature Drift Monitor | `scripts/monitor_feature_drift.py` | PSI-based distribution comparison |
| Alert Dispatcher | `scripts/alert_dispatcher.py` | Unified DingTalk push with cooling/aggregation |
| Orphan Tombstone | `scripts/tombstone_orphans.py` | Isolate contaminated journal entries |
| Journal Dedup | `scripts/dedup_journal.py` | Remove duplicate close entries |
| PnL Normalizer | `scripts/normalize_journal_pnl.py` | MT5-authoritative PnL correction |
| Audit Scheduler | `scripts/setup_audit_schedule.bat` | Windows Task Scheduler hourly trigger |

## Architecture

```
                    ┌──────────────────────┐
                    │  Windows Task        │
                    │  Scheduler (hourly)  │
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
     ┌────────▼────────┐ ┌─────▼──────┐ ┌───────▼────────┐
     │ audit_data      │ │ monitor    │ │ normalize +    │
     │ _integrity.py   │ │ _feature   │ │ tombstone +    │
     │ (9-dim check)   │ │ _drift.py  │ │ dedup (on-demand)│
     └────────┬────────┘ └─────┬──────┘ └───────┬────────┘
              │                │                │
              └────────────────┼────────────────┘
                               │
                    ┌──────────▼───────────┐
                    │  alert_dispatcher.py │
                    │  (DingTalk push)     │
                    └──────────────────────┘
```

## Data Flow
See [Architecture](#architecture) above — the scheduler-driven pipeline (Task Scheduler → audit/PSI/normalize → DingTalk alert dispatch) serves as this module's Data Flow documentation.

## Data Dependencies

- `data/live_trade_journal.jsonl` — XAU trade journal
- `data_btc/live_trade_journal.jsonl` — BTC trade journal
- `data/feature_store/records/` — XAU live features
- `data_btc/feature_store/records/` — BTC live features
- `data/training/balanced_v1/` — training baseline for PSI
- MT5 terminals (EXNESS2 for XAU, MetaTrader 5 for BTC)

## Fix History

| FIX-20260805-007 | 2026-08-05 | cursor-agent | — | **hash-lock 门禁内容基升级 — The Content-Based Gate (DQAF-20260805-001, IC 绝对批准)**. 预检 hash_lock 从 `git status --porcelain`(stat 基, git Stat 幽灵 + CRLF 伪差异假阳性) 升级为内容基: `_run_git_content_snapshot()`(`git diff HEAD --name-only` + `git ls-files --others --exclude-standard`) + `_classify_tree(dirty, untracked)` — dirty tracked source **或** untracked 非探针 source → Sev1; `_audit_*.py` 法证探针豁免 (永不阻断决战日). 与 `_enforce_hash_lock` (canonical train_btc_expected_r_institutional) 保持 "never drift" 契约. 回归锁: tests/training/test_hash_lock_content_gate.py. ReB: HASHLOCK_STAT_PHANTOM. | contract-violation |
| FIX-20260805-006 | 2026-08-05 | cursor-agent | 39dc683f | DingTalk 机器人安全关键词 QuantOs 送达修复: alert_dispatcher._build_markdown 追加页脚 '---/QuantOs 实盘告警系统' — scripts 路径 (gate2/audit/drift/precheck) 告警此前全缺关键词 → errcode=310000 拒收; live 通道与 data-health 页脚已含. 实测 errcode==0 DINGTALK_SENT. 附带发现: DingTalkAlertChannel.send 只查 HTTP 200 不查 errcode (Deferred). | config-drift |
| Date | FIX ID | Description |
|------|--------|-------------|
| 2026-08-05 | FIX-20260805-003 | **Gate 2 先锋哨兵部署 (IC 裁决 2, Red Gap 1)**. `scripts/gate2_sentinel.py` — daily OFI accumulation monitor reusing `inspect_ofi_history.inspect()` (statistics SSOT) + `alert_dispatcher.dispatch_alert()` (DingTalk, cooling built in). 6-state decision machine (data missing / record rollback / Gate2 READY once / 24h stall / near-deadline / normal). Stall detection accounts for broker weekend closure + ~1h daily maintenance halt (IC 2026-08-05 correction: weekend runs skipped, ETA from historical average h1_windows/span_days). Mounted as Windows schtasks `Future\Gate2Sentinel` daily 12:30 (Last Result 0 verified, progress trail `data_btc/state/gate2_sentinel.log`). Design: docs/runbooks/gate2_sentinel_deployment.md. Zero new alert/statistics logic (Decoupling/Iterability preserved). |
| 2026-08-05 | FIX-20260805-004 | **每日战役健康预检 (IC 2026-08-05, 8/19 决战 Red Gap 2)**. `scripts/daily_flow46_precheck.py` — 04:03 Mon-Fri Beijing (周末除外) 综合 8/19 战役就绪报告, 组合复用 `inspect_ofi_history.inspect()` + gate2_sentinel state + `mt5_bridge_health.json` + `git status --porcelain` (精确复制 `_enforce_hash_lock` 过滤器). 5 项检查: sentinel_liveness (>36h Sev1) / ofi_freshness (>4h Sev1, 覆盖每日~1h休市) / hash_lock (脏 source Sev1 = 8/19 头号杀手) / bridge_health (Sev2) / gate2_progress (信息性, 不重发哨兵告警). 全部时间戳归一 timezone-aware UTC 再相减 (IC Timezone Hygiene, 杜绝 8h 恒定偏差). 报告 `data_btc/state/daily_precheck/YYYY-MM-DD.md`, 异常才 DingTalk (正常日静默), exit 0=OK/1=Sev1/2=Sev2 (与哨兵一致). schtasks `Future\DailyFlow46Precheck` WEEKLY Mon-Fri 04:03 + Claude cron 04:10 对话呈现. 首日实抓 CRLF 伪差异复发 (live.yaml 治理写入者 CRLF, 待 DQAF 根因修复). Design: docs/runbooks/daily_flow46_precheck_deployment.md. |
| 2026-07-12 | FIX-20260712-003 | **Phase 2 Layer 3: 2 new intent verification cross-checks**. `_check_brain_intent_alignment`: validates `_intent.trading` (shadow_only/probation_limited/full_live) vs governance status + live.yaml enabled. `_check_brain_config_annotation_freshness`: detects stale _note phrases ("not executed", "shadow only") contradicting probation/live status. Both registered in DataHealthService include_extras. Part of 4-layer config defense system Phase 2. |
| 2026-07-12 | FIX-20260712-002 | **Phase 1 Layer 2: 2 new cross-checks + probation coverage**. (1) `_check_brain_config_governance_status_alignment`: config vs governance STATUS_RANK alignment. (2) `_check_live_yaml_enabled_vs_brain_status`: zombie/intent-conflict detection. (3) Existing registry↔governance check extended from live-only to live+probation. Part of 4-layer config defense system. |
| 2026-06-17 | FIX-005 | Initial deployment: audit + tombstone + dedup + normalize |
| 2026-06-17 | — | GAP 4: automated silent monitoring (--quiet --alert) |
| 2026-06-17 | — | GAP 3: feature drift detection (PSI baseline) |

## Known Issues

- Feature store lacks atomic write protection — monitor uses JSONL line-level validation as defense
- BTC snapshots have 30% historical gap (pre-2026-05-31 era)
- Multi-terminal architecture requires per-data-dir MT5 configuration

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| data_infrastructure | EventWriter, ticket_resolver | Journal/ledger access for audit |
| features_service | Feature store records | PSI baseline comparison |
| deployment_config | MT5 terminal paths | Multi-terminal audit routing |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| N/A (scripts layer) | — | Monitoring scripts are leaf nodes — no core modules depend on them |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|-----------|
| `audit_data_integrity.py --data-dir <path> --quiet --alert` | Windows Task Scheduler | Stable |
| `monitor_feature_drift.py --baseline <path> --live <path> --alert` | Windows Task Scheduler | Stable |
| `alert_dispatcher.py` — unified DingTalk push with cooling/aggregation | All monitoring scripts | Stable |

## Verification
```bash
python scripts/audit_data_integrity.py --data-dir data_btc --quiet
python -m pytest tests/ -k "audit or monitor or alert" -q
```
