# Phantom Contract State Replay Specification

> **Plan ref**: UGR v3.1 §修正1 — Phantom stub 完整状态 + WAL seq 引用
> **Status**: SPECIFICATION (implementation guide for UGR-B01, UGR-B04)
> **Date**: 2026-06-24

---

## 1. PhantomStub Minimum State Set

Every Phantom Contract invocation in production (`-O` mode, `hot_path=False`) writes
one `PhantomStub` record to the WAL.  The stub MUST contain enough information for
the offline verifier to deterministically re-execute the predicate.

### 1.1 Required Fields

```python
@dataclass(slots=True)
class PhantomStub:
    contract_id: str              # Unique contract identifier (e.g. "risk_budget_non_negative")
    recorded_at_wal_seq: int      # WAL sequence number at time of recording
    contract_version: int = 1     # Schema version — bump on predicate signature change
    input_snapshot: dict          # Serialized function arguments (see §3)
    input_hash: str               # SHA256 of serialized input_snapshot (dedup key)
    assumed_ok: bool = True       # Production assumed the predicate passed
    timestamp_wall: str           # ISO-8601 wall clock timestamp (debug only, NOT monotonic)
    caller_module: str            # __name__ of the calling module (attribution)
```

### 1.2 Field Rationale

| Field | Why Required |
|:---|:---|
| `contract_id` | Maps stub → predicate function for replay |
| `recorded_at_wal_seq` | Anchor for state reconstruction; verifier replays WAL up to this seq |
| `contract_version` | Predicate signatures evolve; version mismatch → skip with warning |
| `input_snapshot` | The ACTUAL arguments passed to the predicate — enables deterministic replay |
| `input_hash` | Dedup key; prevents re-verifying identical invocations |
| `assumed_ok` | Records what production assumed; verifier confirms or refutes |
| `timestamp_wall` | Human-readable time for incident correlation |
| `caller_module` | Attribution for alert routing |

### 1.3 What is NOT Stored

- **Full system state** — reconstructed from WAL, not duplicated in stub
- **Predicate source code** — looked up by `contract_id` from a registry
- **Return value of the wrapped function** — the stub records the predicate check, not the decorated function's result

---

## 2. WAL State Reconstruction Protocol

### 2.1 Core Principle

To replay a PhantomStub recorded at WAL sequence `N`, the verifier must reconstruct
the system state as it existed **just before** sequence `N` was written.  This is done
by projecting all WAL entries from the last checkpoint up to `N-1`.

### 2.2 State Projection Model

```
WAL:  [seq=0] [seq=1] ... [checkpoint at seq=K] ... [seq=N-1] [seq=N: PhantomStub] ...
                            ↑                                        ↑
                      start here                              replay target

Replay algorithm:
  1. Load checkpoint at or before N
  2. Initialize StateProjector from checkpoint snapshot
  3. For seq in (checkpoint_seq + 1) ... (N - 1):
       entry = WAL.read(seq)
       projector.apply(entry)        # accumulate state effects
  4. state = projector.snapshot()    # reconstructed state at seq N
  5. stub = WAL.read(N)              # the PhantomStub to verify
  6. result = predicate(stub.input_snapshot, state)  # re-execute
```

### 2.3 MVP Simplification (UGR-B01)

For the initial prototype, the first phantom contract (`risk_budget_non_negative`)
has **no external state dependency** — the predicate only checks its input parameter
(budget value).  State reconstruction is a **no-op** for MVP.

```python
# MVP replay path (no state dependency):
def _replay_mvp(stub: PhantomStub) -> bool:
    predicate = PREDICATE_REGISTRY[stub.contract_id]
    args = deserialize(stub.input_snapshot)
    return predicate(**args)
```

### 2.4 Full State Reconstruction (UGR-B04)

For state-dependent predicates (e.g., `position_count_consistent`), the verifier
needs the `StateProjector`:

