"""Regression tests for scripts.journal_freeze_gate — DQAF-20260819-007.

Locks FIX-20260819-007:

1. **Windows path-separator false-block** — coverage.json (pytest-cov on
   Windows) keys use backslashes (``core\\ledger\\...``) while the gate's
   protected prefixes are canonical forward slashes (``core/ledger/``).  The
   old string ``startswith`` never matched → coverage read 0.0% → every commit
   touching a protected path was falsely BLOCKED.  ``_is_protected`` now
   normalizes paths to forward slashes before matching.

2. **JOURNAL_FREEZE_BYPASS retirement** — the blanket env-var bypass is gone.
   The gate is honest: it reads real coverage and blocks below 80%.  Documented
   emergency exceptions go through ``--no-verify`` (Iron Law #0-bis).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "journal_freeze_gate",
    Path(__file__).resolve().parents[2] / "scripts" / "journal_freeze_gate.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_jfg = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_jfg)


class TestIsProtected:
    """Path matching against the protected prefixes, both separators."""

    def test_backslash_windows_coverage_path_matches(self) -> None:
        # Regression: pytest-cov on Windows emits `core\\ledger\\...`.
        assert _jfg._is_protected("core\\ledger\\__init__.py") is True
        assert _jfg._is_protected("core\\contracts\\label_contract.py") is True

    def test_forward_slash_git_staged_path_matches(self) -> None:
        # git diff --cached --name-only is always forward-slash.
        assert _jfg._is_protected("core/ledger/__init__.py") is True
        assert _jfg._is_protected("core/contracts/label_contract.py") is True

    def test_adjacent_but_unprotected_not_matched(self) -> None:
        assert _jfg._is_protected("core/ledgerx/foo.py") is False
        assert _jfg._is_protected("core/execution/position_manager.py") is False


class TestReadCoveragePct:
    """Weighted line+branch read from coverage.json, backslash-keyed."""

    def _write(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, files: dict[str, dict[str, object]]
    ) -> None:
        (tmp_path / "coverage.json").write_text(json.dumps({"files": files}), encoding="utf-8")
        monkeypatch.setattr(_jfg, "_PROJECT_ROOT", tmp_path)

    def test_backslash_keys_yield_real_percentage(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._write(
            monkeypatch,
            tmp_path,
            {
                "core\\ledger\\a.py": {
                    "summary": {
                        "covered_lines": 90,
                        "num_statements": 100,
                        "covered_branches": 0,
                        "num_branches": 0,
                    }
                },
                "core\\ledger\\b.py": {
                    "summary": {
                        "covered_lines": 0,
                        "num_statements": 100,
                        "covered_branches": 0,
                        "num_branches": 0,
                    }
                },
            },
        )
        # line 45.0, no branches → branch falls back to line → weighted 45.0
        assert _jfg._read_coverage_pct() == 45.0

    def test_branch_coverage_weighted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._write(
            monkeypatch,
            tmp_path,
            {
                "core\\ledger\\a.py": {
                    "summary": {
                        "covered_lines": 90,
                        "num_statements": 100,
                        "covered_branches": 40,
                        "num_branches": 100,
                    }
                }
            },
        )
        # line 90.0, branch 40.0 → weighted (90 + 40) / 2 = 65.0
        assert _jfg._read_coverage_pct() == 65.0

    def test_no_protected_files_returns_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._write(
            monkeypatch,
            tmp_path,
            {
                "core\\execution\\position_manager.py": {
                    "summary": {
                        "covered_lines": 10,
                        "num_statements": 10,
                        "covered_branches": 0,
                        "num_branches": 0,
                    }
                }
            },
        )
        assert _jfg._read_coverage_pct() == 0.0


class TestNoEnvBypass:
    """JOURNAL_FREEZE_BYPASS no longer opens the gate (FIX-20260819-007)."""

    def test_bypass_constant_removed(self) -> None:
        assert not hasattr(_jfg, "_BYPASS_TOKEN")

    def test_env_var_does_not_bypass_block(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_jfg, "_get_staged_files", lambda: ["core/ledger/a.py"])
        monkeypatch.setattr(_jfg, "_read_coverage_pct", lambda: 0.0)
        monkeypatch.setenv("JOURNAL_FREEZE_BYPASS", "APPROVED_BY_ARCH_REVIEW")
        assert _jfg.main() == 1

    def test_unprotected_staged_file_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_jfg, "_get_staged_files", lambda: ["core/execution/a.py"])
        monkeypatch.setattr(_jfg, "_read_coverage_pct", lambda: 0.0)
        assert _jfg.main() == 0

    def test_sufficient_coverage_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_jfg, "_get_staged_files", lambda: ["core/ledger/a.py"])
        monkeypatch.setattr(_jfg, "_read_coverage_pct", lambda: 85.0)
        assert _jfg.main() == 0
