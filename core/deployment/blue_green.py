"""Blue-green deployment manager for zero-downtime live trading cutovers.

Maintains two deployment slots (BLUE / GREEN). At any time one is "live"
(sending real orders) and the other is "standby" (shadow-recording). Promotion
swaps the roles after a health check, with automatic rollback on failure.

Usage:
    from core.deployment.blue_green import BlueGreenManager

    mgr = BlueGreenManager(state_dir="deployments/state")
    mgr.promote()       # cut over to standby after health check
    mgr.rollback()      # revert to previous live slot
    print(mgr.status()) # current deployment topology
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Domain types ──────────────────────────────────────────────────────────────


class SlotState(str, Enum):
    LIVE = "live"
    STANDBY = "standby"
    DRAINING = "draining"
    PROVISIONING = "provisioning"
    FAILED = "failed"


class SlotColor(str, Enum):
    BLUE = "blue"
    GREEN = "green"


@dataclass
class DeploymentSlot:
    """Runtime state for one deployment slot."""

    color: SlotColor
    state: SlotState
    process_id: int | None = None
    port: int = 0
    brain_id: str = ""
    started_at: str = ""
    health_check_at: str = ""
    health_status: str = "unknown"
    error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "color": self.color.value,
            "state": self.state.value,
            "process_id": self.process_id,
            "port": self.port,
            "brain_id": self.brain_id,
            "started_at": self.started_at,
            "health_check_at": self.health_check_at,
            "health_status": self.health_status,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DeploymentSlot:
        return cls(
            color=SlotColor(d.get("color", "blue")),
            state=SlotState(d.get("state", "standby")),
            process_id=d.get("process_id"),
            port=d.get("port", 0),
            brain_id=d.get("brain_id", ""),
            started_at=d.get("started_at", ""),
            health_check_at=d.get("health_check_at", ""),
            health_status=d.get("health_status", "unknown"),
            error_message=d.get("error_message", ""),
        )


@dataclass
class DeploymentTopology:
    """Snapshot of both deployment slots and which is live."""

    blue: DeploymentSlot
    green: DeploymentSlot
    active_color: SlotColor
    deployed_at: str
    deployed_by: str = ""
    version: str = ""

    def live_slot(self) -> DeploymentSlot:
        return self.blue if self.active_color == SlotColor.BLUE else self.green

    def standby_slot(self) -> DeploymentSlot:
        return self.green if self.active_color == SlotColor.BLUE else self.blue

    def to_dict(self) -> dict[str, Any]:
        return {
            "blue": self.blue.to_dict(),
            "green": self.green.to_dict(),
            "active_color": self.active_color.value,
            "deployed_at": self.deployed_at,
            "deployed_by": self.deployed_by,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DeploymentTopology:
        return cls(
            blue=DeploymentSlot.from_dict(d.get("blue", {})),
            green=DeploymentSlot.from_dict(d.get("green", {})),
            active_color=SlotColor(d.get("active_color", "blue")),
            deployed_at=d.get("deployed_at", ""),
            deployed_by=d.get("deployed_by", ""),
            version=d.get("version", ""),
        )


@dataclass
class CutoverResult:
    """Outcome of a blue-green promotion attempt."""

    success: bool
    previous_active: SlotColor
    new_active: SlotColor
    health_check_passed: bool
    cutover_duration_ms: float
    rolled_back: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "previous_active": self.previous_active.value,
            "new_active": self.new_active.value,
            "health_check_passed": self.health_check_passed,
            "cutover_duration_ms": self.cutover_duration_ms,
            "rolled_back": self.rolled_back,
            "error": self.error,
        }


# ── Health probe ──────────────────────────────────────────────────────────────


class HealthProbe:
    """Validates that a deployment slot is healthy enough to receive traffic.

    Checks: process alive, recent heartbeat, no error state, brain loaded.
    """

    def __init__(
        self,
        *,
        required_checks: list[str] | None = None,
        health_timeout_seconds: float = 10.0,
        min_uptime_seconds: float = 30.0,
    ) -> None:
        self.required_checks = required_checks or [
            "ledger_store",
            "risk_service",
            "dispatcher",
        ]
        self.health_timeout_seconds = health_timeout_seconds
        self.min_uptime_seconds = min_uptime_seconds

    def check(self, slot: DeploymentSlot) -> dict[str, Any]:
        """Run health checks against a deployment slot.

        Returns a dict with 'healthy' (bool) and per-check results.
        In production this would call the process's health endpoint;
        here we validate based on slot state and process liveness.
        """
        checks: dict[str, bool] = {}
        started = time.time()

        # Check 1: Process exists
        pid_ok = self._check_process_alive(slot)
        checks["process_alive"] = pid_ok

        # Check 2: Slot not in failed state
        state_ok = slot.state not in (SlotState.FAILED,)
        checks["slot_state_ok"] = state_ok

        # Check 3: Has a brain ID assigned
        brain_ok = bool(slot.brain_id)
        checks["brain_assigned"] = brain_ok

        # Check 4: Recent health check
        heartbeat_ok = self._check_recent_heartbeat(slot)
        checks["heartbeat_recent"] = heartbeat_ok

        # Check 5: Minimum uptime (prevents flip-flop)
        uptime_ok = self._check_min_uptime(slot)
        checks["min_uptime"] = uptime_ok

        elapsed_ms = (time.time() - started) * 1000
        healthy = all(checks.values())

        return {
            "healthy": healthy,
            "checks": checks,
            "elapsed_ms": round(elapsed_ms, 2),
            "slot": slot.color.value,
        }

    def _check_process_alive(self, slot: DeploymentSlot) -> bool:
        if slot.process_id is None:
            return False
        try:
            import os

            os.kill(slot.process_id, 0)
            return True
        except OSError:
            return False

    def _check_recent_heartbeat(self, slot: DeploymentSlot) -> bool:
        if not slot.health_check_at:
            return False
        try:
            last = datetime.fromisoformat(slot.health_check_at)
            age = (
                datetime.now(UTC).replace(tzinfo=None) - last.replace(tzinfo=None)
            ).total_seconds()
            return age < self.health_timeout_seconds * 3
        except (ValueError, TypeError):
            return False

    def _check_min_uptime(self, slot: DeploymentSlot) -> bool:
        if not slot.started_at:
            return True  # unknown — don't block
        try:
            started = datetime.fromisoformat(slot.started_at)
            uptime = (
                datetime.now(UTC).replace(tzinfo=None) - started.replace(tzinfo=None)
            ).total_seconds()
            return uptime >= self.min_uptime_seconds
        except (ValueError, TypeError):
            return True


# ── Blue-green manager ───────────────────────────────────────────────────────


class BlueGreenManager:
    """Orchestrates blue-green deployment cutovers.

    Maintains a state file so deployment topology survives process restarts.
    """

    def __init__(
        self,
        *,
        state_dir: str = "deployments/state",
        health_probe: HealthProbe | None = None,
        pre_cutover_hooks: list[Callable[..., Any]] | None = None,
        post_cutover_hooks: list[Callable[..., Any]] | None = None,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = self._state_dir / "topology.json"
        self._history_dir = self._state_dir / "history"
        self._history_dir.mkdir(parents=True, exist_ok=True)

        self._health_probe = health_probe or HealthProbe()
        self._pre_cutover_hooks = pre_cutover_hooks or []
        self._post_cutover_hooks = post_cutover_hooks or []

        self._topology = self._load_or_init()

    # ── Public API ────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """Return current deployment topology as a dict."""
        topo = self._topology.to_dict()
        topo["live"] = self._topology.live_slot().to_dict()
        topo["standby"] = self._topology.standby_slot().to_dict()
        return topo

    def promote(
        self,
        *,
        deployed_by: str = "",
        version: str = "",
        skip_health_check: bool = False,
        drain_timeout_seconds: float = 5.0,
    ) -> CutoverResult:
        """Promote the standby slot to live.

        1. Health-check standby
        2. Drain current live (stop new orders)
        3. Switch active → standby
        4. Health-check new live
        5. On failure: automatic rollback
        """
        t0 = time.time()
        current_live = self._topology.live_slot()
        standby = self._topology.standby_slot()

        # Step 1: Health-check standby
        if not skip_health_check:
            health = self._health_probe.check(standby)
            standby.health_check_at = datetime.now(UTC).replace(tzinfo=None).isoformat()
            standby.health_status = "healthy" if health["healthy"] else "unhealthy"
            self._save()

            if not health["healthy"]:
                return CutoverResult(
                    success=False,
                    previous_active=self._topology.active_color,
                    new_active=standby.color,
                    health_check_passed=False,
                    cutover_duration_ms=(time.time() - t0) * 1000,
                    error=f"Standby health check failed: {health['checks']}",
                )

        # Step 2: Drain current live
        current_live.state = SlotState.DRAINING
        self._save()

        for hook in self._pre_cutover_hooks:
            try:
                hook(self._topology)
            except Exception:
                logger.exception("Pre-cutover hook failed")

        time.sleep(min(drain_timeout_seconds, 5.0))

        # Step 3: Switch
        previous_color = self._topology.active_color
        current_live.state = SlotState.STANDBY
        standby.state = SlotState.LIVE
        self._topology.active_color = standby.color
        self._topology.deployed_at = datetime.now(UTC).replace(tzinfo=None).isoformat()
        if deployed_by:
            self._topology.deployed_by = deployed_by
        if version:
            self._topology.version = version
        self._save()

        # Step 4: Health-check new live
        if not skip_health_check:
            new_health = self._health_probe.check(standby)
            standby.health_check_at = datetime.now(UTC).replace(tzinfo=None).isoformat()
            standby.health_status = "healthy" if new_health["healthy"] else "unhealthy"
            self._save()

            if not new_health["healthy"]:
                # Auto-rollback
                logger.error("New live slot unhealthy after cutover — rolling back")
                self._rollback_swap()
                return CutoverResult(
                    success=False,
                    previous_active=previous_color,
                    new_active=standby.color,
                    health_check_passed=False,
                    cutover_duration_ms=(time.time() - t0) * 1000,
                    rolled_back=True,
                    error=f"Post-cutover health check failed: {new_health['checks']}",
                )

        # Step 5: Post-cutover hooks
        for hook in self._post_cutover_hooks:
            try:
                hook(self._topology)
            except Exception:
                logger.exception("Post-cutover hook failed")

        # Archive the cutover record
        self._archive_cutover(previous_color, standby.color, success=True)

        return CutoverResult(
            success=True,
            previous_active=previous_color,
            new_active=standby.color,
            health_check_passed=True,
            cutover_duration_ms=(time.time() - t0) * 1000,
        )

    def rollback(self) -> CutoverResult:
        """Revert to the previously-active slot."""
        t0 = time.time()
        previous = self._topology.active_color
        self._rollback_swap()
        new_color = self._topology.active_color

        self._archive_cutover(previous, new_color, success=True)

        return CutoverResult(
            success=True,
            previous_active=previous,
            new_active=new_color,
            health_check_passed=True,
            cutover_duration_ms=(time.time() - t0) * 1000,
        )

    def register_slot(
        self,
        color: SlotColor,
        *,
        process_id: int | None = None,
        port: int = 0,
        brain_id: str = "",
    ) -> None:
        """Register or update a deployment slot."""
        slot = self._topology.blue if color == SlotColor.BLUE else self._topology.green
        if process_id is not None:
            slot.process_id = process_id
        if port:
            slot.port = port
        if brain_id:
            slot.brain_id = brain_id
        if not slot.started_at:
            slot.started_at = datetime.now(UTC).replace(tzinfo=None).isoformat()
        slot.state = SlotState.STANDBY if color != self._topology.active_color else SlotState.LIVE
        self._save()

    def health_check(self, color: SlotColor | None = None) -> dict[str, Any]:
        """Run health probe against one or both slots."""
        if color:
            slot = self._get_slot(color)
            result = self._health_probe.check(slot)
            slot.health_check_at = datetime.now(UTC).replace(tzinfo=None).isoformat()
            slot.health_status = "healthy" if result["healthy"] else "unhealthy"
            self._save()
            return result

        blue_result = self._health_probe.check(self._topology.blue)
        green_result = self._health_probe.check(self._topology.green)
        self._topology.blue.health_check_at = datetime.now(UTC).replace(tzinfo=None).isoformat()
        self._topology.green.health_check_at = datetime.now(UTC).replace(tzinfo=None).isoformat()
        self._topology.blue.health_status = "healthy" if blue_result["healthy"] else "unhealthy"
        self._topology.green.health_status = "healthy" if green_result["healthy"] else "unhealthy"
        self._save()
        return {"blue": blue_result, "green": green_result}

    def mark_failed(self, color: SlotColor, error: str = "") -> None:
        """Mark a slot as failed (e.g., after repeated health check failures)."""
        slot = self._get_slot(color)
        slot.state = SlotState.FAILED
        slot.error_message = error
        self._save()

    # ── Internal ───────────────────────────────────────────────────────────

    def _get_slot(self, color: SlotColor) -> DeploymentSlot:
        if color == SlotColor.BLUE:
            return self._topology.blue
        return self._topology.green

    def _rollback_swap(self) -> None:
        """Swap active color without health checks."""
        live = self._topology.live_slot()
        standby = self._topology.standby_slot()
        live.state = SlotState.STANDBY
        standby.state = SlotState.LIVE
        self._topology.active_color = standby.color
        self._save()

    def _save(self) -> None:
        """Persist topology to disk atomically."""
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._topology.to_dict(), indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(self._state_file)

    def _load_or_init(self) -> DeploymentTopology:
        if self._state_file.exists():
            try:
                data = json.loads(self._state_file.read_text(encoding="utf-8"))
                return DeploymentTopology.from_dict(data)
            except Exception:
                logger.exception("Failed to load topology, reinitializing")
        return self._init_topology()

    def _init_topology(self) -> DeploymentTopology:
        now = datetime.now(UTC).replace(tzinfo=None).isoformat()
        return DeploymentTopology(
            blue=DeploymentSlot(
                color=SlotColor.BLUE,
                state=SlotState.LIVE,
                started_at=now,
            ),
            green=DeploymentSlot(
                color=SlotColor.GREEN,
                state=SlotState.STANDBY,
                started_at=now,
            ),
            active_color=SlotColor.BLUE,
            deployed_at=now,
        )

    def _archive_cutover(self, previous: SlotColor, new: SlotColor, *, success: bool) -> None:
        record = {
            "timestamp": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "previous_active": previous.value,
            "new_active": new.value,
            "success": success,
            "version": self._topology.version,
            "deployed_by": self._topology.deployed_by,
        }
        fname = f"cutover_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
        (self._history_dir / fname).write_text(json.dumps(record, indent=2), encoding="utf-8")

    def cutover_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent cutover records."""
        files = sorted(self._history_dir.glob("cutover_*.json"), reverse=True)
        records: list[dict[str, Any]] = []
        for f in files[:limit]:
            try:  # noqa: SIM105
                records.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:  # BLE001:REVIEWED
                pass
        return records