```python
class StateProjector:
    """Accumulates system state by applying WAL entries in sequence."""
    
    def __init__(self, checkpoint_snapshot: dict | None = None):
        self._positions: dict[str, float] = {}     # position_id → size
        self._balances: dict[str, float] = {}      # account → balance
        self._risk_budget: dict[str, float] = {}   # brain_id → remaining budget
        # ... extensible per contract needs
    
    def apply(self, entry: dict) -> None:
        """Apply one WAL entry's state effect."""
        entry_type = entry.get("type")
        handler = self._handlers.get(entry_type)
        if handler:
            handler(self, entry)
    
    def snapshot(self) -> dict:
        """Return reconstructed state for predicate evaluation."""
        return {
            "positions": dict(self._positions),
            "balances": dict(self._balances),
            "risk_budget": dict(self._risk_budget),
        }
```

### 2.5 Checkpoint Strategy

| Scenario | Start Point | Rationale |
|:---|:---|:---|
| WAL < 10,000 entries | Genesis (seq=0) | Full replay is fast enough |
| WAL ≥ 10,000 entries | Last checkpoint | Avoid O(N) startup cost |
| No checkpoint exists | Genesis | Only option |
| Checkpoint corrupted | Genesis | Safety over speed |

---

## 3. Parameter Serialization Format

### 3.1 JSON-First Protocol

All `input_snapshot` values are serialized as JSON.  The serializer handles:

| Python Type | JSON Representation | Notes |
|:---|:---|:---|
| `int`, `float` | Number | `float('nan')` → `"__NaN__"` string marker |
| `str` | String | |
| `bool` | Boolean | |
| `list`, `tuple` | Array | Tuples serialized as arrays; deserialized as lists |
| `dict` | Object | String keys only |
| `None` | `null` | |
| `numpy.float32/64` | Number | Converted to Python float first |
| `numpy.ndarray` | `{"__ndarray__": [...], "__dtype__": "float32"}` | |
| `Decimal` | `{"__decimal__": "..."}` | String representation |
| `datetime` | `{"__datetime__": "ISO-8601"}` | |
| Unsupported | `{"__unserializable__": "repr(...)"}` | WARNING marker |

### 3.2 Serializer API

```python
class PhantomSerializer:
    """Serialize/deserialize PhantomStub input_snapshot fields."""
    
    @staticmethod
    def serialize(obj: Any) -> Any:
        """Convert a Python value to a JSON-serializable structure."""
        ...
    
    @staticmethod
    def deserialize(data: Any) -> Any:
        """Convert a JSON structure back to Python objects."""
        ...
    
    @staticmethod
    def serialize_args(args: tuple, kwargs: dict) -> dict:
        """Serialize function arguments into input_snapshot format.
        
        Returns: {"args": [...], "kwargs": {...}}
        """
        ...
```

### 3.3 Determinism Guarantee

- `input_snapshot` is captured **before** the predicate executes
- The same `input_snapshot` + same predicate function → same boolean result
- `input_hash = SHA256(json.dumps(input_snapshot, sort_keys=True))` for dedup

---

## 4. Offline Verifier Algorithm

### 4.1 Entry Point

```bash
python scripts/verify_phantom_contracts.py --wal-path data_btc/wal.jsonl
```

### 4.2 Algorithm (Pseudocode)

