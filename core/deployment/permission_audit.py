"""Operation permission audit trail with role-based access matrix.

Records every sensitive operation (who did what, when, from where) and
validates each against a configurable permission matrix.

Usage:
    from core.deployment.permission_audit import AuditTrail, PermissionMatrix

    matrix = PermissionMatrix.with_defaults()
    trail = AuditTrail(matrix, log_dir="audit/logs")

    # Record and validate an operation
    ok = trail.record(
        actor="ops_user",
        operation="deployment.promote",
        resource="blue_green:green",
        detail={"version": "v3.0.0"},
    )
    if not ok:
        raise PermissionError("Not authorized")

    # Query the audit log
    entries = trail.query(actor="ops_user", operation="deployment.promote")
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Domain types ──────────────────────────────────────────────────────────────


@dataclass
class AuditEntry:
    """One recorded operation in the audit trail."""

    timestamp: str
    actor: str
    operation: str
    resource: str
    result: str  # "allowed" | "denied" | "error"
    detail: dict[str, Any] = field(default_factory=dict)
    source_ip: str = ""
    session_id: str = ""
    entry_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "actor": self.actor,
            "operation": self.operation,
            "resource": self.resource,
            "result": self.result,
            "detail": self.detail,
            "source_ip": self.source_ip,
            "session_id": self.session_id,
            "entry_id": self.entry_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AuditEntry:
        return cls(
            timestamp=d.get("timestamp", ""),
            actor=d.get("actor", ""),
            operation=d.get("operation", ""),
            resource=d.get("resource", ""),
            result=d.get("result", "error"),
            detail=d.get("detail", {}),
            source_ip=d.get("source_ip", ""),
            session_id=d.get("session_id", ""),
            entry_id=d.get("entry_id", ""),
        )


# ── Permission matrix ─────────────────────────────────────────────────────────


class PermissionMatrix:
    """Role-based permission matrix.

    Permission rules support exact match and wildcard patterns:
    - ``"*"`` matches everything
    - ``"deployment.*"`` matches all deployment operations
    - ``"*.read"`` matches all read operations
    """

    def __init__(self, rules: dict[str, list[str]] | None = None) -> None:
        self._rules: dict[str, list[str]] = {}
        if rules:
            for role, permissions in rules.items():
                self._rules[role] = list(permissions)

    def grant(self, role: str, permission: str) -> None:
        self._rules.setdefault(role, []).append(permission)

    def revoke(self, role: str, permission: str) -> None:
        if role in self._rules:
            self._rules[role] = [p for p in self._rules[role] if p != permission]

    def check(self, role: str, operation: str) -> bool:
        """Check if a role is permitted to perform an operation."""
        if role not in self._rules:
            return False
        for perm in self._rules[role]:
            if self._match(perm, operation):
                return True
        return False

    def roles(self) -> list[str]:
        return sorted(self._rules.keys())

    def permissions(self, role: str) -> list[str]:
        return list(self._rules.get(role, []))

    def to_dict(self) -> dict[str, list[str]]:
        return {role: list(perms) for role, perms in self._rules.items()}

    @staticmethod
    def _match(pattern: str, operation: str) -> bool:
        if pattern == "*":
            return True
        if pattern.endswith(".*"):
            prefix = pattern[:-2]
            return operation.startswith(prefix + ".")
        if pattern.startswith("*."):
            suffix = pattern[2:]
            return operation.endswith("." + suffix)
        return pattern == operation

    @classmethod
    def with_defaults(cls) -> PermissionMatrix:
        """Create a permission matrix with sensible institutional defaults."""
        return cls(
            {
                "admin": ["*"],
                "operator": [
                    "deployment.*",
                    "monitoring.*",
                    "trading.shadow",
                    "brain.reload",
                    "data.export",
                    "audit.read",
                ],
                "trader": [
                    "trading.*",
                    "monitoring.read",
                    "brain.status",
                    "data.export",
                ],
                "quant": [
                    "training.*",
                    "backtest.*",
                    "brain.*",
                    "data.*",
                    "monitoring.read",
                    "deployment.read",
                    "audit.read",
                ],
                "auditor": [
                    "audit.*",
                    "monitoring.read",
                    "deployment.read",
                    "trading.read",
                    "data.read",
                ],
                "viewer": [
                    "monitoring.read",
                    "deployment.read",
                    "trading.read",
                ],
            }
        )


# ── Audit trail ───────────────────────────────────────────────────────────────


class AuditTrail:
    """Append-only operation audit log with permission enforcement.

    Stores entries as JSONL files, rotated daily. Queries support
    filtering by actor, operation, result, and time range.
    """

    def __init__(
        self,
        matrix: PermissionMatrix | None = None,
        *,
        log_dir: str = "audit/logs",
        auto_validate: bool = True,
    ) -> None:
        self._matrix = matrix or PermissionMatrix.with_defaults()
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._auto_validate = auto_validate

    def record(
        self,
        *,
        actor: str,
        operation: str,
        resource: str = "",
        role: str = "",
        detail: dict[str, Any] | None = None,
        source_ip: str = "",
        session_id: str = "",
    ) -> bool:
        """Record an operation, optionally validating against the permission matrix.

        Returns True if allowed, False if denied (when auto_validate is on).
        When auto_validate is off, always returns True (record-only mode).
        """
        import uuid

        allowed = True
        if self._auto_validate and role:
            allowed = self._matrix.check(role, operation)

        entry = AuditEntry(
            timestamp=datetime.now(UTC).replace(tzinfo=None).isoformat(),
            actor=actor,
            operation=operation,
            resource=resource,
            result="allowed" if allowed else "denied",
            detail=detail or {},
            source_ip=source_ip,
            session_id=session_id,
            entry_id=uuid.uuid4().hex[:16],
        )

        self._write_entry(entry)

        if not allowed:
            logger.warning(
                "Permission denied: actor=%s role=%s operation=%s resource=%s",
                actor,
                role,
                operation,
                resource,
            )
        return allowed

    def query(
        self,
        *,
        actor: str = "",
        operation: str = "",
        result: str = "",
        start: str = "",
        end: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query audit entries with optional filters."""
        results: list[dict[str, Any]] = []
        log_files = sorted(self._log_dir.glob("audit_*.jsonl"), reverse=True)

        for f in log_files:
            if len(results) >= limit:
                break
            try:
                for line in reversed(f.read_text(encoding="utf-8").strip().split("\n")):
                    if not line:
                        continue
                    entry = json.loads(line)
                    if actor and entry.get("actor") != actor:
                        continue
                    if operation and entry.get("operation") != operation:
                        continue
                    if result and entry.get("result") != result:
                        continue
                    if start and entry.get("timestamp", "") < start:
                        continue
                    if end and entry.get("timestamp", "") > end:
                        continue
                    results.append(entry)
                    if len(results) >= limit:
                        break
            except Exception:  # noqa: BLE001
                pass

        return results

    def denied_operations(self, since: str = "", limit: int = 50) -> list[dict[str, Any]]:
        """Return recent denied operations for security review."""
        return self.query(result="denied", start=since, limit=limit)

    def actor_summary(self, actor: str, limit: int = 100) -> dict[str, Any]:
        """Return a summary of operations by a specific actor."""
        entries = self.query(actor=actor, limit=limit)
        operations: dict[str, int] = {}
        results: dict[str, int] = {}
        for e in entries:
            op = e.get("operation", "unknown")
            operations[op] = operations.get(op, 0) + 1
            res = e.get("result", "unknown")
            results[res] = results.get(res, 0) + 1
        return {
            "actor": actor,
            "total_entries": len(entries),
            "by_operation": operations,
            "by_result": results,
            "latest_timestamp": entries[0]["timestamp"] if entries else "",
        }

    def export(self, output_path: str, *, start: str = "", end: str = "") -> str:
        """Export audit trail to a single JSON file."""
        entries = self.query(start=start, end=end, limit=10_000)
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, indent=2, default=str), encoding="utf-8")
        return str(path)

    @property
    def log_dir(self) -> str:
        return str(self._log_dir)

    # ── Internal ───────────────────────────────────────────────────────────

    def _current_log_path(self) -> Path:
        today = datetime.now(UTC).strftime("%Y%m%d")
        return self._log_dir / f"audit_{today}.jsonl"

    def _write_entry(self, entry: AuditEntry) -> None:
        path = self._current_log_path()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict(), default=str) + "\n")
