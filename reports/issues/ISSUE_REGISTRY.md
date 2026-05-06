# Quant OS — Live Trading Issue Registry

**Created:** 2026-05-04T19:30:00+08:00  
**Audit Scope:** Full-system deep audit (12 subsystems, 150+ files)  
**Total Issues Found:** 15 | **Fixed:** 15 | **Remaining:** 0  
**Schema:** ISO/IEC 14764 Problem Resolution Report format  
**Convention:** Issues numbered QO-XXXX (Quant OS ticket), severity per CVSS-inspired scale

---

## Severity Classification

| Level | Code | Definition |
|-------|------|------------|
| CRITICAL | C | System function broken; data loss or silent failure; feedback/evolution loop dead |
| HIGH | H | Significant degradation; wrong data used; feature not working as designed |
| MEDIUM | M | Quality/reliability concern; technical debt with operational impact |
| LOW | L | Cosmetic, future risk, or non-urgent improvement |

## Status Codes

`OPEN` → `ACKNOWLEDGED` → `IN_PROGRESS` → `FIXED` → `VERIFIED` → `CLOSED`
`REJECTED` / `WONTFIX` / `DUPLICATE`

---

## Issue Index

| ID | Severity | System | Title | Status |
|----|----------|--------|-------|--------|
| [QO-0001](#qo-0001) | C | Decision Records | record_shadow_from_proposals() never called in live dispatch path | FIXED |
| [QO-0002](#qo-0002) | C | Decision Records | Decision record path mismatch between producer and consumer | FIXED |
| [QO-0003](#qo-0003) | C | Trade Journal | No position_ticket in any journal entry; P&L computation impossible | FIXED |
| [QO-0004](#qo-0004) | H | Brain Inventory | OU_Params_V6_Sniper artifact file missing; runs as stub | FIXED |
| [QO-0005](#qo-0005) | H | Feature Store | Schema mismatch: XAUUSD (v9_institutional_40/M5) vs XAUUSDc (v9_institutional/M1) | FIXED |
| [QO-0006](#qo-0006) | H | Configuration | Symbol inconsistency: 12+ files use XAUUSD while live system uses XAUUSDc | FIXED |
| [QO-0007](#qo-0007) | H | Risk Control | RiskEvaluationService never instantiated in live intent loop | FIXED |
| [QO-0008](#qo-0008) | H | Training Pipeline | Zero training datasets built; label_builder cannot match open/close pairs | FIXED |
| [QO-0009](#qo-0009) | M | Feature Store | Feature warmer replicates M5 data across all timeframes (fake M15/M30/H1) | FIXED |
| [QO-0010](#qo-0010) | M | Error Handling | 6+ bare `except: pass` blocks silently swallow failures | FIXED |
| [QO-0011](#qo-0011) | M | Alpha Registry | Corrupted entry: MT5 path parsed as alpha_id "5\\terminal64.exe" | FIXED |
| [QO-0012](#qo-0012) | M | Replay Baseline | 2 of 4 baseline subdirectories empty; incomplete coverage | FIXED |
| [QO-0013](#qo-0013) | L | Daily Recap | Recap runs before trading activity; misses actual trades | FIXED |
| [QO-0014](#qo-0014) | L | Code Quality | Hardcoded Path.cwd() references; depends on launch directory | FIXED |
| [QO-0015](#qo-0015) | L | Localization | Garbled Chinese characters in daily recap run_state field | FIXED |

---

## Detailed Issue Descriptions

### QO-0001

| Field | Value |
|-------|-------|
| **Title** | `record_shadow_from_proposals()` never called in live dispatch path |
| **Severity** | CRITICAL |
| **File** | `scripts/live_intent_loop.py:807-881` |
| **Root Cause** | The `if args.no_mt5:` branch (lines 769-795) calls `record_shadow_from_proposals()` for verification-only mode. The `else:` branch (lines 807-881) that performs actual live trading dispatches orders but never persists shadow decisions. |
| **Impact** | Zero decision records generated during live multi-brain trading. Feedback loop, brain leaderboard, and retraining trigger all operate on empty data. The entire self-evolving loop's data source is severed. |
| **Reproduction** | `python main.py live` → check `data/decisions/2026-05-04/` → no files created |
| **Fix Approach** | Add `_record_shadow_decisions(proposals, ...)` call after signal generation (before dispatch) in the live else-branch |

### QO-0002

| Field | Value |
|-------|-------|
| **Title** | Decision record path mismatch between producer and consumer |
| **Severity** | CRITICAL |
| **File** | `core/ledger/storage/jsonl_ledger_store.py:14` vs `scripts/feedback_loop.py:119` |
| **Root Cause** | `JsonlLedgerStore` writes to `data/{date_key}/{symbol}.decisions.jsonl` but `feedback_loop.py` reads from `data/decisions/{date_key}/{symbol}.decisions.jsonl`. The `decisions/` subdirectory is only present in the consumer path. |
| **Impact** | Even when decision records ARE written (e.g., from daily_ops shadow ensemble), the feedback loop cannot find them. The gap makes QO-0001's impact doubly severe — both production and consumer paths are misaligned. |
| **Reproduction** | `python -c "from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore; s = JsonlLedgerStore('data'); print(s._build_path('2026-05-04', 'XAUUSD'))"` → `data/2026-05-04/XAUUSD.decisions.jsonl` (no `decisions/` prefix) |
| **Fix Approach** | Align both paths to use `data/decisions/{date_key}/{symbol}.decisions.jsonl` |

### QO-0003

| Field | Value |
|-------|-------|
| **Title** | No position_ticket in any journal entry; P&L computation impossible |
| **Severity** | CRITICAL |
| **File** | `scripts/mt5_bridge_worker.py:410-418` |
| **Root Cause** | Journal entry is written (line 418) from pre-execution envelope fields. The `position_ticket` is returned post-execution in `detail.order` (retcode 10009), but is never written back to the journal record. All 10 journal entries show `"position_ticket": null`. |
| **Impact** | `label_builder.py` matches open/close pairs by ticket → all 10 trades are `"unlabeled"` → no P&L can be computed → `dataset_builder.py` has no usable labels → zero training datasets produced. |
| **Reproduction** | `cat data/live_trade_journal.jsonl \| jq '.position_ticket'` → all null |
| **Fix Approach** | After receiving order result (line ~350-360), update the journal entry with `position_ticket = detail.get("order")` before archiving |

### QO-0004

| Field | Value |
|-------|-------|
| **Title** | OU_Params_V6_Sniper artifact file missing; runs as stub |
| **Severity** | HIGH |
| **File** | `configs/brains/ou_params_v6.json:9` |
| **Root Cause** | Config references `D:\\ai\\Meta_ppo_v6\\arb_params.json` which does not exist. The brain adapter falls back to stub backend, outputting up=0.5, down=0.5, confidence=0.5 (neutral) every cycle. |
| **Impact** | Wastes a voting slot in the 3-brain ensemble. The stub's neutral vote drags agreement_score down and provides zero signal. |
| **Reproduction** | `ls D:\\ai\\Meta_ppo_v6\\arb_params.json` → FileNotFoundError |
| **Fix Approach** | Either (a) train and produce `arb_params.json`, or (b) disable the brain in `configs/live.yaml` until artifact is ready. Recommend (b) for immediate safety, (a) as follow-up. |

### QO-0005

| Field | Value |
|-------|-------|
| **Title** | Feature store schema mismatch between XAUUSD and XAUUSDc |
| **Severity** | HIGH |
| **File** | `scripts/features/feature_store_warmer.py` vs `core/deployment/feature_update_producer.py:44-53` |
| **Root Cause** | feature_store_warmer registers `v9_institutional_40:1.0.0` with symbol=XAUUSD, timeframe=M5. feature_update_producer registers `v9_institutional:1.0` with symbol=XAUUSDc, timeframe=M1. These are two completely different schema namespaces. Shadow ensemble queries for `v9_institutional_40` / XAUUSD / M5 → finds stale April 1-18 data. Live intent loop writes `v9_institutional` / XAUUSDc / M1 → never queried by ensemble. |
| **Impact** | Shadow ensemble runs on stale April data instead of live May 4 features. Multi-brain consensus is computed from outdated market conditions. |
| **Reproduction** | Check schemas.json → two entries with different names, symbols, timeframes, and versions |
| **Fix Approach** | Unify to one schema (`v9_institutional_40` / `1.0.0`). Update feature_update_producer to use symbol=XAUUSDc, timeframe=M5, schema_name=v9_institutional_40. Update shadow ensemble query to use symbol=XAUUSDc. |

### QO-0006

| Field | Value |
|-------|-------|
| **Title** | Symbol inconsistency across 12+ configuration and script files |
| **Severity** | HIGH |
| **File** | Multiple (see below) |
| **Root Cause** | Live trading uses XAUUSDc (MT5 broker convention). Shadow ensemble, healthchecks, brain configs, and daily_ops use XAUUSD. These symbols are semantically the same instrument but stored under different keys. |
| **Affected Files** | `configs/live.yaml` (mt5.default_symbol, healthcheck), `configs/brains/*.json` (3 files), `configs/brains/brain_entries.json`, `scripts/daily_ops.py:100,215`, `scripts/live_shadow_ensemble.py:168`, `scripts/training/dataset_builder.py:49,74` |
| **Impact** | Feature queries return empty/stale data. Shadow reports show wrong symbol. Healthcheck probes wrong symbol. |
| **Fix Approach** | Centralize symbol resolution: all internal subsystems use XAUUSDc. Add symbol normalization in feature store query layer. Update configs and scripts to reference XAUUSDc consistently. |

### QO-0007

| Field | Value |
|-------|-------|
| **Title** | RiskEvaluationService never instantiated in live intent loop |
| **Severity** | HIGH |
| **File** | `scripts/live_intent_loop.py:699-715` |
| **Root Cause** | `parliament.build_candidate()` is called but no risk context (drawdown_pct, exposure, concentration, mode) is supplied. `RiskEvaluationService` exists as a class (`core/risk/risk_evaluation_service.py`) but is never imported or called in the live trading path. |
| **Impact** | Risk policies (max drawdown, max exposure, concentration limits, circuit breaker) are defined but never enforced during live trading. Only cooldown (interval-based) and max_positions provide any protection. |
| **Reproduction** | `grep -r "RiskEvaluationService" scripts/live_intent_loop.py` → no matches |
| **Fix Approach** | Import RiskEvaluationService, build risk context from MT5 account state, evaluate before dispatch. Log RiskVerdict warnings. |

### QO-0008

| Field | Value |
|-------|-------|
| **Title** | Zero training datasets built; label_builder cannot match open/close pairs |
| **Severity** | HIGH |
| **File** | `scripts/training/label_builder.py:119-127`, `scripts/training/dataset_builder.py:74` |
| **Root Cause** | label_builder relies on position_ticket to match open/close pairs for P&L computation. Since QO-0003 ensures all tickets are null, all trades are `"unlabeled"`. dataset_builder queries features for XAUUSD (not XAUUSDc), finding either stale data or nothing. |
| **Impact** | `data/training/` contains zero Parquet/NPZ files. Self-evolving loop's "data → train" step produces no output. The entire Phase B/C evolution pipeline is starved of input data. |
| **Reproduction** | `ls data/training/*.parquet data/training/*.npz` → no matches |
| **Fix Approach** | Depends on QO-0003 fix. Once position_tickets are captured: (a) fix label_builder ticket matching, (b) fix dataset_builder symbol → XAUUSDc, (c) generate first training dataset |

### QO-0009

| Field | Value |
|-------|-------|
| **Title** | Feature warmer replicates M5 data across all timeframes |
| **Severity** | MEDIUM |
| **File** | `scripts/features/feature_store_warmer.py:258-264` |
| **Root Cause** | The warmer computes features at M5 granularity, then copies the same values into M15, M30, H1 slots with a comment: "Use same features for all timeframes (M5 granularity, approximation)". Real multi-timeframe features require computing each timeframe independently. |
| **Impact** | M15/M30/H1 features are statistically identical to M5 — they provide no additional information. Multi-timeframe models (like V9) are trained on features that don't represent actual multi-timeframe dynamics. |
| **Reproduction** | Compare M5_Ret_1 and M15_Ret_1 values in `features.jsonl` → identical across all records |
| **Fix Approach** | Compute each timeframe independently from the corresponding OHLC bars. Requires MT5 bar data at M5, M15, M30, H1 resolutions. |

### QO-0010

| Field | Value |
|-------|-------|
| **Title** | 6+ bare `except: pass` blocks silently swallow failures |
| **Severity** | MEDIUM |
| **File** | `scripts/live_intent_loop.py:679,693,794,901`; `scripts/daily_ops.py:303` |
| **Root Cause** | Multiple critical operations (feature store write, brain inference, decision recording, tracker save) are wrapped in `try: ... except: pass` with zero logging. When these fail, there is no diagnostic signal. |
| **Impact** | Feature persistence failures, brain inference failures, and decision recording failures are invisible. Operators cannot distinguish "no data to record" from "recording crashed". |
| **Reproduction** | `grep -n "except:" scripts/live_intent_loop.py` → lines 679, 693, 794, 901 |
| **Fix Approach** | Replace each `except: pass` with `except Exception as exc: logger.warning(f"...: {exc}")` using a module-level logger. |

### QO-0011

| Field | Value |
|-------|-------|
| **Title** | Corrupted alpha registry entry from MT5 path parsing |
| **Severity** | MEDIUM |
| **File** | `data/alpha_registry.json:5` |
| **Root Cause** | `alpha_id` = `"5\\terminal64.exe"` appears to be a fragment of `D:\MetaTrader 5\terminal64.exe` after backslash splitting during path parsing. The alpha registration code likely split the path on `\` and took a fragment. |
| **Impact** | Corrupted entry in registry. If governance iterates over alphas, this entry would cause errors. |
| **Reproduction** | `jq '.records[0].alpha_id' data/alpha_registry.json` → "5\\terminal64.exe" |
| **Fix Approach** | Remove corrupted entry. Fix path-to-alpha_id conversion to use basename without extension, not path splitting. |

### QO-0012

| Field | Value |
|-------|-------|
| **Title** | Replay baseline: 2 of 4 subdirectories empty |
| **Severity** | MEDIUM |
| **File** | `data/replays/v9_shadow_baselines/manifest.json` |
| **Root Cause** | `neutral_stability/` and `risk_boundary/` baseline categories have zero files. Only `actionable_decisions/` and `edge/` have 2 files each (4 total). |
| **Impact** | Shadow-live comparison is incomplete. Neutral stability and risk boundary scenarios are not baselined, so divergence in these categories cannot be detected. |
| **Reproduction** | `find data/replays/v9_shadow_baselines -name "*.json" | wc -l` → 4 |
| **Fix Approach** | Run shadow ensemble across full scenario spectrum and commit baselines for all 4 categories. |

### QO-0013

| Field | Value |
|-------|-------|
| **Title** | Daily recap runs before trading activity; misses actual trades |
| **Severity** | LOW |
| **File** | `scripts/daily_ops.py` → scheduled via cron/daily trigger |
| **Root Cause** | Today's recap ran at 06:13 UTC (14:13 local), but the first and only trade occurred at 11:19 UTC (19:19 local). The recap reports `total: 0` for the day that actually had trading activity. |
| **Impact** | Daily recaps show zero activity on days when trading occurred. Misleading for operators reviewing historical recaps. |
| **Reproduction** | Compare recap timestamp (06:13Z) vs journal entry timestamp (11:19Z) for 2026-05-04 |
| **Fix Approach** | Schedule recap after market close (e.g., 22:00 UTC) or make it query the full 24h window regardless of run time. |

### QO-0014

| Field | Value |
|-------|-------|
| **Title** | Hardcoded Path.cwd() references; behavior depends on launch directory |
| **Severity** | LOW |
| **File** | `scripts/live_intent_loop.py:96,107,153,525`; `scripts/send_live_order.py:39,43` |
| **Root Cause** | Multiple `Path.cwd()`, `os.getcwd()`, and `__file__`-relative path resolutions assume the process is launched from the project root. |
| **Impact** | If launched from a different directory (e.g., via systemd, task scheduler, or a different shell), path resolutions break. |
| **Reproduction** | `cd /tmp && python /path/to/live_intent_loop.py ...` → file not found errors |
| **Fix Approach** | Replace `Path.cwd()` with `Path(__file__).resolve().parent.parent` to derive PROJECT_ROOT from the script's own location. |

### QO-0015

| Field | Value |
|-------|-------|
| **Title** | Garbled Chinese characters in daily recap run_state field |
| **Severity** | LOW |
| **File** | `scripts/live_daily_recap.py` — run_state string |
| **Root Cause** | Chinese text encoded in a non-UTF-8 codec (likely GBK) and written to a UTF-8 JSON log. Python's default encoding on Windows may be cp936. |
| **Impact** | run_state field is unreadable in reports. |
| **Reproduction** | `cat data/logs/daily_recap_2026-05-04.log | grep run_state` |
| **Fix Approach** | Add `encoding="utf-8"` to all file open() calls. Use `PYTHONUTF8=1` env var. |

---

## Fix Dependency Graph

```
QO-0003 (ticket capture) ✓
  └── QO-0008 (training data) ✓
        └── Phase C self-evolving loop ✅

QO-0001 (decision persistence) ✓
  └── QO-0002 (path alignment) ✓
        └── QO-0008 (feedback data) ✓

QO-0005 (schema mismatch) ✓
  └── QO-0006 (symbol consistency) ✓
        └── Shadow ensemble accuracy ✓

QO-0004 (OU_Params artifact) ✓ → Ensemble voting quality
QO-0007 (risk service) ✓ → Live trading safety
QO-0009 (fake timeframes) ✓ → Model training quality
QO-0010 (silent failures) ✓ → Debug-ability
QO-0011 (corrupted alpha) ✓ → Registry integrity
QO-0012 (baseline gaps) ✓ → Comparison coverage
QO-0013 (recap timing) ✓ → Monitoring accuracy
QO-0014 (cwd dependency) ✓ → Portability
QO-0015 (encoding) ✓ → Report readability
```

**All 15 issues resolved. Phase A audit complete.**

---

## Fix Record Template

Each fix will append an entry to FIX_LOG.md with:

```markdown
### Fix QO-XXXX — YYYY-MM-DDThh:mm:ssZ

**Engineer:** cursor-agent
**Files Changed:**
- `path/to/file.py:line` — description of change

**Change Summary:**
Brief description of what was changed and why.

**Verification:**
- [ ] Unit test passes
- [ ] Integration test passes
- [ ] Manual smoke test

**Before/After:**
```diff
- old code
+ new code
```
```
