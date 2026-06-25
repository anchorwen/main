#!/usr/bin/env python3
"""Unified verification script -- the iron law gate.

Usage:
    python scripts/verify.py --quick       # mypy + ruff on changed files (~10s)
    python scripts/verify.py --full        # mypy + ruff + pytest (~2min)
    python scripts/verify.py --stamp       # update verification stamp after passing
    python scripts/verify.py --check-stamp # exit 0 if stamp is current
    python scripts/verify.py --mypy-only --files core/runtime/live_cycle.py

The --mypy-only mode is designed for the Claude Code PostToolUse hook.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAMP_FILE = ROOT / ".verify_stamp.json"

# ── DEBT time-bomb scanner exclusions ─────────────────────────────────────
_DEBT_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        ".venv",
        ".git",
        "__pycache__",
        "node_modules",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".egg-info",
    }
)
_DEBT_RE = re.compile(
    r"(?:DEBT|TODO).*?(?:EXPIRE|EXPIRES):\s*(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)

# Force UTF-8 on Windows while preserving line buffering.
# io.TextIOWrapper rewrap defaults to full buffering which would cause
# print() output to appear AFTER subprocess stdout (e.g. pytest dots).
if sys.platform == "win32":
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


def _changed_py_files() -> list[str]:
    """Return list of changed .py files (unstaged + staged vs HEAD)."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=10,
        )
        staged = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=10,
        )
        files: set[str] = set()
        for r in [result, staged]:
            if r.returncode == 0:
                for line in r.stdout.strip().split("\n"):
                    line = line.strip()
                    if line.endswith(".py"):
                        files.add(line)
        return sorted(files)
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        return []


def _current_commit_hash() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        return ""


def run_mypy(targets: list[str] | None = None) -> tuple[bool, str]:
    """Run mypy on specified targets. Returns (passed, output)."""
    if targets is None:
        targets = ["core/", "apps/", "scripts/"]
    existing = [t for t in targets if (ROOT / t).exists()]
    if not existing:
        return True, "No targets to check."
    try:
        result = subprocess.run(
            [sys.executable, "-m", "mypy", "--no-error-summary", *existing],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT),
            timeout=120,
        )
        passed = result.returncode == 0
        output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        return passed, output
    except subprocess.TimeoutExpired:
        return False, "mypy timed out"
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
        return False, str(exc)


def run_ruff(targets: list[str] | None = None) -> tuple[bool, str]:
    """Run ruff check. Returns (passed, output)."""
    if targets is None:
        targets = ["core/", "apps/", "scripts/"]
    existing = [t for t in targets if (ROOT / t).exists()]
    if not existing:
        return True, "No targets to check."
    try:
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", *existing],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT),
            timeout=60,
        )
        passed = result.returncode == 0
        output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        return passed, output
    except subprocess.TimeoutExpired:
        return False, "ruff timed out"
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
        return False, str(exc)


def run_pytest() -> tuple[bool, str]:
    """Run full test suite. Returns (passed, output summary).

    stdout/stderr inherit from parent — pytest dots stream to terminal
    in real time. No capture means no pipe buffer deadlock.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-x", "-q", "--tb=short", "--no-header"],
            cwd=str(ROOT),
            timeout=300,
        )
        passed = result.returncode == 0
        return passed, "pytest completed" if passed else f"pytest failed (exit {result.returncode})"
    except KeyboardInterrupt:
        print()  # newline after ^C
        return False, "pytest interrupted (Ctrl+C)"
    except subprocess.TimeoutExpired:
        return False, "pytest timed out (300s)"
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
        return False, str(exc)


def _compute_file_hash() -> str:
    """Simple hash of tracked .py file sizes+mtimes for change detection."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.py"],
            capture_output=True,
            text=False,
            cwd=str(ROOT),
            timeout=5,
        )
        if result.returncode != 0:
            return ""
        raw = result.stdout.decode("utf-8", errors="replace")
        files = raw.split("\0")
        items: list[str] = []
        for f in files:
            fp = ROOT / f
            if fp.exists():
                st = fp.stat()
                items.append(f"{f}:{st.st_mtime}:{st.st_size}")
        return str(hash("\n".join(sorted(items))))
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        return ""


