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

| Date | FIX ID | Description |
|------|--------|-------------|
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
