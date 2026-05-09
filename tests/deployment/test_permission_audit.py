"""Tests for permission audit trail and role-based access matrix."""

from __future__ import annotations

import json
import os

import pytest

from core.deployment.permission_audit import (
    AuditEntry,
    AuditTrail,
    PermissionMatrix,
)

# ── PermissionMatrix ──────────────────────────────────────────────────────────


class TestPermissionMatrix:
    def test_exact_match(self):
        pm = PermissionMatrix({"admin": ["trading.execute"]})
        assert pm.check("admin", "trading.execute")
        assert not pm.check("admin", "trading.read")

    def test_wildcard_all(self):
        pm = PermissionMatrix({"admin": ["*"]})
        assert pm.check("admin", "anything.here")
        assert pm.check("admin", "trading.execute")

    def test_prefix_wildcard(self):
        pm = PermissionMatrix({"operator": ["deployment.*"]})
        assert pm.check("operator", "deployment.promote")
        assert pm.check("operator", "deployment.rollback")
        assert not pm.check("operator", "trading.execute")

    def test_suffix_wildcard(self):
        pm = PermissionMatrix({"viewer": ["*.read"]})
        assert pm.check("viewer", "trading.read")
        assert pm.check("viewer", "deployment.read")
        assert not pm.check("viewer", "trading.execute")

    def test_unknown_role_denied(self):
        pm = PermissionMatrix()
        assert not pm.check("unknown", "anything")

    def test_grant_and_revoke(self):
        pm = PermissionMatrix()
        pm.grant("trader", "trading.execute")
        assert pm.check("trader", "trading.execute")
        pm.revoke("trader", "trading.execute")
        assert not pm.check("trader", "trading.execute")

    def test_multiple_permissions_per_role(self):
        pm = PermissionMatrix({"quant": ["training.run", "backtest.run", "data.read"]})
        assert pm.check("quant", "training.run")
        assert pm.check("quant", "backtest.run")
        assert pm.check("quant", "data.read")
        assert not pm.check("quant", "trading.execute")

    def test_roles_list(self):
        pm = PermissionMatrix.with_defaults()
        roles = pm.roles()
        assert "admin" in roles
        assert "operator" in roles
        assert "auditor" in roles

    def test_permissions_list(self):
        pm = PermissionMatrix.with_defaults()
        perms = pm.permissions("admin")
        assert "*" in perms

    def test_to_dict_roundtrip(self):
        pm = PermissionMatrix.with_defaults()
        d = pm.to_dict()
        restored = PermissionMatrix(d)
        assert restored.roles() == pm.roles()
        assert restored.check("admin", "anything")


# ── AuditEntry ────────────────────────────────────────────────────────────────


class TestAuditEntry:
    def test_to_dict_and_back(self):
        entry = AuditEntry(
            timestamp="2026-05-01T12:00:00",
            actor="ops_user",
            operation="deployment.promote",
            resource="blue_green:green",
            result="allowed",
            detail={"version": "v3.0.0"},
            source_ip="10.0.0.1",
            session_id="sess_abc",
            entry_id="e123",
        )
        d = entry.to_dict()
        restored = AuditEntry.from_dict(d)
        assert restored.actor == "ops_user"
        assert restored.operation == "deployment.promote"
        assert restored.result == "allowed"
        assert restored.detail["version"] == "v3.0.0"

    def test_from_dict_defaults(self):
        entry = AuditEntry.from_dict({})
        assert entry.actor == ""
        assert entry.result == "error"


# ── AuditTrail ────────────────────────────────────────────────────────────────


