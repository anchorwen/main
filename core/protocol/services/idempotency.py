import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


class IdempotencyStore:
    """Tracks idempotency keys to prevent duplicate dispatch.

    Uses a file-backed store partitioned by date. Each key is recorded
    with its timestamp and associated message_id, enabling both
    duplicate detection and audit trail.
    """

    def __init__(self, base_dir: str, ttl_hours: int = 48):
        self._base_dir = Path(base_dir)
        self._ttl = timedelta(hours=ttl_hours)

    def check_and_claim(self, *, idempotency_key: str, message_id: str, date_key: str) -> dict:
        if not idempotency_key:
            return {"status": "no_key", "duplicate": False, "claimed": False}

        existing = self._find_existing(idempotency_key, date_key)
        if existing is not None:
            return {
                "status": "duplicate",
                "duplicate": True,
                "claimed": False,
                "original_message_id": existing.get("message_id"),
                "original_timestamp": existing.get("timestamp"),
            }

        self._write_claim(idempotency_key, message_id, date_key)
        return {"status": "claimed", "duplicate": False, "claimed": True}

    def is_duplicate(self, *, idempotency_key: str, date_key: str) -> bool:
        if not idempotency_key:
            return False
        return self._find_existing(idempotency_key, date_key) is not None

    def _find_existing(self, idempotency_key: str, date_key: str) -> dict | None:
        for dk in self._lookup_date_keys(date_key):
            path = self._store_path(dk)
            if not path.exists():
                continue
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    record = json.loads(line)
                    if record.get("idempotency_key") == idempotency_key:
                        return record
        return None

    def _write_claim(self, idempotency_key: str, message_id: str, date_key: str) -> None:
        path = self._store_path(date_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "idempotency_key": idempotency_key,
            "message_id": message_id,
            "timestamp": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "date_key": date_key,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _store_path(self, date_key: str) -> Path:
        return self._base_dir / date_key / "idempotency_claims.jsonl"

    def _lookup_date_keys(self, date_key: str) -> list[str]:
        parsed = datetime.fromisoformat(date_key).date()
        days_back = int(self._ttl.total_seconds() / 86400) + 1
        return [(parsed - timedelta(days=d)).isoformat() for d in range(days_back + 1)]


class DuplicateDetector:
    """Detects duplicate messages in the communication record stream."""

    def __init__(self, communication_reader):
        self._reader = communication_reader

    def find_duplicates(self, *, date_key: str, target: str) -> list[dict]:
        records = self._reader.list_records(date_key=date_key, target=target)
        seen_keys: dict[str, bool] = {}
        duplicates: list[dict] = []

        for record in records:
            idem_key = record.get("envelope", {}).get("idempotency_key")
            if not idem_key:
                continue
            if idem_key in seen_keys:
                duplicates.append(
                    {
                        "idempotency_key": idem_key,
                        "duplicate_message_id": record.get("message_id"),
                        "original_message_id": seen_keys[idem_key],
                    }
                )
            else:
                seen_keys[idem_key] = record.get("message_id")

        return duplicates

    def is_duplicate_envelope(self, *, date_key: str, target: str, idempotency_key: str) -> bool:
        if not idempotency_key:
            return False
        records = self._reader.list_records(date_key=date_key, target=target)
        count = sum(
            1 for r in records if r.get("envelope", {}).get("idempotency_key") == idempotency_key
        )
        return count > 1
