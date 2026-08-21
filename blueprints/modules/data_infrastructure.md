# data_infrastructure — Append-Only Event Stream

## Purpose
Immutable event stream infrastructure (FIX-20260611-021). Replaces mutable JSON read-modify-write with append-only event sourcing.

## Key Files
| File | Role |
|------|------|
| `core/data/__init__.py` | Package init |
| `core/data/event_writer.py` | Single-process, thread-safe, append-only EventWriter |
| `core/data/ticket_resolver.py` | **DQAF-20260623-070**: Unified position ticket ID resolution (SSOT extraction path) |
| `core/data/projections.py` | Pure-function projection engine (event stream → governance state) |
| `core/data/wap.py` | **Write-Audit-Publish (WAP)**: atomic staging to production with rollback (FIX-20260611-022) |
| `core/data/write_ahead_log.py` | Hash-chained WAL — append-only, fsync'd, HMAC-signed with checkpoint + rotation (UGR v3.1) |
| `scripts/migration/migrate_to_event_stream.py` | One-shot brain_pnl_ledger → ledger_events.jsonl migration |

## Data Flow
```
Live Events (trade, close, modify, daily_ops)
    ↓
EventWriter.append(event) — thread-safe, append-only, FileLock'd
    ↓
ledger_events.jsonl / live_trade_journal.jsonl (immutable SSOT)
    ↓
projections.rebuild() — pure-function replay from event stream
    ↓
Governance state / PnL state / Alpha state (ephemeral .json views)
```
WAL path: `WriteAheadLog.append() → fsync → hash-chain → rotate → checkpoint`
WAP path: `write_staging → validate → publish (atomic rename) or rollback`

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| contracts_resilience | WriteAheadLog, WALConfig | WAL integrity layer |
| runtime_live | EventWriter | Dual-write hook during live cycle |
| deployment_lifecycle | projections | State rebuild on restart |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| daily_ops | projections, EventWriter | Daily state rebuild + event emission |
| feedback_pnl | EventWriter | PnL event recording |
| execution_orders | EventWriter | Trade/close event recording |
| monitor_dashboard | ticket_resolver | Unified ticket extraction for audit |

## Known Issues
- WAP store `write_staging` uses tempfile + os.replace() — atomic on POSIX, best-effort on NTFS.
- `projections.rebuild()` replays full event stream on every call; checkpoint-based incremental replay is PLANNED.
- `migrate_to_event_stream.py` is one-shot; no incremental migration path for partial event streams.

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|-----------|
| `EventWriter.append(event: BaseEvent) → None` — thread-safe, FileLock'd | live_cycle, daily_ops, feedback_pnl | Stable |
| `projections.rebuild(data_dir: str) → dict` — pure-function replay | daily_ops, deployment_lifecycle | Stable |
| `resolve_ticket(entry: dict) → str` — unified ticket extraction | label_builder, daily_ops, audit | Stable |
| `WriteAheadLog.append(entry: bytes) → int` — hash-chained, fsync'd | contracts_resilience | Stable |

## Verification
```bash
python -m pytest tests/ -k "event_writer or ticket_resolver or projections or wal or wap" -q
```

## Fix History

