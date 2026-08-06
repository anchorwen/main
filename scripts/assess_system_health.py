"""
Omega Institutional System Architecture Health Assessment
Iron Law #11 compliant — all statistics from script stdout.

Evaluates 8 dimensions against A-grade targets.
"""

import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

print("=" * 70)
print("Omega Institutional System Architecture & Module Health Assessment")
print(f"Date: {datetime.now().isoformat()}")
print("Weekend status: XAU CLOSED, BTC 24h TRADING")
print("=" * 70)

# ── Dimension 1: Code Architecture ──
print("\n## D1: Code Architecture Quality\n")

core_py = list(Path("core").rglob("*.py"))
sizes = []
for f in core_py:
    try:
        lines = len(f.read_text(encoding="utf-8").splitlines())
        sizes.append((str(f), lines))
    except (
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
        OSError,
    ):  # BLE001:FOG (audit script, fail-safe read)
        pass
sizes.sort(key=lambda x: -x[1])

print("Top 15 largest modules:")
for path, lines in sizes[:15]:
    bar = "#" * (lines // 200)
    print(f"  {lines:5d}  {path}  {bar}")

total_core = sum(s for _, s in sizes)
file_count = len(sizes)
avg_size = total_core / file_count if file_count else 0
print(
    f"\n  Core/ total: {total_core:,} lines across {file_count} files, avg {avg_size:.0f} lines/file"
)

monoliths = [(p, s) for p, s in sizes if s > 1000]
print(f"  Monoliths (>1000 lines): {len(monoliths)}")
for p, s in monoliths:
    print(f"    {p}: {s}")

new_modules = [
    "management_phase.py",
    "trail_dispatch.py",
    "modify_trail_dispatch.py",
    "mia_close.py",
    "ou_hurst.py",
    "cooldown.py",
    "pre_close_check.py",
    "timeframe_scaling.py",
    "strategy_config_validator.py",
    "position_ownership.py",
    "trend_volume_guard.py",
    "net_out_close_handler.py",
]
extracted = [(p, s) for p, s in sizes if any(n in p for n in new_modules)]
print(f"\n  Extracted modules (Strangler Fig): {len(extracted)}")
for p, s in extracted:
    print(f"    {p}: {s} lines")

# ── Dimension 2: Test Coverage ──
print("\n## D2: Test Coverage\n")

cov_path = Path("coverage.json")
if cov_path.exists():
    cov = json.loads(cov_path.read_text(encoding="utf-8"))
    zero_cov = []
    low_cov = []
    for filepath, data in cov.get("files", {}).items():
        if filepath.startswith("core"):
            stmts = data.get("summary", {}).get("num_statements", 0)
            covered = data.get("summary", {}).get("covered_lines", 0)
            if stmts > 20 and covered == 0:
                zero_cov.append((filepath, stmts))
            elif stmts > 20 and covered / max(stmts, 1) < 0.2:
                low_cov.append((filepath, stmts, covered / max(stmts, 1)))

    print(f"  Zero-coverage files (>20 stmts): {len(zero_cov)}")
    for fp, stmts in sorted(zero_cov, key=lambda x: -x[1]):
        print(f"    {stmts:4d} stmts  {fp}")

    total_stmts = sum(
        d.get("summary", {}).get("num_statements", 0) for d in cov.get("files", {}).values()
    )
    total_covered = sum(
        d.get("summary", {}).get("covered_lines", 0) for d in cov.get("files", {}).values()
    )
    pct = total_covered / max(total_stmts, 1) * 100
    print(f"\n  Overall coverage: {total_covered:,}/{total_stmts:,} = {pct:.1f}%")

test_files = list(Path("tests").rglob("test_*.py"))
print(f"  Test files: {len(test_files)}")

# ── Dimension 3: Type Safety ──
print("\n## D3: Type Safety (Mypy)\n")
baseline = json.loads(Path("mypy_baseline.json").read_text(encoding="utf-8"))
core_errs = {k: v for k, v in baseline.items() if k.startswith("core")}
scripts_errs = {k: v for k, v in baseline.items() if k.startswith("scripts")}
total_core = sum(core_errs.values())
total_scripts = sum(scripts_errs.values())
print(f"  core/ errors: {total_core} across {len(core_errs)} files")
print(f"  scripts/ errors: {total_scripts} across {len(scripts_errs)} files")
print(f"  Total: {total_core + total_scripts} errors across {len(baseline)} files")
if core_errs:
    for k, v in sorted(core_errs.items(), key=lambda x: -x[1]):
        print(f"    {v:4d}  {k}")

# ── Dimension 4: BLE001 Governance ──
print("\n## D4: Exception Governance (BLE001)\n")

ble001_stats = {"REVIEWED": 0, "FOG": 0, "AUDITED": 0, "UNREVIEWED": 0}
all_py = []
for d in ["core", "scripts", "apps"]:
    for root, _dirs, files in os.walk(d):
        for fname in files:
            if fname.endswith(".py"):
                all_py.append(os.path.join(root, fname))

total_excepts = 0
for fp in all_py:
    try:
        content = Path(fp).read_text(encoding="utf-8")
        for line in content.splitlines():
            if re.match(r"\s*except\s+Exception", line):
                total_excepts += 1
                if "BLE001:REVIEWED" in line:
                    ble001_stats["REVIEWED"] += 1
                elif (
                    "BLE001:FOG" in line or "fail_open_guard" in line or "log_and_continue" in line
                ):
                    ble001_stats["FOG"] += 1
                elif "BLE001:AUDITED" in line:
                    ble001_stats["AUDITED"] += 1
                else:
                    ble001_stats["UNREVIEWED"] += 1
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG (audit script)
        pass

print(f"  Total bare excepts (core + scripts + apps): {total_excepts}")
for cat, count in ble001_stats.items():
    pct = count / max(total_excepts, 1) * 100
    print(f"  {cat}: {count} ({pct:.1f}%)")

# ── Dimension 5: Module Coupling ──
print("\n## D5: Module Coupling\n")

large_files = [
    "core/runtime/live_cycle.py",
    "core/runtime/management_phase.py",
    "scripts/live_intent_loop.py",
    "core/execution/strategy_line.py",
]

for fp in large_files:
    if Path(fp).exists():
        content = Path(fp).read_text(encoding="utf-8")
        imports = [
            l
            for l in content.splitlines()
            if l.strip().startswith("from core.") or l.strip().startswith("import core.")
        ]
        modules = set()
        for imp in imports:
            parts = imp.split()
            if "from" in imp:
                mod = parts[1]
                # Get top-level module group
                if mod.startswith("core."):
                    mod = mod.split(".")[1]
                modules.add(mod)
        print(f"  {fp}: {len(imports)} imports from {len(modules)} module groups")

# ── Dimension 6: Blueprint Coverage ──
print("\n## D6: Blueprint Coverage\n")
blueprint_dir = Path("blueprints/modules")
if blueprint_dir.exists():
    bps = list(blueprint_dir.glob("*.md"))
    print(f"  Module blueprints: {len(bps)}")
    bp_count = len(list(Path("blueprints").rglob("*.md")))
    print(f"  Total blueprint documents: {bp_count}")

# ── Dimension 7: Runtime Health ──
print("\n## D7: Runtime Health (Live Systems)\n")
for symbol, data_dir in [("XAU", "data"), ("BTC", "data_btc")]:
    market_status = "CLOSED (weekend)" if symbol == "XAU" else "24h TRADING"
    print(f"  [{symbol}] {market_status}")

    gv_path = Path(data_dir) / "governance_state.json"
    if gv_path.exists():
        gv = json.loads(gv_path.read_text(encoding="utf-8"))
        brains = gv.get("brain_states", {})
        states: Counter[str] = Counter()
        for _bid, bs in brains.items():
            if isinstance(bs, dict):
                states[bs.get("status", "unknown")] += 1
        print(f"    Governance: {dict(states)}")

    health_path = Path(data_dir) / "reports" / "mt5_bridge_health.json"
    if health_path.exists():
        h = json.loads(health_path.read_text(encoding="utf-8"))
        mt5_ok = h.get("mt5_connected", False)
        outbox = h.get("outbox_pending", 0)
        hb = h.get("last_heartbeat_utc", "unknown")
        print(f"    Bridge: MT5={mt5_ok}, outbox={outbox}, hb={hb}")

    snap_path = Path(data_dir) / "position_snapshots.jsonl"
    if snap_path.exists():
        snaps = snap_path.read_text(encoding="utf-8").strip().split("\n")
        open_pos = 0
        for snap_line in snaps:
            if snap_line.strip():
                try:
                    snap = json.loads(snap_line)
                    if snap.get("state") == "open":
                        open_pos += 1
                except (
                    RuntimeError,
                    ValueError,
                    KeyError,
                    TypeError,
                    OSError,
                ):  # BLE001:FOG (audit script)
                    pass
        print(f"    Open positions: {open_pos}")

    alert_path = Path(data_dir) / "logs" / "alert_audit.jsonl"
    if alert_path.exists():
        alerts = alert_path.read_text(encoding="utf-8").strip().split("\n")
        sev1_sev2 = 0
        for a in alerts:
            if a.strip():
                try:
                    alert = json.loads(a)
                    sev = str(alert.get("severity", ""))
                    ts = str(alert.get("timestamp", ""))
                    if ("Sev 1" in sev or "Sev 2" in sev) and "2026-06-20" in ts:
                        sev1_sev2 += 1
                except (
                    RuntimeError,
                    ValueError,
                    KeyError,
                    TypeError,
                    OSError,
                ):  # BLE001:FOG (audit script)
                    pass
        print(f"    Today Sev 1/2 alerts: {sev1_sev2}")
    print()

# ── Dimension 8: System Completeness ──
print("## D8: System Completeness Indicators\n")
iron_laws = (
    Path("CLAUDE.md").read_text(encoding="utf-8").count("Iron Law #")
    if Path("CLAUDE.md").exists()
    else 0
)
print(f"  Iron Laws defined: {iron_laws}")
memory_count = len(list((Path.home() / ".claude/projects/d--future/memory").glob("*.md")))
print(f"  Memory files: {memory_count}")

print("\n" + "=" * 70)
print("ASSESSMENT COMPLETE")
print("=" * 70)
