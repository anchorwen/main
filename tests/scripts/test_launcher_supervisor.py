"""TDD tests for P9 launcher-hub supervisor probe (TECH_DEBT-015 清偿).

Pure-logic matrix: hub/launcher matching, three-state classification,
dual-start lock freshness, restart decision, wmic line parsing.
No live processes are touched — all functions are pure.
"""

from __future__ import annotations

import json

import pytest

from scripts.launcher_supervisor import (
    LOCK_STALENESS_SECONDS,
    acquire_lock,
    classify_state,
    enumerate_process_lines,
    evaluate,
    hub_matches,
    hub_restart_command,
    launcher_matches,
    lock_is_fresh,
    lock_payload,
    parse_pid_from_wmic_line,
    release_lock,
    should_restart,
)

# ── Real command lines observed on the live system (2026-08-21) ──
_HUB_LINE = 'node,"C:\\Users\\Administrator\\AppData\\Local\\Programs\\Python\\Python311\\python.exe  main.py live",16004'
_HUB_LINE_WITH_FLAG = 'node,"python main.py live --config configs/live.yaml",9999'
_HUB_LINE_NONLIVE = 'node,"python main.py status",10000'
_HUB_LINE_LIVECYCLE = 'node,"python main.py live_cycle.py",10001'
_XAU_LAUNCHER = (
    'node,"python D:\\future\\scripts\\live_launcher.py configs/live.yaml --bar-sync",15368'
)
_BTC_LAUNCHER = (
    'node,"python D:\\future\\scripts\\live_launcher.py D:\\future\\configs\\live_btc.yaml",8776'
)
_FOREIGN_LAUNCHER = 'node,"C:\\Windows\\py.exe scripts\\live_intent_loop.py",20000'
_CURSOR_LAUNCHER = 'node,"python D:\\cursor\\scripts\\live_launcher.py --base-dir data",20001'
_HEADER = "node,commandline,processid"


# ── hub_matches ──────────────────────────────────────────────────────
class TestHubMatches:
    @pytest.mark.parametrize(
        ("cmdline", "expected"),
        [
            (_HUB_LINE, True),  # live hub, default config
            (_HUB_LINE_WITH_FLAG, True),  # hub with explicit --config
            ('node,"python main.py live",1', True),  # minimal
            ('node,"C:\\x\\python.exe main.py live",1', True),  # absolute python
            (_HUB_LINE_NONLIVE, False),  # `status` subcommand is not `live`
            (_HUB_LINE_LIVECYCLE, False),  # live_cycle.py is a script, not the hub
            (_XAU_LAUNCHER, False),  # launcher is not the hub
        ],
    )
    def test_hub_match(self, cmdline: str, expected: bool) -> None:
        assert hub_matches(cmdline) is expected


# ── launcher_matches ────────────────────────────────────────────────
class TestLauncherMatches:
    @pytest.mark.parametrize(
        ("cmdline", "expected"),
        [
            (_XAU_LAUNCHER, True),  # XAU launcher via configs/live.yaml
            (_BTC_LAUNCHER, True),  # BTC launcher via live_btc.yaml
            (_HUB_LINE, False),  # hub is not a launcher
            (_FOREIGN_LAUNCHER, False),  # D:\cursor supervisor spawn (P8: not ours)
            (_CURSOR_LAUNCHER, False),  # D:\cursor launcher without our config
            ('node,"python live_launcher.py",1', False),  # no config marker
        ],
    )
    def test_launcher_match(self, cmdline: str, expected: bool) -> None:
        assert launcher_matches(cmdline) is expected


# ── three-state classification ──────────────────────────────────────
class TestClassifyState:
    @pytest.mark.parametrize(
        ("hub", "launcher", "expected"),
        [
            (True, False, "HEALTHY"),  # hub alive (launcher state irrelevant)
            (True, True, "HEALTHY"),
            (False, True, "DEGRADED"),  # hub dead, trading still running → alert-only
            (False, False, "RECOVERY"),  # full-chain gap (8/10) → restart hub
        ],
    )
    def test_classify(self, hub: bool, launcher: bool, expected: str) -> None:
        assert classify_state(hub_alive=hub, launcher_alive=launcher) == expected


class TestShouldRestart:
    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            ("HEALTHY", False),
            ("DEGRADED", False),  # never restart on top of a live trading process
            ("RECOVERY", True),
        ],
    )
    def test_decision(self, state: str, expected: bool) -> None:
        assert should_restart(state) is expected


