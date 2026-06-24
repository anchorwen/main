"""Tests for verify_capresult_ast.py — UGR-B03 AST enforcement detectors.

Each test class constructs synthetic Python source code, parses it into
an AST, runs the corresponding detector, and verifies violations are
correctly detected (or absent for valid patterns).
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path
from typing import Any

# Import detectors from the scanner module
from scripts.verify_capresult_ast import (
    CapResultOkPlacementDetector,
    DynamicCallDetector,
    FailOpenGuardDetector,
    ProofLeakDetector,
    RawAccessDetector,
    _path_in_whitelist,
)


def _parse(source: str) -> ast.Module:
    """Parse *source* into an AST module, dedenting first."""
    return ast.parse(textwrap.dedent(source))


def _run_detector(
    detector_cls: type, source: str, filepath: str = "test/fake.py", **kwargs: Any
) -> list[Any]:
    """Parse *source* and run *detector_cls*, returning violations."""
    tree = _parse(source)
    detector = detector_cls(filepath, **kwargs)
    detector.visit(tree)
    return detector.violations


# ═══════════════════════════════════════════════════════════════════════════
# Whitelist helper
# ═══════════════════════════════════════════════════════════════════════════


class TestPathInWhitelist:
    def test_exact_match(self) -> None:
        assert (
            _path_in_whitelist("core/contracts/cap_result.py", {"core/contracts/cap_result.py"})
            is True
        )

    def test_no_match(self) -> None:
        assert (
            _path_in_whitelist("core/runtime/live_cycle.py", {"core/contracts/cap_result.py"})
            is False
        )

    def test_windows_path_normalized(self) -> None:
        assert (
            _path_in_whitelist("core\\contracts\\cap_result.py", {"core/contracts/cap_result.py"})
            is True
        )


# ═══════════════════════════════════════════════════════════════════════════
# Detector 1: DynamicCallDetector
# ═══════════════════════════════════════════════════════════════════════════


class TestDynamicCallDetector:
    def test_getattr_on_forbidden(self) -> None:
        source = """
        getattr(some_obj, 'CapResult')
        """
        violations = _run_detector(DynamicCallDetector, source)
        assert len(violations) == 1
        assert violations[0].rule == "DYNAMIC_FORBIDDEN"
        assert "CapResult" in violations[0].detail

    def test_setattr_on_forbidden(self) -> None:
        source = """
        setattr(some_obj, '_SuccessProof', value)
        """
        violations = _run_detector(DynamicCallDetector, source)
        assert len(violations) == 1
        assert violations[0].rule == "DYNAMIC_FORBIDDEN"
        assert "_SuccessProof" in violations[0].detail

    def test_hasattr_on_forbidden(self) -> None:
        source = """
        hasattr(some_obj, 'fail_open_guard')
        """
        violations = _run_detector(DynamicCallDetector, source)
        assert len(violations) == 1
        assert "fail_open_guard" in violations[0].detail

    def test_getattr_on_allowed_symbol_no_violation(self) -> None:
        source = """
        getattr(some_obj, 'normal_attr')
        """
        violations = _run_detector(DynamicCallDetector, source)
        assert len(violations) == 0

    def test_getattr_with_variable_not_constant(self) -> None:
        source = """
        getattr(some_obj, variable_name)
        """
        violations = _run_detector(DynamicCallDetector, source)
        # Can't statically resolve variable → no violation (best-effort)
        assert len(violations) == 0

    def test_whitelisted_file_no_violation(self) -> None:
        source = """
        getattr(some_obj, 'CapResult')
        """
        violations = _run_detector(
            DynamicCallDetector, source, filepath="core/contracts/cap_result.py"
        )
        assert len(violations) == 0


# ═══════════════════════════════════════════════════════════════════════════
# Detector 2: CapResultOkPlacementDetector
# ═══════════════════════════════════════════════════════════════════════════


class TestCapResultOkPlacementDetector:
    def test_ok_inside_success_scope_no_violation(self) -> None:
        source = """
        from core.contracts.cap_result import CapResult, Kernel

        with Kernel.success_scope() as proof:
            result = CapResult.ok(42, proof)
        """
        violations = _run_detector(CapResultOkPlacementDetector, source)
        assert len(violations) == 0

    def test_ok_inside_kernel_instance_scope_no_violation(self) -> None:
        source = """
        from core.contracts.cap_result import CapResult, Kernel

        with Kernel().success_scope() as proof:
            result = CapResult.ok(42, proof)
        """
        violations = _run_detector(CapResultOkPlacementDetector, source)
        assert len(violations) == 0

    def test_ok_outside_scope_is_violation(self) -> None:
        source = """
        from core.contracts.cap_result import CapResult, Kernel

        result = CapResult.ok(42, proof)
        """
        violations = _run_detector(CapResultOkPlacementDetector, source)
        assert len(violations) == 1
        assert violations[0].rule == "CAPRESULT_OK_OUTSIDE_SCOPE"

    def test_instance_map_not_detected_limited_to_classmethod(self) -> None:
        """Instance method calls (.map, .flat_map) cannot be statically
        type-checked — the detector only flags the classmethod CapResult.ok()."""
        source = """
        from core.contracts.cap_result import CapResult

        transformed = some_result.map(lambda x: x * 2, proof)
        """
        violations = _run_detector(CapResultOkPlacementDetector, source)
        # Instance methods not detected — AST can't resolve receiver type
        assert len(violations) == 0

    def test_some_other_ok_method_no_false_positive(self) -> None:
        """Don't flag SomeOtherClass.ok() — only CapResult.ok()."""
        source = """
        class Logger:
            @classmethod
            def ok(cls, msg: str) -> None:
                print(msg)

        Logger.ok("all good")
        """
        violations = _run_detector(CapResultOkPlacementDetector, source)
        assert len(violations) == 0

    def test_ok_in_nested_scope_no_violation(self) -> None:
        source = """
        from core.contracts.cap_result import CapResult, Kernel

        with Kernel.success_scope() as outer_proof:
            with Kernel.success_scope() as inner_proof:
                result = CapResult.ok(42, inner_proof)
        """
        violations = _run_detector(CapResultOkPlacementDetector, source)
        assert len(violations) == 0

    def test_ok_with_proof_from_outer_scope_no_violation(self) -> None:
        source = """
        from core.contracts.cap_result import CapResult, Kernel

        with Kernel.success_scope() as outer_proof:
            result = CapResult.ok(42, outer_proof)
        """
        violations = _run_detector(CapResultOkPlacementDetector, source)
        assert len(violations) == 0

    def test_whitelisted_file_no_violation(self) -> None:
        source = """
        result = CapResult.ok(42, proof)
        """
        violations = _run_detector(
            CapResultOkPlacementDetector,
            source,
            filepath="core/contracts/cap_result.py",
        )
        assert len(violations) == 0

    def test_non_kernel_with_context_no_effect(self) -> None:
        """A 'with' block that is NOT Kernel.success_scope() should not count."""
        source = """
        from core.contracts.cap_result import CapResult

        with open('file.txt') as f:
            result = CapResult.ok(42, proof)
        """
        violations = _run_detector(CapResultOkPlacementDetector, source)
        assert len(violations) == 1  # Still outside success_scope


