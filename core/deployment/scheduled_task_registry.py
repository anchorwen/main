"""Task registry for scheduled jobs — eliminates core→scripts reverse imports.

Scripts register their callables at module import time. The scheduler resolves
tasks by name, so core/deployment never imports from scripts/ directly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_registry: dict[str, Callable[..., Any]] = {}


def register(name: str, fn: Callable[..., Any]) -> None:
    """Register a callable under a well-known task name.

    Idempotent — calling twice with the same name overwrites silently.
    Scripts call this at module level so registration happens on first import.
    """
    _registry[name] = fn


def get_task(name: str) -> Callable[..., Any] | None:
    """Return the registered callable, or None if not yet registered."""
    return _registry.get(name)


def list_registered() -> list[str]:
    """Return sorted list of registered task names (for diagnostics)."""
    return sorted(_registry.keys())


def is_registered(name: str) -> bool:
    """Check whether a task name has been registered."""
    return name in _registry
