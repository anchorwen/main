"""Persistent usage store for Alpha risk budget daily counters."""
from datetime import date
import json
from pathlib import Path
from typing import Any

from core.runtime.alpha_budget_contracts import AlphaBudgetUsageContractValidator
from core.runtime.schema_versions import SCHEMA_ALPHA_BUDGET_USAGE


class AlphaBudgetUsageStore:
    """JSON-backed daily Alpha budget usage counters."""

    def __init__(self, path: str | Path, usage_date: str | None = None):
        self._path = Path(path)
        self._date = usage_date or date.today().isoformat()
        self._payload = self._load()
        if self._payload.get("usage_date") != self._date:
            self._payload = self._empty_payload()

    def get(self, alpha_id: str) -> int:
        return int((self._payload.get("counts") or {}).get(alpha_id, 0))

    def increment(self, alpha_id: str) -> int:
        counts = self._payload.setdefault("counts", {})
        counts[alpha_id] = self.get(alpha_id) + 1
        self.save()
        return counts[alpha_id]

    def reset(self) -> None:
        self._payload = self._empty_payload()
        self.save()

    def counts(self) -> dict[str, int]:
        return {key: int(value) for key, value in (self._payload.get("counts") or {}).items()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_ALPHA_BUDGET_USAGE,
            "usage_date": self._date,
            "counts": self.counts(),
        }

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = AlphaBudgetUsageContractValidator.validate(self.to_dict())
        self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return self._empty_payload()
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        return AlphaBudgetUsageContractValidator.validate(payload)

    def _empty_payload(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_ALPHA_BUDGET_USAGE, "usage_date": self._date, "counts": {}}
