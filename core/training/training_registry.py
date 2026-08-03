"""SQLite-backed training run registry with ACID guarantees.

Replaces the flat JSON file approach with a proper database, supporting:
  - Concurrent writes from parallel Optuna trials
  - SHA256 model hashing for tamper-proof audit
  - Full training lineage tracking (contract → model → brain)
  - Comparison queries across runs

Database location is configurable; defaults to ``data/training/registry.db``.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from core.training.model_hashing import hash_model_file, verify_model_hash

# ── ORM Base ──────────────────────────────────────────────────────────────────


class Base(DeclarativeBase):
    pass


# ── Entity ────────────────────────────────────────────────────────────────────


class TrainingRunRecord(Base):
    __tablename__ = "training_runs"

    # Identity
    run_id = Column(String(64), primary_key=True)
    contract_id = Column(String(256), index=True, nullable=False)
    model_hash = Column(String(64), unique=True, nullable=True)  # NULL until artifacts saved
    timestamp = Column(DateTime, nullable=False)

    # Training metrics
    train_sharpe = Column(Float, nullable=True)
    forward_sharpe = Column(Float, nullable=True)
    overfit_gap = Column(Float, nullable=True)
    train_win_rate = Column(Float, nullable=True)
    forward_win_rate = Column(Float, nullable=True)
    profit_factor = Column(Float, nullable=True)
    max_drawdown = Column(Float, nullable=True)
    cpcv_sharpe_std = Column(Float, nullable=True)

    # Architecture info
    arch = Column(String(64), nullable=True)
    feature_schema = Column(String(128), nullable=True)
    contract_group = Column(String(64), nullable=True)
    n_samples = Column(Integer, nullable=True)
    n_features = Column(Integer, nullable=True)

    # Gate results
    quality_gate_passed = Column(Boolean, default=False, nullable=False)
    status = Column(
        String(32), default="FAILED", nullable=False
    )  # FAILED | SHADOW | LIVE | RETIRED

    # Phase 5 lineage (FIX-20260803-006) — the model's birth certificate.
    # Cross-checked by scripts/training/verify_lineage.py against each enabled
    # brain config.  NULL on legacy pre-FIX rows = MISSING lineage.
    dataset_hash = Column(String(64), nullable=True)  # SHA256 of training NPZ
    label_contract_id = Column(String(128), nullable=True)  # label contract SSOT
    trained_by_commit_hash = Column(String(64), nullable=True)  # git HEAD at train time
    oos_verdict = Column(String(32), nullable=True)  # PASS | FAIL | INSUFFICIENT_OOS | None

    # File references
    config_path = Column(String(512), nullable=True)
    model_path = Column(String(512), nullable=True)
    shap_report_path = Column(String(512), nullable=True)
    eval_report_path = Column(String(512), nullable=True)

    # Metadata
    notes = Column(String(1024), nullable=True)


# ── Registry ──────────────────────────────────────────────────────────────────


class TrainingRegistry:
    """SQLite-backed registry for training run provenance."""

    def __init__(self, db_path: str = "data/training/registry.db") -> None:
        db_path_resolved = str(Path(db_path).resolve())
        self.engine = create_engine(
            f"sqlite:///{db_path_resolved}",
            connect_args={"check_same_thread": False},  # allow multi-thread Optuna writes
            echo=False,
        )

        # Enable WAL mode for concurrent read/write performance
        @event.listens_for(self.engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ARG001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    # ── CRUD ──────────────────────────────────────────────────────────────

    def add_run(self, record: TrainingRunRecord) -> None:
        """Insert a new training run record.  Generates run_id if not set."""
        if not record.run_id:
            record.run_id = _generate_run_id()
        if record.timestamp is None:
            record.timestamp = datetime.now(UTC)
        with self.Session() as session:
            session.add(record)
            session.commit()

    def add_or_update(self, record: TrainingRunRecord) -> None:
        """Merge: update existing run by run_id or model_hash, or insert if new."""
        if not record.run_id:
            record.run_id = _generate_run_id()
        if record.timestamp is None:
            record.timestamp = datetime.now(UTC)
        with self.Session() as session:
            existing = session.get(TrainingRunRecord, record.run_id)
            # Fallback: try to find by model_hash if the hash already exists
            if existing is None and record.model_hash:
                existing = (
                    session.query(TrainingRunRecord).filter_by(model_hash=record.model_hash).first()
                )
            if existing is not None:
                for col in TrainingRunRecord.__table__.columns:
                    if col.name == "run_id":
                        continue
                    val = getattr(record, col.name)
                    if val is not None:
                        setattr(existing, col.name, val)
            else:
                session.add(record)
            session.commit()

    def get_run(self, run_id: str) -> TrainingRunRecord | None:
        """Fetch a single run by ID."""
        with self.Session() as session:
            return session.get(TrainingRunRecord, run_id)

    def get_run_by_hash(self, model_hash: str) -> TrainingRunRecord | None:
        """Look up a run by model hash."""
        with self.Session() as session:
            return session.query(TrainingRunRecord).filter_by(model_hash=model_hash).first()

    def list_runs(
        self,
        contract_id: str | None = None,
        status: str | None = None,
        arch: str | None = None,
        limit: int = 100,
    ) -> list[TrainingRunRecord]:
        """Query runs with optional filters, ordered by timestamp descending."""
        with self.Session() as session:
            q = session.query(TrainingRunRecord)
            if contract_id:
                q = q.filter_by(contract_id=contract_id)
            if status:
                q = q.filter_by(status=status)
            if arch:
                q = q.filter_by(arch=arch)
            return q.order_by(TrainingRunRecord.timestamp.desc()).limit(limit).all()

    def update_status(self, run_id: str, new_status: str) -> bool:
        """Transition a run's status (e.g., SHADOW → LIVE).  Returns False if not found."""
        with self.Session() as session:
            record = session.get(TrainingRunRecord, run_id)
            if record is None:
                return False
            record.status = new_status
            session.commit()
            return True

    def delete_run(self, run_id: str) -> bool:
        """Remove a run record. Returns False if not found."""
        with self.Session() as session:
            record = session.get(TrainingRunRecord, run_id)
            if record is None:
                return False
            session.delete(record)
            session.commit()
            return True

    # ── Queries ───────────────────────────────────────────────────────────

    def best_run(self, contract_id: str) -> TrainingRunRecord | None:
        """Find the best run for a contract by forward Sharpe."""
        with self.Session() as session:
            return (
                session.query(TrainingRunRecord)
                .filter_by(contract_id=contract_id)
                .filter(TrainingRunRecord.forward_sharpe.isnot(None))
                .order_by(TrainingRunRecord.forward_sharpe.desc())
                .first()
            )

    def compare_runs(self, run_id_a: str, run_id_b: str) -> dict[str, Any] | None:
        """Side-by-side comparison of two runs. Returns None if either missing."""
        a = self.get_run(run_id_a)
        b = self.get_run(run_id_b)
        if a is None or b is None:
            return None

        metric_fields = [
            "train_sharpe",
            "forward_sharpe",
            "overfit_gap",
            "train_win_rate",
            "forward_win_rate",
            "profit_factor",
            "max_drawdown",
            "cpcv_sharpe_std",
        ]
        metrics: dict[str, dict[str, float | None]] = {}
        for field in metric_fields:
            va = getattr(a, field)
            vb = getattr(b, field)
            delta = (va - vb) if (va is not None and vb is not None) else None
            metrics[field] = {"a": va, "b": vb, "delta": delta}

        return {
            "run_a": run_id_a,
            "run_b": run_id_b,
            "contract_id": a.contract_id,
            "metrics": metrics,
            "a_status": a.status,
            "b_status": b.status,
        }

    def count(self, contract_id: str | None = None, status: str | None = None) -> int:
        """Count runs matching filters."""
        with self.Session() as session:
            q = session.query(TrainingRunRecord)
            if contract_id:
                q = q.filter_by(contract_id=contract_id)
            if status:
                q = q.filter_by(status=status)
            return q.count()

    # ── Hash verification ─────────────────────────────────────────────────

    def verify_hash(self, run_id: str) -> bool:
        """Verify that the model file still matches its registered hash.

        Returns False if the run doesn't exist, has no model_path, no
        model_hash, or the file hash no longer matches.
        """
        record = self.get_run(run_id)
        if record is None:
            return False
        if not record.model_hash or not record.model_path:
            return False
        model_path = Path(record.model_path)
        if not model_path.exists():
            return False
        return verify_model_hash(model_path, record.model_hash)

    def set_model_hash(self, run_id: str, model_path: str) -> str | None:
        """Compute and store the hash for a run's model file. Returns the hex
        digest, or None if the file doesn't exist."""
        p = Path(model_path)
        if not p.exists():
            return None
        h = hash_model_file(p)
        with self.Session() as session:
            record = session.get(TrainingRunRecord, run_id)
            if record is not None:
                record.model_hash = h
                record.model_path = str(p)
                session.commit()
        return h


# ── Helpers ───────────────────────────────────────────────────────────────────


def _generate_run_id() -> str:
    """Generate a compact, sortable run ID: 12 hex chars of randomness."""
    return secrets.token_hex(6)


def create_registry(db_path: str = "data/training/registry.db") -> TrainingRegistry:
    """Factory: create or open a registry at the given path."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return TrainingRegistry(db_path)