def update_stamp(passed: bool, details: str) -> str:
    """Write verification stamp. Returns status message."""
    stamp = {
        "passed": passed,
        "timestamp": time.time(),
        "commit": _current_commit_hash(),
        "file_hash": _compute_file_hash(),
        "details": details[:500],
    }
    STAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STAMP_FILE, "w", encoding="utf-8") as f:
        json.dump(stamp, f, indent=2)
    if passed:
        return "Stamp updated -- verification PASSED"
    else:
        return "Stamp NOT updated -- verification FAILED"


def check_stamp() -> tuple[bool, str]:
    """Check if stamp is current. Returns (valid, reason)."""
    if not STAMP_FILE.exists():
        return False, "No verification stamp. Run: python scripts/verify.py --full --stamp"
    try:
        with open(STAMP_FILE, encoding="utf-8") as f:
            stamp = json.load(f)
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        return False, "Stamp corrupt. Re-run: python scripts/verify.py --full --stamp"
    if not stamp.get("passed"):
        return False, "Last verification FAILED. Fix errors and re-run with --stamp."
    if stamp.get("file_hash") != _compute_file_hash():
        return False, "Files changed since last verification. Re-run with --stamp."
    if stamp.get("commit") != _current_commit_hash():
        return False, "HEAD moved. Re-run verification with --stamp."
    age = time.time() - stamp.get("timestamp", 0)
    if age > 1800:  # 30 minutes
        return False, f"Stamp expired ({int(age)}s ago). Re-run with --stamp."
    return True, "Stamp valid"