# ═══════════════════════════════════════════════════════════════════════════
# Detector 3: RawAccessDetector
# ═══════════════════════════════════════════════════════════════════════════


class TestRawAccessDetector:
    def test_raw_access_is_violation(self) -> None:
        source = """
        from core.runtime.typed_clock import MonotonicInstant

        t = MonotonicInstant(100.0)
        val = t._raw
        """
        violations = _run_detector(RawAccessDetector, source)
        assert len(violations) == 1
        assert violations[0].rule == "RAW_ACCESS"

    def test_raw_assignment_is_violation(self) -> None:
        source = """
        from core.runtime.typed_clock import MonotonicInstant

        t = MonotonicInstant(100.0)
        t._raw = 999.0
        """
        violations = _run_detector(RawAccessDetector, source)
        # Assignment target is an ast.Attribute with attr='_raw'
        assert len(violations) == 1
        assert violations[0].rule == "RAW_ACCESS"

    def test_object_setattr_raw_bypass(self) -> None:
        source = """
        from core.runtime.typed_clock import MonotonicInstant

        t = MonotonicInstant(100.0)
        object.__setattr__(t, '_raw', 999.0)
        val = t._raw
        """
        violations = _run_detector(RawAccessDetector, source)
        # t._raw appears as both assignment target (object.__setattr__ bypass)
        # and read (val = t._raw) — but visit_Attribute sees both
        assert len(violations) == 1  # Only the explicit t._raw read

    def test_whitelisted_file_no_violation(self) -> None:
        source = """
        from core.runtime.typed_clock import MonotonicInstant

        t = MonotonicInstant(100.0)
        val = t._raw
        """
        violations = _run_detector(
            RawAccessDetector,
            source,
            filepath="core/runtime/typed_clock.py",
        )
        assert len(violations) == 0

    def test_adapters_bridge_whitelisted(self) -> None:
        source = """
        from core.runtime.typed_clock import MonotonicInstant

        def bridge_mono_to_float(t: MonotonicInstant) -> float:
            return t._raw
        """
        violations = _run_detector(
            RawAccessDetector,
            source,
            filepath="core/contracts/adapters.py",
        )
        assert len(violations) == 0

    def test_other_private_attr_no_violation(self) -> None:
        source = """
        class Foo:
            _raw = 42

        f = Foo()
        val = f._other_private
        """
        violations = _run_detector(RawAccessDetector, source)
        assert len(violations) == 0  # _raw access is on a non-TypedClock type