```python
def verify_phantom_contracts(wal_path: Path) -> VerificationReport:
    """
    Offline replay of all PhantomStub records in the WAL.
    
    1. Scan WAL for PhantomStub entries
    2. Group by contract_id, dedup by input_hash
    3. For each unique stub:
       a. Reconstruct state at recorded_at_wal_seq
       b. Deserialize input_snapshot
       c. Look up predicate by contract_id
       d. Re-execute predicate
       e. Compare result to assumed_ok
    4. Report violations
    """
    report = VerificationReport()
    wal = WriteAheadLog(wal_path)
    projector = StateProjector()
    seen_hashes: set[str] = set()
    
    for seq, entry in wal.enumerate():
        if entry.get("type") == "phantom_stub":
            stub = PhantomStub(**entry)
            
            # Dedup: same input_hash → same predicate result
            if stub.input_hash in seen_hashes:
                continue
            seen_hashes.add(stub.input_hash)
            
            # Replay
            state = projector.snapshot()
            predicate = PREDICATE_REGISTRY.get(stub.contract_id)
            
            if predicate is None:
                report.add_warning(f"Unknown contract_id: {stub.contract_id}")
                continue
            
            if stub.contract_version != CURRENT_VERSIONS[stub.contract_id]:
                report.add_warning(
                    f"Version mismatch: {stub.contract_id} "
                    f"v{stub.contract_version} != v{CURRENT_VERSIONS[stub.contract_id]}"
                )
                continue
            
            args = PhantomSerializer.deserialize(stub.input_snapshot)
            actual_ok = predicate(**args, _state=state)
            
            if actual_ok != stub.assumed_ok:
                report.add_violation(Violation(
                    contract_id=stub.contract_id,
                    wal_seq=seq,
                    assumed_ok=stub.assumed_ok,
                    actual_ok=actual_ok,
                    input_hash=stub.input_hash,
                    timestamp=stub.timestamp_wall,
                ))
        
        else:
            # Non-stub WAL entry: accumulate state
            projector.apply(entry)
    
    return report
```

### 4.3 Exit Codes

| Exit | Meaning | CI Action |
|:---|:---|:---|
| 0 | All stubs replayed, all `assumed_ok` confirmed | PASS |
| 1 | ≥1 `assumed_ok=True` but predicate returned `False` | FAIL — contract violation in production |
| 2 | WAL file missing or unreadable | SKIP — no data to verify |
| 3 | Predicate registry incomplete (unknown contract_id) | WARN — configuration gap |

### 4.4 Performance Budget

| Scenario | Target | Notes |
|:---|:---|:---|
| 1,000 stubs, no state deps | < 5 seconds | Pure JSON deserialization + function call |
| 10,000 stubs, full state replay | < 5 minutes | Per the 5-minute review requirement |
| 100,000 stubs | Incremental mode | Only verify stubs since last checkpoint |

---

## 5. Predicate Registry

Each Phantom Contract registers its predicate in a global registry:

```python
# core/contracts/phantom_registry.py

PREDICATE_REGISTRY: dict[str, Callable[..., bool]] = {}

def register_predicate(contract_id: str, version: int):
    """Decorator to register a phantom predicate function."""
    def decorator(fn):
        PREDICATE_REGISTRY[contract_id] = fn
        CURRENT_VERSIONS[contract_id] = version
        return fn
    return decorator

# Usage:
@register_predicate("risk_budget_non_negative", version=1)
def _check_risk_budget_non_negative(budget: float, *, _state: dict | None = None) -> bool:
    return budget >= 0.0
```

---

## 6. Contract Classification

### 6.1 Hot-Path (no phantom stub — `__debug__` + InvariantEngine only)

| Contract ID | Check Site | Reason for Hot-Path |
|:---|:---|:---|
| `risk_budget_non_negative` | `evaluate_risk()` | Called per signal — WAL overhead unacceptable |
| `exit_latency_bounded` | `exit_watchdog.dispatch()` | Per-tick dispatch — latency-sensitive |
| `position_count_consistent` | `live_cycle` | Per-cycle validation — hot loop |
| `no_silent_cap_unwrap` | `live_cycle` | Per-cycle — zero overhead required |

### 6.2 Non-Hot-Path (phantom stub — auditable offline)

| Contract ID | Check Site | Frequency |
|:---|:---|:---|
| `training_readiness` | `daily_ops` step | Daily |
| `governance_alignment` | `daily_ops` step | Daily |
| `model_card_completeness` | `daily_ops` step | Daily |
| `data_health_report_completeness` | `daily_ops` step | Daily |
| `alpha_lifecycle_valid` | `daily_ops` step | Daily |

---

## 7. References

- UGR v3.1 Plan: `C:\Users\Administrator\.claude\plans\sunny-wishing-wave.md`
- Phantom Contract design: v3.1 §修正1
- WAL design: v3.1 §修正4
- Implementation batch: UGR-B01 (prototype), UGR-B04 (full)
