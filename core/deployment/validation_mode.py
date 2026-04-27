"""Validation mode helpers for deployment services."""

from core.deployment.domain_keys import VALIDATION_MODE_DEEP


def resolve_validation_mode(container, validation_mode: str | None) -> str:
    """Resolve explicit mode first, then container default, then deep."""
    if validation_mode is not None:
        return validation_mode
    return getattr(container, "validation_mode", VALIDATION_MODE_DEEP)
