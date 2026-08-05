"""Content-based hash-lock regression suite (DQAF-20260805-001, IC absolute approval).

The 8/19 battle gate must be immune to the git stat-cache phantom: a process
rewriting a tracked file with byte-identical content (mtime bump only) makes
`git status --porcelain` report a persistent ` M` that never self-heals —
false-positiveing a strict hash-lock at battle time.  The gate now compares
WORKTREE to HEAD by CONTENT (`git diff HEAD --name-only`); identical content
never blocks, real semantic changes always do.

Also locked: untracked source files block (a new uncommitted module is as much
a lineage break as a modified tracked file), EXCEPT the IC-mandated
`_audit_*.py` forensic probes (uncommitted by design — must never block).

Run: python -m pytest tests/training/test_hash_lock_content_gate.py -v
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from scripts.training.train_btc_expected_r_institutional import _enforce_hash_lock

REPO_SRC = "scripts/foo.py"
CONTENT = "X = 1\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Throwaway git repo with one committed tracked source file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "hashlock@test")
    _git(repo, "config", "user.name", "hashlock-test")
    _git(repo, "config", "core.autocrlf", "false")  # deterministic LF on all hosts
    _git(repo, "config", "core.filemode", "false")  # Windows-safe
    src = repo / REPO_SRC
    src.parent.mkdir(parents=True)
    src.write_text(CONTENT, encoding="utf-8", newline="\n")
    _git(repo, "add", REPO_SRC)
    _git(repo, "commit", "-qm", "init")
    return repo


def _touch_mtime(path: Path) -> None:
    """Deterministic stat invalidation without sleeping (mtime granularity-safe)."""
    now = time.time()
    os.utime(path, (now + 10.0, now + 10.0))


def test_clean_tree_passes(git_repo: Path) -> None:
    _enforce_hash_lock(False, cwd=str(git_repo))  # must not raise


def test_content_identical_rewrite_never_blocks(git_repo: Path) -> None:
    """The stat phantom: byte-identical rewrite + mtime bump.  Content-based
    gate is immune — this is the exact 8/19 false-positive being eliminated."""
    src = git_repo / REPO_SRC
    src.write_text(CONTENT, encoding="utf-8", newline="\n")  # identical bytes
    _touch_mtime(src)  # force git to re-stat
    # Sanity: the content diff is empty (the whole premise of the fix).
    out = subprocess.run(
        ["git", "-C", str(git_repo), "diff", "HEAD", "--name-only"],
        capture_output=True,
        text=True,
    )
    assert out.stdout.strip() == ""
    _enforce_hash_lock(False, cwd=str(git_repo))  # must not raise


def test_real_content_change_blocks(git_repo: Path) -> None:
    src = git_repo / REPO_SRC
    src.write_text("X = 2\n", encoding="utf-8", newline="\n")
    with pytest.raises(SystemExit):
        _enforce_hash_lock(False, cwd=str(git_repo))


def test_forensic_probe_never_blocks(git_repo: Path) -> None:
    """IC-mandated _audit_*.py probes stay uncommitted — the gate must NOT
    trip on them (they are present on the 8/19 battle tree by design)."""
    probe = git_repo / "scripts" / "_audit_magic_alignment_safety.py"
    probe.write_text("x = 1\n", encoding="utf-8", newline="\n")
    _enforce_hash_lock(False, cwd=str(git_repo))  # must not raise


def test_non_probe_untracked_source_blocks(git_repo: Path) -> None:
    """A genuine forgotten new source file breaks lineage as much as a
    modified tracked file — must block (IC: 不漏抓新增文件)."""
    src = git_repo / "scripts" / "scratch.py"
    src.write_text("x = 1\n", encoding="utf-8", newline="\n")
    with pytest.raises(SystemExit):
        _enforce_hash_lock(False, cwd=str(git_repo))


def test_allow_dirty_bypasses(git_repo: Path) -> None:
    src = git_repo / REPO_SRC
    src.write_text("X = 3\n", encoding="utf-8", newline="\n")
    _enforce_hash_lock(True, cwd=str(git_repo))  # dev override, never blocks


def test_gitignored_data_prefix_never_blocks(git_repo: Path) -> None:
    """data/ data_btc/ are gitignored projections — a stray file there must
    never trip the gate (git ls-files --exclude-standard omits it)."""
    (git_repo / ".gitignore").write_text("data_btc/\n", encoding="utf-8", newline="\n")
    _git(git_repo, "add", ".gitignore")
    _git(git_repo, "commit", "-qm", "gitignore")
    stray = git_repo / "data_btc" / "state" / "daily_precheck" / "2026-08-19.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("x\n", encoding="utf-8", newline="\n")
    _enforce_hash_lock(False, cwd=str(git_repo))  # gitignored -> no block
