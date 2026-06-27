"""Tests for blue-green deployment manager."""

from __future__ import annotations

import os

import pytest

from core.deployment.blue_green import (
    BlueGreenManager,
    CutoverResult,
    DeploymentSlot,
    DeploymentTopology,
    HealthProbe,
    SlotColor,
    SlotState,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_state_dir(tmp_path):
    return str(tmp_path / "deployments" / "state")


@pytest.fixture
def mgr(temp_state_dir):
    return BlueGreenManager(state_dir=temp_state_dir)


# ── SlotState / SlotColor ─────────────────────────────────────────────────────


class TestEnums:
    def test_slot_state_values(self):
        assert SlotState.LIVE.value == "live"
        assert SlotState.STANDBY.value == "standby"
        assert SlotState.DRAINING.value == "draining"
        assert SlotState.FAILED.value == "failed"

    def test_slot_color_values(self):
        assert SlotColor.BLUE.value == "blue"
        assert SlotColor.GREEN.value == "green"


# ── DeploymentSlot ────────────────────────────────────────────────────────────


class TestDeploymentSlot:
    def test_default_standby(self):
        slot = DeploymentSlot(color=SlotColor.BLUE, state=SlotState.STANDBY)
        assert slot.color == SlotColor.BLUE
        assert slot.state == SlotState.STANDBY
        assert slot.process_id is None
        assert slot.port == 0

    def test_to_dict_and_back(self):
        slot = DeploymentSlot(
            color=SlotColor.GREEN,
            state=SlotState.LIVE,
            process_id=12345,
            port=8001,
            brain_id="v9_institutional_01",
            started_at="2026-05-01T00:00:00",
            health_status="healthy",
        )
        d = slot.to_dict()
        restored = DeploymentSlot.from_dict(d)
        assert restored.color == SlotColor.GREEN
        assert restored.state == SlotState.LIVE
        assert restored.process_id == 12345
        assert restored.port == 8001
        assert restored.brain_id == "v9_institutional_01"
        assert restored.health_status == "healthy"

    def test_from_dict_defaults(self):
        slot = DeploymentSlot.from_dict({})
        assert slot.color == SlotColor.BLUE
        assert slot.state == SlotState.STANDBY


# ── DeploymentTopology ────────────────────────────────────────────────────────


class TestDeploymentTopology:
    def test_live_and_standby_blue_active(self):
        topo = DeploymentTopology(
            blue=DeploymentSlot(color=SlotColor.BLUE, state=SlotState.LIVE),
            green=DeploymentSlot(color=SlotColor.GREEN, state=SlotState.STANDBY),
            active_color=SlotColor.BLUE,
            deployed_at="",
        )
        assert topo.live_slot().color == SlotColor.BLUE
        assert topo.standby_slot().color == SlotColor.GREEN

    def test_live_and_standby_green_active(self):
        topo = DeploymentTopology(
            blue=DeploymentSlot(color=SlotColor.BLUE, state=SlotState.STANDBY),
            green=DeploymentSlot(color=SlotColor.GREEN, state=SlotState.LIVE),
            active_color=SlotColor.GREEN,
            deployed_at="",
        )
        assert topo.live_slot().color == SlotColor.GREEN
        assert topo.standby_slot().color == SlotColor.BLUE

    def test_to_dict_and_back(self):
        topo = DeploymentTopology(
            blue=DeploymentSlot(color=SlotColor.BLUE, state=SlotState.LIVE, port=8000),
            green=DeploymentSlot(color=SlotColor.GREEN, state=SlotState.STANDBY, port=8001),
            active_color=SlotColor.BLUE,
            deployed_at="2026-05-01T00:00:00",
            deployed_by="ops",
            version="v2.1.0",
        )
        d = topo.to_dict()
        restored = DeploymentTopology.from_dict(d)
        assert restored.active_color == SlotColor.BLUE
        assert restored.live_slot().port == 8000
        assert restored.standby_slot().port == 8001
        assert restored.version == "v2.1.0"
        assert restored.deployed_by == "ops"


# ── BlueGreenManager ──────────────────────────────────────────────────────────


class TestBlueGreenManagerInit:
    def test_creates_state_directory(self, temp_state_dir):
        BlueGreenManager(state_dir=temp_state_dir)
        assert os.path.isdir(temp_state_dir)

    def test_default_topology_blue_live(self, mgr):
        status = mgr.status()
        assert status["active_color"] == "blue"
        assert status["live"]["color"] == "blue"
        assert status["live"]["state"] == "live"
        assert status["standby"]["color"] == "green"
        assert status["standby"]["state"] == "standby"

    def test_state_survives_reload(self, temp_state_dir):
        mgr1 = BlueGreenManager(state_dir=temp_state_dir)
        mgr1.register_slot(SlotColor.GREEN, brain_id="test_brain")
        mgr2 = BlueGreenManager(state_dir=temp_state_dir)
        assert mgr2.status()["standby"]["brain_id"] == "test_brain"


class TestBlueGreenManagerRegister:
    def test_register_updates_slot(self, mgr):
        mgr.register_slot(SlotColor.BLUE, process_id=42, port=8000, brain_id="brain_1")
        status = mgr.status()
        assert status["live"]["process_id"] == 42
        assert status["live"]["port"] == 8000
        assert status["live"]["brain_id"] == "brain_1"

    def test_register_standby_does_not_change_active(self, mgr):
        mgr.register_slot(SlotColor.GREEN, brain_id="standby_brain")
        status = mgr.status()
        assert status["active_color"] == "blue"
        assert status["standby"]["state"] == "standby"

    def test_register_sets_started_at(self, mgr):
        mgr.register_slot(SlotColor.GREEN, process_id=99)
        assert mgr.status()["standby"]["started_at"] != ""


class TestBlueGreenManagerPromote:
    def test_promote_swaps_active_color(self, mgr):
        mgr.register_slot(SlotColor.GREEN, process_id=os.getpid(), brain_id="v9")
        result = mgr.promote(skip_health_check=True, drain_timeout_seconds=0)
        assert result.success
        assert result.previous_active == SlotColor.BLUE
        assert result.new_active == SlotColor.GREEN
        assert mgr.status()["active_color"] == "green"

    def test_promote_fails_when_standby_unhealthy(self, mgr):
        # No process ID → health check will fail
        result = mgr.promote()
        assert not result.success
        assert not result.health_check_passed

    def test_promote_rolls_back_on_post_cutover_failure(self, mgr, monkeypatch):
        mgr.register_slot(SlotColor.GREEN, process_id=os.getpid(), brain_id="v9")
        # Let first health check pass, but make post-cutover check fail
        call_count = 0

        def flaky_check(slot):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"healthy": True, "checks": {"all": True}, "slot": "green", "elapsed_ms": 1}
            return {"healthy": False, "checks": {"all": False}, "slot": "green", "elapsed_ms": 1}

        monkeypatch.setattr(mgr._health_probe, "check", flaky_check)

        result = mgr.promote(drain_timeout_seconds=0)
        assert not result.success
        assert result.rolled_back
        # Should have reverted to blue
        assert mgr.status()["active_color"] == "blue"

    def test_promote_records_cutover_history(self, mgr):
        mgr.register_slot(SlotColor.GREEN, process_id=os.getpid(), brain_id="v9")
        mgr.promote(skip_health_check=True, drain_timeout_seconds=0)
        history = mgr.cutover_history()
        assert len(history) >= 1
        assert history[0]["success"] is True
        assert history[0]["previous_active"] == "blue"
        assert history[0]["new_active"] == "green"

    def test_promote_updates_deployed_info(self, mgr):
        mgr.register_slot(SlotColor.GREEN, process_id=os.getpid(), brain_id="v9")
        mgr.promote(
            deployed_by="alice", version="v3.0.0", skip_health_check=True, drain_timeout_seconds=0
        )
        status = mgr.status()
        assert status["deployed_by"] == "alice"
        assert status["version"] == "v3.0.0"

    def test_promote_runs_hooks(self, mgr):
        hook_calls = []

        def pre_hook(topo):
            hook_calls.append("pre")

        def post_hook(topo):
            hook_calls.append("post")

        mgr._pre_cutover_hooks = [pre_hook]
        mgr._post_cutover_hooks = [post_hook]

        mgr.register_slot(SlotColor.GREEN, process_id=os.getpid(), brain_id="v9")
        mgr.promote(skip_health_check=True, drain_timeout_seconds=0)

        assert "pre" in hook_calls
        assert "post" in hook_calls


