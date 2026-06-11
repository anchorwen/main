"""Tests for Write-Audit-Publish (WAP) staging pattern.

FIX-20260611-022: Verify atomic staging, audit, publish, rollback.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from core.data.wap import WAPStore, create_wap_store


class TestWAPStore:
    def setup_method(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.staging = Path(self.tmp.name) / "staging"
        self.production = Path(self.tmp.name) / "production"
        self.wap = WAPStore(self.staging, self.production)

    def teardown_method(self):
        self.tmp.cleanup()

    def test_stage_writes_to_staging(self):
        self.wap.stage("test.json", '{"key": "value"}')
        staged = self.staging / "test.json"
        assert staged.exists()
        assert staged.read_text() == '{"key": "value"}'
        # Production should NOT be touched
        assert not (self.production / "test.json").exists()

    def test_stage_json(self):
        self.wap.stage_json("data.json", {"a": 1, "b": 2})
        staged = self.staging / "data.json"
        assert staged.exists()
        import json

        data = json.loads(staged.read_text())
        assert data == {"a": 1, "b": 2}

    def test_audit_valid_json_passes(self):
        self.wap.stage("valid.json", '{"ok": true}')
        errors = self.wap.audit("valid.json")
        assert errors == []

    def test_audit_invalid_json_fails(self):
        self.wap.stage("bad.json", "not valid json {{{")
        errors = self.wap.audit("bad.json")
        assert len(errors) > 0

    def test_audit_empty_file_fails(self):
        self.wap.stage("empty.json", "")
        errors = self.wap.audit("empty.json")
        assert len(errors) > 0

    def test_audit_missing_file_fails(self):
        errors = self.wap.audit("nonexistent.json")
        assert len(errors) > 0

    def test_audit_custom_validator(self):
        def must_have_key(path: Path) -> list[str]:
            import json

            data = json.loads(path.read_text())
            if "required_key" not in data:
                return ["Missing required_key"]
            return []

        self.wap.stage("data.json", '{"required_key": 42}')
        errors = self.wap.audit("data.json", validator=must_have_key)
        assert errors == []

        self.wap.stage("bad.json", '{"wrong_key": 1}')
        errors = self.wap.audit("bad.json", validator=must_have_key)
        assert len(errors) == 1

    def test_publish_moves_to_production(self):
        self.wap.stage("test.json", "content")
        assert self.wap.publish("test.json")
        # Staging should be empty now
        assert not (self.staging / "test.json").exists()
        # Production should have the content
        assert (self.production / "test.json").exists()
        assert (self.production / "test.json").read_text() == "content"

    def test_publish_missing_staged_fails(self):
        assert not self.wap.publish("nonexistent.json")

    def test_reject_discards_staged(self):
        self.wap.stage("test.json", "data")
        self.wap.reject("test.json")
        assert not (self.staging / "test.json").exists()
        assert not (self.production / "test.json").exists()

    def test_snapshot_and_rollback(self):
        # Create initial production state
        self.wap.stage("state.json", "v1")
        self.wap.publish("state.json")

        # Snapshot before changing
        snap = self.wap.snapshot("state.json")
        assert snap is not None
        assert snap.exists()

        # Change production
        self.wap.stage("state.json", "v2")
        self.wap.publish("state.json")
        assert (self.production / "state.json").read_text() == "v2"

        # Rollback should restore v1
        assert self.wap.rollback("state.json")
        assert (self.production / "state.json").read_text() == "v1"

    def test_rollback_no_snapshot_fails(self):
        assert not self.wap.rollback("nonexistent.json")

    def test_snapshot_nonexistent_production(self):
        assert self.wap.snapshot("no_file.json") is None

    def test_cleanup_snapshots(self):
        import time as _time

        # Create multiple snapshots (with delay to ensure unique timestamps)
        self.wap.stage("data.json", "v0")
        self.wap.publish("data.json")
        for i in range(10):
            self.wap.snapshot("data.json")
            _time.sleep(0.01)  # Ensure unique timestamp filenames

        assert len(self.wap.list_snapshots("data.json")) == 10
        removed = self.wap.cleanup_snapshots("data.json", keep=3)
        assert removed == 7
        assert len(self.wap.list_snapshots("data.json")) == 3

    def test_create_wap_store(self):
        wap = create_wap_store(self.tmp.name)
        assert wap._staging.exists()
        assert wap._production.exists()