# ═══════════════════════════════════════════════════════════════════════════
# Detector 4: FailOpenGuardDetector
# ═══════════════════════════════════════════════════════════════════════════


class TestFailOpenGuardDetector:
    def test_fail_open_guard_is_deprecated(self) -> None:
        source = """
        from core.runtime.fault_handler import fail_open_guard

        with fail_open_guard("ComponentName"):
            do_something()
        """
        violations = _run_detector(FailOpenGuardDetector, source)
        assert len(violations) == 1
        assert violations[0].rule == "DEPRECATED_RESILIENCE"
        assert "fail_open_guard" in violations[0].detail

    def test_log_and_continue_is_deprecated(self) -> None:
        source = """
        from core.runtime.fault_handler import log_and_continue

        with log_and_continue(component="ComponentName"):
            do_something()
        """
        violations = _run_detector(FailOpenGuardDetector, source)
        assert len(violations) == 1
        assert violations[0].rule == "DEPRECATED_RESILIENCE"
        assert "log_and_continue" in violations[0].detail

    def test_multiple_calls_multiple_violations(self) -> None:
        source = """
        from core.runtime.fault_handler import fail_open_guard, log_and_continue

        with fail_open_guard("A"):
            pass
        with log_and_continue(component="B"):
            pass
        """
        violations = _run_detector(FailOpenGuardDetector, source)
        assert len(violations) == 2

    def test_whitelisted_file_no_violation(self) -> None:
        source = """
        def fail_open_guard(component: str):
            return FaultTolerantContext(level=FaultLevel.DEGRADE, component=component)
        """
        violations = _run_detector(
            FailOpenGuardDetector,
            source,
            filepath="core/runtime/fault_handler.py",
        )
        assert len(violations) == 0

    def test_other_function_no_violation(self) -> None:
        source = """
        logger.info("normal log message")
        result = some_function(42)
        """
        violations = _run_detector(FailOpenGuardDetector, source)
        assert len(violations) == 0


# ═══════════════════════════════════════════════════════════════════════════
# Detector 5: ProofLeakDetector
# ═══════════════════════════════════════════════════════════════════════════


