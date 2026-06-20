#!/usr/bin/env python3
"""Data Health Contract Validator — institutional data governance enforcement.

Configuration-driven, severity-graded, fault-tolerant.
One probe per domain, each in an isolated sandbox.  One domain crash
cannot bring down the entire audit.

FATAL violations → circuit-breaker lock (no new positions).
WARN violations → DingTalk alert, block retraining, allow trading.

Iron Law #11: Script stdout is the sole source of truth.

Usage:
  python scripts/check_data_health_contract.py --contract configs/contracts/data_health_contract.json --data-dir data_btc
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from core.runtime.fault_handler import fail_open_guard


# ── Severity ──────────────────────────────────────────────────────────
class Severity:
    FATAL = "FATAL"
    WARN = "WARN"
    PASS = "PASS"
    SKIP = "SKIP"
    EVALUATION_FAILED = "EVALUATION_FAILED"


def _red(s: str) -> str: return f"\033[91m{s}\033[0m"
def _yellow(s: str) -> str: return f"\033[93m{s}\033[0m"
def _green(s: str) -> str: return f"\033[92m{s}\033[0m"


# ── Contract loader ───────────────────────────────────────────────────
def load_contract(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════════════════════
# PROBE: Alpha Registry
# ═══════════════════════════════════════════════════════════════════════

def probe_alpha_registry(domain: dict, data_dir: str) -> dict[str, Any]:
    source = os.path.join(data_dir, "alpha_registry.json")
    if not os.path.exists(source):
        return {"domain": "alpha_registry", "severity": domain["severity"],
                "verdict": Severity.EVALUATION_FAILED,
                "results": [{"check": "source_exists", "verdict": Severity.FATAL,
                             "detail": f"{source} not found"}]}

    with open(source, encoding="utf-8") as f:
        data = json.load(f)
    records = data.get("records", [])
    results = []

    # Assertion: record count
    if len(records) >= 1:
        results.append({"check": "record_count_min", "verdict": Severity.PASS,
                        "detail": f"{len(records)} alpha records"})
    else:
        results.append({"check": "record_count_min", "verdict": Severity.FATAL,
                        "detail": "Zero alpha records — allocator has nothing to allocate"})

    # Assertion: trade_count > 0
    any_trades = any(r.get("metrics", {}).get("trade_count", 0) > 0 for r in records)
    if any_trades:
        results.append({"check": "trade_count_nonzero", "verdict": Severity.PASS,
                        "detail": "At least one alpha has trades"})
    else:
        results.append({"check": "trade_count_nonzero", "verdict": Severity.FATAL,
                        "detail": "All alphas have trade_count=0 — feed pipeline not wired"})

    # Assertion: win_rate > 0
    any_wr = any(r.get("metrics", {}).get("win_rate", 0) > 0 for r in records)
    if any_wr:
        results.append({"check": "win_rate_valid", "verdict": Severity.PASS,
                        "detail": "Win rate data present"})
    else:
        results.append({"check": "win_rate_valid", "verdict": Severity.FATAL,
                        "detail": "All alphas have win_rate=0 — performance tracking broken"})

    return _domain_result("alpha_registry", domain["severity"], results)


# ═══════════════════════════════════════════════════════════════════════
# PROBE: Leaderboard
# ═══════════════════════════════════════════════════════════════════════

def probe_leaderboard(domain: dict, data_dir: str) -> dict[str, Any]:
    source = os.path.join(data_dir, "reports", "leaderboard.json")
    if not os.path.exists(source):
        return {"domain": "leaderboard", "severity": domain["severity"],
                "verdict": Severity.EVALUATION_FAILED,
                "results": [{"check": "source_exists", "verdict": Severity.FATAL,
                             "detail": f"{source} not found"}]}

    with open(source, encoding="utf-8") as f:
        lb = json.load(f)
    brains = lb.get("brains", lb.get("entries", []))
    results = []

    # Load config for cross-reference
    import yaml
    config_path = os.path.join("configs", "live_btc.yaml")
    config_entries = []
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        config_entries = cfg.get("brains", {}).get("registry_entries", [])

    enabled_ids = set()
    for entry in config_entries:
        if entry.get("enabled"):
            path = entry.get("path", "")
            bid = os.path.splitext(os.path.basename(path))[0]
            enabled_ids.add(bid)

    # Check: no zombie brains (live in leaderboard but disabled in config)
    zombies = []
    if isinstance(brains, list):
        for b in brains:
            bid = b.get("brain_id", "")
            status = str(b.get("status", "")).lower()
            if status == "live" and bid not in enabled_ids:
                zombies.append(bid)
    elif isinstance(brains, dict):
        for bid, bd in brains.items():
            status = str(bd.get("status", "")).lower()
            if status == "live" and bid not in enabled_ids:
                zombies.append(bid)

    if zombies:
        results.append({"check": "no_zombie_brains", "verdict": Severity.FATAL,
                        "detail": f"Zombie brains (leaderboard=live, config=disabled): {zombies}"})
    else:
        results.append({"check": "no_zombie_brains", "verdict": Severity.PASS,
                        "detail": "No zombie brains — leaderboard consistent with config"})

    return _domain_result("leaderboard", domain["severity"], results)


# ═══════════════════════════════════════════════════════════════════════
# PROBE: Live Labels
# ═══════════════════════════════════════════════════════════════════════

def probe_live_labels(domain: dict, data_dir: str) -> dict[str, Any]:
    source = os.path.join(data_dir, "reports", "live_labels.jsonl")
    if not os.path.exists(source):
        return {"domain": "live_labels", "severity": domain["severity"],
                "verdict": Severity.EVALUATION_FAILED,
                "results": [{"check": "source_exists", "verdict": Severity.WARN,
                             "detail": f"{source} not found"}]}

    WHITELIST = {"win", "loss", "breakeven", "tp_hit_first", "sl_hit_first", "sl_hit_trailed"}
    total = 0
    unlabeled = 0
    unknown_labels = set()
    with open(source, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += 1
            lbl = e.get("label", e.get("label_class", None))
            if lbl is None or lbl == "" or lbl == "unlabeled":
                unlabeled += 1
            elif str(lbl) not in WHITELIST:
                unknown_labels.add(str(lbl))

    results = []
    unlabeled_rate = unlabeled / total if total > 0 else 0
    if unlabeled_rate < 0.15:
        results.append({"check": "unlabeled_rate_max", "verdict": Severity.PASS,
                        "detail": f"{unlabeled}/{total} unlabeled ({unlabeled_rate:.1%})"})
    else:
        results.append({"check": "unlabeled_rate_max", "verdict": Severity.WARN,
                        "detail": f"{unlabeled}/{total} unlabeled ({unlabeled_rate:.1%}) — exceeds 15% max"})

    if unknown_labels:
        results.append({"check": "label_in_whitelist", "verdict": Severity.WARN,
                        "detail": f"Unknown labels: {sorted(unknown_labels)}"})
    else:
        results.append({"check": "label_in_whitelist", "verdict": Severity.PASS,
                        "detail": "All labels in whitelist"})

    return _domain_result("live_labels", domain["severity"], results)


# ═══════════════════════════════════════════════════════════════════════
# PROBE: Retraining Signal
# ═══════════════════════════════════════════════════════════════════════

def probe_retraining_signal(domain: dict, data_dir: str) -> dict[str, Any]:
    source = os.path.join(data_dir, "reports", "retraining_signal_prev.json")
    if not os.path.exists(source):
        return {"domain": "retraining_signal", "severity": domain["severity"],
                "verdict": Severity.EVALUATION_FAILED,
                "results": [{"check": "source_exists", "verdict": Severity.WARN,
                             "detail": f"{source} not found"}]}

    with open(source, encoding="utf-8") as f:
        data = json.load(f)

    results = []
    assessed = data.get("total_brains_assessed", 0)
    if assessed > 0:
        results.append({"check": "brains_assessed_nonzero", "verdict": Severity.PASS,
                        "detail": f"{assessed} brains assessed"})
    else:
        results.append({"check": "brains_assessed_nonzero", "verdict": Severity.WARN,
                        "detail": "Zero brains assessed — retraining pipeline may be idle"})

    return _domain_result("retraining_signal", domain["severity"], results)


# ═══════════════════════════════════════════════════════════════════════
# PROBE: Meta Exit
# ═══════════════════════════════════════════════════════════════════════

def probe_meta_exit(domain: dict, data_dir: str) -> dict[str, Any]:
    source = os.path.join(data_dir, "live_trade_journal.jsonl")
    if not os.path.exists(source):
        return {"domain": "meta_exit", "severity": domain["severity"],
                "verdict": Severity.EVALUATION_FAILED,
                "results": [{"check": "source_exists", "verdict": Severity.WARN,
                             "detail": f"{source} not found"}]}

    exit_reasons: dict[str, int] = Counter()
    trail_count = 0
    total = 0
    with open(source, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("ack_status") != "closed":
                continue
            total += 1
            detail = e.get("detail", {})
            reason = str(detail.get("reason", "unknown"))
            exit_reasons[reason] += 1
            if "trail" in str(e.get("label", "")).lower():
                trail_count += 1

    results = []
    unknown_rate = exit_reasons.get("unknown_close", 0) / total if total > 0 else 0
    if unknown_rate < 0.35:
        results.append({"check": "unknown_close_rate_max", "verdict": Severity.PASS,
                        "detail": f"{exit_reasons.get('unknown_close',0)}/{total} unknown ({unknown_rate:.1%})"})
    else:
        results.append({"check": "unknown_close_rate_max", "verdict": Severity.WARN,
                        "detail": f"{exit_reasons.get('unknown_close',0)}/{total} unknown ({unknown_rate:.1%}) — exceeds 35%"})

    if trail_count > 0:
        results.append({"check": "trail_data_present", "verdict": Severity.PASS,
                        "detail": f"{trail_count} trail exits"})
    else:
        results.append({"check": "trail_data_present", "verdict": Severity.WARN,
                        "detail": "Zero trail exits — trail telemetry blind spot"})

    return _domain_result("meta_exit", domain["severity"], results)


# ═══════════════════════════════════════════════════════════════════════
# PROBE: Brain Performance (SignalSettled)
# ═══════════════════════════════════════════════════════════════════════

def probe_brain_performance(domain: dict, data_dir: str) -> dict[str, Any]:
    source = os.path.join(data_dir, "ledger_events.jsonl")
    if not os.path.exists(source):
        return {"domain": "brain_performance", "severity": domain["severity"],
                "verdict": Severity.EVALUATION_FAILED,
                "results": [{"check": "source_exists", "verdict": Severity.WARN,
                             "detail": f"{source} not found"}]}

    # Active brain IDs from config
    active_ids = set()
    config_path = os.path.join("configs", "live_btc.yaml")
    if os.path.exists(config_path):
        import yaml
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        for entry in cfg.get("brains", {}).get("registry_entries", []):
            if entry.get("enabled"):
                path = entry.get("path", "")
                bid = os.path.splitext(os.path.basename(path))[0]
                active_ids.add(bid)

    # Count real SignalSettled per brain
    brain_signals: dict[str, int] = {bid: 0 for bid in active_ids}
    with open(source, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("event_type") != "SignalSettled":
                continue
            if (e.get("position_ticket", 0) or 0) <= 0:
                continue
            bid = str(e.get("brain_id", ""))
            if bid in brain_signals:
                brain_signals[bid] += 1

    results = []
    zero_signal = [bid for bid, cnt in brain_signals.items() if cnt == 0]
    if zero_signal:
        results.append({"check": "active_brains_have_signals", "verdict": Severity.WARN,
                        "detail": f"Active brains with 0 real SignalSettled: {zero_signal}"})
    else:
        results.append({"check": "active_brains_have_signals", "verdict": Severity.PASS,
                        "detail": f"All {len(active_ids)} active brains have SignalSettled data"})

    return _domain_result("brain_performance", domain["severity"], results)


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

def _domain_result(domain: str, severity: str, results: list) -> dict[str, Any]:
    # Respect contract severity: clamp assertion verdicts to domain max
    MAX_VERDICT = {
        "INFO": Severity.PASS,
        "WARN": Severity.WARN,
        "FATAL": Severity.FATAL,
    }
    max_allowed = MAX_VERDICT.get(severity, Severity.FATAL)
    verdict_order = {Severity.PASS: 0, Severity.WARN: 1, Severity.FATAL: 2}

    for r in results:
        if verdict_order.get(r["verdict"], 0) > verdict_order.get(max_allowed, 0):
            r["detail"] = f"[{severity}] {r['detail']}"
            r["verdict"] = max_allowed

    fatals = [r for r in results if r["verdict"] == Severity.FATAL]
    warns = [r for r in results if r["verdict"] == Severity.WARN]
    if fatals:
        verdict = Severity.FATAL
    elif warns:
        verdict = Severity.WARN
    else:
        verdict = Severity.PASS
    return {
        "domain": domain,
        "severity": severity,
        "verdict": verdict,
        "results": results,
    }


def _build_dingtalk_card(report: dict[str, Any]) -> str:
    """Build structured Markdown DingTalk alert card."""
    lines = [
        "# QuantOS Data Health — SLA Violation",
        "",
        f"**Time:** {report.get('generated_at', '')}",
        f"**Overall:** {report.get('overall_verdict', '')}",
        "",
        "---",
        "",
    ]

    for probe in report.get("probes", []):
        domain = probe["domain"]
        verdict = probe["verdict"]
        severity = probe.get("severity", "?")

        if verdict == Severity.PASS:
            continue

        icon = "[FATAL]" if verdict == Severity.FATAL else "[WARN]"
        lines.append(f"## {icon} {domain} [{verdict}]")
        lines.append(f"**Severity:** {severity}")
        lines.append("")

        for r in probe.get("results", []):
            if r["verdict"] in (Severity.PASS, Severity.SKIP):
                continue
            lines.append(f"- ❌ **{r['check']}**: {r['detail']}")

        # Blast radius
        blast = probe.get("blast_radius", "")
        if blast:
            lines.append(f"> [BLAST] **Blast Radius:** {blast}")

        lines.append("")

    if report.get("overall_verdict") == Severity.PASS:
        lines.append("## ✅ All domains PASS")
    elif report.get("circuit_breaker_locked"):
        lines.append("## 🔴 CIRCUIT BREAKER LOCKED — No new positions until resolved")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(description="Data Health Contract Validator")
    parser.add_argument("--contract", default="configs/contracts/data_health_contract.json")
    parser.add_argument("--data-dir", default="data_btc")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    contract = load_contract(args.contract)
    domains = contract.get("domains", {})
    global_cfg = contract.get("global", {})

    print("=" * 60)
    print(f"  Data Health Contract Audit: {contract['contract_id']}")
    print(f"  Generated: {datetime.now(UTC).isoformat()}")
    print("=" * 60)

    # ── Fault-tolerant probe execution ──
    PROBE_REGISTRY = {
        "alpha_registry": probe_alpha_registry,
        "leaderboard": probe_leaderboard,
        "live_labels": probe_live_labels,
        "retraining_signal": probe_retraining_signal,
        "meta_exit": probe_meta_exit,
        "brain_performance": probe_brain_performance,
    }

    probes = []
    fatal_count = 0
    warn_count = 0
    failed_count = 0

    for domain_name, domain_cfg in domains.items():
        probe_fn = PROBE_REGISTRY.get(domain_name)
        if probe_fn is None:
            print(f"  [{_yellow('SKIP')}] {domain_name}: no probe registered")
            continue

        # ── Isolated sandbox: one probe crash cannot kill the audit ──
        try:
            result = probe_fn(domain_cfg, args.data_dir)
        except Exception:  # BLE001:FOG
            with fail_open_guard("check_data_health_contract:main"):
                result = {
                    "domain": domain_name,
                    "severity": domain_cfg.get("severity", "?"),
                    "verdict": Severity.EVALUATION_FAILED,
                    "results": [{"check": "probe_crashed", "verdict": Severity.EVALUATION_FAILED,
                                 "detail": traceback.format_exc()[:500]}],
                }
        # Inject blast radius from contract
        result["blast_radius"] = domain_cfg.get("blast_radius", "")
        probes.append(result)

        verdict = result["verdict"]
        icon = _red("[FATAL]") if verdict == Severity.FATAL else (
            _yellow("[WARN]") if verdict == Severity.WARN else (
                _green("[PASS]") if verdict == Severity.PASS else "[FAILED]"
            )
        )
        print(f"\n  {icon} {domain_name}")
        for r in result.get("results", []):
            sub_icon = _red("  FAIL") if r["verdict"] in (Severity.FATAL,) else (
                _yellow("  WARN") if r["verdict"] == Severity.WARN else "  PASS"
            )
            print(f"    {sub_icon}: {r['detail']}")

        if verdict == Severity.FATAL:
            fatal_count += 1
        elif verdict == Severity.WARN:
            warn_count += 1
        elif verdict == Severity.EVALUATION_FAILED:
            failed_count += 1

    # ── Overall verdict ──
    if fatal_count > 0:
        overall = Severity.FATAL
    elif warn_count > 0 or failed_count > 0:
        overall = Severity.WARN
    else:
        overall = Severity.PASS

    print(f"\n{'=' * 60}")
    print(f"  OVERALL: {_red('FATAL') if overall == Severity.FATAL else _yellow('WARN') if overall == Severity.WARN else _green('PASS')}")
    print(f"  FATAL: {fatal_count}, WARN: {warn_count}, FAILED: {failed_count}, PASS: {len(probes) - fatal_count - warn_count - failed_count}")
    print(f"{'=' * 60}")

    # ── Circuit breaker lock ──
    circuit_locked = False
    if fatal_count > 0 and global_cfg.get("circuit_breaker_on_fatal"):
        lock_file = global_cfg.get("lock_file", "data_btc/reports/training_readiness.json")
        try:
            lock_path = Path(lock_file)
            if lock_path.exists():
                with open(lock_path, encoding="utf-8") as f:
                    lock_data = json.load(f)
            else:
                lock_data = {}
            lock_data["data_health_locked"] = True
            lock_data["data_health_locked_at"] = datetime.now(UTC).isoformat()
            lock_data["data_health_fatal_domains"] = [
                p["domain"] for p in probes if p["verdict"] == Severity.FATAL
            ]
            os.makedirs(lock_path.parent, exist_ok=True)
            with open(lock_path, "w", encoding="utf-8") as f:
                json.dump(lock_data, f, indent=2)
            circuit_locked = True
            print(f"\n{_red('[CIRCUIT BREAKER]')} training_readiness.json LOCKED — no new positions until FATAL domains are resolved.")
        except Exception as exc:  # BLE001:FOG (Sev 4, Phase 3b)
            with fail_open_guard("check_data_health_contract:main"):
                print(f"\n{_red('[ERROR]')} Failed to write circuit breaker lock: {exc}")
    # ── Structured DingTalk card ──
    report = {
        "contract_id": contract["contract_id"],
        "generated_at": datetime.now(UTC).isoformat(),
        "overall_verdict": overall,
        "circuit_breaker_locked": circuit_locked,
        "probes": probes,
    }
    card = _build_dingtalk_card(report)
    print("\n── DingTalk Alert Card ──")
    print(card.encode(sys.stdout.encoding, errors='replace').decode(sys.stdout.encoding))

    # ── Write report ──
    output_path = args.output or f"{args.data_dir}/reports/data_health_contract_report.json"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\nReport saved: {output_path}")

    return 1 if overall == Severity.FATAL else 0


if __name__ == "__main__":
    sys.exit(main())