class TestBlueGreenManagerRollback:
    def test_rollback_reverts_active(self, mgr):
        # First promote green
        mgr.register_slot(SlotColor.GREEN, process_id=os.getpid(), brain_id="v9")
        mgr.promote(skip_health_check=True, drain_timeout_seconds=0)
        assert mgr.status()["active_color"] == "green"

        # Then rollback
        result = mgr.rollback()
        assert result.success
        assert mgr.status()["active_color"] == "blue"

    def test_rollback_records_history(self, mgr):
        mgr.rollback()
        history = mgr.cutover_history()
        assert len(history) >= 1
        assert "rollback" in history[0]["timestamp"] or history[0]["success"] is True


class TestBlueGreenManagerHealth:
    def test_health_check_both(self, mgr):
        results = mgr.health_check()
        assert "blue" in results
        assert "green" in results
        assert "healthy" in results["blue"]
        assert "healthy" in results["green"]

    def test_health_check_single(self, mgr):
        result = mgr.health_check(SlotColor.BLUE)
        assert result["slot"] == "blue"
        assert "healthy" in result

    def test_health_check_updates_slot_state(self, mgr):
        mgr.health_check()
        status = mgr.status()
        assert status["live"]["health_check_at"] != ""
        assert status["standby"]["health_check_at"] != ""


class TestBlueGreenManagerMarkFailed:
    def test_mark_failed_updates_state(self, mgr):
        mgr.mark_failed(SlotColor.GREEN, "process crashed")
        assert mgr.status()["standby"]["state"] == "failed"
        assert "process crashed" in mgr.status()["standby"]["error_message"]


