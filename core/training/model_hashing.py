"""Cryptographic model hashing for tamper-proof audit trail.

Produces SHA256 fingerprints of model artifact files.  The hash is stored
in the TrainingRegistry at registration time and verified before every
promotion (shadow → live) to detect silent model file corruption or
tampering.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def hash_model_file(path: Path) -> str:
    """SHA256 of a single model artifact file.

    Reads in 64 KB chunks to handle large files efficiently.
    Returns hex-encoded digest string.
    """
    sha = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest()


def hash_file(path: Path) -> str:
    """SHA256 of an arbitrary file (e.g. training dataset NPZ).

    Reads in 64 KB chunks.  Returns hex-encoded digest string.  Phase 5
    lineage: every model's brain config carries ``dataset_hash`` = SHA256 of
    the exact dataset it was trained on.
    """
    sha = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest()


def hash_models_ensemble(paths: list[Path]) -> str:
    """Combined SHA256 for a multi-seed ensemble.

    Hashes each model individually then hashes the concatenation
    of their hex digests (sorted by path for determinism).
    """
    sha = hashlib.sha256()
    for p in sorted(paths, key=lambda p: p.as_posix()):
        sha.update(hash_model_file(p).encode("ascii"))
    return sha.hexdigest()


def verify_model_hash(path: Path, expected_hash: str) -> bool:
    """Check whether a model file matches its registered hash."""
    try:
        return hash_model_file(path) == expected_hash
    except (FileNotFoundError, OSError):
        return False