# ── evaluate over raw wmic lines ────────────────────────────────────
class TestEvaluate:
    def test_empty_scan_recovers(self) -> None:
        # No hub AND no launcher → full gap → RECOVERY.
        state, _hub, _launcher = evaluate([_HEADER])
        assert state == "RECOVERY"

    def test_hub_alive(self) -> None:
        state, hub_pids, launcher_pids = evaluate([_HEADER, _HUB_LINE, _XAU_LAUNCHER])
        assert state == "HEALTHY"
        assert hub_pids == [16004]
        assert launcher_pids == [15368]

    def test_degraded_hub_dead_launcher_alive(self) -> None:
        state, hub_pids, launcher_pids = evaluate([_HEADER, _XAU_LAUNCHER, _BTC_LAUNCHER])
        assert state == "DEGRADED"
        assert hub_pids == []
        assert launcher_pids == [15368, 8776]

    def test_both_launchers_recover_only_hub_started(self) -> None:
        state, hub_pids, launcher_pids = evaluate([_HEADER, _FOREIGN_LAUNCHER])
        assert state == "RECOVERY"
        assert launcher_pids == []  # foreign intent (P8: not ours) → not our launcher


# ── dual-start lock ─────────────────────────────────────────────────
class TestLock:
    def test_lock_payload_roundtrip(self) -> None:
        payload = lock_payload(pid=4321, now_ts=1000.0)
        assert payload == {"pid": 4321, "started_at_unix": 1000.0}
        assert json.loads(json.dumps(payload)) == payload

    def test_fresh_lock_is_fresh(self) -> None:
        now = 2_000_000_000.0
        payload = lock_payload(pid=1, now_ts=now - 60)  # started 1 min ago
        assert lock_is_fresh(payload, now) is True

    def test_stale_lock_is_stale(self) -> None:
        now = 2_000_000_000.0
        payload = lock_payload(pid=1, now_ts=now - LOCK_STALENESS_SECONDS - 1)
        assert lock_is_fresh(payload, now) is False

    def test_malformed_lock_is_stale(self) -> None:
        now = 2_000_000_000.0
        assert lock_is_fresh({}, now) is False
        assert lock_is_fresh({"pid": 1}, now) is False
        assert lock_is_fresh({"started_at_unix": "not-a-number"}, now) is False

    def test_future_timestamp_is_stale(self) -> None:
        now = 2_000_000_000.0
        payload = lock_payload(pid=1, now_ts=now + 3600)  # clock skew
        assert lock_is_fresh(payload, now) is False


# ── lock file (dual-start protection) ──────────────────────────────
class TestLockFile:
    def test_acquire_release_roundtrip(self, tmp_path) -> None:
        path = str(tmp_path / "launcher_supervisor.lock")
        ok, _reason = acquire_lock(path, pid=111, now=1_000.0)
        assert ok is True
        # fresh lock blocks a second acquirer (dual-start protection #1)
        ok2, reason2 = acquire_lock(path, pid=222, now=1_000.0 + 60)
        assert ok2 is False
        assert "held" in reason2
        # owner releases → next acquirer succeeds
        assert release_lock(path, owner_pid=111) is True
        ok3, _ = acquire_lock(path, pid=333, now=2_000.0)
        assert ok3 is True
        release_lock(path, owner_pid=333)

    def test_stale_lock_takeover(self, tmp_path) -> None:
        path = str(tmp_path / "launcher_supervisor.lock")
        assert acquire_lock(path, pid=111, now=1_000.0)[0] is True
        # 15 min later (beyond 12-min TTL): owner gone → takeover succeeds
        ok, _reason = acquire_lock(path, pid=222, now=1_000.0 + 15 * 60)
        assert ok is True
        assert release_lock(path, owner_pid=222) is True

    def test_release_foreign_lock_is_noop(self, tmp_path) -> None:
        path = str(tmp_path / "launcher_supervisor.lock")
        assert acquire_lock(path, pid=111, now=1_000.0)[0] is True
        # a non-owner may not clear the lock
        assert release_lock(path, owner_pid=999) is False
        assert release_lock(path, owner_pid=111) is True


# ── restart command ─────────────────────────────────────────────────
class TestRestartCommand:
    def test_hub_restart_command(self) -> None:
        cmd = hub_restart_command(project_root="D:\\future", python_exe="python")
        assert cmd == ["python", "D:\\future\\main.py", "live"]


# ── wmic line parsing ───────────────────────────────────────────────
class TestParsePid:
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            (_HUB_LINE, 16004),
            ('node,"python foo",42', 42),
            (_HEADER, None),  # header row
            ('node,"python foo",', None),  # empty pid
            ('node,"python foo",abc', None),  # non-digit pid
            ("", None),
        ],
    )
    def test_pid_parse(self, line: str, expected: int | None) -> None:
        assert parse_pid_from_wmic_line(line) == expected


# ── process enumeration is well-formed (no raise on empty input) ────
class TestEnumerateProcessLines:
    def test_returns_list(self) -> None:
        lines = enumerate_process_lines(timeout=0.1)
        assert isinstance(lines, list)
