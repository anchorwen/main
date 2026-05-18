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
|--------|------|--------|--------|---------|------------|
| FIX-20260517-023 | 2026-05-17 | cursor-agent | — | Dashboard redesign: Chinese localization, layout restructure (5-row→4-row+tab), brain detail endpoint with sparkline, P0 fix shadow/live same file | missing-feature |
| — | 2026-05-17 | cursor-agent | — | P0: Fixed `_serve_api_decisions()` — shadow and live previously both read `XAUUSDc.decisions.jsonl`. Live now reads `live_trade_journal.jsonl`. | config-drift |

## Verification
```bash
python apps/monitor/live_trading_dashboard.py --port 8080 &
curl -s http://127.0.0.1:8080/api/brain/OU_Params_V6_Sniper | python -m json.tool | head -20
curl -s http://127.0.0.1:8080/ | grep "量化交易"
kill %1
```