class TestAuditTrail:
    @pytest.fixture
    def tmp_log_dir(self, tmp_path):
        return str(tmp_path / "audit_test_logs")

    @pytest.fixture
    def audit(self, tmp_log_dir):
        matrix = PermissionMatrix({"ops": ["deployment.*", "trading.read"]})
        return AuditTrail(matrix, log_dir=tmp_log_dir)

    def test_record_allowed_operation(self, audit):
        ok = audit.record(
            actor="alice",
            operation="deployment.promote",
            role="ops",
            detail={"version": "v2"},
        )
        assert ok is True

    def test_record_denied_operation(self, audit):
        ok = audit.record(
            actor="bob",
            operation="trading.execute",
            role="ops",
        )
        assert ok is False

    def test_record_without_validation(self, tmp_log_dir):
        audit = AuditTrail(log_dir=tmp_log_dir, auto_validate=False)
        ok = audit.record(
            actor="carol",
            operation="any.operation",
            role="nonexistent",
        )
        assert ok is True

    def test_query_by_actor(self, audit):
        audit.record(actor="alice", operation="deployment.promote", role="ops")
        audit.record(actor="bob", operation="trading.read", role="ops")
        audit.record(actor="alice", operation="deployment.rollback", role="ops")

        results = audit.query(actor="alice")
        assert len(results) == 2
        assert all(r["actor"] == "alice" for r in results)

    def test_query_by_operation(self, audit):
        audit.record(actor="alice", operation="deployment.promote", role="ops")
        audit.record(actor="bob", operation="deployment.promote", role="ops")

        results = audit.query(operation="deployment.promote")
        assert len(results) == 2

    def test_query_by_result(self, audit):
        audit.record(actor="alice", operation="deployment.promote", role="ops")
        audit.record(actor="bob", operation="trading.execute", role="ops")

        denied = audit.query(result="denied")
        assert len(denied) == 1
        assert denied[0]["actor"] == "bob"

    def test_query_limit(self, audit):
        for i in range(10):
            audit.record(actor=f"user_{i}", operation="deployment.promote", role="ops")
        results = audit.query(limit=5)
        assert len(results) == 5

    def test_denied_operations(self, audit):
        audit.record(actor="bob", operation="trading.execute", role="ops")
        denied = audit.denied_operations()
        assert len(denied) == 1
        assert denied[0]["result"] == "denied"

    def test_actor_summary(self, audit):
        audit.record(actor="alice", operation="deployment.promote", role="ops")
        audit.record(actor="alice", operation="deployment.promote", role="ops")
        audit.record(actor="alice", operation="trading.read", role="ops")

        summary = audit.actor_summary("alice")
        assert summary["actor"] == "alice"
        assert summary["by_operation"]["deployment.promote"] == 2
        assert summary["by_operation"]["trading.read"] == 1

    def test_export(self, audit, tmp_path):
        audit.record(actor="alice", operation="deployment.promote", role="ops")
        out = tmp_path / "exports" / "audit.json"
        path = audit.export(str(out))
        assert os.path.exists(path)
        data = json.loads(open(path).read())
        assert len(data) == 1

    def test_empty_query_returns_list(self, audit):
        results = audit.query()
        assert results == []


# ── Default matrix completeness ───────────────────────────────────────────────


class TestDefaultPermissionMatrix:
    def test_admin_can_do_anything(self):
        pm = PermissionMatrix.with_defaults()
        assert pm.check("admin", "any.random.operation")

    def test_operator_cannot_trade_live(self):
        pm = PermissionMatrix.with_defaults()
        # operator has deployment.* and trading.shadow only
        assert pm.check("operator", "trading.shadow")
        assert not pm.check("operator", "trading.execute")

    def test_auditor_read_only(self):
        pm = PermissionMatrix.with_defaults()
        assert pm.check("auditor", "audit.read")
        assert pm.check("auditor", "monitoring.read")
        assert not pm.check("auditor", "trading.execute")
        assert not pm.check("auditor", "deployment.promote")

    def test_viewer_read_only(self):
        pm = PermissionMatrix.with_defaults()
        assert pm.check("viewer", "monitoring.read")
        assert not pm.check("viewer", "trading.execute")
