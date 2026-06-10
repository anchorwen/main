"""DataHealthService — unified data health monitoring for live trading.

Iron Law for Monitoring #1 (Stability & Isolation):
  - All file I/O wrapped in fail_open_guard — a check failure must NEVER
    propagate to the trading main loop.
  - LIGHT mode targets <50ms latency (CRITICAL-tier only, tail reads, no
    directory scans, no full-file parsing).

Iron Law for Monitoring #2 (Recoverability):
  - State persisted via atomic write (tmp + os.replace).
  - Corrupt state file → silent return to fresh_health_state().

Iron Law for Monitoring #3 (Decoupling):
  - DataHealthService produces HealthReport ONLY.  Zero alert_hub calls,
    zero notify_trade calls, zero side effects beyond file reads + state writes.

Iron Law for Monitoring #4 (Iterability):
  - All checks registered via @health_check decorator.  Engine dispatch
    iterates the registry filtered by tier — no if/elif chains.
"""

from __future__ import annotations

import glob
import json
import os
import time
from datetime import UTC, datetime
from typing import Any

from core.observability.data_health_schema import (
    DEFAULT_THRESHOLDS,
    CrossCheckResult,
    HealthReport,
    OrphanFinding,
    SourceCheckResult,
    SourceStatus,
    Tier,
    build_alert_context,
    fresh_health_state,
    get_checks,
    health_check,
)

# ── Helpers ────────────────────────────────────────────────────────────────


def _utc_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def _age_minutes(iso_ts: str | None) -> float:
    """Compute age in minutes from an ISO timestamp string."""
    if not iso_ts:
        return -1.0
    try:
        s = str(iso_ts)[:19]
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return (datetime.now(UTC) - dt).total_seconds() / 60.0
    except (ValueError, TypeError, OSError):
        return -1.0


def _safe_json_load(path: str) -> dict[str, Any] | None:
    """Load a JSON file; return None on any failure (Iron Law #1)."""
    try:
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 — Iron Law #1: never crash on bad data
        return None


def _safe_jsonl_count(path: str) -> int | None:
    """Count lines in a JSONL file; return None on failure."""
    try:
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return sum(1 for _ in f)
    except Exception:  # noqa: BLE001
        return None


def _safe_jsonl_last(path: str, tail_bytes: int = 8192) -> dict[str, Any] | None:
    """Read the last line of a JSONL file using tail-read; return None on failure.

    FIX-20260610-007: Optimized for large files (XAU features.jsonl is 69MB).
    Instead of reading the entire file, seeks to the last tail_bytes and
    extracts the last valid JSON line.  Falls back to full read if the file
    is smaller than tail_bytes.
    """
    try:
        if not os.path.exists(path):
            return None
        fsize = os.path.getsize(path)
        if fsize == 0:
            return None
        with open(path, encoding="utf-8") as f:
            if fsize <= tail_bytes:
                # Small file — read entirely for simplicity
                last = None
                for line in f:
                    line = line.strip()
                    if line:
                        last = line
                return json.loads(last) if last else None
            # Large file — tail-read: seek to last tail_bytes
            f.seek(max(0, fsize - tail_bytes))
            chunk = f.read(tail_bytes)
            # Find the last complete JSON line in the chunk
            lines = chunk.strip().split("\n")
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue  # partial first line of chunk, try previous
            # Fallback: if tail chunk only has partial lines, read more
            f.seek(max(0, fsize - tail_bytes * 2))
            chunk = f.read(tail_bytes * 2)
            lines = chunk.strip().split("\n")
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
            return None
    except Exception:  # noqa: BLE001
        return None


def _safe_jsonl_tail_stats(path: str, max_scan: int = 500) -> dict[str, Any]:
    """Scan the last N lines of a JSONL for basic stats.

    Returns dict with: line_count, pnl_null_count, pnl_null_rate,
    close_count, open_count, retry_count, label_distribution.
    Returns empty dict on failure (Iron Law #1).
    """
    try:
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()

        total = len(lines)
        tail = lines[-max_scan:] if total > max_scan else lines

        pnl_null = 0
        close_count = 0
        open_count = 0
        retry_count = 0
        labels: dict[str, int] = {}

        for line in tail:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            action = rec.get("action", "")
            if action == "close":
                close_count += 1
                if rec.get("pnl") is None:
                    pnl_null += 1
                label = rec.get("label", "unknown")
                labels[label] = labels.get(label, 0) + 1
                ack = rec.get("ack_status", "")
                if ack == "rejected":
                    retry_count += 1
            elif action == "open":
                open_count += 1

        pnl_null_rate = pnl_null / close_count if close_count > 0 else 0.0

        return {
            "total_lines": total,
            "tail_scanned": len(tail),
            "close_count_tail": close_count,
            "open_count_tail": open_count,
            "pnl_null_count": pnl_null,
            "pnl_null_rate": round(pnl_null_rate, 4),
            "retry_count": retry_count,
            "label_distribution": labels,
        }
    except Exception:  # noqa: BLE001
        return {}


# ── DataHealthService ──────────────────────────────────────────────────────


