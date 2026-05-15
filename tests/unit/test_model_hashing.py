"""Tests for model_hashing.py — cryptographic model hashing."""

import tempfile
from pathlib import Path

from core.training.model_hashing import (
    hash_model_file,
    hash_models_ensemble,
    verify_model_hash,
)


def test_hash_deterministic():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        f.write(b'{"key": "value"}')
        tmp = Path(f.name)
    try:
        h1 = hash_model_file(tmp)
        h2 = hash_model_file(tmp)
        assert h1 == h2
        assert len(h1) == 64  # SHA256 hex
    finally:
        tmp.unlink()


def test_hash_different_content():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        f.write(b"content_a")
        tmp_a = Path(f.name)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        f.write(b"content_b")
        tmp_b = Path(f.name)
    try:
        assert hash_model_file(tmp_a) != hash_model_file(tmp_b)
    finally:
        tmp_a.unlink()
        tmp_b.unlink()


def test_verify_model_hash():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        f.write(b"verify_me")
        tmp = Path(f.name)
    try:
        h = hash_model_file(tmp)
        assert verify_model_hash(tmp, h)
        assert not verify_model_hash(tmp, "deadbeef" * 8)
    finally:
        tmp.unlink()


def test_verify_model_hash_missing_file():
    assert not verify_model_hash(Path("/nonexistent/model.json"), "a" * 64)


def test_hash_models_ensemble_deterministic():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        f.write(b"model_a")
        tmp_a = Path(f.name)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        f.write(b"model_b")
        tmp_b = Path(f.name)
    try:
        h1 = hash_models_ensemble([tmp_a, tmp_b])
        h2 = hash_models_ensemble([tmp_a, tmp_b])
        assert h1 == h2
        assert isinstance(h1, str)
        assert len(h1) == 64
    finally:
        tmp_a.unlink()
        tmp_b.unlink()


def test_hash_models_ensemble_order_independent():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        f.write(b"model_a")
        tmp_a = Path(f.name)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        f.write(b"model_b")
        tmp_b = Path(f.name)
    try:
        h_ab = hash_models_ensemble([tmp_a, tmp_b])
        h_ba = hash_models_ensemble([tmp_b, tmp_a])
        assert h_ab == h_ba  # sorted by path
    finally:
        tmp_a.unlink()
        tmp_b.unlink()


def test_hash_ensemble_different_from_individual():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        f.write(b"model_x")
        tmp = Path(f.name)
    try:
        h_single = hash_model_file(tmp)
        h_ensemble = hash_models_ensemble([tmp])
        assert h_single != h_ensemble
    finally:
        tmp.unlink()
