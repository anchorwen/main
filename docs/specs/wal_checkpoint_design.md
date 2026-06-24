# WAL Rotation & Checkpoint Design Specification

> **Plan ref**: UGR v3.1 §修正4 — Hash-Chained WAL rotation 兼容
> **Status**: SPECIFICATION (implementation guide for UGR-A03, UGR-B02)
> **Date**: 2026-06-24

---

## 1. Rotation Sequence

### 1.1 Trigger Conditions

| Trigger | Default Threshold | Rationale |
|:---|:---|:---|
| File size | 100 MB | Prevents unbounded single-file growth |
| Entry count | 1,000,000 records | Alternative to size-based trigger |
| Time-based | 24 hours | Ensures regular checkpoint cadence |
| Manual | Operator signal | Emergency rotation via admin CLI |

### 1.2 Sequence Diagram

```
Time ──────────────────────────────────────────────────────────────────────►

Active WAL:  wal.jsonl
              │ seq=N-1  │ seq=N    │ ... │ seq=M  │ seq=M+1 │
              │ hash: hN  │ hash: hN+1 │   │ hash: hM │ (in-mem)│
              └────────────────────────────────────────────────────┘
                                                 │
                                        ROTATION TRIGGERED at M
                                                 │
                                                 ▼
              ┌──────────────────────────────────────────────────────┐
              │  Step 1: fsync() active WAL                          │
              │  Step 2: Compute checkpoint_hash = hM                │
              │  Step 3: Atomic write: wal.checkpoint.tmp → .checkpoint│
              │  Step 4: os.replace() activate checkpoint            │
              │  Step 5: Rename wal.jsonl → wal_20260624_001.jsonl   │
              │  Step 6: Create new wal.jsonl (empty, next_seq=M+1)  │
              └──────────────────────────────────────────────────────┘
                                                 │
                                                 ▼
New Active:  wal.jsonl (empty, genesis at M+1)
Archived:    wal_20260624_001.jsonl (seq 0...M, verified by checkpoint)

On restart:
  1. Read checkpoint file → last_seq=M, checkpoint_hash=hM
  2. Verify archived WAL from genesis to M against hM
  3. Verify active WAL from M+1 forward (chain from hM)
```

### 1.3 Atomic Checkpoint Write

```python
def _write_checkpoint_atomic(checkpoint_path: Path, data: dict) -> None:
    """Write checkpoint file atomically via temp + os.replace."""
    tmp_path = Path(str(checkpoint_path) + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp_path_fd = os.open(str(tmp_path), os.O_RDWR)
    os.fsync(tmp_path_fd)
    os.close(tmp_path_fd)
    os.replace(str(tmp_path), str(checkpoint_path))  # Atomic on POSIX + Windows
```

---

## 2. Checkpoint File Format

### 2.1 Schema

```json
{
  "version": 1,
  "last_seq": 12345,
  "checkpoint_hash": "a1b2c3d4e5f6...",
  "rotated_at": "2026-06-24T14:30:00+00:00",
  "genesis_hash": "GENESIS_uGRv31_20260624",
  "segment_file": "wal_20260624_001.jsonl",
  "segment_first_seq": 0,
  "segment_last_seq": 12345,
  "prev_checkpoint_hash": "sha256_of_previous_checkpoint_file"
}
```

### 2.2 Field Rationale

| Field | Purpose |
|:---|:---|
| `version` | Schema version — bump on format change |
| `last_seq` | Last sequence number in the rotated segment |
| `checkpoint_hash` | SHA256 of last record's `record_hash` in the segment |
| `rotated_at` | Wall clock timestamp (ISO-8601, for humans) |
| `genesis_hash` | Sentinel hash; verifies this segment belongs to our WAL |
| `segment_file` | Filename of the rotated segment (for lookup) |
| `segment_first_seq` | First seq in this segment (for multi-segment verification) |
| `segment_last_seq` | Last seq in this segment (= last_seq) |
| `prev_checkpoint_hash` | Hash of previous checkpoint file (chains checkpoints together) |

### 2.3 Checkpoint Chain

