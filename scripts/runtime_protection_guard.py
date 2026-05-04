"""Automatic runtime protection guard for live trading.

Delegates to live_dispatch_policy.py (single writer for live_dispatch_block.flag).
Journal-quality thresholds live in scripts/guards/journal_quality.py.
"""

from __future__ import annotations

from scripts.guards.journal_quality import evaluate_guard  # noqa: F401

# Backwards-compatible export for tests and callers
__all__ = ["evaluate_guard", "main"]


def main(argv: list[str] | None = None) -> int:
    from scripts import live_dispatch_policy

    return live_dispatch_policy.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