| FIX-20260821-001 | 2026-08-21 | cursor-agent | — | **DCI 审计 7 处硬编码停滞阈值收敛 (DQAF-20260820-005, TECH_DEBT-011 清偿)**: `scripts/audit_data_chain_integrity.py` 全部 staleness 站点改经 `_is_stale` → `staleness_anchor` 单点调用 (S1 feature 12h per-symbol / S1 bar_sync 12h / S2 regime 12h / S4 ledger 24h / S4 gm 6h / S5 state 24h / S6 precheck 30h). market_type 派生: `data_btc→crypto_24_7`, 其余→forex_24_5; feature 站点 per-symbol (XAU→forex_24_5). S3 dormant/baseline 24h 机制**保留** — naive 锚定会反转休眠逻辑 → 假阳性. 实证 (Iron Law #11): `data --now Sat` grade 🟢92 stale_faults=[] (原周末 -5 假阳性消除); `data_btc` grade 84 仅 S3 零回归. 回归锁 `tests/runtime/test_dci_calendar.py` (周末冻结不再误报 / 周一重开仍抓 / BTC 不放松). | L3 — 硬编码年龄阈值非日历感知 |
| FIX-20260807-005 | 2026-08-07 | cursor-agent | 51cf6985 | 全数据链审计工具 4 盲区修正 (DQAF-20260807-004 坐实): (1) 孤儿/close-without-open 改用 resolve_identity 不可变身份回链 — BTC 208→117, 剔除 netting 换票假阳性; (2) auto_orphan_rejected 合成收尾识别为设计 (cleanup_orphan_opens 拒绝单清理, linked=245/unlinked=0), baseline scoped-out; (3) position_snapshots 停滞在零开单休眠防御态标记 baseline (journal 无近期 open/close=设计); (4) 影子风暴拒单 (XAUUSD缺c 281) 识别为生成器信号 baseline, 真实拒单 10 保留 fresh. 基线重锁: BTC 85 / XAU 92. | type-confusion |
| FIX-20260807-004 | 2026-08-07 | cursor-agent | d9c6725d | **全数据链完整性审计 (Phase 0, DQAF-20260807-004 立项)**. `scripts/audit_data_chain_integrity.py` — 6 段 (入口/决策/派发/记账SSOT/投影/对账) Data Chain Integrity Index, 只读幂等 `--data-dir`, baseline 回归比对. 基线: BTC 83🟡 (S3/S4: 208 close-without-open SEV1, ghost vol 10) | XAU 89🟡 (S3: auto_orphan_rejected 245 活动信号, dispatch rejected 291). 新增基础设施 FIX, 蓝图登记不改行为. | L3 — 全数据链无统一完整性观测基线 |
| FIX-20260628-165 | 2026-06-28 | cursor-agent | — | **P2: XAU Journal orphan backfill 完成 (L1)**. backfill_journal_orphans.py --cutoff 2026-06-16: 6 XAU recent orphans (2026-06-10~15, ack=accepted, open_message_id=N/A) → 6 synthetic opens inserted (_source=orphan_backfill, ack=synthetic). Coverage: 95.1%→95.6% (1,353/1,416). Remaining 63 orphans all BTCUSDc rejected — correct exclusion per IC Mandate. Data-only operation, no code changes. ReB: `JOURNAL_ORPHAN_BACKFILL_EXTENDED_CUTOFF`. | L1 — 6 XAU closes written without matching opens during pre-JournalGate gap (2026-06-10~15) |
| FIX-20260708-001 | 2026-07-08 | cursor-agent | — | **resolve_identity() — canonical immutable-identity join authority** (DQAF-20260708-001 L3). New sibling to resolve(): prefers immutable position_identifier (stable across MT5 partial-close/netting re-ticketing), degrades to the mutable ticket for legacy records. Single authority for open<->close pairing / orphan detection / gate admission / training-label pairing; broker-facing sites keep resolve(). | RC-02 — type-confusion (mutable ticket used as stable identity) |
| FIX-20260621-042 | 2026-06-21 | cursor-agent | — | **DQAF-042 机构级实盘数据方向普查 — P0 毒丸阻断 + IMMUTABLE_LEDGER_AND_EPHEMERAL_PROJECTION**: 4路并行审计代理+70 commits逐条对账+12蓝图交叉引用→10项发现对账确认8项坐实。根因: 系统陷入"人工修复→自动化覆写"死循环。修复: (1) live_journal_metrics.py补全字段, (2) BrainLeaderboard.rank()入参Schema强校验+DataIntegrityError毒丸, (3) daily_ops.py毒丸阻断, (4) DataIntegrityError Fail-Closed异常, (5) .gitignore明确IMMUTABLE_LEDGER_AND_EPHEMERAL_PROJECTION原则。 | RC-07 + RC-03 + RC-06 |
| FIX-20260624-089 | 2026-06-24 | cursor-agent | — | **UGR-B02: WAL rotation + atomic checkpoint + HMAC signature**. rotate() + verify_integrity_from_checkpoint() + load_checkpoint(). | RC-12 |
| FIX-20260623-073 | 2026-06-23 | cursor-agent | — | **DQAF-073: Unified Ticket Resolution**. Created `core/data/ticket_resolver.py` — single canonical path for position ticket extraction. Migrated label_builder, daily_ops, and audit_data_exhaustive to use `resolve_ticket()`. Resolves schema drift: 83 files previously used 4+ different extraction patterns for the same business concept. P0: 3 key consumers migrated. P1: touch-migration. P2: full sweep. | L3 — schema drift (4 extraction patterns for same concept) |
| FIX-20260615-006 | 2026-06-15 | cursor-agent | — | **XAU/BTC L3 交叉感染: get_event_writer(base_dir) 移除默认值** — 现在调用方必须显式提供 base_dir。 | L3 — base_dir="data" 默认值 |
| FIX-20260613-067 | 2026-06-13 | cursor-agent | c992678 | FileLock Atomic Exclusive Create: replaced os.replace() (always overwrites) with os.O_CREAT|O_EXCL for true cross-process mutual exclusion. Added same-instance re-acquire guard. 23/23 tests pass. | contract-violation |
| FIX-20260611-021 | 2026-06-11 | cursor-agent | 49610cd | Bug fixes: UUID ordering (line-based checkpoint) + checkpoint key mismatch (_ensure_brain_state). Both found by Hypothesis PBT. | contract-violation |
| FIX-20260611-021 | 2026-06-11 | cursor-agent | — | **Event Sourcing Foundation**: Append-only event stream architecture. Contracts (Pydantic extra=forbid), Unified Writer (threading.Lock), Migration (source=migration tag), Projection Engine (checkpoint + incremental replay), Dual-Write Hook (opt-in EventWriter in BrainPnLStore). | RC-06 |
| FIX-20260607-145 | 2026-06-07 | cursor-agent | — | **Journal compaction: atomic prune of old rejected entries (>30d)**: `compact_journal()` in `journal_cleanup.py` with `os.replace()` atomic swap + FileLock. | RC-11 |
| FIX-20260612-014 | 2026-06-12 | cursor-agent | — | **Temp-file + atomic swap for repair_journal**: replaced write_text() overwrite with temp-file + os.replace() pattern (same as compact_journal). Lock acquired BEFORE re-reading — eliminates stale-snapshot window permanently. Consolidates duplicate removal. Closes Deferred Architecture Fix #1. | RC-03 |
| FIX-20260617-101 | 2026-06-17 | cursor-agent | — | Institutional Data Integrity Framework: Three-Layer Defense against silent data loss. DLR-001: 34 BTC opens lost. L1 Pydantic write-boundary validator, L2 DataHealthService + daemon guard, L3 cross-symbol consistency audit. 9 files, 0 hot path changes. | RC-06, RC-09 |
| FIX-20260620-022 | 2026-06-20 | cursor-agent | — | **Manual order contamination cleanup**: Removed 6 vol=0.2 manual order entries from BTC journal (tickets 3946120976/3946156487/3946120704). Cleared 500 backtest-contaminated entries from brain_pnl_ledger.json (5 brains). Reset 3 BTC brain governance performance_metrics to zero (pnl_r went from -$10,265 to $0). Strategy PnL corrected from -$280.52 to +$59.06. All data files backed up as .bak_20260620_manual_fix. | RC-06 (data-contamination — backtest PnL injected into live governance) |