class DataHealthService:
    """Unified data health monitor — two invocation modes.

    LIGHT mode (per-cycle, <50ms): CRITICAL-tier checks only, tail reads, no
      directory scans.  Safe to call synchronously from the main trading loop.

    FULL mode (daily_ops, ~1-5s): all tiers + cross-source validation + orphan
      detection.  Designed for once-daily execution.
    """

    def __init__(
        self,
        base_dir: str,
        symbol: str,
        *,
        thresholds: dict[str, float | int] | None = None,
        mode: str = "full",
        position_manager: Any = None,  # FIX-20260611-002: for position_limit check
    ):
        self._base_dir = base_dir
        self._symbol = symbol
        self._thresholds = dict(DEFAULT_THRESHOLDS)
        if thresholds:
            self._thresholds.update(thresholds)
        self._mode = mode
        self._start_time = time.perf_counter()
        self._position_manager = position_manager
        # ── FIX-20260611-002: behavioral compliance incremental scanner ──
        self._cached_behavioral_metrics: Any = None
        self._position_exceeded_streak: int = 0

    # ── Threshold helpers ──────────────────────────────────────────────

    def _t(self, key: str) -> float:
        return float(self._thresholds.get(key, 0))

    # ── Top-level dispatch ─────────────────────────────────────────────

    def run_full(self) -> HealthReport:
        """Run all registered checks (CRITICAL + HIGH + MEDIUM) + cross-source
        validation + orphan detection."""
        return self._run(tiers=(Tier.CRITICAL, Tier.HIGH, Tier.MEDIUM), include_extras=True)

    def run_lightweight(self) -> HealthReport:
        """Run CRITICAL-tier checks only.  Target <50ms latency."""
        return self._run(tiers=(Tier.CRITICAL,), include_extras=False)

    def _run(self, tiers: tuple[Tier, ...], include_extras: bool) -> HealthReport:
        report = HealthReport(
            schema_version="data_health_report.v1",
            generated_at=_utc_iso(),
            base_dir=self._base_dir,
            symbol=self._symbol,
        )

        sources: list[SourceCheckResult] = []
        for meta in get_checks():
            if meta.tier not in tiers:
                continue
            try:
                result = meta.func(self)
            except Exception as exc:  # noqa: BLE001 — Iron Law #1
                result = SourceCheckResult(
                    source=meta.source,
                    tier=meta.tier,
                    status=SourceStatus.FAIL,
                    primary_code=f"{meta.source.upper()}_CHECK_CRASHED",
                    message=f"Check raised {type(exc).__name__}: {exc}",
                    checked_at=_utc_iso(),
                )
            if result.metrics:
                pass  # already set
            sources.append(result)

        report.sources = sources

        # Cross-source validation (FULL only)
        if include_extras:
            report.cross_checks = [
                self._check_journal_vs_pnl_ledger(),
                self._check_open_vs_close_convergence(),
            ]
            report.orphans = self._detect_orphan_subsystems()

        # Aggregate
        fail_count = sum(
            1 for s in sources if s.status in (SourceStatus.FAIL, SourceStatus.MISSING)
        )
        warn_count = sum(1 for s in sources if s.status == SourceStatus.WARN)
        report.primary_codes = [
            s.primary_code for s in sources if s.primary_code and s.status != SourceStatus.PASS
        ]
        if fail_count > 0:
            report.alert_level = "CRITICAL"
        elif warn_count > 2:
            report.alert_level = "WARNING"
        else:
            report.alert_level = "OK"
        report.aggregated = {
            "total_sources": len(sources),
            "pass_count": sum(1 for s in sources if s.status == SourceStatus.PASS),
            "warn_count": warn_count,
            "fail_count": fail_count,
            "missing_count": sum(1 for s in sources if s.status == SourceStatus.MISSING),
            "skipped_count": sum(1 for s in sources if s.status == SourceStatus.SKIPPED),
        }
        report.elapsed_ms = round((time.perf_counter() - self._start_time) * 1000, 2)

        return report

    # ══════════════════════════════════════════════════════════════════════
    # CRITICAL-TIER CHECKS
    # ══════════════════════════════════════════════════════════════════════

    @health_check(
        tier=Tier.CRITICAL,
        source="trade_journal",
        description="PnL null rate, entry completeness, retry rate",
    )
    def check_trade_journal(self) -> SourceCheckResult:
        """Check live_trade_journal.jsonl for PnL completeness and entry quality."""
        jl_path = os.path.join(self._base_dir, "live_trade_journal.jsonl")
        stats = _safe_jsonl_tail_stats(jl_path, max_scan=500)

        if not stats:
            return SourceCheckResult(
                source="trade_journal",
                tier=Tier.CRITICAL,
                status=SourceStatus.MISSING,
                primary_code="JOURNAL_FILE_MISSING",
                message="live_trade_journal.jsonl not found or unreadable",
                checked_at=_utc_iso(),
            )

        pnl_null_rate = stats.get("pnl_null_rate", 0.0)
        close_count = stats.get("close_count_tail", 0)
        retry_count = stats.get("retry_count", 0)
        labels = stats.get("label_distribution", {})

        # P1: PnL null rate
        if pnl_null_rate > self._t("journal_pnl_null_max_pct"):
            status = SourceStatus.FAIL
            code = "JOURNAL_PNL_NULL_RATE_HIGH"
        elif pnl_null_rate > self._t("journal_pnl_null_max_pct") * 0.5:
            status = SourceStatus.WARN
            code = "JOURNAL_PNL_NULL_RATE_ELEVATED"
        else:
            status = SourceStatus.PASS
            code = "JOURNAL_OK"

        message = (
            f"Close entries (tail 500): {close_count}, "
            f"PnL null: {stats.get('pnl_null_count', 0)} ({pnl_null_rate:.1%}), "
            f"Retries: {retry_count}"
        )

        # Annotate if 'trail' label is absent (ReB-20260610-001 blindspot)
        if "trail" not in labels and close_count > 10:
            message += " | WARNING: 'trail' exit label never recorded (TRAIL_TELEMETRY_BLINDSPOT)"

        return SourceCheckResult(
            source="trade_journal",
            tier=Tier.CRITICAL,
            status=status,
            primary_code=code,
            metrics=stats,
            message=message,
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.CRITICAL,
        source="feature_store",
        description="Feature store freshness, zero/nan rate, growth rate",
    )
    def check_feature_store(self) -> SourceCheckResult:
        """Check feature store for staleness and data quality."""
        fs_path = os.path.join(
            self._base_dir,
            "feature_store",
            "records",
            f"symbol={self._symbol}",
            "timeframe=M5",
            "features.jsonl",
        )
        last = _safe_jsonl_last(fs_path)
        if last is None:
            return SourceCheckResult(
                source="feature_store",
                tier=Tier.CRITICAL,
                status=SourceStatus.MISSING,
                primary_code="FEATURE_STORE_MISSING",
                message="features.jsonl not found or unreadable",
                checked_at=_utc_iso(),
            )

        # Freshness
        event_ts = last.get("event_time", "")
        age_min = _age_minutes(event_ts)
        max_age = self._t("feature_store_max_age_minutes")

        if age_min < 0:
            status = SourceStatus.WARN
            code = "FEATURE_STORE_TIMESTAMP_UNREADABLE"
            message = "Cannot parse event_time from last record"
        elif age_min > max_age * 2:
            status = SourceStatus.FAIL
            code = "FEATURE_STORE_STALE"
            message = f"Last feature record age {age_min:.1f} min (threshold: {max_age} min)"
        elif age_min > max_age:
            status = SourceStatus.WARN
            code = "FEATURE_STORE_STALE"
            message = f"Last feature record age {age_min:.1f} min (threshold: {max_age} min)"
        else:
            status = SourceStatus.PASS
            code = "FEATURE_STORE_OK"
            message = f"Last feature record age {age_min:.1f} min"

        # Zero/Nan check on last record features
        metrics: dict[str, Any] = {"age_minutes": round(age_min, 1)}
        feats = last.get("features", last.get("feature_vector", []))
        if isinstance(feats, list) and len(feats) > 0:
            zero_count = sum(1 for v in feats if isinstance(v, int | float) and v == 0)
            # NaN check
            nan_count = sum(
                1
                for v in feats
                if isinstance(v, float) and (v != v)  # NaN check
            )
            metrics["feature_dim"] = len(feats)
            metrics["zero_count"] = zero_count
            metrics["zero_pct"] = round(zero_count / len(feats), 4)
            metrics["nan_count"] = nan_count

            if metrics["zero_pct"] > self._t("feature_store_max_zero_pct"):
                if status == SourceStatus.PASS:
                    status = SourceStatus.WARN
                code = "FEATURE_STORE_ZERO_RATE"
                message += f" | Zero rate {metrics['zero_pct']:.1%} exceeds threshold"
            if nan_count > 0:
                if status == SourceStatus.PASS:
                    status = SourceStatus.WARN
                code = "FEATURE_STORE_NAN_DETECTED"
                message += f" | {nan_count} NaN values in last feature vector"

        return SourceCheckResult(
            source="feature_store",
            tier=Tier.CRITICAL,
            status=status,
            primary_code=code,
            metrics=metrics,
            message=message,
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.CRITICAL,
        source="execution_state",
        description="Execution state schema, staleness, breaker consistency",
    )
    def check_execution_state(self) -> SourceCheckResult:
        """Check state/execution_state.json for integrity and breaker consistency."""
        es_path = os.path.join(self._base_dir, "state", "execution_state.json")
        es = _safe_json_load(es_path)

        if es is None:
            return SourceCheckResult(
                source="execution_state",
                tier=Tier.CRITICAL,
                status=SourceStatus.MISSING,
                primary_code="EXEC_STATE_MISSING",
                message="execution_state.json not found or unreadable",
                checked_at=_utc_iso(),
            )

        # Schema check
        if "schema_version" not in es:
            return SourceCheckResult(
                source="execution_state",
                tier=Tier.CRITICAL,
                status=SourceStatus.FAIL,
                primary_code="EXEC_STATE_SCHEMA_INVALID",
                message="execution_state.json missing schema_version field",
                checked_at=_utc_iso(),
            )

        # Staleness
        saved_at = es.get("saved_at_utc", es.get("updated_at", ""))
        age_min = _age_minutes(saved_at)
        max_age = self._t("execution_state_max_age_minutes")

        if age_min > max_age * 2:
            status = SourceStatus.FAIL
            code = "EXEC_STATE_STALE"
        elif age_min > max_age:
            status = SourceStatus.WARN
            code = "EXEC_STATE_STALE"
        else:
            status = SourceStatus.PASS
            code = "EXEC_STATE_OK"

        message = f"execution_state age {age_min:.1f} min"

        # Breaker consistency check
        cb_tripped = es.get("circuit_breaker_tripped", False)
        cb_tripped_at = es.get("_circuit_breaker_tripped_at", es.get("circuit_breaker_tripped_at"))
        degraded = es.get("_consecutive_degraded_cycles", es.get("consecutive_degraded_cycles", 0))
        stale_cycles = es.get("_consecutive_stale_cycles", es.get("consecutive_stale_cycles", 0))

        metrics: dict[str, Any] = {
            "age_minutes": round(age_min, 1),
            "circuit_breaker_tripped": cb_tripped,
            "consecutive_degraded": degraded,
            "consecutive_stale_cycles": stale_cycles,
        }

        # Pattern: breaker tripped but no trip_reason or timestamp
        if cb_tripped and not cb_tripped_at:
            status = SourceStatus.WARN
            code = "EXEC_STATE_BREAKER_NO_TIMESTAMP"
            message += " | breaker tripped but no timestamp recorded"
        # Pattern: stale counters > 0 but breaker not tripped (inconsistency)
        if not cb_tripped and (degraded > 3 or stale_cycles > 3):
            metrics["inconsistent_counters"] = True
            if status == SourceStatus.PASS:
                status = SourceStatus.WARN
            code = "EXEC_STATE_STALE_COUNTERS_NO_BREAKER"
            message += " | stale/degraded counters non-zero but breaker not tripped"

        return SourceCheckResult(
            source="execution_state",
            tier=Tier.CRITICAL,
            status=status,
            primary_code=code,
            metrics=metrics,
            message=message,
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.CRITICAL,
        source="governance_state",
        description="Governance live brain count, transition log health",
    )
    def check_governance_state(self) -> SourceCheckResult:
        """Check governance_state.json for live brain presence."""
        gv_path = os.path.join(self._base_dir, "governance_state.json")
        gv = _safe_json_load(gv_path)

        if gv is None:
            return SourceCheckResult(
                source="governance_state",
                tier=Tier.CRITICAL,
                status=SourceStatus.MISSING,
                primary_code="GOV_STATE_MISSING",
                message="governance_state.json not found",
                checked_at=_utc_iso(),
            )

        brain_states = gv.get("brain_states", gv.get("brains", {}))
        if isinstance(brain_states, list):
            live_count = sum(
                1 for b in brain_states if isinstance(b, dict) and b.get("status") == "live"
            )
            total_count = len(brain_states)
        elif isinstance(brain_states, dict):
            live_count = sum(
                1
                for v in brain_states.values()
                if isinstance(v, dict) and v.get("status") == "live"
            )
            total_count = len(brain_states)
        else:
            live_count = 0
            total_count = 0

        min_live = int(self._t("governance_min_live_brains"))

        if total_count == 0:
            status = SourceStatus.WARN
            code = "GOV_NO_BRAINS_REGISTERED"
            message = "No brains registered in governance state"
        elif live_count < min_live:
            status = SourceStatus.FAIL
            code = "GOV_NO_LIVE_BRAINS"
            message = f"Zero live brains ({live_count}/{total_count}) — GOVERNANCE_VACUUM"
        else:
            status = SourceStatus.PASS
            code = "GOV_OK"
            message = f"{live_count}/{total_count} brains live"

        return SourceCheckResult(
            source="governance_state",
            tier=Tier.CRITICAL,
            status=status,
            primary_code=code,
            metrics={"live_brain_count": live_count, "total_brain_count": total_count},
            message=message,
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.CRITICAL,
        source="meta_filter_state",
        description="MetaFilter runtime status via event-interception (tail-read intent log)",
    )
    def check_meta_filter_state(self) -> SourceCheckResult:
        """Check MetaFilter health via event-interception, not state file polling.

        FIX-20260610-007: The old check read meta_filter_state.json which uses
        lazy serialization (async, low-frequency writes).  Buffers are often
        empty even when the filter IS running — producing META_FILTER_NEVER_LOADED
        false positives that destroy alert credibility (wolf-crying effect).

        Now uses _safe_jsonl_last() tail-read on the latest intent log to find
        the `meta_pipeline_wired` event, which is written synchronously at
        startup when the filter loads successfully.  This provides millisecond-
        accurate status without adding I/O burden (8KB tail read).
        """
        import glob as _glob

        metrics: dict[str, Any] = {}

        # ── Primary: event-interception via head-read of intent log ──
        # meta_pipeline_wired is written once at boot (start of log).
        # Head-read ~64KB from the first 1-2 intent logs to find it.
        log_dir = os.path.join(self._base_dir, "logs")
        if os.path.isdir(log_dir):
            intent_logs = sorted(_glob.glob(os.path.join(log_dir, "intent_*.log")), reverse=True)
            wired_entry = None
            wired_log = ""
            for log_path in intent_logs[:2]:  # current + previous log
                try:
                    fsize = os.path.getsize(log_path)
                    if fsize == 0:
                        continue
                    with open(log_path, encoding="utf-8") as f:
                        # Read first 64KB to scan for the wired event
                        chunk = f.read(min(fsize, 65536))
                        for line in chunk.strip().split("\n"):
                            line = line.strip()
                            if '"event": "meta_pipeline_wired"' in line:
                                try:
                                    wired_entry = json.loads(line)
                                    wired_log = os.path.basename(log_path)
                                    break
                                except json.JSONDecodeError:
                                    continue
                        if wired_entry:
                            break
                except Exception:  # noqa: BLE001
                    continue

            if wired_entry is not None:
                lgb_loaded = wired_entry.get("lgb_loaded", False)
                calibrator = wired_entry.get("calibrator_loaded", False)
                features = wired_entry.get("features", 0)
                wired_at = wired_entry.get("time", "")
                age_min = _age_minutes(wired_at)
                metrics["lgb_loaded"] = lgb_loaded
                metrics["calibrator_loaded"] = calibrator
                metrics["feature_count"] = features
                metrics["wired_age_minutes"] = round(age_min, 1)
                metrics["log_source"] = wired_log

                if age_min > 360:
                    # Last wired >6h ago — may have restarted without reload
                    status = SourceStatus.WARN
                    code = "META_FILTER_WIRED_STALE"
                    message = f"MetaFilter wired {age_min:.0f}min ago (LGB={lgb_loaded}, cal={calibrator}, dims={features})"
                else:
                    status = SourceStatus.PASS
                    code = "META_FILTER_OK"
                    message = f"MetaFilter active (LGB={lgb_loaded}, cal={calibrator}, dims={features}, wired {age_min:.0f}min ago)"
                return SourceCheckResult(
                    source="meta_filter_state",
                    tier=Tier.CRITICAL,
                    status=status,
                    primary_code=code,
                    metrics=metrics,
                    message=message,
                    checked_at=_utc_iso(),
                )

        # ── Secondary: fall back to state file if no log found ──
        mf_path = os.path.join(self._base_dir, "meta_filter_state.json")
        mf = _safe_json_load(mf_path)
        if mf is None:
            return SourceCheckResult(
                source="meta_filter_state",
                tier=Tier.CRITICAL,
                status=SourceStatus.MISSING,
                primary_code="META_FILTER_STATE_MISSING",
                message="meta_filter_state.json not found and no intent log with meta_pipeline_wired event",
                checked_at=_utc_iso(),
            )

        pred_buffer = mf.get("pred_buffer", mf.get("pred_history", []))
        pred_count = len(pred_buffer) if isinstance(pred_buffer, list) else 0
        atr_buffer = mf.get("atr_buffer", [])
        atr_count = len(atr_buffer) if isinstance(atr_buffer, list) else 0
        atr_frozen = False
        if atr_count >= 5 and isinstance(atr_buffer, list):
            atr_values = [float(v) for v in atr_buffer if v is not None]
            if atr_values and len(set(round(v, 4) for v in atr_values)) == 1:
                atr_frozen = True
        metrics["pred_buffer_count"] = pred_count
        metrics["atr_buffer_count"] = atr_count
        metrics["atr_frozen"] = atr_frozen
        metrics["log_source"] = "state_file_fallback"

        # FIX-20260610-007: Only flag as failure if no log evidence AND state is empty.
        # Empty state buffers with a cold start are normal (lazy serialization).
        # If the state file has data but is stale, that's still useful info.
        if atr_frozen and atr_count >= 90:
            status = SourceStatus.WARN
            code = "META_FILTER_ATR_FROZEN"
            message = f"ATR buffer frozen ({atr_count} identical values) — M5_ATR_14 may be defaulting to 1.0"
        elif pred_count > 0:
            status = SourceStatus.PASS
            code = "META_FILTER_OK"
            message = f"MetaFilter active via state file (pred={pred_count}, atr={atr_count})"
        else:
            status = SourceStatus.WARN
            code = "META_FILTER_STATE_EMPTY"
            message = f"State file empty (pred={pred_count}, atr={atr_count}) — may be cold start or lazy write; check intent log for meta_pipeline_wired"

        return SourceCheckResult(
            source="meta_filter_state",
            tier=Tier.CRITICAL,
            status=status,
            primary_code=code,
            metrics=metrics,
            message=message,
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.CRITICAL,
        source="mt5_bridge_health",
        description="MT5 bridge heartbeat, connection status",
    )
    def check_mt5_bridge_health(self) -> SourceCheckResult:
        """Check MT5 bridge health report for connectivity."""
        bh_path = os.path.join(self._base_dir, "reports", "mt5_bridge_health.json")
        bh = _safe_json_load(bh_path)

        if bh is None:
            return SourceCheckResult(
                source="mt5_bridge_health",
                tier=Tier.CRITICAL,
                status=SourceStatus.MISSING,
                primary_code="BRIDGE_HEALTH_MISSING",
                message="mt5_bridge_health.json not found",
                checked_at=_utc_iso(),
            )

        # Check heartbeat freshness
        last_ack = bh.get("bridge_last_ack_utc", bh.get("last_ack_utc", ""))
        age_sec = (_age_minutes(last_ack) * 60.0) if last_ack else -1
        max_age = self._t("bridge_heartbeat_max_age_seconds")

        pid = bh.get("pid", "?")
        connected = bh.get("connected", bh.get("connection_ok", True))
        pending = bh.get("outbox_pending", bh.get("pending_count", 0))

        if age_sec < 0:
            status = SourceStatus.WARN
            code = "BRIDGE_TIMESTAMP_UNREADABLE"
            message = f"Cannot parse bridge heartbeat timestamp (PID={pid})"
        elif age_sec > max_age * 2:
            status = SourceStatus.FAIL
            code = "BRIDGE_HEARTBEAT_STALE"
            message = f"Bridge heartbeat age {age_sec:.0f}s (PID={pid}) — possible disconnect"
        elif age_sec > max_age:
            status = SourceStatus.WARN
            code = "BRIDGE_HEARTBEAT_STALE"
            message = f"Bridge heartbeat age {age_sec:.0f}s (PID={pid})"
        elif not connected:
            status = SourceStatus.FAIL
            code = "BRIDGE_DISCONNECTED"
            message = f"Bridge reports disconnected (PID={pid})"
        else:
            status = SourceStatus.PASS
            code = "BRIDGE_OK"
            message = f"Bridge healthy (PID={pid}, age={age_sec:.0f}s, pending={pending})"

        return SourceCheckResult(
            source="mt5_bridge_health",
            tier=Tier.CRITICAL,
            status=status,
            primary_code=code,
            metrics={
                "age_seconds": round(age_sec, 1),
                "pid": pid,
                "connected": connected,
                "outbox_pending": pending,
            },
            message=message,
            checked_at=_utc_iso(),
        )

    # ══════════════════════════════════════════════════════════════════════
    # HIGH-TIER CHECKS (FULL mode only)
    # ══════════════════════════════════════════════════════════════════════

    @health_check(
        tier=Tier.HIGH,
        source="position_snapshots",
        description="Position snapshot trail distance, bar coverage",
    )
    def check_position_snapshots(self) -> SourceCheckResult:
        """Check position_snapshots.jsonl for trail activity and coverage."""
        ps_path = os.path.join(self._base_dir, "position_snapshots.jsonl")
        stats = _safe_jsonl_tail_stats(ps_path, max_scan=200)

        if not stats:
            return SourceCheckResult(
                source="position_snapshots",
                tier=Tier.HIGH,
                status=SourceStatus.MISSING,
                primary_code="POS_SNAP_MISSING",
                message="position_snapshots.jsonl not found",
                checked_at=_utc_iso(),
            )

        # Read actual snapshots for trail analysis
        last = _safe_jsonl_last(ps_path)
        last_time = last.get("time", "") if last else ""

        # Check for recent snapshots
        if last and isinstance(last, dict):
            trail_dist = last.get("trailing_sl_distance", 0) or 0
            bars = last.get("bars_held", 0) or 0

            status = SourceStatus.PASS
            code = "POS_SNAP_OK"
            message = f"Last snapshot: {bars} bars held, trail_dist={trail_dist:.1f}"

            # Staleness check
            age_min = _age_minutes(last_time)
            if age_min > 60:
                status = SourceStatus.WARN
                code = "POS_SNAP_STALE"
                message += f" | Stale ({age_min:.0f} min)"
        else:
            status = SourceStatus.PASS
            code = "POS_SNAP_OK"
            message = "Snapshots present, no positions currently open"

        return SourceCheckResult(
            source="position_snapshots",
            tier=Tier.HIGH,
            status=status,
            primary_code=code,
            metrics={"total_lines": stats.get("total_lines", 0), "last_time": last_time},
            message=message,
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.HIGH,
        source="calibrator_feed_state",
        description="Calibrator feed sample count, staleness",
    )
    def check_calibrator_feed_state(self) -> SourceCheckResult:
        """Check calibrator_feed_state.json for sample accumulation."""
        cf_path = os.path.join(self._base_dir, "calibrator_feed_state.json")
        cf = _safe_json_load(cf_path)

        if cf is None:
            return SourceCheckResult(
                source="calibrator_feed_state",
                tier=Tier.HIGH,
                status=SourceStatus.MISSING,
                primary_code="CAL_FEED_MISSING",
                message="calibrator_feed_state.json not found",
                checked_at=_utc_iso(),
            )

        sample_count = cf.get("sample_count", 0)
        last_line = cf.get("last_line", 0)
        updated = cf.get("updated_utc", "")
        age_min = _age_minutes(updated)

        # Phase determination: COLD < 50, WARM 50-200, HOT > 200
        if sample_count < 50:
            phase = "COLD"
            phase_msg = f"{sample_count}/50 to WARM"
        elif sample_count < 200:
            phase = "WARM"
            phase_msg = f"{sample_count}/200 to HOT"
        else:
            phase = "HOT"
            phase_msg = f"{sample_count} samples"

        if age_min > 12 * 60:
            status = SourceStatus.FAIL
            code = "CAL_FEED_STALLED"
        elif age_min > 6 * 60:
            status = SourceStatus.WARN
            code = "CAL_FEED_STALE"
        else:
            status = SourceStatus.PASS if sample_count >= 50 else SourceStatus.WARN
            code = f"CAL_FEED_{phase}"

        return SourceCheckResult(
            source="calibrator_feed_state",
            tier=Tier.HIGH,
            status=status,
            primary_code=code,
            metrics={
                "sample_count": sample_count,
                "last_line": last_line,
                "age_minutes": round(age_min, 1),
                "phase": phase,
            },
            message=f"Calibrator feed: {phase} ({phase_msg}), age {age_min:.0f} min",
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.HIGH,
        source="conformal_calibrator",
        description="Conformal calibrator cold_started, history freshness",
    )
    def check_conformal_calibrator(self) -> SourceCheckResult:
        """Check conformal_calibrator_state.json for active learning."""
        cc_path = os.path.join(self._base_dir, "conformal_calibrator_state.json")
        cc = _safe_json_load(cc_path)

        if cc is None:
            return SourceCheckResult(
                source="conformal_calibrator",
                tier=Tier.HIGH,
                status=SourceStatus.MISSING,
                primary_code="CONFORMAL_MISSING",
                message="conformal_calibrator_state.json not found",
                checked_at=_utc_iso(),
            )

        cold_started = cc.get("cold_started", True)
        total_computations = cc.get("total_computations", 0)
        history = cc.get("history", [])
        history_count = len(history) if isinstance(history, list) else 0

        # Check last history entry freshness
        last_ts = ""
        if isinstance(history, list) and history:
            last_entry = history[-1]
            if isinstance(last_entry, dict):
                last_ts = last_entry.get("timestamp", "")

        age_days = _age_minutes(last_ts) / (60 * 24) if last_ts else -1

        if cold_started and total_computations == 0:
            status = SourceStatus.FAIL
            code = "CONFORMAL_COLD_STALLED"
            message = (
                f"Cold-started, {total_computations} computations, {history_count} history entries"
            )
        elif cold_started and history_count < 50:
            status = SourceStatus.WARN
            code = "CONFORMAL_COLD_WARMING"
            message = f"Cold-started, {history_count}/50 history entries to warm"
        elif age_days > 7:
            status = SourceStatus.FAIL
            code = "CONFORMAL_HISTORY_FROZEN"
            message = f"History frozen: last entry {age_days:.0f} days ago"
        elif age_days > 3:
            status = SourceStatus.WARN
            code = "CONFORMAL_HISTORY_STALE"
            message = f"History stale: last entry {age_days:.0f} days ago"
        else:
            status = SourceStatus.PASS
            code = "CONFORMAL_OK"
            message = f"Active: {total_computations} computations, {history_count} history entries"

        return SourceCheckResult(
            source="conformal_calibrator",
            tier=Tier.HIGH,
            status=status,
            primary_code=code,
            metrics={
                "cold_started": cold_started,
                "total_computations": total_computations,
                "history_count": history_count,
                "last_history_age_days": round(age_days, 1) if age_days >= 0 else -1,
            },
            message=message,
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.HIGH,
        source="brain_performance",
        description="Per-brain record count, dimension completeness",
    )
    def check_brain_performance(self) -> SourceCheckResult:
        """Check brain_performance.json for record coverage."""
        bp_path = os.path.join(self._base_dir, "brain_performance.json")
        bp = _safe_json_load(bp_path)

        if bp is None:
            return SourceCheckResult(
                source="brain_performance",
                tier=Tier.HIGH,
                status=SourceStatus.MISSING,
                primary_code="BRAIN_PERF_MISSING",
                message="brain_performance.json not found",
                checked_at=_utc_iso(),
            )

        brain_ids = bp.get("brain_ids", [])
        window_size = bp.get("window_size", 100)
        min_records = int(self._t("brain_perf_min_records_per_brain"))

        low_record_brains = []
        for bid in brain_ids:
            records = bp.get(bid, [])
            if isinstance(records, list) and len(records) < min_records:
                low_record_brains.append(f"{bid}({len(records)})")

        total = len(brain_ids)

        if total == 0:
            status = SourceStatus.WARN
            code = "BRAIN_PERF_EMPTY"
            message = "No brains tracked in brain_performance"
        elif low_record_brains:
            status = SourceStatus.WARN
            code = "BRAIN_PERF_LOW_RECORDS"
            message = f"{len(low_record_brains)}/{total} brains have <{min_records} records: {', '.join(low_record_brains[:5])}"
        else:
            status = SourceStatus.PASS
            code = "BRAIN_PERF_OK"
            message = f"{total} brains tracked, all >= {min_records} records"

        return SourceCheckResult(
            source="brain_performance",
            tier=Tier.HIGH,
            status=status,
            primary_code=code,
            metrics={
                "total_brains": total,
                "low_record_count": len(low_record_brains),
                "window_size": window_size,
            },
            message=message,
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.HIGH,
        source="brain_pnl_ledger",
        description="PnL ledger settled vs pending ratio, entry age",
    )
    def check_brain_pnl_ledger(self) -> SourceCheckResult:
        """Check brain_pnl_ledger.json for settlement health."""
        pl_path = os.path.join(self._base_dir, "brain_pnl_ledger.json")
        pl = _safe_json_load(pl_path)

        if pl is None:
            return SourceCheckResult(
                source="brain_pnl_ledger",
                tier=Tier.HIGH,
                status=SourceStatus.MISSING,
                primary_code="PNL_LEDGER_MISSING",
                message="brain_pnl_ledger.json not found",
                checked_at=_utc_iso(),
            )

        settled = pl.get("settled", {})
        pending = pl.get("pending", {})
        settled_count = sum(len(v) for v in settled.values()) if isinstance(settled, dict) else 0
        pending_count = sum(len(v) for v in pending.values()) if isinstance(pending, dict) else 0

        if settled_count == 0 and pending_count == 0:
            status = SourceStatus.SKIPPED
            code = "PNL_LEDGER_EMPTY"
            message = "No settled or pending entries — no trades yet"
        elif pending_count > settled_count * 2:
            status = SourceStatus.WARN
            code = "PNL_LEDGER_PENDING_BACKLOG"
            message = f"Pending ({pending_count}) >> Settled ({settled_count}) — settlement lag"
        else:
            status = SourceStatus.PASS
            code = "PNL_LEDGER_OK"
            message = f"Settled: {settled_count}, Pending: {pending_count}"

        return SourceCheckResult(
            source="brain_pnl_ledger",
            tier=Tier.HIGH,
            status=status,
            primary_code=code,
            metrics={"settled_count": settled_count, "pending_count": pending_count},
            message=message,
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.HIGH,
        source="bar_sync_state",
        description="Bar sync lag, total bars growth",
    )
    def check_bar_sync_state(self) -> SourceCheckResult:
        """Check bar_sync_state.json for synchronization health."""
        bs_path = os.path.join(self._base_dir, "bar_sync_state.json")
        bs = _safe_json_load(bs_path)

        if bs is None:
            return SourceCheckResult(
                source="bar_sync_state",
                tier=Tier.HIGH,
                status=SourceStatus.MISSING,
                primary_code="BAR_SYNC_MISSING",
                message="bar_sync_state.json not found",
                checked_at=_utc_iso(),
            )

        lag = bs.get("lag_count", 0)
        total = bs.get("total_bars_seen", 0)
        last_sync = bs.get("last_sync_utc", "")
        age_min = _age_minutes(last_sync)
        max_lag = int(self._t("bar_sync_max_lag_count"))

        if age_min > 15:
            status = SourceStatus.FAIL
            code = "BAR_SYNC_STALE"
            message = f"Bar sync stale: {age_min:.0f} min since last sync"
        elif lag > max_lag:
            status = SourceStatus.FAIL
            code = "BAR_SYNC_LAG_HIGH"
            message = f"Bar sync lag {lag} exceeds threshold {max_lag}"
        elif lag > max_lag // 2:
            status = SourceStatus.WARN
            code = "BAR_SYNC_LAG_ELEVATED"
            message = f"Bar sync lag {lag}"
        else:
            status = SourceStatus.PASS
            code = "BAR_SYNC_OK"
            message = f"Bar sync lag={lag}, total_bars={total}, age={age_min:.0f} min"

        return SourceCheckResult(
            source="bar_sync_state",
            tier=Tier.HIGH,
            status=status,
            primary_code=code,
            metrics={"lag_count": lag, "total_bars_seen": total, "age_minutes": round(age_min, 1)},
            message=message,
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.HIGH,
        source="golden_master",
        description="Golden master cycle count, last entry age",
    )
    def check_golden_master(self) -> SourceCheckResult:
        """Check golden_master.jsonl for replay data health."""
        gm_path = os.path.join(self._base_dir, "golden_master.jsonl")
        last = _safe_jsonl_last(gm_path)

        if last is None:
            return SourceCheckResult(
                source="golden_master",
                tier=Tier.HIGH,
                status=SourceStatus.MISSING,
                primary_code="GOLDEN_MASTER_MISSING",
                message="golden_master.jsonl not found or empty",
                checked_at=_utc_iso(),
            )

        cycle = last.get("cycle", 0)
        ts = last.get("timestamp_utc", "")
        age_min = _age_minutes(ts)

        # Check if trading is blocked
        summary = last.get("summary", {})
        trade_decisions = summary.get("trade_decisions", 0)
        outputs = last.get("outputs", {})
        blocked_reasons = set()
        for _strategy, decision in outputs.items():
            if isinstance(decision, dict):
                reason = decision.get("reason", "")
                # FIX-20260610-007: distinguish pathological blocks from normal
                # operational states.  Reentry cooldowns, low confidence, regime
                # gates are healthy — they don't indicate a system problem.
                # Only budget_paused, circuit_breaker, governance blocks, and
                # complete inference failures are pathological.
                if (
                    "budget_paused" in reason
                    or "circuit_breaker" in reason
                    or "governance_blocked" in reason
                    or "no_live_brain" in reason
                ):
                    blocked_reasons.add(reason)

        metrics: dict[str, Any] = {
            "cycle": cycle,
            "age_minutes": round(age_min, 1),
            "trade_decisions": trade_decisions,
        }

        if age_min > 30:
            status = SourceStatus.FAIL
            code = "GOLDEN_MASTER_STALE"
            message = f"Last GM cycle {cycle} age {age_min:.0f} min"
        elif cycle == 0:
            status = SourceStatus.FAIL
            code = "GOLDEN_MASTER_EMPTY"
            message = "GM recorded 0 cycles"
        elif blocked_reasons:
            status = SourceStatus.WARN
            code = "GOLDEN_MASTER_TRADING_BLOCKED"
            message = f"Cycle {cycle}: trading blocked — {', '.join(list(blocked_reasons)[:3])}"
            metrics["blocked_reasons"] = list(blocked_reasons)
        else:
            status = SourceStatus.PASS
            code = "GOLDEN_MASTER_OK"
            message = f"Cycle {cycle}, {trade_decisions} decisions, age {age_min:.0f} min"

        return SourceCheckResult(
            source="golden_master",
            tier=Tier.HIGH,
            status=status,
            primary_code=code,
            metrics=metrics,
            message=message,
            checked_at=_utc_iso(),
        )

    # ══════════════════════════════════════════════════════════════════════
    # MEDIUM-TIER CHECKS (FULL mode only)
    # ══════════════════════════════════════════════════════════════════════

    @health_check(
        tier=Tier.MEDIUM,
        source="alpha_registry",
        description="Alpha registry alpha_count, orphan detection",
    )
    def check_alpha_registry(self) -> SourceCheckResult:
        """Check alpha_registry.json for alpha availability."""
        ar_path = os.path.join(self._base_dir, "alpha_registry.json")
        ar = _safe_json_load(ar_path)

        if ar is None:
            return SourceCheckResult(
                source="alpha_registry",
                tier=Tier.MEDIUM,
                status=SourceStatus.MISSING,
                primary_code="ALPHA_REGISTRY_MISSING",
                message="alpha_registry.json not found",
                checked_at=_utc_iso(),
            )

        alpha_count = ar.get("alpha_count", 0)
        records = ar.get("records", [])

        if alpha_count == 0:
            status = SourceStatus.FAIL
            code = "ALPHA_REGISTRY_EMPTY"
            message = "No alphas registered — ORPHAN_SUBSYSTEM"
        else:
            active = sum(1 for r in records if isinstance(r, dict) and r.get("state") == "active")
            status = SourceStatus.PASS
            code = "ALPHA_REGISTRY_OK"
            message = f"{alpha_count} alphas, {active} active"

        return SourceCheckResult(
            source="alpha_registry",
            tier=Tier.MEDIUM,
            status=status,
            primary_code=code,
            metrics={"alpha_count": alpha_count},
            message=message,
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.MEDIUM,
        source="regime_detector_state",
        description="Regime detector warmup, sample count",
    )
    def check_regime_detector_state(self) -> SourceCheckResult:
        """Check regime_detector_state.json for health."""
        rd_path = os.path.join(self._base_dir, "regime_detector_state.json")
        rd = _safe_json_load(rd_path)

        if rd is None:
            return SourceCheckResult(
                source="regime_detector_state",
                tier=Tier.MEDIUM,
                status=SourceStatus.MISSING,
                primary_code="REGIME_STATE_MISSING",
                message="regime_detector_state.json not found",
                checked_at=_utc_iso(),
            )

        warmed = rd.get("is_warmed_up", False)
        count = rd.get("count", 0)
        atr_mean = rd.get("atr_mean", 0)

        if not warmed:
            status = SourceStatus.WARN
            code = "REGIME_NOT_WARMED"
            message = f"Regime detector not warmed up ({count} samples)"
        else:
            status = SourceStatus.PASS
            code = "REGIME_OK"
            message = f"Warmed up: {count} samples, ATR mean={atr_mean:.1f}"

        return SourceCheckResult(
            source="regime_detector_state",
            tier=Tier.MEDIUM,
            status=status,
            primary_code=code,
            metrics={"is_warmed_up": warmed, "sample_count": count, "atr_mean": round(atr_mean, 1)},
            message=message,
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.MEDIUM,
        source="leaderboard",
        description="Leaderboard staleness, brain health summary",
    )
    def check_leaderboard(self) -> SourceCheckResult:
        """Check reports/leaderboard.json for staleness and health signals."""
        lb_path = os.path.join(self._base_dir, "reports", "leaderboard.json")
        lb = _safe_json_load(lb_path)

        if lb is None:
            return SourceCheckResult(
                source="leaderboard",
                tier=Tier.MEDIUM,
                status=SourceStatus.MISSING,
                primary_code="LEADERBOARD_MISSING",
                message="leaderboard.json not found",
                checked_at=_utc_iso(),
            )

        generated = lb.get("generated_at", "")
        age_h = _age_minutes(generated) / 60.0
        total = lb.get("total_brains", 0)
        brains = lb.get("brains", [])

        critical_count = sum(
            1 for b in brains if isinstance(b, dict) and b.get("health_signal") == "critical"
        )
        zero_vote = sum(1 for b in brains if isinstance(b, dict) and b.get("vote_weight", 0) == 0)

        message = ""
        if age_h > 12:
            status = SourceStatus.WARN
            code = "LEADERBOARD_STALE"
        elif critical_count > 3:
            status = SourceStatus.WARN
            code = "LEADERBOARD_MANY_CRITICAL"
        elif zero_vote == total and total > 0:
            status = SourceStatus.WARN
            code = "LEADERBOARD_ZERO_VOTE_WEIGHT"
            message = f"All {total} brains have vote_weight=0"
        else:
            status = SourceStatus.PASS
            code = "LEADERBOARD_OK"

        if not message or code == "LEADERBOARD_OK":
            message = f"{total} brains, {critical_count} critical, age {age_h:.1f}h"

        return SourceCheckResult(
            source="leaderboard",
            tier=Tier.MEDIUM,
            status=status,
            primary_code=code,
            metrics={
                "total_brains": total,
                "critical_count": critical_count,
                "zero_vote_count": zero_vote,
                "age_hours": round(age_h, 1),
            },
            message=message,
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.MEDIUM,
        source="daily_ops_state",
        description="Daily ops last run time, completion check",
    )
    def check_daily_ops_state(self) -> SourceCheckResult:
        """Check state/daily_ops_state.json for schedule adherence."""
        do_path = os.path.join(self._base_dir, "state", "daily_ops_state.json")
        do = _safe_json_load(do_path)

        if do is None:
            return SourceCheckResult(
                source="daily_ops_state",
                tier=Tier.MEDIUM,
                status=SourceStatus.MISSING,
                primary_code="DAILY_OPS_STATE_MISSING",
                message="daily_ops_state.json not found",
                checked_at=_utc_iso(),
            )

        last_ts = do.get("last_daily_ops_utc", 0)
        if isinstance(last_ts, int | float) and last_ts > 0:
            from datetime import datetime

            last_dt = datetime.fromtimestamp(float(last_ts), tz=UTC)
            age_h = (
                datetime.now(UTC).replace(tzinfo=None) - last_dt.replace(tzinfo=None)
            ).total_seconds() / 3600
        else:
            age_h = -1

        if age_h < 0:
            status = SourceStatus.WARN
            code = "DAILY_OPS_TIMESTAMP_INVALID"
            message = "Cannot parse last_daily_ops_utc"
        elif age_h > 30:
            status = SourceStatus.FAIL
            code = "DAILY_OPS_MISSED"
            message = f"Last daily ops {age_h:.0f}h ago — may have missed a cycle"
        elif age_h > 25:
            status = SourceStatus.WARN
            code = "DAILY_OPS_OVERDUE"
            message = f"Last daily ops {age_h:.0f}h ago"
        else:
            status = SourceStatus.PASS
            code = "DAILY_OPS_OK"
            message = f"Last daily ops {age_h:.1f}h ago"

        return SourceCheckResult(
            source="daily_ops_state",
            tier=Tier.MEDIUM,
            status=status,
            primary_code=code,
            metrics={"age_hours": round(age_h, 1)},
            message=message,
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.MEDIUM,
        source="live_labels",
        description="Live labels entry count vs journal close count",
    )
    def check_live_labels(self) -> SourceCheckResult:
        """Check reports/live_labels.jsonl for label data health."""
        ll_path = os.path.join(self._base_dir, "reports", "live_labels.jsonl")
        count = _safe_jsonl_count(ll_path)

        if count is None or count == 0:
            return SourceCheckResult(
                source="live_labels",
                tier=Tier.MEDIUM,
                status=SourceStatus.SKIPPED if count == 0 else SourceStatus.MISSING,
                primary_code="LIVE_LABELS_EMPTY",
                message="live_labels.jsonl empty or not found",
                checked_at=_utc_iso(),
            )

        # Check for unlabeled entries
        try:
            with open(ll_path, encoding="utf-8") as f:
                unlabeled = sum(1 for line in f if '"label": "unlabeled"' in line)
        except Exception:  # noqa: BLE001
            unlabeled = 0

        if unlabeled > 3:
            status = SourceStatus.WARN
            code = "LIVE_LABELS_UNLABELED"
            message = f"{count} entries, {unlabeled} unlabeled"
        else:
            status = SourceStatus.PASS
            code = "LIVE_LABELS_OK"
            message = f"{count} entries"

        return SourceCheckResult(
            source="live_labels",
            tier=Tier.MEDIUM,
            status=status,
            primary_code=code,
            metrics={"entry_count": count, "unlabeled_count": unlabeled},
            message=message,
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.MEDIUM,
        source="exit_watchdog_alerts",
        description="Exit watchdog alert frequency anomaly",
    )
    def check_exit_watchdog_alerts(self) -> SourceCheckResult:
        """Check exit watchdog alert frequency for retry storms."""
        ew_path = os.path.join(self._base_dir, "reports", "exit_watchdog_alerts.jsonl")
        count = _safe_jsonl_count(ew_path)

        if count is None or count == 0:
            return SourceCheckResult(
                source="exit_watchdog_alerts",
                tier=Tier.MEDIUM,
                status=SourceStatus.PASS,
                primary_code="EXIT_WATCHDOG_OK",
                message="No exit watchdog alerts",
                checked_at=_utc_iso(),
            )

        # Check last alert freshness
        last = _safe_jsonl_last(ew_path)
        last_ts = last.get("timestamp_utc", "") if last else ""
        age_h = _age_minutes(last_ts) / 60.0 if last_ts else -1

        if count > 30:
            status = SourceStatus.WARN
            code = "EXIT_WATCHDOG_ALERT_SPIKE"
            message = f"{count} alerts — possible retry storm"
        elif age_h > 24:
            status = SourceStatus.PASS
            code = "EXIT_WATCHDOG_OK"
            message = f"{count} alerts, last {age_h:.0f}h ago"
        else:
            status = SourceStatus.PASS
            code = "EXIT_WATCHDOG_OK"
            message = f"{count} alerts, last {age_h:.1f}h ago"

        return SourceCheckResult(
            source="exit_watchdog_alerts",
            tier=Tier.MEDIUM,
            status=status,
            primary_code=code,
            metrics={"alert_count": count, "last_age_hours": round(age_h, 1)},
            message=message,
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.MEDIUM,
        source="retraining_signal",
        description="Retraining signal assessments, degradation detection",
    )
    def check_retraining_signal(self) -> SourceCheckResult:
        """Check retraining_signal_prev.json for assessment activity."""
        rs_path = os.path.join(self._base_dir, "reports", "retraining_signal_prev.json")
        rs = _safe_json_load(rs_path)

        if rs is None:
            return SourceCheckResult(
                source="retraining_signal",
                tier=Tier.MEDIUM,
                status=SourceStatus.MISSING,
                primary_code="RETRAIN_SIGNAL_MISSING",
                message="retraining_signal_prev.json not found",
                checked_at=_utc_iso(),
            )

        assessed = rs.get("total_brains_assessed", 0)
        degraded = rs.get("degraded_count", 0)
        urgency = rs.get("overall_urgency", "unknown")

        if assessed == 0 and urgency == "ok":
            status = SourceStatus.WARN
            code = "RETRAIN_NO_ASSESSMENTS"
            message = f"0 brains assessed (urgency={urgency}) — retraining pipeline may be idle"
        elif degraded > 0:
            status = SourceStatus.WARN
            code = "RETRAIN_DEGRADATION_DETECTED"
            message = f"{degraded} brains degraded, {assessed} assessed"
        else:
            status = SourceStatus.PASS
            code = "RETRAIN_OK"
            message = f"{assessed} assessed, {degraded} degraded, urgency={urgency}"

        return SourceCheckResult(
            source="retraining_signal",
            tier=Tier.MEDIUM,
            status=status,
            primary_code=code,
            metrics={
                "total_brains_assessed": assessed,
                "degraded_count": degraded,
                "urgency": urgency,
            },
            message=message,
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.MEDIUM,
        source="feature_store_schemas",
        description="Feature store schema registry integrity and drift",
    )
    def check_feature_store_schemas(self) -> SourceCheckResult:
        """Check feature_store/schemas.json for schema version and dimension count."""
        fs_path = os.path.join(self._base_dir, "feature_store", "schemas.json")
        fs = _safe_json_load(fs_path)
        if fs is None:
            return SourceCheckResult(
                source="feature_store_schemas",
                tier=Tier.MEDIUM,
                status=SourceStatus.MISSING,
                primary_code="FS_SCHEMAS_MISSING",
                message="feature_store/schemas.json not found",
                checked_at=_utc_iso(),
            )
        schema_count = len(fs) if isinstance(fs, dict) else 0
        if schema_count == 0:
            return SourceCheckResult(
                source="feature_store_schemas",
                tier=Tier.MEDIUM,
                status=SourceStatus.FAIL,
                primary_code="FS_SCHEMAS_EMPTY",
                message="No feature schemas registered",
                checked_at=_utc_iso(),
            )
        valid = 0
        dims_by_schema: dict[str, int] = {}
        for _name, schema in fs.items():
            if isinstance(schema, dict) and isinstance(schema.get("fields"), list):
                dims_by_schema[_name] = len(schema["fields"])
                valid += 1
        status = SourceStatus.PASS if valid == schema_count else SourceStatus.WARN
        code = "FS_SCHEMAS_OK" if valid == schema_count else "FS_SCHEMAS_INCOMPLETE"
        return SourceCheckResult(
            source="feature_store_schemas",
            tier=Tier.MEDIUM,
            status=status,
            primary_code=code,
            metrics={
                "schema_count": schema_count,
                "valid_schemas": valid,
                "dimensions": dims_by_schema,
            },
            message=f"{valid}/{schema_count} schemas valid, dims={dims_by_schema}",
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.MEDIUM,
        source="alert_delivery",
        description="Alert audit log — delivery success rate and recent activity",
    )
    def check_alert_delivery(self) -> SourceCheckResult:
        """Check logs/alert_audit.jsonl and alert_undelivered.jsonl for delivery health."""
        audit_path = os.path.join(self._base_dir, "logs", "alert_audit.jsonl")
        undelivered_path = os.path.join(self._base_dir, "logs", "alert_undelivered.jsonl")
        audit_count = _safe_jsonl_count(audit_path) or 0
        undelivered_count = _safe_jsonl_count(undelivered_path) or 0
        last = _safe_jsonl_last(audit_path)
        last_ts = last.get("timestamp_utc", last.get("time", "")) if last else ""
        age_h = _age_minutes(last_ts) / 60.0 if last_ts else -1
        fail_rate = undelivered_count / max(audit_count + undelivered_count, 1)
        if audit_count == 0:
            status = SourceStatus.WARN
            code = "ALERT_AUDIT_EMPTY"
            message = "No alert audit entries — alert system may not be running"
        elif fail_rate > 0.10:
            status = SourceStatus.FAIL
            code = "ALERT_DELIVERY_FAIL_RATE_HIGH"
            message = f"{undelivered_count}/{audit_count + undelivered_count} undelivered ({fail_rate:.1%})"
        elif age_h > 6:
            status = SourceStatus.WARN
            code = "ALERT_AUDIT_STALE"
            message = f"Last alert {age_h:.0f}h ago — alert system may be silent"
        else:
            status = SourceStatus.PASS
            code = "ALERT_DELIVERY_OK"
            message = (
                f"{audit_count} delivered, {undelivered_count} undelivered, last {age_h:.1f}h ago"
            )
        return SourceCheckResult(
            source="alert_delivery",
            tier=Tier.MEDIUM,
            status=status,
            primary_code=code,
            metrics={
                "audit_count": audit_count,
                "undelivered_count": undelivered_count,
                "fail_rate": round(fail_rate, 4),
                "last_age_hours": round(age_h, 1),
            },
            message=message,
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.MEDIUM,
        source="data_health_self",
        description="DataHealthService own state file — self-monitoring integrity",
    )
    def check_data_health_self(self) -> SourceCheckResult:
        """Check state/data_health_state.json for self-monitoring integrity."""
        dh_path = os.path.join(self._base_dir, "state", "data_health_state.json")
        dh = _safe_json_load(dh_path)
        if dh is None:
            return SourceCheckResult(
                source="data_health_self",
                tier=Tier.MEDIUM,
                status=SourceStatus.MISSING,
                primary_code="DH_SELF_MISSING",
                message="data_health_state.json not found",
                checked_at=_utc_iso(),
            )
        sv = dh.get("schema_version", "")
        if sv != "data_health_state.v2":
            return SourceCheckResult(
                source="data_health_self",
                tier=Tier.MEDIUM,
                status=SourceStatus.WARN,
                primary_code="DH_SELF_SCHEMA_OLD",
                message=f"Schema {sv or 'missing'} — expected data_health_state.v2",
                checked_at=_utc_iso(),
            )
        updated = dh.get("updated_at", "")
        age_h = _age_minutes(updated) / 60.0
        sources = dh.get("sources", {})
        source_count = len(sources)
        status = SourceStatus.WARN if age_h > 24 else SourceStatus.PASS
        code = "DH_SELF_STALE" if age_h > 24 else "DH_SELF_OK"
        return SourceCheckResult(
            source="data_health_self",
            tier=Tier.MEDIUM,
            status=status,
            primary_code=code,
            metrics={
                "schema_version": sv,
                "source_count": source_count,
                "age_hours": round(age_h, 1),
            },
            message=f"Self-check: {source_count} sources tracked, age {age_h:.1f}h",
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.MEDIUM,
        source="alpha_allocation",
        description="Portfolio allocation output — alpha_count check",
    )
    def check_alpha_allocation(self) -> SourceCheckResult:
        """Check reports/alpha_allocation.json for portfolio allocation health."""
        aa_path = os.path.join(self._base_dir, "reports", "alpha_allocation.json")
        aa = _safe_json_load(aa_path)
        if aa is None:
            return SourceCheckResult(
                source="alpha_allocation",
                tier=Tier.MEDIUM,
                status=SourceStatus.SKIPPED,
                primary_code="ALPHA_ALLOC_MISSING",
                message="alpha_allocation.json not found",
                checked_at=_utc_iso(),
            )
        alpha_count = aa.get("alpha_count", 0)
        allocatable = aa.get("allocatable_count", 0)
        if alpha_count == 0:
            status = SourceStatus.WARN
            code = "ALPHA_ALLOC_EMPTY"
        else:
            status = SourceStatus.PASS
            code = "ALPHA_ALLOC_OK"
        return SourceCheckResult(
            source="alpha_allocation",
            tier=Tier.MEDIUM,
            status=status,
            primary_code=code,
            metrics={"alpha_count": alpha_count, "allocatable_count": allocatable},
            message=f"{alpha_count} alphas, {allocatable} allocatable",
            checked_at=_utc_iso(),
        )

    # ══════════════════════════════════════════════════════════════════════
    # CROSS-SOURCE VALIDATION (FULL mode only)
    # ══════════════════════════════════════════════════════════════════════

    def _check_journal_vs_pnl_ledger(self) -> CrossCheckResult:
        """Audit PnL ledger: freshness of most recent settled entry.

        FIX-20260610-007: Replaced the broken count comparison (journal trade
        count vs PnL ledger brain-entry count = apples vs oranges) with a
        meaningful freshness check — when was the last brain-trade-outcome
        recorded?  If the PnL ledger hasn't been updated in >24h, the
        settlement pipeline may be stalled.
        """
        pl_path = os.path.join(self._base_dir, "brain_pnl_ledger.json")
        if not os.path.exists(pl_path):
            return CrossCheckResult(
                check_name="pnl_ledger_freshness",
                status=SourceStatus.SKIPPED,
                primary_code="CROSS_PNL_LEDGER_MISSING",
                message="brain_pnl_ledger.json not found",
                checked_at=_utc_iso(),
            )

        try:
            pl = json.loads(open(pl_path, encoding="utf-8").read())
        except Exception:  # noqa: BLE001
            return CrossCheckResult(
                check_name="pnl_ledger_freshness",
                status=SourceStatus.FAIL,
                primary_code="CROSS_PNL_LEDGER_CORRUPT",
                message="brain_pnl_ledger.json unreadable",
                checked_at=_utc_iso(),
            )

        settled = pl.get("settled", {})
        total_settled = 0
        latest_ts = ""
        for brain_trades in settled.values():
            if isinstance(brain_trades, list):
                total_settled += len(brain_trades)
                for trade in brain_trades:
                    if isinstance(trade, dict):
                        ct = trade.get("close_time", "")
                        if ct and (not latest_ts or ct > latest_ts):
                            latest_ts = ct

        if total_settled == 0:
            return CrossCheckResult(
                check_name="pnl_ledger_freshness",
                status=SourceStatus.SKIPPED,
                primary_code="CROSS_PNL_LEDGER_EMPTY",
                message="No settled entries in PnL ledger",
                metrics={"total_settled": 0, "latest_close": ""},
                checked_at=_utc_iso(),
            )

        age_h = _age_minutes(latest_ts) / 60.0 if latest_ts else -1
        journal_closes = 0
        try:
            jl_path = os.path.join(self._base_dir, "live_trade_journal.jsonl")
            if os.path.exists(jl_path):
                with open(jl_path, encoding="utf-8") as f:
                    for line in f:
                        if '"action": "close"' in line:
                            journal_closes += 1
        except Exception:  # noqa: BLE001
            pass

        if age_h > 48:
            status = SourceStatus.FAIL
            code = "CROSS_PNL_LEDGER_STALE"
        elif age_h > 24:
            status = SourceStatus.WARN
            code = "CROSS_PNL_LEDGER_STALE"
        else:
            status = SourceStatus.PASS
            code = "CROSS_OK"

        return CrossCheckResult(
            check_name="pnl_ledger_freshness",
            status=status,
            primary_code=code,
            metrics={
                "total_settled": total_settled,
                "latest_close_age_hours": round(age_h, 1),
                "journal_closes": journal_closes,
            },
            message=f"PnL ledger: {total_settled} settled, last close {age_h:.1f}h ago (journal: {journal_closes} closes)",
            checked_at=_utc_iso(),
        )

    def _check_open_vs_close_convergence(self) -> CrossCheckResult:
        """Check that open and close counts converge within 24h."""
        jl_path = os.path.join(self._base_dir, "live_trade_journal.jsonl")

        open_count = 0
        close_count = 0
        try:
            if os.path.exists(jl_path):
                with open(jl_path, encoding="utf-8") as f:
                    for line in f:
                        if '"action": "open"' in line:
                            open_count += 1
                        elif '"action": "close"' in line:
                            close_count += 1
        except Exception:  # noqa: BLE001
            pass

        if open_count == 0:
            return CrossCheckResult(
                check_name="open_vs_close_convergence",
                status=SourceStatus.SKIPPED,
                primary_code="CROSS_OPEN_CLOSE_SKIPPED",
                message="No open entries to compare",
                checked_at=_utc_iso(),
            )

        ratio = close_count / max(open_count, 1)
        # Closed should be >= open_count - active_positions.
        # A ratio < 0.3 means most opens don't have a matching close.
        min_ratio = self._t("journal_close_open_ratio_min")

        if ratio < min_ratio:
            status = SourceStatus.WARN
        else:
            status = SourceStatus.PASS

        return CrossCheckResult(
            check_name="open_vs_close_convergence",
            status=status,
            primary_code="CROSS_OPEN_CLOSE_DIVERGENCE"
            if status != SourceStatus.PASS
            else "CROSS_OK",
            metrics={
                "open_count": open_count,
                "close_count": close_count,
                "ratio": round(ratio, 4),
            },
            message=f"Opens={open_count} vs Closes={close_count} (ratio={ratio:.2f})",
            checked_at=_utc_iso(),
        )

    # ══════════════════════════════════════════════════════════════════════
    # ORPHAN SUBSYSTEM DETECTION (FULL mode only)
    # ══════════════════════════════════════════════════════════════════════

    # Known (path_suffix, zero_check_key) pairs for orphan detection.
    _ORPHAN_SIGNATURES: list[tuple[str, str | None, str]] = [
        ("alpha_registry.json", "alpha_count", "zero_data"),
        ("alpha_performance.json", "alpha_count", "zero_data"),
        ("conformal_calibrator_state.json", "cold_started", "empty_init"),
        ("calibrator_feed_state.json", "sample_count", "zero_data"),
        ("brain_performance.json", None, "empty_init"),  # checks if file is empty or has no records
        ("reports/retraining_signal_prev.json", "total_brains_assessed", "zero_data"),
    ]

    def _detect_orphan_subsystems(self) -> list[OrphanFinding]:
        """Scan for subsystems whose state files exist but contain only initial/empty data.

        Generalizes ReB-20260608-002 (ORPHAN_SUBSYSTEM_DETECTION).
        """
        findings: list[OrphanFinding] = []

        for rel_path, zero_key, pattern in self._ORPHAN_SIGNATURES:
            full_path = os.path.join(self._base_dir, rel_path)
            if not os.path.exists(full_path):
                continue  # file doesn't exist → not an orphan, just not deployed

            data = _safe_json_load(full_path)
            if data is None:
                findings.append(
                    OrphanFinding(
                        source_path=rel_path,
                        pattern="never_written",
                        detail="File exists but cannot be parsed as JSON",
                    )
                )
                continue

            if zero_key is not None:
                val = data.get(zero_key)
                if val is None or val == 0 or val == [] or val == {}:
                    findings.append(
                        OrphanFinding(
                            source_path=rel_path,
                            pattern=pattern,
                            detail=f"{zero_key}={val} — subsystem appears unwired",
                        )
                    )
            else:
                # Check for empty dict/list at top level
                if not data or all(not v for v in data.values() if not isinstance(v, bool)):
                    findings.append(
                        OrphanFinding(
                            source_path=rel_path,
                            pattern=pattern,
                            detail="All fields empty or zero — subsystem appears unwired",
                        )
                    )

        return findings

    # ══════════════════════════════════════════════════════════════════════
    # STATE PERSISTENCE
    # ══════════════════════════════════════════════════════════════════════

    def _state_path(self) -> str:
        return os.path.join(self._base_dir, "state", "data_health_state.json")

    def load_health_state(self) -> dict[str, Any]:
        """Load persisted health state — graceful degradation on corruption.

        Iron Law for Monitoring #2: corrupt file → silent return to fresh state.
        """
        path = self._state_path()
        try:
            if not os.path.exists(path):
                return fresh_health_state(self._symbol)
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
            # Validate minimum schema
            if not isinstance(state, dict) or "schema_version" not in state:
                return fresh_health_state(self._symbol)
            # Ensure v2 structure exists
            if "sources" not in state:
                state["sources"] = {}
            if "legacy" not in state:
                state["legacy"] = {"last_close_count": 0, "checked_at": ""}
            state["symbol"] = self._symbol
            return state
        except (json.JSONDecodeError, FileNotFoundError, OSError, TypeError, ValueError):
            return fresh_health_state(self._symbol)

    def save_health_state(self, report: HealthReport) -> None:
        """Persist health state snapshot with atomic write.

        Iron Law for Monitoring #2: write to .tmp → os.replace (atomic).
        """
        path = self._state_path()
        current = self.load_health_state()
        now = _utc_iso()

        # Update per-source records
        sources_state: dict[str, dict[str, Any]] = current.get("sources", {})
        for src in report.sources:
            record = sources_state.get(src.source, {})
            # Track error history (last 3)
            errors = record.get("error_history", [])
            if isinstance(errors, list):
                if src.status in (SourceStatus.FAIL, SourceStatus.WARN, SourceStatus.MISSING):
                    errors.insert(
                        0,
                        {
                            "utc": now,
                            "code": src.primary_code,
                            "message": src.message[:200],
                        },
                    )
                    errors = errors[:3]
                elif src.status == SourceStatus.PASS:
                    # Clear error history on recovery
                    if errors and errors[0].get("code") != "RECOVERED":
                        errors.insert(0, {"utc": now, "code": "RECOVERED", "message": ""})
                        errors = errors[:3]

            # Compute trend
            trend = "stable"
            if len(errors) >= 2:
                # More errors in recent half → degrading
                mid = len(errors) // 2
                recent_errors = sum(
                    1 for e in errors[:mid] if e.get("code", "") not in ("RECOVERED", "")
                )
                older_errors = sum(
                    1 for e in errors[mid:] if e.get("code", "") not in ("RECOVERED", "")
                )
                if recent_errors > older_errors:
                    trend = "degrading"
                elif recent_errors < older_errors:
                    trend = "improving"

            sources_state[src.source] = {
                "last_check_utc": now,
                "last_status": src.status.value,
                "last_primary_code": src.primary_code,
                "metrics_snapshot": {
                    k: v
                    for k, v in src.metrics.items()
                    if isinstance(v, int | float | str | bool | type(None))
                },
                "error_history": errors,
                "trend": trend,
            }

        # Update cross-check records
        cross_state: dict[str, dict[str, Any]] = current.get("cross_checks", {})
        for cc in report.cross_checks:
            cross_state[cc.check_name] = {
                "last_check_utc": now,
                "last_status": cc.status.value,
                "delta_pct": cc.metrics.get("delta_pct"),
            }

        # Compute journal close count for legacy compat
        jl_count = 0
        for src in report.sources:
            if src.source == "trade_journal":
                jl_count = src.metrics.get("close_count_tail", 0)
                break

        state = {
            "schema_version": "data_health_state.v2",
            "updated_at": now,
            "symbol": self._symbol,
            "last_full_run_utc": now
            if self._mode == "full"
            else current.get("last_full_run_utc", ""),
            "last_lightweight_run_utc": now
            if self._mode != "full"
            else current.get("last_lightweight_run_utc", ""),
            "overall_status": report.alert_level,
            "sources": sources_state,
            "cross_checks": cross_state,
            "orphan_subsystems": [
                {"source_path": o.source_path, "pattern": o.pattern, "detail": o.detail}
                for o in report.orphans
            ],
            "legacy": {
                "last_close_count": jl_count,
                "checked_at": now,
            },
        }

        # Atomic write (Iron Law #2)
        try:
            tmp_path = path + ".tmp"
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, default=str)
            os.replace(tmp_path, path)
        except OSError:
            pass  # Iron Law #1: persistence failure must not crash

    # ══════════════════════════════════════════════════════════════════════
    # ALERT CONTEXT (for external dispatcher — Iron Law #3)
    # ══════════════════════════════════════════════════════════════════════

    # ── FIX-20260611-002: Behavioral compliance — incremental log scanner ──

    def _hydrate_behavioral_metrics(self) -> None:
        """Incrementally scan intent log for behavioral compliance counters.

        Uses seek/tell cursor to avoid double-counting across overlapping
        time windows.  Counters are RESET each tick (accumulation bug fix);
        only the file cursor persists.  Hard fuse at max_lines_per_tick=500
        to prevent I/O from exceeding the 16ms per-tick budget.
        """
        from core.observability.data_health_schema import BehavioralMetrics

        # ── Resolve current log path ──
        _log_pattern = os.path.join(self._base_dir, "logs", "intent_*.log")
        _log_files = sorted(glob.glob(_log_pattern))
        _current_log_path = _log_files[-1] if _log_files else ""

        # ── Preserve cursor across log rotation ──
        _current_offset = 0
        if self._cached_behavioral_metrics is not None:
            if self._cached_behavioral_metrics.intent_log_path == _current_log_path:
                _current_offset = self._cached_behavioral_metrics.last_log_byte_offset
            # else: log rotated → reset cursor to 0

        # ── Reset counters each tick, preserve cursor (accumulation bug fix) ──
        self._cached_behavioral_metrics = BehavioralMetrics(
            last_log_byte_offset=_current_offset,
            intent_log_path=_current_log_path,
        )
        _metrics = self._cached_behavioral_metrics

        if not _current_log_path or not os.path.exists(_current_log_path):
            return

        _max_lines = int(self._t("behavioral_max_lines_per_tick"))
        try:
            with open(_current_log_path, encoding="utf-8") as f:
                f.seek(_current_offset)
                _lines_read = 0
                for _line in f:
                    _lines_read += 1
                    if _lines_read > _max_lines:
                        break
                    # ── Fast substring matching (no JSON parse for perf) ──
                    if '"event": "consensus_blocked_by_main_eval"' in _line:
                        _metrics.gate_bypass_count += 1
                    elif '"event": "brain_alert"' in _line:
                        _bid = ""
                        if '"brain_id": "' in _line:
                            _start = _line.index('"brain_id": "') + 13
                            _end = _line.index('"', _start)
                            _bid = _line[_start:_end]
                        _metrics.brain_alerts[_bid or "unknown"] = (
                            _metrics.brain_alerts.get(_bid or "unknown", 0) + 1
                        )
                    elif '"event": "intent_dispatched"' in _line:
                        _metrics.intent_dispatched_count += 1
                    elif '"should_trade": false' in _line:
                        _metrics.strategy_rejections += 1
                    elif '"event": "cycle_end"' in _line:
                        _metrics.cycle_count += 1

                # ── Update cursor for next tick ──
                _metrics.last_log_byte_offset = f.tell()
        except Exception:  # noqa: BLE001
            pass  # best-effort — never crash the audit tick

    # ── FIX-20260611-002: Behavioral compliance checks ──

    @health_check(
        tier=Tier.CRITICAL,
        source="gate_bypass",
        description="Phase 10 dispatch bypassed main eval gates",
    )
    def check_gate_bypass(self) -> SourceCheckResult:
        self._hydrate_behavioral_metrics()
        _count = self._cached_behavioral_metrics.gate_bypass_count
        _max = int(self._t("gate_bypass_max_count"))
        if _count > _max:
            return SourceCheckResult(
                source="gate_bypass",
                tier=Tier.CRITICAL,
                status=SourceStatus.FAIL,
                primary_code="GATE_BYPASS_DETECTED",
                message=f"Phase 10 consensus dispatch bypassed main eval {_count} time(s)",
                metrics={"bypass_count": _count, "max_allowed": _max},
                checked_at=_utc_iso(),
            )
        return SourceCheckResult(
            source="gate_bypass", tier=Tier.CRITICAL,
            status=SourceStatus.PASS, primary_code="GATE_BYPASS_OK",
            message="No gate bypass events detected",
            metrics={"bypass_count": _count}, checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.CRITICAL,
        source="position_limit",
        description="Concurrent positions vs max_positions",
    )
    def check_position_limit(self) -> SourceCheckResult:
        # ── Authoritative source: position_manager (MT5-synced), not intent log ──
        _max_positions = 2  # default; overridden by live_btc.yaml config
        _current = 0
        if self._position_manager is not None:
            try:
                _positions = self._position_manager.get_all_positions()
                _current = len(_positions)
            except Exception:  # noqa: BLE001
                pass

        # ── Read max_positions from config ──
        _cfg_path = os.path.join(self._base_dir, "..", "configs")
        for _yaml_name in ("live_btc.yaml", "live.yaml"):
            _yp = os.path.join(_cfg_path, _yaml_name)
            if os.path.exists(_yp):
                try:
                    import yaml
                    with open(_yp, encoding="utf-8") as f:
                        _cfg = yaml.safe_load(f)
                    _mp = _cfg.get("max_positions")
                    if _mp is not None:
                        _max_positions = int(_mp)
                        break
                except Exception:  # noqa: BLE001
                    pass

        if _current > _max_positions:
            self._position_exceeded_streak += 1
            _consecutive_needed = int(self._t("position_limit_consecutive_alerts"))
            if self._position_exceeded_streak >= _consecutive_needed:
                return SourceCheckResult(
                    source="position_limit",
                    tier=Tier.CRITICAL,
                    status=SourceStatus.FAIL,
                    primary_code="POSITION_LIMIT_EXCEEDED_PERSISTENT",
                    message=(
                        f"Concurrent positions ({_current}) > max_positions "
                        f"({_max_positions}) for {self._position_exceeded_streak} "
                        f"consecutive audits — likely a system-level leak"
                    ),
                    metrics={
                        "current_positions": _current,
                        "max_positions": _max_positions,
                        "consecutive_alerts": self._position_exceeded_streak,
                    },
                    checked_at=_utc_iso(),
                )
            return SourceCheckResult(
                source="position_limit",
                tier=Tier.CRITICAL,
                status=SourceStatus.WARN,
                primary_code="POSITION_LIMIT_EXCEEDED_TRANSIENT",
                message=(
                    f"Concurrent positions ({_current}) > max_positions "
                    f"({_max_positions}) — transient (streak {self._position_exceeded_streak}/"
                    f"{_consecutive_needed})"
                ),
                metrics={
                    "current_positions": _current,
                    "max_positions": _max_positions,
                    "consecutive_alerts": self._position_exceeded_streak,
                },
                checked_at=_utc_iso(),
            )
        # Reset streak
        self._position_exceeded_streak = 0
        return SourceCheckResult(
            source="position_limit", tier=Tier.CRITICAL,
            status=SourceStatus.PASS, primary_code="POSITION_LIMIT_OK",
            message=f"Concurrent positions ({_current}) within limit ({_max_positions})",
            metrics={"current_positions": _current, "max_positions": _max_positions},
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.HIGH,
        source="brain_output_health",
        description="Brain prediction silence detection",
    )
    def check_brain_output_health(self) -> SourceCheckResult:
        self._hydrate_behavioral_metrics()
        _alerts = self._cached_behavioral_metrics.brain_alerts
        _cycles = self._cached_behavioral_metrics.cycle_count
        _min_brains = int(self._t("brain_output_min_productive_brains"))
        _max_alerts = int(self._t("brain_output_max_alerts_per_brain"))

        _productive = sum(1 for c in _alerts.values() if c > 0)

        if _productive == 0 and _cycles > 0:
            return SourceCheckResult(
                source="brain_output_health",
                tier=Tier.HIGH,
                status=SourceStatus.FAIL,
                primary_code="BRAIN_SILENCE_ALL",
                message=f"Zero brain alerts across {_cycles} cycles — possible full-brain silence",
                metrics={"productive_brains": 0, "cycles": _cycles, "alerts": _alerts},
                checked_at=_utc_iso(),
            )
        if _productive < _min_brains:
            return SourceCheckResult(
                source="brain_output_health",
                tier=Tier.HIGH,
                status=SourceStatus.WARN,
                primary_code="BRAIN_SILENCE_LOW",
                message=f"Only {_productive}/{_min_brains} brains producing output",
                metrics={"productive_brains": _productive, "alerts": _alerts},
                checked_at=_utc_iso(),
            )
        # Check individual brain alert spikes
        _overactive = [bid for bid, c in _alerts.items() if c > _max_alerts]
        if _overactive:
            return SourceCheckResult(
                source="brain_output_health",
                tier=Tier.HIGH,
                status=SourceStatus.WARN,
                primary_code="BRAIN_ALERT_SPIKE",
                message=f"Brains with >{_max_alerts} alerts: {_overactive}",
                metrics={"overactive_brains": _overactive, "alerts": _alerts},
                checked_at=_utc_iso(),
            )
        return SourceCheckResult(
            source="brain_output_health", tier=Tier.HIGH,
            status=SourceStatus.PASS, primary_code="BRAIN_OUTPUT_OK",
            message=f"{_productive} brains producing output across {_cycles} cycles",
            metrics={"productive_brains": _productive, "cycles": _cycles, "alerts": _alerts},
            checked_at=_utc_iso(),
        )

    @health_check(
        tier=Tier.HIGH,
        source="trade_activity",
        description="Trade frequency anomaly — distinguishes 'thinking' from 'dead'",
    )
    def check_trade_activity(self) -> SourceCheckResult:
        self._hydrate_behavioral_metrics()
        _trades = self._cached_behavioral_metrics.intent_dispatched_count
        _rejections = self._cached_behavioral_metrics.strategy_rejections

        if _trades > 0:
            return SourceCheckResult(
                source="trade_activity", tier=Tier.HIGH,
                status=SourceStatus.PASS, primary_code="TRADE_ACTIVITY_OK",
                message=f"{_trades} trades dispatched — system is active",
                metrics={"trades": _trades, "rejections": _rejections},
                checked_at=_utc_iso(),
            )
        if _rejections > 0:
            return SourceCheckResult(
                source="trade_activity", tier=Tier.HIGH,
                status=SourceStatus.PASS,
                primary_code="TRADE_ACTIVITY_REJECTING",
                message=f"0 trades but {_rejections} rejections — system thinking, market unfavorable",
                metrics={"trades": 0, "rejections": _rejections},
                checked_at=_utc_iso(),
            )
        # Zero trades AND zero rejections → system may be dead
        _cycles = self._cached_behavioral_metrics.cycle_count
        _max_silent = int(self._t("trade_activity_max_silent_cycles"))
        if _cycles >= _max_silent:
            return SourceCheckResult(
                source="trade_activity",
                tier=Tier.HIGH,
                status=SourceStatus.FAIL,
                primary_code="TRADE_ACTIVITY_SILENT",
                message=(
                    f"0 trades AND 0 rejections across {_cycles} cycles — "
                    f"system may be stalled (main eval not running?)"
                ),
                metrics={"cycles_silent": _cycles, "max_silent": _max_silent},
                checked_at=_utc_iso(),
            )
        return SourceCheckResult(
            source="trade_activity", tier=Tier.HIGH,
            status=SourceStatus.PASS,
            primary_code="TRADE_ACTIVITY_SILENT_BUT_RECENT",
            message=f"0 trades in {_cycles} cycles, below alert threshold ({_max_silent})",
            metrics={"cycles_silent": _cycles, "max_silent": _max_silent},
            checked_at=_utc_iso(),
        )

    def build_alert_context(self, report: HealthReport) -> dict[str, Any]:
        """Convert report to alert context keys for external evaluation.

        This method produces data only — the caller (daily_ops / live_intent_loop)
        passes the dict to AlertService.evaluate().  DataHealthService itself
        never calls alert_hub or sends notifications (Iron Law #3).
        """
        return build_alert_context(report)
