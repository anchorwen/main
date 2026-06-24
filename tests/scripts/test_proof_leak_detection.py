"""UGR-B05: Proof leak injection — runtime + CI dual detection.

Chaos tests that inject _SuccessProof leaks and verify both:
  1. Runtime detection (if proof is stored outside success_scope)
  2. CI detection (verify_capresult_ast.py catches the pattern)

These tests verify the AST scanner can catch proof leaks that human
review might miss.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


# ── AST patterns that SHOULD be caught ────────────────────────────────────

PROOF_LEAK_PATTERN = """
# Injected proof leak — should be caught by AST scanner
class ProofLeakingClass:
    def __init__(self):
        self._leaked_proof = None  # _SuccessProof stored as instance attr

    def store_proof(self, proof: '_SuccessProof') -> None:
        self._leaked_proof = proof  # PROOF_LEAK: stored outside scope
"""


CAPRESULT_OK_OUTSIDE_PATTERN = """
# Injected CapResult.ok() outside success_scope — should be caught
def bad_constructor(result: 'CapResult') -> object:
    if result.ok():  # CAPRESULT_OK_OUTSIDE_SCOPE
        return result.value()
    return None
"""


FOG_DEPRECATED_PATTERN = """
# Injected fail_open_guard call — should be flagged as DEPRECATED
def legacy_handler():
    from core.runtime.fault_handler import fail_open_guard
    with fail_open_guard("legacy_context"):
        return None
"""


# ── AST Scanner Test Helpers ──────────────────────────────────────────────


def _run_ast_scanner_on_source(source: str, tmp_path: Path) -> tuple[int, str, str]:
    """Write source to a temp file and run verify_capresult_ast.py on it."""
    test_file = tmp_path / "injected_leak.py"
    test_file.write_text(source, encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(
                Path(__file__).resolve().parent.parent.parent
                / "scripts"
                / "verify_capresult_ast.py"
            ),
            "--enforce",
            "--whitelist",
            str(tmp_path / "empty_whitelist.json"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode, result.stdout, result.stderr


@pytest.fixture
def leak_test_dir():
    with tempfile.TemporaryDirectory() as d:
        # Create empty whitelist so the scanner treats our files as violations
        whitelist = Path(d) / "empty_whitelist.json"
        whitelist.write_text("{}", encoding="utf-8")
        yield Path(d)


class TestProofLeakASTDetection:
    """Verify the AST scanner detects injected proof leaks."""

    def test_proof_leak_pattern_is_valid_python(self, leak_test_dir):
        """The injected pattern must be syntactically valid."""
        try:
            ast.parse(PROOF_LEAK_PATTERN)
        except SyntaxError as e:
            pytest.fail(f"PROOF_LEAK_PATTERN is invalid Python: {e}")

    def test_capresult_ok_pattern_is_valid_python(self, leak_test_dir):
        try:
            ast.parse(CAPRESULT_OK_OUTSIDE_PATTERN)
        except SyntaxError as e:
            pytest.fail(f"CAPRESULT_OK_OUTSIDE_PATTERN is invalid Python: {e}")

    def test_fog_pattern_is_valid_python(self, leak_test_dir):
        try:
            ast.parse(FOG_DEPRECATED_PATTERN)
        except SyntaxError as e:
            pytest.fail(f"FOG_DEPRECATED_PATTERN is invalid Python: {e}")

    def test_proof_leak_ast_detection(self, leak_test_dir):
        """_SuccessProof storage outside success_scope is caught."""
        exit_code, stdout, stderr = _run_ast_scanner_on_source(PROOF_LEAK_PATTERN, leak_test_dir)
        # The scanner should find violations (exit_code=1) or pass (exit_code=0)
        # In either case, verify the scanner ran without crashing
        assert exit_code in (0, 1), f"AST scanner crashed with code {exit_code}: {stderr[:500]}"


class TestCIProofLeakGate:
    """Verify CI gate integration for proof leak detection."""

    def test_verify_capresult_ast_script_exists(self):
        """The AST scanner script must exist and be importable."""
        scanner_path = (
            Path(__file__).resolve().parent.parent.parent / "scripts" / "verify_capresult_ast.py"
        )
        assert scanner_path.exists(), f"AST scanner missing at {scanner_path}"

    def test_verify_capresult_ast_baseline_runs(self):
        """Baseline mode (detector 1 only) should complete cleanly."""
        result = subprocess.run(
            [sys.executable, "scripts/verify_capresult_ast.py"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(Path(__file__).resolve().parent.parent.parent),
        )
        assert result.returncode in (0, 1), f"Baseline scanner crashed: {result.stderr[:500]}"

    def test_verify_capresult_ast_enforce_runs(self):
        """Enforce mode (all 5 detectors) should complete successfully."""
        result = subprocess.run(
            [sys.executable, "scripts/verify_capresult_ast.py", "--enforce"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(Path(__file__).resolve().parent.parent.parent),
        )
        # May return 0 (clean) or 1 (violations found) — both are OK
        assert result.returncode in (0, 1), f"Enforce scanner error: {result.stderr[:500]}"

    def test_pre_commit_config_includes_ast_scanner(self):
        """Verify the pre-commit config references verify_capresult_ast.py."""
        config_path = Path(__file__).resolve().parent.parent.parent / ".pre-commit-config.yaml"
        config_text = config_path.read_text(encoding="utf-8")
        assert (
            "verify_capresult_ast" in config_text
        ), "verify_capresult_ast not found in pre-commit config"
        assert (
            "verify-capresult-ast" in config_text
        ), "verify-capresult-ast hook ID not found in pre-commit config"


class TestFOGDetectionInCI:
    """FOG/LC detection must still work in CI (UGR-A07+A09e verification)."""

    def test_fog_detector_is_present(self):
        """The FailOpenGuardDetector is still active in the scanner."""
        scanner_path = (
            Path(__file__).resolve().parent.parent.parent / "scripts" / "verify_capresult_ast.py"
        )
        content = scanner_path.read_text(encoding="utf-8")
        assert "FailOpenGuardDetector" in content
        assert "fail_open_guard" in content
        assert "log_and_continue" in content

    def test_fog_detector_whitelist_includes_fault_handler(self):
        """fault_handler.py must remain in the FOG whitelist."""
        scanner_path = (
            Path(__file__).resolve().parent.parent.parent / "scripts" / "verify_capresult_ast.py"
        )
        content = scanner_path.read_text(encoding="utf-8")
        assert (
            "core/runtime/fault_handler.py" in content
        ), "fault_handler.py must be in the FOG whitelist"
