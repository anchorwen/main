"""Tests for training_registry.py — SQLite-backed training run registry."""

import tempfile
from pathlib import Path
from typing import Any, ClassVar

from core.training.training_registry import (
    TrainingRunRecord,
    create_registry,
)


def _make_record(**overrides) -> TrainingRunRecord:
    """Create a minimal TrainingRunRecord for testing."""
    defaults = {
        "run_id": "test_run_001",
        "contract_id": "barrier_12bar_xgboost_v2",
        "timestamp": None,  # auto-set by add_run
        "arch": "xgboost",
        "feature_schema": "v9_institutional_40",
        "n_samples": 1000,
        "n_features": 40,
        "train_sharpe": 1.2,
        "forward_sharpe": 0.9,
        "overfit_gap": 0.3,
        "train_win_rate": 0.55,
        "forward_win_rate": 0.52,
        "profit_factor": 1.5,
        "max_drawdown": 15.0,
        "cpcv_sharpe_std": 0.15,
        "quality_gate_passed": True,
        "status": "SHADOW",
        "model_path": "data/models/test_model.json",
    }
    defaults.update(overrides)
    return TrainingRunRecord(**defaults)


class TestTrainingRegistry:
    """Tests for the SQLite TrainingRegistry."""

    tmpdir: ClassVar[str]
    db_path: ClassVar[str]
    registry: ClassVar[Any]

    @classmethod
    def setup_class(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="test_registry_")
        cls.db_path = str(Path(cls.tmpdir) / "test_registry.db")
        cls.registry = create_registry(cls.db_path)

    @classmethod
    def teardown_class(cls):
        import shutil

        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setup_method(self):
        """Clean all records before each test."""
        with self.registry.Session() as session:
            session.query(TrainingRunRecord).delete()
            session.commit()

    # ── CRUD ──────────────────────────────────────────────────────────────

    def test_add_and_get(self):
        record = _make_record()
        self.registry.add_run(record)
        fetched = self.registry.get_run("test_run_001")
        assert fetched is not None
        assert fetched.contract_id == "barrier_12bar_xgboost_v2"
        assert fetched.train_sharpe == 1.2
        assert fetched.status == "SHADOW"
        assert fetched.timestamp is not None

    def test_add_generates_run_id_if_empty(self):
        record = _make_record(run_id="")
        self.registry.add_run(record)
        assert record.run_id != ""
        assert len(record.run_id) == 12  # 6 bytes hex

    def test_get_nonexistent(self):
        assert self.registry.get_run("nonexistent") is None

    def test_list_runs_by_contract(self):
        self.registry.add_run(_make_record(run_id="r1", contract_id="c_a"))
        self.registry.add_run(_make_record(run_id="r2", contract_id="c_b"))
        self.registry.add_run(_make_record(run_id="r3", contract_id="c_a"))
        results = self.registry.list_runs(contract_id="c_a")
        assert len(results) == 2
        ids = {r.run_id for r in results}
        assert ids == {"r1", "r3"}

    def test_list_runs_by_status(self):
        self.registry.add_run(_make_record(run_id="r1", status="SHADOW"))
        self.registry.add_run(_make_record(run_id="r2", status="FAILED"))
        self.registry.add_run(_make_record(run_id="r3", status="SHADOW"))
        results = self.registry.list_runs(status="SHADOW")
        assert len(results) == 2

    def test_list_runs_by_arch(self):
        self.registry.add_run(_make_record(run_id="r1", arch="xgboost"))
        self.registry.add_run(_make_record(run_id="r2", arch="lightgbm"))
        results = self.registry.list_runs(arch="xgboost")
        assert len(results) == 1
        assert results[0].run_id == "r1"

    def test_list_runs_limit(self):
        for i in range(5):
            self.registry.add_run(_make_record(run_id=f"r{i}"))
        results = self.registry.list_runs(limit=2)
        assert len(results) == 2

    def test_update_status(self):
        self.registry.add_run(_make_record(run_id="r1", status="SHADOW"))
        assert self.registry.update_status("r1", "LIVE")
        fetched = self.registry.get_run("r1")
        assert fetched.status == "LIVE"

    def test_update_status_nonexistent(self):
        assert not self.registry.update_status("ghost", "LIVE")

    def test_delete_run(self):
        self.registry.add_run(_make_record(run_id="r1"))
        assert self.registry.delete_run("r1")
        assert self.registry.get_run("r1") is None

    def test_delete_nonexistent(self):
        assert not self.registry.delete_run("ghost")

    def test_add_or_update_new(self):
        record = _make_record(run_id="new_run")
        self.registry.add_or_update(record)
        assert self.registry.get_run("new_run") is not None

    def test_add_or_update_existing(self):
        self.registry.add_run(_make_record(run_id="r1", status="SHADOW", train_sharpe=1.0))
        updated = _make_record(run_id="r1", status="LIVE", train_sharpe=1.5)
        self.registry.add_or_update(updated)
        fetched = self.registry.get_run("r1")
        assert fetched.status == "LIVE"
        assert fetched.train_sharpe == 1.5

    # ── Queries ───────────────────────────────────────────────────────────

    def test_best_run(self):
        self.registry.add_run(_make_record(run_id="r1", forward_sharpe=0.5))
        self.registry.add_run(_make_record(run_id="r2", forward_sharpe=1.2))
        self.registry.add_run(_make_record(run_id="r3", forward_sharpe=0.8))
        best = self.registry.best_run("barrier_12bar_xgboost_v2")
        assert best is not None
        assert best.run_id == "r2"

    def test_best_run_none_for_contract(self):
        self.registry.add_run(_make_record(run_id="r1"))
        best = self.registry.best_run("other_contract")
        assert best is None

    def test_compare_runs(self):
        self.registry.add_run(_make_record(run_id="r1", forward_sharpe=1.0, train_sharpe=1.5))
        self.registry.add_run(_make_record(run_id="r2", forward_sharpe=0.5, train_sharpe=1.0))
        comp = self.registry.compare_runs("r1", "r2")
        assert comp is not None
        assert comp["metrics"]["forward_sharpe"]["a"] == 1.0
        assert comp["metrics"]["forward_sharpe"]["b"] == 0.5
        assert comp["metrics"]["forward_sharpe"]["delta"] == 0.5

    def test_compare_runs_missing(self):
        self.registry.add_run(_make_record(run_id="r1"))
        assert self.registry.compare_runs("r1", "r2") is None

    def test_count(self):
        self.registry.add_run(_make_record(run_id="r1", status="SHADOW"))
        self.registry.add_run(_make_record(run_id="r2", status="FAILED"))
        self.registry.add_run(_make_record(run_id="r3", status="SHADOW"))
        assert self.registry.count() == 3
        assert self.registry.count(status="SHADOW") == 2
        assert self.registry.count(status="LIVE") == 0

    # ── Model hash ────────────────────────────────────────────────────────

    def test_set_and_verify_hash(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            f.write(b"model_content_for_hash_test")
            tmp = Path(f.name)
        try:
            self.registry.add_run(_make_record(run_id="h1", model_path=None, model_hash=None))
            h = self.registry.set_model_hash("h1", str(tmp))
            assert h is not None
            assert len(h) == 64

            assert self.registry.verify_hash("h1")

            # Corrupt the file
            tmp.write_bytes(b"tampered")
            assert not self.registry.verify_hash("h1")
        finally:
            tmp.unlink()

    def test_set_model_hash_missing_file(self):
        self.registry.add_run(_make_record(run_id="h2"))
        h = self.registry.set_model_hash("h2", "/nonexistent/model.json")
        assert h is None

    def test_verify_hash_no_model_path(self):
        self.registry.add_run(_make_record(run_id="h3", model_path=None))
        assert not self.registry.verify_hash("h3")

    def test_get_run_by_hash(self):
        self.registry.add_run(_make_record(run_id="h4", model_hash="abc123def456" * 4))
        fetched = self.registry.get_run_by_hash("abc123def456" * 4)
        assert fetched is not None
        assert fetched.run_id == "h4"

    def test_get_run_by_hash_missing(self):
        assert self.registry.get_run_by_hash("nonexistent_hash") is None


class TestCreateRegistry:
    """Test the factory function."""

    def test_create_registry_creates_dir_and_db(self):
        registry = None
        tmp = tempfile.mkdtemp(prefix="test_registry_")
        try:
            db_path = str(Path(tmp) / "subdir" / "test.db")
            registry = create_registry(db_path)
            assert Path(db_path).exists()
            assert registry.count() == 0
        finally:
            if registry is not None:
                registry.engine.dispose()
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)