class TestBlueGreenManagerHistory:
    def test_empty_history(self, mgr):
        assert mgr.cutover_history() == []

    @pytest.mark.slow  # FIX-20260627-147: 5×promote+rollback, each promote sleeps 5s
    def test_history_limited(self, mgr):
        mgr.register_slot(SlotColor.GREEN, process_id=os.getpid(), brain_id="v9")
        for _ in range(5):
            mgr.promote(skip_health_check=True)
            mgr.rollback()
        history = mgr.cutover_history(limit=3)
        assert len(history) == 3


# ── HealthProbe ───────────────────────────────────────────────────────────────


class TestHealthProbe:
    def test_dead_process_fails(self):
        probe = HealthProbe()
        slot = DeploymentSlot(
            color=SlotColor.BLUE,
            state=SlotState.LIVE,
            process_id=99999,  # nonexistent PID
        )
        result = probe.check(slot)
        assert not result["healthy"]
        assert not result["checks"]["process_alive"]

    def test_live_process_passes(self):
        probe = HealthProbe()
        slot = DeploymentSlot(
            color=SlotColor.BLUE,
            state=SlotState.LIVE,
            process_id=os.getpid(),
            brain_id="test_brain",
            started_at="2026-01-01T00:00:00",  # long ago → min uptime ok
        )
        result = probe.check(slot)
        # Process exists and brain assigned → healthy
        assert result["checks"]["process_alive"]
        assert result["checks"]["brain_assigned"]

    def test_no_process_id_fails(self):
        probe = HealthProbe()
        slot = DeploymentSlot(color=SlotColor.GREEN, state=SlotState.STANDBY)
        result = probe.check(slot)
        assert not result["checks"]["process_alive"]

    def test_failed_state_fails(self):
        probe = HealthProbe()
        slot = DeploymentSlot(
            color=SlotColor.BLUE,
            state=SlotState.FAILED,
            process_id=os.getpid(),
        )
        result = probe.check(slot)
        assert not result["checks"]["slot_state_ok"]

    def test_no_brain_id_fails(self):
        probe = HealthProbe()
        slot = DeploymentSlot(
            color=SlotColor.BLUE,
            state=SlotState.LIVE,
            process_id=os.getpid(),
        )
        result = probe.check(slot)
        assert not result["checks"]["brain_assigned"]

    def test_custom_required_checks(self):
        probe = HealthProbe(required_checks=["process_alive"])
        slot = DeploymentSlot(
            color=SlotColor.BLUE,
            state=SlotState.LIVE,
            process_id=os.getpid(),
        )
        result = probe.check(slot)
        # All checks still run, only required ones determine healthy
        assert "brain_assigned" in result["checks"]

    def test_min_uptime_not_satisfied(self):
        probe = HealthProbe(min_uptime_seconds=999999)
        # Use a timestamp 60 seconds ago — well under 999999s threshold.
        # Hardcoded date (2026-05-21) would drift past threshold over time.
        from datetime import UTC, datetime, timedelta

        _recent = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        slot = DeploymentSlot(
            color=SlotColor.BLUE,
            state=SlotState.LIVE,
            process_id=os.getpid(),
            brain_id="test",
            started_at=_recent,
        )
        result = probe.check(slot)
        assert not result["checks"]["min_uptime"]

    def test_no_started_at_defaults_ok(self):
        probe = HealthProbe(min_uptime_seconds=999999)
        slot = DeploymentSlot(
            color=SlotColor.BLUE,
            state=SlotState.LIVE,
            process_id=os.getpid(),
            brain_id="test",
            started_at="",
        )
        result = probe.check(slot)
        assert result["checks"]["min_uptime"]  # unknown → don't block

    def test_recent_heartbeat(self):
        probe = HealthProbe(health_timeout_seconds=10.0)
        slot = DeploymentSlot(
            color=SlotColor.BLUE,
            state=SlotState.LIVE,
            process_id=os.getpid(),
            brain_id="test",
            health_check_at="2026-01-01T00:00:00",
        )
        result = probe.check(slot)
        assert not result["checks"]["heartbeat_recent"]  # too old


# ── CutoverResult ─────────────────────────────────────────────────────────────


class TestCutoverResult:
    def test_to_dict_success(self):
        r = CutoverResult(
            success=True,
            previous_active=SlotColor.BLUE,
            new_active=SlotColor.GREEN,
            health_check_passed=True,
            cutover_duration_ms=150.0,
        )
        d = r.to_dict()
        assert d["success"] is True
        assert d["previous_active"] == "blue"
        assert d["new_active"] == "green"
        assert d["rolled_back"] is False

    def test_to_dict_failure_with_rollback(self):
        r = CutoverResult(
            success=False,
            previous_active=SlotColor.BLUE,
            new_active=SlotColor.GREEN,
            health_check_passed=False,
            cutover_duration_ms=250.0,
            rolled_back=True,
            error="Health check failed",
        )
        d = r.to_dict()
        assert d["success"] is False
        assert d["rolled_back"] is True
        assert "Health check failed" in d["error"]