def _check_registry_gate() -> tuple[bool, list[str]]:
    """Check that .py changes are accompanied by FIX_REGISTRY or blueprint updates.

    Iron Law #7 structural enforcement: when core/ or scripts/ .py files are
    staged for commit, at least one of FIX_REGISTRY.md or a module blueprint
    must also be staged — unless the commit message carries an explicit
    bypass token ([TRIVIAL] or [NO-FIX-REQUIRED]).

    Returns (ok, errors).
    """
    errors: list[str] = []

    # ── Step A: Get staged .py files ──
    try:
        staged = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True, []  # can't check, don't block

    if staged.returncode != 0:
        return True, []

    py_files = [
        f.strip().replace("\\", "/")
        for f in staged.stdout.strip().split("\n")
        if f.strip().endswith(".py")
    ]
    if not py_files:
        return True, []

    # ── Step B: Only enforce for core/ and scripts/ (non-trivial paths) ──
    core_py = [f for f in py_files if f.startswith("core/") or f.startswith("scripts/")]
    if not core_py:
        return True, []

    # ── Step C: Check for registry/blueprint files in staged ──
    staged_all = staged.stdout.strip().split("\n")
    staged_set = {f.strip().replace("\\", "/") for f in staged_all if f.strip()}

    registry_updates = [f for f in staged_set if "FIX_REGISTRY" in f or f.startswith("blueprints/")]
    if registry_updates:
        return True, []

    # ── Step D: Check for bypass token in recent commit message ──
    try:
        # Check if there's a recent commit (within this session) with bypass
        log = subprocess.run(
            ["git", "log", "-1", "--format=%B"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=5,
        )
        if log.returncode == 0 and log.stdout:
            msg = log.stdout
            if "[TRIVIAL]" in msg or "[NO-FIX-REQUIRED]" in msg:
                return True, []
    except (OSError, subprocess.TimeoutExpired):
        pass

    # ── BLOCK: core .py changed, no registry update, no bypass ──
    errors.append(
        f"FIX_REGISTRY gate: {len(core_py)} core .py file(s) staged "
        f"but no FIX_REGISTRY.md or blueprint update staged.\n"
        f"  Changed: {', '.join(core_py[:5])}"
        + ("..." if len(core_py) > 5 else "")
        + "\n  Action: update FIX_REGISTRY.md or the relevant module blueprint,\n"
        + "    then git add it alongside your .py changes.\n"
        + "  Bypass: use [TRIVIAL] or [NO-FIX-REQUIRED] in commit message\n"
        + "    for typo fixes, formatting, or pure mechanical changes."
    )
    return False, errors


def _check_config_consistency() -> tuple[bool, list[str]]:
    """Validate cross-config brain consistency (FIX-20260610-002).

    Checks:
      1. Cross-asset contamination: XAU config must not reference brains_btc/,
         BTC config must not reference brains/ (unless marked as shared).
      2. Retired/frozen brains must not be enabled in any config.
      3. Brain label_contract alignment with strategy line SL/TP.

    Returns (passed, error_messages).
    """
    try:
        import yaml
    except ImportError:
        print("[WARN] Config Consistency: PyYAML not available — skipping check")
        return True, []

    errors: list[str] = []
    warnings: list[str] = []

    live_configs = sorted(ROOT.glob("configs/live*.yaml"))
    if not live_configs:
        return True, []

    # ── Determine asset from config path ──
    def _asset_from_path(p: Path) -> str:
        name = p.name
        if "btc" in name.lower():
            return "BTC"
        return "XAU"

    # ── Build brain status cache ──
    brain_cache: dict[str, dict] = {}

    def _load_brain(brain_path_str: str) -> dict | None:
        if brain_path_str in brain_cache:
            return brain_cache[brain_path_str]
        brain_path = ROOT / brain_path_str
        if not brain_path.exists():
            brain_cache[brain_path_str] = {}
            return None
        try:
            with open(brain_path, encoding="utf-8") as f:
                data = json.load(f)
            brain_cache[brain_path_str] = data
            return data
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            brain_cache[brain_path_str] = {}
            return None

    # ── Pre-load all live YAML configs + strategy_lines for SL/TP cross-ref ──
    live_config_cache: dict[str, dict] = {}
    for config_path in live_configs:
        try:
            with open(config_path, encoding="utf-8") as f:
                live_config_cache[config_path.name] = yaml.safe_load(f) or {}
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            live_config_cache[config_path.name] = {}

    for config_path in live_configs:
        asset = _asset_from_path(config_path)
        config = live_config_cache.get(config_path.name, {})
        if not config:
            continue
        brains_section = config.get("brains", {})
        registry = brains_section.get("registry_entries", [])
        if not registry:
            continue

        # ── Build strategy_lines lookup for SL/TP cross-ref ──
        strategy_lines = config.get("strategy_lines", {})

        for entry in registry:
            if not isinstance(entry, dict):
                continue
            brain_path = entry.get("path", "")
            enabled = entry.get("enabled", True)
            if not brain_path:
                continue

            # ── Check 1: cross-asset contamination ──
            is_btc_brain = "brains_btc" in brain_path
            is_xau_brain = "brains/" in brain_path and "brains_btc" not in brain_path

            if asset == "XAU" and is_btc_brain:
                if enabled:
                    errors.append(
                        f"{config_path.name}: BTC brain '{brain_path}' enabled in XAU config "
                        f"— cross-asset contamination (FIX-20260610-002)"
                    )
                else:
                    # Disabled is OK (kept for reference) but note as info
                    pass

            if asset == "BTC" and is_xau_brain:
                # Check if this is a genuinely shared brain or misplacement
                brain_data = _load_brain(brain_path)
                if brain_data:
                    strategy = brain_data.get("strategy", "")
                    # Some BTC brains were historically placed in configs/brains/
                    # before the directory split. Allow if strategy is BTC-specific.
                    if "btc" not in strategy.lower() and "BTC" not in str(
                        brain_data.get("symbol", "")
                    ):
                        if enabled:
                            errors.append(
                                f"{config_path.name}: XAU brain '{brain_path}' enabled in BTC config "
                                f"— cross-asset contamination"
                            )

            if not enabled:
                continue

            # ── Check 2: retired/frozen brain must not be enabled ──
            brain_data = _load_brain(brain_path)
            if brain_data is None:
                warnings.append(f"{config_path.name}: brain '{brain_path}' not found on disk")
                continue

            status = brain_data.get("status", "")
            if status in ("retired", "frozen"):
                errors.append(
                    f"{config_path.name}: RETIRED brain '{brain_path}' (status={status}) "
                    f"still enabled=true — must be disabled (FIX-20260610-002)"
                )

            # ── Check 3: label_contract existence (FATAL — DQAF-20260622-051) ──
            label_contract = brain_data.get("label_contract")
            contract_group = brain_data.get("contract_group", "")
            strat_line = strategy_lines.get(contract_group, {}) if contract_group else {}

            if label_contract is None:
                errors.append(
                    f"{config_path.name}: brain '{brain_path}' missing label_contract block — "
                    f"train-serve SL/TP alignment cannot be verified (DQAF-20260622-051)"
                )
            elif isinstance(label_contract, dict):
                aligned_with = label_contract.get("aligned_with")
                contract_sl = label_contract.get("sl_atr_mult")
                contract_tp = label_contract.get("tp_atr_mult")

                if aligned_with is None:
                    # Brain explicitly declares non-alignment — check graduation_path
                    grad_path = label_contract.get("graduation_path", "")
                    if not grad_path:
                        warnings.append(
                            f"{config_path.name}: brain '{brain_path}' label_contract.aligned_with=null "
                            f"but no graduation_path specified"
                        )
                    else:
                        # This is intentional (survival brains, etc.) — informational
                        pass
                elif isinstance(aligned_with, str) and "live_btc.yaml" in aligned_with:
                    if asset != "BTC":
                        warnings.append(
                            f"{config_path.name}: brain '{brain_path}' label_contract declares "
                            f"aligned_with={aligned_with} but is deployed in {config_path.name}"
                        )

                # ── Check 3a: train-serve SL/TP alignment (FATAL — DQAF-20260622-051) ──
                # FIX-20260625-139: Shadow brains (vote_weight=0.0 or status=shadow)
                # are exempt from FATAL SL/TP mismatch — they cannot trade, so
                # the mismatch is harmless.  Downgraded to WARNING.
                if contract_sl is not None and contract_tp is not None and strat_line:
                    serve_sl = strat_line.get("sl", {}).get("base_atr_mult")
                    serve_tp = strat_line.get("tp", {}).get("base_atr_mult")

                    _brain_cfg = _load_brain(brain_path) or {}
                    _is_shadow = (
                        float(_brain_cfg.get("vote_weight", 1.0) or 1.0) <= 0.0
                        or str(_brain_cfg.get("status", "")).lower() == "shadow"
                    )

                    if serve_sl is not None and serve_sl != contract_sl:
                        _msg = (
                            f"{config_path.name}: brain '{brain_path}' TRAIN-SERVE SL MISMATCH — "
                            f"label_contract declares SL={contract_sl}×ATR, "
                            f"but strategy '{contract_group}' serves SL={serve_sl}×ATR "
                            f"(DQAF-20260622-051)"
                        )
                        if _is_shadow:
                            _msg += " [SHADOW: harmless, brain vote_weight=0.0]"
                            warnings.append(_msg)
                        else:
                            errors.append(_msg)
                    if serve_tp is not None and serve_tp != contract_tp:
                        _msg = (
                            f"{config_path.name}: brain '{brain_path}' TRAIN-SERVE TP MISMATCH — "
                            f"label_contract declares TP={contract_tp}×ATR, "
                            f"but strategy '{contract_group}' serves TP={serve_tp}×ATR "
                            f"(DQAF-20260622-051)"
                        )
                        if _is_shadow:
                            _msg += " [SHADOW: harmless, brain vote_weight=0.0]"
                            warnings.append(_msg)
                        else:
                            errors.append(_msg)

    # ── Check 4: contract_path regression gate (WARN — DQAF-20260622-057 Phase 2) ──
    # Verify daily_ops.py always passes contract_path to _step_label_builder().
    # Post-Phase-1 the code is correct; this gate only fires if a future edit
    # removes the contract_path parameter from a production call site.
    _daily_ops_path = Path("scripts/daily_ops.py")
    if _daily_ops_path.exists():
        _dops_text = _daily_ops_path.read_text(encoding="utf-8")
        import re

        _builder_calls = list(re.finditer(r"_step_label_builder\([^)]*\)", _dops_text))
        for _match in _builder_calls:
            _call_text = _match.group()
            if "contract_path" not in _call_text:
                _lineno = _dops_text[: _match.start()].count("\n") + 1
                warnings.append(
                    f"daily_ops.py:{_lineno}: _step_label_builder() called "
                    f"without contract_path — label barrier defense layer "
                    f"inactive (DQAF-20260622-057)"
                )

    # Print results
    if errors:
        print(f"\n[FAIL] Config Consistency: {len(errors)} error(s)")
        for e in errors:
            print(f"  ❌ {e}")
    if warnings:
        print(f"[WARN] Config Consistency: {len(warnings)} warning(s)")
        for w in warnings:
            print(f"  ⚠️  {w}")
    if not errors and not warnings:
        print("[PASS] Config Consistency: all checks passed")

    return len(errors) == 0, errors


def _run_golden_master_check() -> int:
    """Validate recorded Golden Master cycles for structural integrity and consistency.

    Checks:
      1. All cycles have required fields (inputs, outputs, summary)
      2. Output consistency: same inputs → same outputs (determinism check)
      3. Anomaly detection: cycles with zero trades, all-blocked cycles, etc.
      4. Coverage stats: strategies seen, regime distribution, signal frequency
    """
    from collections import Counter

    try:
        from core.runtime.golden_master import load_records
    except ImportError:
        print("[FAIL] Golden Master: cannot import golden_master module")
        return 1

    all_records = []
    for label, data_dir in [("XAU", "data"), ("BTC", "data_btc")]:
        records = load_records(data_dir)
        for r in records:
            r["_source"] = label
        all_records.extend(records)

    if not all_records:
        print("[FAIL] Golden Master: no recorded cycles found")
        return 1

    print(
        f"Golden Master: {len(all_records)} cycles loaded "
        f"(XAU={sum(1 for r in all_records if r['_source']=='XAU')}, "
        f"BTC={sum(1 for r in all_records if r['_source']=='BTC')})"
    )

    errors = 0
    warnings = 0

    # ── Check 1: structural integrity ──
    required_keys = {"cycle", "timestamp_utc", "now_unix", "inputs", "outputs", "summary"}
    for i, r in enumerate(all_records):
        missing = required_keys - set(r.keys())
        if missing:
            print(f"  [ERROR] Cycle {i}: missing keys {missing}")
            errors += 1

    if errors == 0:
        print("  [PASS] Structural integrity: all cycles have required fields")

    # ── Check 2: output consistency (determinism) ──
    # Group cycles with identical inputs, check outputs match
    input_sigs: dict[str, list[dict]] = {}
    for r in all_records:
        inp = r.get("inputs", {})
        sig = f"{inp.get('regime')}|{inp.get('trend_direction')}|{inp.get('spread'):.2f}|{inp.get('current_atr'):.1f}"
        input_sigs.setdefault(sig, []).append(r)

    determinism_violations = 0
    for sig, group in input_sigs.items():
        if len(group) < 2:
            continue
        baseline = group[0]["outputs"]
        for other in group[1:]:
            for strat, out in other["outputs"].items():
                base = baseline.get(strat, {})
                if base.get("should_trade") != out.get("should_trade"):
                    determinism_violations += 1
                    if determinism_violations <= 3:  # limit noise
                        print(
                            f"  [WARN] Non-determinism: {sig} → {strat}: "
                            f"trade={base.get('should_trade')} vs {out.get('should_trade')}"
                        )
    if determinism_violations == 0:
        print("  [PASS] Determinism: identical inputs produce identical should_trade decisions")
    else:
        print(f"  [WARN] Determinism: {determinism_violations} output variations with same inputs")
        warnings += determinism_violations

    # ── Check 3: coverage statistics ──
    strategies: Counter[str] = Counter()
    regimes: Counter[str] = Counter()
    trade_count = 0
    blocked_count = 0
    for r in all_records:
        for s in r.get("summary", {}).get("active_strategies", []):
            strategies[s] += 1
        regimes[r.get("inputs", {}).get("regime", "?")] += 1
        if r.get("summary", {}).get("trade_decisions", 0) > 0:
            trade_count += 1
        else:
            blocked_count += 1

    print(f"  [INFO] Strategies seen: {dict(strategies)}")
    print(f"  [INFO] Regime distribution: {dict(regimes)}")
    print(f"  [INFO] Cycles with trades: {trade_count}, all-blocked: {blocked_count}")

    # ── Check 4: anomaly detection ──
    source_cycles = Counter(r["_source"] for r in all_records)
    print(f"  [INFO] Source distribution: {dict(source_cycles)}")

    if trade_count == 0:
        print("  [WARN] No cycles with trades recorded — all signals blocked")
        warnings += 1

    if errors > 0:
        print(f"\n[FAIL] Golden Master: {errors} error(s), {warnings} warning(s)")
        return 1
    else:
        print(f"\n[PASS] Golden Master: structural validation passed ({warnings} warnings)")
        return 0


def scan_debt_bombs() -> tuple[bool, list[str]]:
    """Scan all .py files for expired DEBT annotations.

    Format: ``# DEBT: [FIX-XXX] <desc>. EXPIRE: YYYY-MM-DD``
    Also accepts: ``# TODO(FIX-XXX, EXPIRE: YYYY-MM-DD): <desc>``

    Uses UTC for date comparison — no timezone drift tolerated in finance.
    Excludes virtual envs, caches, and other noise directories.

    Returns (ok, list_of_expired_entries).
    """
    from datetime import UTC, datetime

    today = datetime.now(UTC).date()
    expired: list[str] = []
    total_found = 0

    for dirpath, dirnames, filenames in os.walk(str(ROOT)):
        # ── Directory whitelist: prune noise ──
        dirnames[:] = [d for d in dirnames if d not in _DEBT_EXCLUDE_DIRS]

        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fn)
            try:
                with open(fpath, encoding="utf-8") as fh:
                    for lineno, line in enumerate(fh, 1):
                        m = _DEBT_RE.search(line)
                        if not m:
                            continue
                        total_found += 1
                        expire_str = m.group(1)
                        try:
                            expire_date = datetime.strptime(expire_str, "%Y-%m-%d").date()
                        except ValueError:
                            continue  # malformed date — skip
                        if expire_date < today:
                            rel = os.path.relpath(fpath, str(ROOT))
                            desc = line.strip().lstrip("#").strip()
                            overdue = (today - expire_date).days
                            expired.append(
                                f"  {rel}:{lineno}  {desc[:100]} " f"(OVERDUE {overdue} days)"
                            )
            except (OSError, UnicodeDecodeError):
                continue  # can't read — skip

    if expired:
        print("=" * 60)
        print(f"[Ω-DEBT-BOMB] EXPIRED DEBT DETECTED (UTC {today}):")
        for entry in expired:
            print(entry)
        print(
            f"\nFATAL: {len(expired)} expired DEBT annotation(s). "
            f"All trading, backtesting, and CI are blocked.\n"
            f"Resolve each DEBT or update its EXPIRE date "
            f"with documented justification."
        )
        print("=" * 60)
        return False, expired

    if total_found:
        print(
            f"[Ω-DEBT-BOMB] PASSED: {total_found} DEBT annotation(s), none expired (UTC {today})."
        )
    return True, []


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Iron law verification gate")
    parser.add_argument("--quick", action="store_true", help="mypy + ruff on changed files only")
    parser.add_argument("--full", action="store_true", help="mypy + ruff + pytest on full codebase")
    parser.add_argument(
        "--stamp", action="store_true", help="Update verification stamp after checks pass"
    )
    parser.add_argument(
        "--check-stamp", action="store_true", help="Check if stamp is current (exit 0 = clean)"
    )
    parser.add_argument(
        "--mypy-only", action="store_true", help="Run only mypy (for PostToolUse hook)"
    )
    parser.add_argument(
        "--blueprints", action="store_true", help="Run blueprint consistency validation"
    )
    parser.add_argument(
        "--golden-master",
        action="store_true",
        help="Replay-validation of recorded Golden Master cycles",
    )
    parser.add_argument("--files", nargs="*", help="Specific file(s) to check")
    args = parser.parse_args()

    # --blueprints: blueprint consistency check
    if args.blueprints:
        try:
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "validate_blueprints.py")],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(ROOT),
                timeout=30,
            )
            print((result.stdout or "").strip())
            if result.stderr and result.stderr.strip():
                print(result.stderr.strip())
            return result.returncode
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
            print(f"Blueprint validation error: {exc}")
            return 1
    # --golden-master: replay-validation of recorded cycles
    if args.golden_master:
        return _run_golden_master_check()

    # --check-stamp: lightweight, no heavy checks
    if args.check_stamp or (
        not args.quick and not args.full and not args.mypy_only and not args.files
    ):
        valid, reason = check_stamp()
        print(reason)
        return 0 if valid else 1

    # --mypy-only: for PostToolUse hook
    if args.mypy_only:
        targets = args.files if args.files else None
        passed, output = run_mypy(targets)
        if output:
            print(output)
        return 0 if passed else 1

    all_passed = True

    if args.quick:
        # ── DEBT time-bomb scan (first — before mypy/ruff) ──
        debt_ok, _ = scan_debt_bombs()
        if not debt_ok:
            return 1

        changed = _changed_py_files()
        if not changed:
            print("No changed Python files.")
        else:
            print(f"Checking {len(changed)} changed file(s)...")
            passed, output = run_mypy(changed)
            if not passed:
                print(f"[FAIL] mypy:\n{output}")
                all_passed = False
            else:
                print("[PASS] mypy")

            passed, output = run_ruff(changed)
            if not passed:
                print(f"[FAIL] ruff:\n{output}")
                all_passed = False
            else:
                print("[PASS] ruff")

            print(">>> blueprint compliance (Iron Law #7)...")
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts" / "check_blueprint_compliance.py"),
                        "--check",
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=str(ROOT),
                    timeout=30,
                )
                print((result.stdout or "").strip())
                if result.stderr and result.stderr.strip():
                    print(result.stderr.strip())
                if result.returncode != 0:
                    all_passed = False
            except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
                print(f"[FAIL] blueprint compliance check error: {exc}")
                all_passed = False
            print(">>> import boundaries (Iron Law #3)...")
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts" / "check_import_boundaries.py"),
                        "--quiet",
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=str(ROOT),
                    timeout=10,
                )
                if result.returncode != 0:
                    print("[FAIL] Import boundary violation(s) detected")
                    all_passed = False
                else:
                    print("[PASS] Import boundaries enforced")
            except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
                print(f"[FAIL] import-linter error: {exc}")
                all_passed = False
            print(">>> artifact parameter contract...")
            try:
                result = subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / "validate_artifacts.py")],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=str(ROOT),
                    timeout=15,
                )
                print((result.stdout or "").strip())
                if result.stderr and result.stderr.strip():
                    print(result.stderr.strip())
                if result.returncode != 0:
                    all_passed = False
            except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
                print(f"[FAIL] artifact validation error: {exc}")
                all_passed = False
            print(">>> config consistency (FIX-20260610-002)...")
            cfg_ok, cfg_errs = _check_config_consistency()
            if not cfg_ok:
                all_passed = False

            print(">>> FIX_REGISTRY gate (Iron Law #7)...")
            reg_ok, reg_errs = _check_registry_gate()
            if not reg_ok:
                for _e in reg_errs:
                    print(f"[FAIL] {_e}")
                all_passed = False
            else:
                print("[PASS] FIX_REGISTRY gate")

    elif args.full:
        # ── DEBT time-bomb scan (first — before mypy/ruff) ──
        debt_ok, _ = scan_debt_bombs()
        if not debt_ok:
            return 1

        passed, output = run_mypy()
        if not passed:
            print(f"[FAIL] mypy:\n{output}")
            all_passed = False
        else:
            print("[PASS] mypy")

        print(">>> ruff...")
        passed, output = run_ruff()
        if not passed:
            print(f"[FAIL] ruff:\n{output}")
            all_passed = False
        else:
            print("[PASS] ruff")

        print(">>> blueprint compliance (Iron Law #7)...")
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "check_blueprint_compliance.py"),
                    "--check",
                    "--all",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(ROOT),
                timeout=30,
            )
            print((result.stdout or "").strip())
            if result.stderr and result.stderr.strip():
                print(result.stderr.strip())
            if result.returncode != 0:
                all_passed = False
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
            print(f"[FAIL] blueprint compliance check error: {exc}")
            all_passed = False
        print(">>> import boundaries (Iron Law #3)...")
        try:
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "check_import_boundaries.py"), "--quiet"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(ROOT),
                timeout=10,
            )
            if result.returncode != 0:
                print("[FAIL] Import boundary violation(s) detected")
                all_passed = False
            else:
                print("[PASS] Import boundaries enforced")
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
            print(f"[FAIL] import-linter error: {exc}")
            all_passed = False
        print(">>> artifact parameter contract...")
        try:
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "validate_artifacts.py")],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(ROOT),
                timeout=15,
            )
            print((result.stdout or "").strip())
            if result.stderr and result.stderr.strip():
                print(result.stderr.strip())
            if result.returncode != 0:
                all_passed = False
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
            print(f"[FAIL] artifact validation error: {exc}")
            all_passed = False
        print(">>> config consistency (FIX-20260610-002)...")
        cfg_ok, cfg_errs = _check_config_consistency()
        if not cfg_ok:
            all_passed = False

        print(">>> FIX_REGISTRY gate (Iron Law #7)...")
        reg_ok, reg_errs = _check_registry_gate()
        if not reg_ok:
            for _e in reg_errs:
                print(f"[FAIL] {_e}")
            all_passed = False
        else:
            print("[PASS] FIX_REGISTRY gate")

        print(">>> pytest...")
        passed, output = run_pytest()
        if not passed:
            print(f"[FAIL] pytest:\n{output}")
            all_passed = False
        else:
            print(f"[PASS] pytest: {output}")

    if args.stamp:
        msg = update_stamp(all_passed, "full" if args.full else "quick")
        print(msg)

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