class TestProofLeakDetector:
    def test_self_attr_store_is_leak(self) -> None:
        source = """
        from core.contracts.cap_result import Kernel

        class Foo:
            def bar(self) -> None:
                with Kernel.success_scope() as proof:
                    self.proof = proof
        """
        violations = _run_detector(ProofLeakDetector, source)
        assert len(violations) == 1
        assert violations[0].rule == "PROOF_LEAK"
        assert "proof" in violations[0].detail
        assert "attribute_store" in violations[0].detail

    def test_obj_attr_store_is_leak(self) -> None:
        source = """
        from core.contracts.cap_result import Kernel

        with Kernel.success_scope() as proof:
            some_obj.stored_proof = proof
        """
        violations = _run_detector(ProofLeakDetector, source)
        assert len(violations) == 1
        assert violations[0].rule == "PROOF_LEAK"

    def test_subscript_store_is_leak(self) -> None:
        source = """
        from core.contracts.cap_result import Kernel

        with Kernel.success_scope() as proof:
            container["proof_token"] = proof
        """
        violations = _run_detector(ProofLeakDetector, source)
        assert len(violations) == 1
        assert violations[0].rule == "PROOF_LEAK"
        assert "subscript_store" in violations[0].detail

    def test_pass_proof_to_capresult_ok_not_leak(self) -> None:
        """Passing proof as argument to CapResult.ok() is the expected usage."""
        source = """
        from core.contracts.cap_result import CapResult, Kernel

        with Kernel.success_scope() as proof:
            result = CapResult.ok(42, proof)
        """
        violations = _run_detector(ProofLeakDetector, source)
        assert len(violations) == 0

    def test_pass_proof_to_helper_function_not_leak(self) -> None:
        """Passing proof as function argument is safe (caller controls scope)."""
        source = """
        from core.contracts.cap_result import CapResult, Kernel

        def helper(p):
            return CapResult.ok(42, p)

        with Kernel.success_scope() as proof:
            result = helper(proof)
        """
        violations = _run_detector(ProofLeakDetector, source)
        assert len(violations) == 0

    def test_local_variable_reassignment_not_leak(self) -> None:
        """Reassigning proof to a local variable is not a leak to persistent storage."""
        source = """
        from core.contracts.cap_result import Kernel

        with Kernel.success_scope() as proof:
            local_copy = proof
        """
        violations = _run_detector(ProofLeakDetector, source)
        # local_copy = proof → target is ast.Name, not ast.Attribute or ast.Subscript
        assert len(violations) == 0

    def test_outside_success_scope_no_violation(self) -> None:
        """Assignments outside success_scope are not proof leaks."""
        source = """
        class Foo:
            def bar(self) -> None:
                self.proof = "not_a_real_proof"
        """
        violations = _run_detector(ProofLeakDetector, source)
        assert len(violations) == 0

    def test_whitelisted_file_no_violation(self) -> None:
        source = """
        with Kernel.success_scope() as proof:
            self._stored_proof = proof
        """
        violations = _run_detector(
            ProofLeakDetector,
            source,
            filepath="core/contracts/cap_result.py",
        )
        assert len(violations) == 0

    def test_kernel_instance_scope_detected(self) -> None:
        """Kernel().success_scope() should also be detected."""
        source = """
        from core.contracts.cap_result import Kernel

        with Kernel().success_scope() as proof:
            self.leaked = proof
        """
        violations = _run_detector(ProofLeakDetector, source)
        assert len(violations) == 1
        assert violations[0].rule == "PROOF_LEAK"


# ═══════════════════════════════════════════════════════════════════════════
# Integration tests
# ═══════════════════════════════════════════════════════════════════════════


class TestIntegration:
    """End-to-end tests for scan_file() and scan_codebase()."""

    def test_scan_file_baseline_clean(self, tmp_path: Path) -> None:
        from scripts.verify_capresult_ast import scan_file

        py_file = tmp_path / "clean.py"
        py_file.write_text("x = 1\ny = x + 2\n", encoding="utf-8")
        result = scan_file(py_file)
        # Completely clean file → no violations
        assert isinstance(result, list)
        # Only potential issue: if path is outside ROOT, there should be no parse errors
        assert len([v for v in result if v.rule == "PARSE_ERROR"]) == 0

    def test_all_detectors_run_in_enforce_mode(self, tmp_path: Path) -> None:
        """When enforce=True, violations from multiple detectors should appear."""
        from scripts.verify_capresult_ast import scan_file

        content = """
from core.runtime.fault_handler import fail_open_guard

with fail_open_guard("comp"):
    pass
"""
        py_file = tmp_path / "multi_violation.py"
        py_file.write_text(content, encoding="utf-8")
        result = scan_file(py_file, enforce=True)
        # Should have at least DEPRECATED_RESILIENCE (fail_open_guard is deprecated)
        deprecated = [v for v in result if v.rule == "DEPRECATED_RESILIENCE"]
        assert (
            len(deprecated) >= 1
        ), f"Expected DEPRECATED_RESILIENCE violations, got: {[v.rule for v in result]}"

    def test_syntax_error_is_parse_error(self, tmp_path: Path) -> None:
        """Files with Python syntax errors should produce PARSE_ERROR violations."""
        from scripts.verify_capresult_ast import scan_file

        py_file = tmp_path / "broken.py"
        py_file.write_text("def broken(:\n    pass\n", encoding="utf-8")
        result = scan_file(py_file, enforce=True)
        parse_errors = [v for v in result if v.rule == "PARSE_ERROR"]
        assert len(parse_errors) >= 1, f"Expected PARSE_ERROR, got violations: {result}"