```
Genesis ──► Checkpoint #1 ──► Checkpoint #2 ──► ... ──► Current
   │              │                 │
   └─ prev=null   └─ prev=hash(C1)  └─ prev=hash(C2)
```

Each checkpoint file contains a self-hash reference to its predecessor. This allows:
- Verification that no checkpoint was inserted or deleted
- Detection of checkpoint file tampering
- Walking backward through rotation history

### 2.4 Checkpoint Hash Integrity

To protect the checkpoint file itself from tampering, write a sibling `.checksig` file:

```python
# checkpoint file: wal.checkpoint
# signature file:  wal.checksig

def _write_checkpoint_signature(checkpoint_path: Path, checkpoint_data: bytes) -> None:
    """Write HMAC-SHA256 signature of checkpoint file for integrity verification."""
    import hmac
    secret = _get_machine_secret()  # Derived from machine-specific stable secret
    sig = hmac.digest(secret, checkpoint_data, "sha256")
    sig_path = checkpoint_path.with_suffix(".checksig")
    sig_path.write_bytes(sig)
```

---

## 3. verify_integrity() Algorithm

### 3.1 Entry Point

```python
def verify_integrity(self) -> tuple[bool, str]:
    """Verify hash chain integrity from last checkpoint to end of active WAL.
    
    Returns:
        (True, "") if integrity confirmed.
        (False, reason) if tampering or corruption detected.
    """
```

### 3.2 Algorithm Pseudocode

```python
def verify_integrity(self) -> tuple[bool, str]:
    # ── Phase 1: Find starting point ──────────────────────────────────
    checkpoints = self._list_checkpoints()  # Sorted by last_seq
    
    if checkpoints:
        # Start from the most recent checkpoint
        cp = checkpoints[-1]
        if not self._verify_checkpoint_signature(cp):
            return False, f"Checkpoint {cp.path} signature invalid"
        
        start_seq = cp.last_seq + 1
        expected_hash = cp.checkpoint_hash
    else:
        # No checkpoint — start from genesis
        start_seq = 0
        expected_hash = self.GENESIS_HASH
    
    # ── Phase 2: Verify archived segments ────────────────────────────
    for segment in self._list_segments():
        if segment.last_seq <= (checkpoints[-1].last_seq if checkpoints else -1):
            continue  # Already covered by checkpoint
        
        for seq, record in self._read_segment(segment):
            if seq < start_seq:
                continue
            
            # Each record: {"seq": N, "prev_hash": "...", "record_hash": "..."}
            actual_prev = record.get("prev_hash", "")
            
            if actual_prev != expected_hash:
                return False, (
                    f"Hash chain broken at seq={seq} in {segment.path}: "
                    f"expected prev_hash={expected_hash[:16]}..., "
                    f"got prev_hash={actual_prev[:16]}..."
                )
            
            expected_hash = record.get("record_hash", "")
    
    # ── Phase 3: Verify active WAL ────────────────────────────────────
    for seq, record in self._read_segment(self._active_wal):
        if seq < start_seq:
            continue
        
        actual_prev = record.get("prev_hash", "")
        if actual_prev != expected_hash:
            return False, (
                f"Hash chain broken at seq={seq} in active WAL: "
                f"expected prev_hash={expected_hash[:16]}..., "
                f"got prev_hash={actual_prev[:16]}..."
            )
        
        expected_hash = record.get("record_hash", "")
    
    return True, ""
```

### 3.3 Performance

| WAL Size | Records | Verify Time | Memory |
|:---|:---|:---|:---|
| 10 MB | ~50,000 | < 1 second | < 50 MB |
| 100 MB | ~500,000 | < 5 seconds | < 100 MB |
| 1 GB | ~5,000,000 | < 60 seconds | < 200 MB (streaming) |

Streaming mode: read one record at a time, compute hash, discard — no full-file load.

### 3.4 Incremental Verification

For frequent checks (every verify.py --full), only verify records since the last verified seq:

```python
def verify_incremental(self, since_seq: int | None = None) -> tuple[bool, str]:
    """Verify only records since `since_seq` (from .verify_stamp)."""
    if since_seq is None:
        since_seq = self._load_verify_stamp()  # Persisted after last successful verify
    # ... same algorithm, but skip records with seq <= since_seq
    # On success: self._save_verify_stamp(last_verified_seq)
```

