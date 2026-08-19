#!/usr/bin/env python3
"""Shared mypy scope policy for the iron-law type gate (A2 法证出域 + 红线冻结).

Forensic artifacts are OUT of the maintained type-check scope:
- ``scripts/archive/``  — frozen historical/investigation scripts (read-only by definition,
  never touched; ~118 pre-existing type errors are historical records, not live debt).
- ``scripts/_audit_*``  — transient forensic probes (IC mandate: untracked, non-blocking,
  cleaned up after 8/19).  Type-gating throwaway analysis scripts would burn 2min mypy
  feedback cycles on code that is deleted, not maintained.

Red-line frozen debt — files LOCKED for 8/19 (training / live runtime / feature / OFI):
the unified-check errors below are frozen until the post-8/19 cleanup (TECH_DEBT-008).
They are the ONLY reason the ``--full`` mypy gate passes on these files today.  The
allowance blocks NEW errors (a file exceeding its frozen count fails the gate) while
letting the pre-existing drift ride out the battle window untouched (Iron Law: 红线不动).

Both ``verify.py`` and ``pre_commit_mypy.py`` import from this module so the rules live in
ONE place (Iterability: 同逻辑不分散多文件).  This is a documented scope decision for
by-definition-unmaintained / locked code — NOT a mechanism to hide debt in maintained code.
"""

from __future__ import annotations

# POSIX-style path prefixes of forensic artifacts (paths normalized to forward slashes).
FORENSIC_PREFIXES: tuple[str, ...] = (
    "scripts/_audit_",
    "scripts/archive/",
)

# Unified-check error allowances for red-line frozen files (see module docstring).
# Post-8/19 cleanup trigger: fix the root causes, then REMOVE the entry + re-run
# `python scripts/pre_commit_mypy.py --update-baseline`.
# TECH_DEBT-008 已清偿 (FIX-20260819-003): 8 处根因清除 (含 zombie-fuse 告警静默),
# allowance 全数删除 — 冻结期结束。
RED_LINE_FROZEN_ALLOWANCE: dict[str, int] = {}


def is_forensic(rel_path: str) -> bool:
    """Return True if *rel_path* (repo-relative, any separator) is a forensic artifact.

    The type gate skips these files so ``verify.py --full`` stays green without
    papering over real type debt in maintained scripts.
    """
    p = rel_path.replace("\\", "/").lstrip("/")
    return p.startswith(FORENSIC_PREFIXES)


def allowed_errors(rel_path: str, baseline: dict[str, int]) -> int:
    """Max errors tolerated for *rel_path* = max(baseline, red-line frozen allowance)."""
    p = rel_path.replace("\\", "/")
    return max(baseline.get(p, 0), RED_LINE_FROZEN_ALLOWANCE.get(p, 0))
