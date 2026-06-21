"""Unit tests for core/state/writer.py and core/state/catalog.py.

Verifies the three physical guarantees of the State Governance Protocol:
    1. Schema Dictatorship — dirty data rejected at the gate
    2. Atomic Write — no partial/corrupt files on disk
    3. Cross-Symbol Guard — btc_swing cannot leak into XAU registry

Usage:
    python -m pytest tests/test_state_writer.py -v
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def tmp_data_dir():
    """Create a temporary data directory for test writes."""
    with tempfile.TemporaryDirectory(prefix="test_state_") as td:
        yield Path(td)


@pytest.fixture
def xau_writer(tmp_data_dir):
    """Create a StateWriter bound to a temp XAU data directory."""
    from core.state.writer import StateWriter
    return StateWriter(str(tmp_data_dir), symbol="XAUUSDc")


@pytest.fixture
def btc_writer(tmp_data_dir):
    """Create a StateWriter bound to a temp BTC data directory."""
    from core.state.writer import StateWriter
    return StateWriter(str(tmp_data_dir), symbol="BTCUSDc")


# ═══════════════════════════════════════════════════════════════════════════════
# Catalog Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCatalog:
    """Verify catalog integrity."""

    def test_all_artifacts_have_validators(self):
        """Every catalog entry must have a callable schema_validator."""
        from core.state.catalog import CATALOG

        for logical_id, artifact in CATALOG.items():
            assert callable(artifact.schema_validator), (
                f"{logical_id}: schema_validator is not callable"
            )

    def test_all_artifacts_have_ttl(self):
        """Every catalog entry must declare a TTL (0 = no check)."""
        from core.state.catalog import CATALOG

        for logical_id, artifact in CATALOG.items():
            assert artifact.ttl_seconds >= 0, (
                f"{logical_id}: ttl_seconds must be >= 0"
            )

    def test_all_path_templates_use_json(self):
        """All state files must end in .json."""
        from core.state.catalog import CATALOG

        for logical_id, artifact in CATALOG.items():
            assert artifact.path_template.endswith(".json"), (
                f"{logical_id}: path_template must end with .json, got {artifact.path_template!r}"
            )

    def test_lookup_known_id(self):
        """Lookup of known artifacts succeeds."""
        from core.state.catalog import lookup

        artifact = lookup("LEADERBOARD")
        assert artifact.logical_id == "LEADERBOARD"
        assert artifact.ttl_seconds == 86400

    def test_lookup_unknown_id_raises(self):
        """Lookup of unknown artifact raises KeyError."""
        from core.state.catalog import lookup

        with pytest.raises(KeyError, match="Unknown state artifact"):
            lookup("NONEXISTENT_ARTIFACT_ID")

    def test_cross_symbol_guard_registry(self):
        """ALPHA_REGISTRY must have cross_symbol_guard enabled."""
        from core.state.catalog import lookup

        artifact = lookup("ALPHA_REGISTRY")
        assert artifact.cross_symbol_guard is True, (
            "ALPHA_REGISTRY must have cross_symbol_guard=True"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Schema Validation Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSchemaValidation:
    """Verify that dirty data is rejected at the gate."""

    def test_valid_leaderboard_passes(self, xau_writer):
        """Well-formed leaderboard data must pass validation."""
        from core.state.catalog import lookup

        data = {
            "leaderboard": [
                {"brain_id": "test_v1", "score": 0.85, "rank": 1},
            ],
            "total_brains": 1,
        }
        result = xau_writer.write_artifact(
            lookup("LEADERBOARD"), "XAUUSDc", data, dry_run=True
        )
        assert result["validated"] is True
        assert result["dry_run"] is True

    def test_empty_leaderboard_rejected(self, xau_writer):
        """Empty dict must be rejected for leaderboard."""
        from core.state.catalog import DataIntegrityError, lookup

        with pytest.raises(DataIntegrityError, match="must not be an empty dict"):
            xau_writer.write_artifact(
                lookup("LEADERBOARD"), "XAUUSDc", {}, dry_run=True
            )

    def test_non_dict_rejected(self, xau_writer):
        """Non-dict data must be rejected."""
        from core.state.catalog import DataIntegrityError, lookup

        with pytest.raises(DataIntegrityError, match="Expected dict"):
            xau_writer.write_artifact(
                lookup("LEADERBOARD"), "XAUUSDc", "not_a_dict", dry_run=True  # type: ignore
            )

    def test_alpha_allocation_missing_recommendations(self, xau_writer):
        """alpha_allocation without 'recommendations' key must be rejected."""
        from core.state.catalog import DataIntegrityError, lookup

        with pytest.raises(DataIntegrityError, match="must contain 'recommendations'"):
            xau_writer.write_artifact(
                lookup("ALPHA_ALLOCATION"), "XAUUSDc", {"total_notional": 1000}, dry_run=True
            )

    def test_alpha_allocation_valid_passes(self, xau_writer):
        """Valid alpha allocation passes validation."""
        from core.state.catalog import lookup

        data = {
            "recommendations": [
                {"alpha_id": "xau_live_v1", "notional": 50000},
            ],
            "total_notional": 50000,
        }
        result = xau_writer.write_artifact(
            lookup("ALPHA_ALLOCATION"), "XAUUSDc", data, dry_run=True
        )
        assert result["validated"] is True

    def test_governance_state_missing_brains(self, xau_writer):
        """governance_state without brain_states must be rejected."""
        from core.state.catalog import DataIntegrityError, lookup

        with pytest.raises(DataIntegrityError, match="must contain 'brain_states'"):
            xau_writer.write_artifact(
                lookup("GOVERNANCE_STATE"), "XAUUSDc", {"some_other_key": 1}, dry_run=True
            )

    def test_daily_ops_state_missing_timestamp(self, xau_writer):
        """daily_ops_state without last_daily_ops_utc must be rejected."""
        from core.state.catalog import DataIntegrityError, lookup

        with pytest.raises(DataIntegrityError, match="must contain 'last_daily_ops_utc'"):
            xau_writer.write_artifact(
                lookup("DAILY_OPS_STATE"), "XAUUSDc", {"other": "data"}, dry_run=True
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-Symbol Contamination Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCrossSymbolGuard:
    """Verify that btc_swing cannot leak into XAU registry and vice versa."""

    def test_btc_swing_in_xau_registry_rejected(self, xau_writer):
        """Writing btc_swing to XAU alpha_registry must raise."""
        from core.state.catalog import CrossSymbolContaminationError, lookup

        data = {
            "records": [
                {"alpha_id": "btc_swing", "notional": 100000},
                {"alpha_id": "xau_live", "notional": 50000},
            ],
        }
        with pytest.raises(CrossSymbolContaminationError, match="cross-symbol contamination"):
            xau_writer.write_artifact(
                lookup("ALPHA_REGISTRY"), "XAUUSDc", data, dry_run=True
            )

    def test_xau_in_btc_registry_accepted(self, btc_writer):
        """xau_live in BTC registry should also be caught."""
        from core.state.catalog import CrossSymbolContaminationError, lookup

        data = {
            "records": [
                {"alpha_id": "xau_live", "notional": 50000},
                {"alpha_id": "btc_swing", "notional": 100000},
            ],
        }
        with pytest.raises(CrossSymbolContaminationError, match="cross-symbol contamination"):
            btc_writer.write_artifact(
                lookup("ALPHA_REGISTRY"), "BTCUSDc", data, dry_run=True
            )

    def test_pure_xau_registry_accepted(self, xau_writer):
        """XAU registry with only XAU alphas passes."""
        from core.state.catalog import lookup

        data = {
            "records": [
                {"alpha_id": "xau_live", "notional": 50000},
                {"alpha_id": "alpha_xau_swing", "notional": 30000},
            ],
        }
        result = xau_writer.write_artifact(
            lookup("ALPHA_REGISTRY"), "XAUUSDc", data, dry_run=True
        )
        assert result["validated"] is True

    def test_pure_btc_registry_accepted(self, btc_writer):
        """BTC registry with only BTC alphas passes."""
        from core.state.catalog import lookup

        data = {
            "records": [
                {"alpha_id": "btc_swing", "notional": 100000},
                {"alpha_id": "alpha_btc_trend", "notional": 50000},
            ],
        }
        result = btc_writer.write_artifact(
            lookup("ALPHA_REGISTRY"), "BTCUSDc", data, dry_run=True
        )
        assert result["validated"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# Atomic Write Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestAtomicWrite:
    """Verify atomic write guarantees: no partial files, no corruption."""

    def test_write_creates_file(self, xau_writer, tmp_data_dir):
        """A successful write creates the target file with correct content."""
        from core.state.catalog import lookup

        data = {"leaderboard": [{"brain_id": "test", "score": 1.0}], "total_brains": 1}
        result = xau_writer.write_artifact(
            lookup("LEADERBOARD"), "XAUUSDc", data
        )

        assert result["written"] is True
        target = tmp_data_dir / "reports" / "leaderboard.json"
        assert target.exists(), f"Expected {target} to exist after write"
        assert target.stat().st_size > 0, "File must not be empty"

        # Verify content is valid JSON and matches what we wrote
        read_back = json.loads(target.read_text(encoding="utf-8"))
        assert read_back == data

    def test_no_temp_file_left_behind(self, xau_writer, tmp_data_dir):
        """After a successful write, no .tmp files should remain."""
        from core.state.catalog import lookup

        data = {"leaderboard": [{"brain_id": "test", "score": 0.9}], "total_brains": 1}
        xau_writer.write_artifact(lookup("LEADERBOARD"), "XAUUSDc", data)

        # No .tmp files anywhere in the data directory tree
        tmp_files = list(tmp_data_dir.rglob("*.tmp"))
        assert len(tmp_files) == 0, f"Found orphaned .tmp files: {tmp_files}"

    def test_target_never_partial(self, xau_writer, tmp_data_dir):
        """Target file must either not exist or be complete valid JSON — never partial."""
        from core.state.catalog import lookup

        data = {"leaderboard": [{"brain_id": "complete", "score": 0.95}], "total_brains": 1}
        xau_writer.write_artifact(lookup("LEADERBOARD"), "XAUUSDc", data)

        target = tmp_data_dir / "reports" / "leaderboard.json"
        content = target.read_text(encoding="utf-8")

        # Must be valid JSON — parse it
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            pytest.fail(f"Written file is not valid JSON: {exc}")

        # Must have expected structure (not truncated)
        assert "leaderboard" in parsed
        assert len(parsed["leaderboard"]) == 1

    def test_write_then_overwrite(self, xau_writer, tmp_data_dir):
        """Subsequent writes should atomically replace the file."""
        from core.state.catalog import lookup

        data_v1 = {"leaderboard": [{"brain_id": "v1", "score": 0.5}], "total_brains": 1}
        xau_writer.write_artifact(lookup("LEADERBOARD"), "XAUUSDc", data_v1)

        data_v2 = {"leaderboard": [{"brain_id": "v2", "score": 0.99}], "total_brains": 1}
        xau_writer.write_artifact(lookup("LEADERBOARD"), "XAUUSDc", data_v2)

        target = tmp_data_dir / "reports" / "leaderboard.json"
        read_back = json.loads(target.read_text(encoding="utf-8"))
        assert read_back["leaderboard"][0]["brain_id"] == "v2", (
            "Overwrite must replace entire content"
        )

    def test_nested_directory_created(self, xau_writer, tmp_data_dir):
        """Writing to a path with non-existent parent dirs creates them."""
        from core.state.catalog import lookup

        # daily_ops_state lives in state/ subdirectory
        data = {"last_daily_ops_utc": 1719000000.0}
        xau_writer.write_artifact(lookup("DAILY_OPS_STATE"), "XAUUSDc", data)

        target = tmp_data_dir / "state" / "daily_ops_state.json"
        assert target.exists()
        assert target.parent.is_dir()

    def test_dry_run_does_not_write(self, xau_writer, tmp_data_dir):
        """Dry-run mode must not touch disk."""
        from core.state.catalog import lookup

        data = {"leaderboard": [{"brain_id": "ghost", "score": 0.0}], "total_brains": 1}
        result = xau_writer.write_artifact(
            lookup("LEADERBOARD"), "XAUUSDc", data, dry_run=True
        )

        assert result["written"] is True
        assert result["dry_run"] is True
        target = tmp_data_dir / "reports" / "leaderboard.json"
        assert not target.exists(), "dry_run must not create files"


# ═══════════════════════════════════════════════════════════════════════════════
# Convenience Methods Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestConvenienceMethods:
    """Verify convenience methods work correctly."""

    def test_write_leaderboard_convenience(self, xau_writer, tmp_data_dir):
        """write_leaderboard() convenience method."""
        data = {"leaderboard": [{"brain_id": "b", "score": 0.5}], "total_brains": 1}
        result = xau_writer.write_leaderboard(data, dry_run=True)
        assert result["validated"] is True

    def test_write_alpha_allocation_convenience(self, xau_writer):
        """write_alpha_allocation() convenience method."""
        data = {"recommendations": [{"alpha_id": "x", "notional": 100}]}
        result = xau_writer.write_alpha_allocation(data, dry_run=True)
        assert result["validated"] is True

    def test_write_governance_convenience(self, xau_writer):
        """write_governance_state() convenience method."""
        data = {"brain_states": {"b1": {"status": "live"}}}
        result = xau_writer.write_governance_state(data, dry_run=True)
        assert result["validated"] is True

    def test_write_alpha_registry_convenience_blocks_foreign(self, xau_writer):
        """write_alpha_registry() with contaminated data must be blocked."""
        from core.state.catalog import CrossSymbolContaminationError

        data = {"records": [{"alpha_id": "btc_swing", "notional": 100000}]}
        with pytest.raises(CrossSymbolContaminationError):
            xau_writer.write_alpha_registry(data, dry_run=True)


# ═══════════════════════════════════════════════════════════════════════════════
# DataIntegrityError Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestDataIntegrityError:
    """Verify the error type carries useful diagnostic information."""

    def test_error_carries_artifact_id(self):
        from core.state.catalog import DataIntegrityError

        exc = DataIntegrityError("test", artifact_id="LEADERBOARD")
        assert exc.artifact_id == "LEADERBOARD"

    def test_error_carries_violations(self):
        from core.state.catalog import DataIntegrityError

        exc = DataIntegrityError("test", violations=["missing:x", "missing:y"])
        assert exc.violations == ["missing:x", "missing:y"]

    def test_cross_symbol_error_carries_foreign_ids(self):
        from core.state.catalog import CrossSymbolContaminationError

        exc = CrossSymbolContaminationError(
            "contamination", artifact_id="ALPHA_REGISTRY", foreign_ids=["btc_swing"]
        )
        assert exc.foreign_ids == ["btc_swing"]
        assert exc.artifact_id == "ALPHA_REGISTRY"