---

## 4. Genesis Hash Strategy

### 4.1 Design

The genesis hash is the `prev_hash` of the first record (seq=0) in a new WAL.
It serves as the anchor of the entire hash chain.

```python
# core/data/write_ahead_log.py

GENESIS_HASH: str = "GENESIS_uGRv31_20260624"
```

### 4.2 Why Hardcoded

| Approach | Risk |
|:---|:---|
| Random at runtime | Different each restart → can't verify across restarts |
| From config file | Config could be tampered with |
| Hardcoded constant | Source-controlled, code-reviewable, CI-enforceable |

### 4.3 First Record

```python
def _write_first_record(self, payload: dict) -> None:
    """Write the first record in a new WAL segment."""
    record = {
        "seq": self._next_seq,
        "prev_hash": GENESIS_HASH,
        "payload": payload,
    }
    record["record_hash"] = _compute_record_hash(record)
    self._append_and_fsync(record)
```

### 4.4 Rotation Genesis Continuity

After rotation, the new WAL file's first record uses `prev_hash = checkpoint_hash` (from the checkpoint). This means the genesis hash is only used for a truly fresh WAL (first-ever startup). After the first rotation, the checkpoint hash becomes the effective "genesis" for the next segment.

### 4.5 Multi-Segment Verification

```
Segment 0:  GENESIS → h0 → h1 → ... → hN → checkpoint_0.hash = hN
Segment 1:  checkpoint_0.hash → hN+1 → ... → hM → checkpoint_1.hash = hM
Segment 2:  checkpoint_1.hash → hM+1 → ...
```

To verify segment K: start from `checkpoint_{K-1}.checkpoint_hash` (or GENESIS for K=0).

---

## 5. WAL Record Schema

### 5.1 Standard Record

```json
{
  "seq": 12345,
  "prev_hash": "sha256_hex...",
  "record_hash": "sha256_hex...",
  "type": "phantom_stub" | "event" | "snapshot" | "checkpoint_marker",
  "timestamp_wall": "2026-06-24T14:30:01.123456+00:00",
  "payload": { ... }
}
```

### 5.2 Hash Computation

```python
import hashlib, json

def _compute_record_hash(record: dict) -> str:
    """SHA256(prev_hash + serialized_payload)."""
    prev = record.get("prev_hash", GENESIS_HASH)
    payload_str = json.dumps(record.get("payload", {}), sort_keys=True, ensure_ascii=False)
    content = prev.encode() + payload_str.encode("utf-8")
    return hashlib.sha256(content).hexdigest()
```

---

## 6. Test Plan

### 6.1 Unit Tests (UGR-A03)

| Test | Description |
|:---|:---|
| `test_genesis_record_has_correct_prev_hash` | First record's prev_hash = GENESIS_HASH |
| `test_append_updates_hash_chain` | Each append links prev_hash → record_hash |
| `test_verify_integrity_passes_on_clean_chain` | No tampering → verify returns (True, "") |
| `test_verify_integrity_detects_single_bit_flip` | Corrupted record → verify returns (False, ...) |
| `test_verify_integrity_detects_record_deletion` | Missing record → chain broken |
| `test_verify_integrity_detects_record_insertion` | Inserted record → hash mismatch |

### 6.2 Rotation Tests (UGR-B02)

| Test | Description |
|:---|:---|
| `test_rotate_writes_atomic_checkpoint` | Rotation creates checkpoint file atomically |
| `test_verify_across_rotation_boundary` | Hash chain continuous across rotation |
| `test_checkpoint_chain_self_verifying` | prev_checkpoint_hash links checkpoints |
| `test_checkpoint_signature_detects_tamper` | Modified checkpoint → signature mismatch |
| `test_multi_segment_recovery` | Restart with N segments → verify all pass |

---

## 7. References

- UGR v3.1 Plan §修正4: Hash-Chained WAL rotation 兼容
- Phantom State Replay Spec: `docs/specs/phantom_state_replay.md` (§2 Checkpoint Strategy)
- Implementation batches: UGR-A03 (basic WAL), UGR-B02 (hash chain + rotation)
