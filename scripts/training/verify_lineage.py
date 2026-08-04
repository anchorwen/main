"""verify_lineage.py - the lineage iron gate for every ENABLED live brain.

Phase 5 (血缘/版本化 - M3 战役五 / IC 最高批准, FIX-20260803-006).

Every model that reaches production must carry its birth certificate:
unforgeable Git hash, dataset hash, contract hash, feature schema, artifact
hash, and a strategy-line magic.  This gate makes any brain that lacks a full
lineage record *impossible to hide* - it is scanned every run and exits
non-zero until the record is complete.

Checks per enabled brain (from the live config's registry_entries):
  1. artifact_hash_present   - artifact_hash field non-empty
  2. artifact_file_exists    - artifact_path exists on disk
  3. artifact_hash_matches   - sha256(artifact) == artifact_hash  (tamper-proof)
  4. registry_row            - a TrainingRunRecord exists with model_hash == artifact_hash
  5. commit_hash             - trained_by_commit_hash present and != "unknown"
  6. dataset_hash            - dataset_hash present (Phase 5 lineage field)
  7. label_contract_id       - label_contract_id present
  8. feature_schema_known    - feature_schema_id ∈ canonical schema SSOT
  9. magic_matches_line      - magic == live.yaml strategy_line.magic

Verdicts:
  PASS    - check passed
  FAIL    - integrity violation (tampered artifact / wrong magic / unknown schema)
  MISSING - lineage gap (legacy hand-written configs / pre-FIX rows) → migration
            guidance printed

Exit codes:
  0 - every enabled brain fully passes
  1 - ≥1 integrity FAIL (dangerous: the model or its wiring is not what it claims)
  2 - ≥1 MISSING lineage (legacy config without a birth certificate - must migrate)

Usage:
  # BTC (dual-asset: live_btc.yaml + brains_btc + data_btc registry)
  python scripts/training/verify_lineage.py \
      --live configs/live_btc.yaml --brains-dir configs/brains_btc \
      --registry-db data_btc/training/registry.db

  # XAU (defaults)
  python scripts/training/verify_lineage.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _sha256_file(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _known_schemas() -> set[str]:
    """Canonical feature-schema set (alias-resolved)."""
    from core.deployment.brain_config_validator import SCHEMA_ALIASES, SCHEMA_DIMENSIONS

    known = set(SCHEMA_DIMENSIONS)
    for canonical in SCHEMA_DIMENSIONS:
        for alias, target in SCHEMA_ALIASES.items():
            if target == canonical:
                known.add(alias)
    return known


def _strategy_line_magics(live: dict[str, Any]) -> dict[str, int]:
    """{strategy_line_name: magic} from the live config."""
    out: dict[str, int] = {}
    lines = live.get("strategy_lines", {})
    if not isinstance(lines, dict):
        return out
    for name, cfg in lines.items():
        if isinstance(cfg, dict) and isinstance(cfg.get("magic"), int):
            out[name] = cfg["magic"]
    return out


def _registry_hashes(registry_db: str | Path) -> dict[str, dict[str, Any]]:
    """{model_hash: row-dict} for all training runs in the registry."""
    from core.training.training_registry import create_registry

    if not Path(registry_db).exists():
        return {}
    registry = create_registry(str(registry_db))
    out: dict[str, dict[str, Any]] = {}
    for run in registry.list_runs(limit=100000):
        if run.model_hash:
            out[run.model_hash] = {
                "run_id": run.run_id,
                "status": run.status,
                "dataset_hash": run.dataset_hash,
                "label_contract_id": run.label_contract_id,
                "trained_by_commit_hash": run.trained_by_commit_hash,
                "oos_verdict": run.oos_verdict,
            }
    return out


def _enabled_brain_paths(live: dict[str, Any]) -> list[str]:
    entries = live.get("brains", {}).get("registry_entries", [])
    if not isinstance(entries, list):
        return []
    paths: list[str] = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("enabled") and entry.get("path"):
            paths.append(str(entry["path"]))
    return paths


def verify_brain(
    cfg: dict[str, Any],
    project_root: Path,
    registry_hashes: dict[str, dict[str, Any]],
    line_magics: dict[str, int],
) -> list[dict[str, str]]:
    """Run all checks for one brain config.  Returns a list of check results."""
    brain_id = cfg.get("brain_id", "?")
    results: list[dict[str, str]] = []

    # Lineage-exemption (FIX-20260805-001): legacy brains grandfathered by an
    # explicit IC migration mandate.  Only the four *lineage-gap* checks may be
    # exempted (registry_row/commit_hash/dataset_hash/label_contract_id are
    # about whether training went through the institutional pipeline and cannot
    # be truthfully reconstructed for pre-M3 ad-hoc artifacts).  Integrity
    # FAILs - tampered artifact, wrong magic, unknown schema - are NEVER
    # downgraded.  A malformed exemption block invalidates itself (FAIL).
    _EXEMPTABLE = {"registry_row", "commit_hash", "dataset_hash", "label_contract_id"}
    exempted_checks: set[str] = set()
    exempt_note = ""
    exempt_block = cfg.get("lineage_exempt")
    if exempt_block is not None:
        if isinstance(exempt_block, dict):
            exempted = exempt_block.get("exempts")
            fix_id = str(exempt_block.get("fix_id", "") or "")
            granted_by = str(exempt_block.get("granted_by", "") or "")
            if (
                isinstance(exempted, list)
                and set(exempted) <= _EXEMPTABLE
                and fix_id
                and granted_by
            ):
                exempted_checks = set(exempted)
                exempt_note = f"{fix_id} / {granted_by}"
        if not exempt_note:
            results.append(
                {
                    "brain": brain_id,
                    "check": "lineage_exempt_valid",
                    "verdict": "FAIL",
                    "detail": "lineage_exempt block malformed - exemption ignored "
                    f"(must be a dict with non-empty fix_id/granted_by and exempts "
                    f"subset of {sorted(_EXEMPTABLE)})",
                }
            )

    def add(check: str, verdict: str, detail: str) -> None:
        if verdict == "MISSING" and check in exempted_checks:
            verdict = "PASS"
            detail = f"EXEMPT ({exempt_note}) - {detail}"
        results.append({"brain": brain_id, "check": check, "verdict": verdict, "detail": detail})

    # 1. artifact_hash present
    artifact_hash = str(cfg.get("artifact_hash", "") or "")
    if not artifact_hash:
        add("artifact_hash_present", "FAIL", "artifact_hash field is empty")
    else:
        add("artifact_hash_present", "PASS", artifact_hash[:12] + "...")

    # 2/3. artifact file + hash match
    artifact_path = cfg.get("artifact_path", "")
    if artifact_hash and artifact_path:
        full = Path(artifact_path)
        if not full.is_absolute():
            full = project_root / full
        if not full.exists():
            add("artifact_file_exists", "FAIL", f"artifact not found: {full}")
        else:
            add("artifact_file_exists", "PASS", str(full))
            if _sha256_file(full) == artifact_hash:
                add("artifact_hash_matches", "PASS", "sha256(file) == artifact_hash")
            else:
                add(
                    "artifact_hash_matches",
                    "FAIL",
                    "sha256(file) != artifact_hash - artifact was modified since training",
                )
    elif not artifact_hash:
        add("artifact_file_exists", "SKIP", "no artifact_hash to verify against")
        add("artifact_hash_matches", "SKIP", "no artifact_hash to verify against")
    else:
        add("artifact_file_exists", "FAIL", "artifact_path is empty")

    # 4. registry row
    row = registry_hashes.get(artifact_hash) if artifact_hash else None
    if row is None:
        add(
            "registry_row",
            "MISSING",
            "no TrainingRunRecord with model_hash == artifact_hash - "
            "train via the institutional pipeline to register one",
        )
    else:
        add(
            "registry_row",
            "PASS",
            f"run_id={row.get('run_id')} status={row.get('status')} "
            f"oos={row.get('oos_verdict')}",
        )

    # 5. commit hash
    commit = str(cfg.get("trained_by_commit_hash", "") or "")
    if not commit or commit == "unknown":
        add(
            "commit_hash",
            "MISSING",
            "trained_by_commit_hash missing or 'unknown' - hand-written config "
            "(build_brain_config() injects it automatically)",
        )
    else:
        add("commit_hash", "PASS", commit)

    # 6. dataset hash
    ds_hash = str(cfg.get("dataset_hash", "") or "")
    if not ds_hash:
        add(
            "dataset_hash",
            "MISSING",
            "dataset_hash missing - legacy config predates Phase 5 lineage",
        )
    else:
        add("dataset_hash", "PASS", ds_hash[:12] + "...")

    # 7. label contract id
    lbl = str(cfg.get("label_contract_id", "") or "")
    if not lbl:
        add(
            "label_contract_id",
            "MISSING",
            "label_contract_id missing - legacy config predates Phase 5 lineage",
        )
    else:
        add("label_contract_id", "PASS", lbl)

    # 8. feature schema known
    schema_id = cfg.get("feature_schema_id", "") or cfg.get("feature_schema", "")
    if schema_id and schema_id in _known_schemas():
        add("feature_schema_known", "PASS", schema_id)
    elif schema_id:
        add("feature_schema_known", "FAIL", f"schema '{schema_id}' not in canonical SSOT")
    else:
        add("feature_schema_known", "FAIL", "feature_schema_id missing")

    # 9. magic matches strategy line
    group = str(cfg.get("contract_group", "") or "")
    magic = cfg.get("magic")
    if not group or not isinstance(magic, int):
        add("magic_matches_line", "FAIL", f"contract_group={group!r} magic={magic!r}")
    elif group in line_magics:
        if magic == line_magics[group]:
            add("magic_matches_line", "PASS", f"{group} magic={magic}")
        else:
            add(
                "magic_matches_line",
                "FAIL",
                f"{group} magic={magic} but strategy_line magic={line_magics[group]} - "
                "broker fills would attribute to the wrong line",
            )
    else:
        add(
            "magic_matches_line",
            "MISSING",
            f"contract_group '{group}' not in live strategy_lines - cannot verify magic",
        )

    return results


MIGRATION_GUIDANCE = """\
  Migration guidance for a MISSING-lineage (legacy) brain:
    1. Locate the training artifacts that produced the model.
    2. Re-run training through the institutional pipeline so build_brain_config()
       injects trained_by_commit_hash / dataset_hash / label_contract_id and the
       TrainingRunRecord is written to the registry:
         python scripts/training/train.py --contract <contract.yaml> --allow-dirty
       (or scripts/training/train_btc_expected_r_institutional.py for twin-towers)
    3. Fix any magic mismatch by setting the brain's magic to the strategy-line
       magic in the live config.
    4. Re-run this gate until it exits 0.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify_lineage",
        description="Lineage iron gate - every enabled live brain must carry a birth certificate",
    )
    parser.add_argument(
        "--live", type=Path, default=PROJECT_ROOT / "configs/live.yaml", help="Live config"
    )
    parser.add_argument(
        "--brains-dir",
        type=Path,
        default=PROJECT_ROOT / "configs/brains",
        help="Directory containing brain config JSONs",
    )
    parser.add_argument(
        "--registry-db",
        type=str,
        default="data/training/registry.db",
        help="Training registry SQLite path",
    )
    args = parser.parse_args(argv)

    live_path = args.live
    if not live_path.exists():
        print(f"[verify_lineage] ERROR: live config not found: {live_path}", file=sys.stderr)
        return 2

    live = _load_yaml(live_path)
    brains_dir = args.brains_dir
    registry_hashes = _registry_hashes(args.registry_db)
    line_magics = _strategy_line_magics(live)

    paths = _enabled_brain_paths(live)
    if not paths:
        print(f"[verify_lineage] No enabled brains found in {live_path}")
        return 0

    print("=" * 78)
    print(f"  verify_lineage - {live_path.name}")
    print(f"  registry: {args.registry_db}")
    print(f"  enabled brains: {len(paths)}")
    print("=" * 78)

    all_results: list[dict[str, str]] = []

    def _resolve_cfg(rel: str) -> Path | None:
        p = Path(rel)
        if p.is_absolute():
            return p if p.exists() else None
        # live.yaml paths are project-relative (e.g. configs/brains_btc/BTC_X.json)
        cand = PROJECT_ROOT / p
        if cand.exists():
            return cand
        # Fallback: bare filename inside --brains-dir
        if brains_dir:
            fb = brains_dir / p
            if fb.exists():
                return fb
            fb2 = brains_dir / p.name
            if fb2.exists():
                return fb2
        return None

    for rel in paths:
        cfg_path = _resolve_cfg(rel)
        if cfg_path is None:
            all_results.append(
                {
                    "brain": rel,
                    "check": "config_file",
                    "verdict": "FAIL",
                    "detail": "brain config not found (tried project-root and --brains-dir)",
                }
            )
            continue
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            all_results.append(
                {
                    "brain": rel,
                    "check": "config_file",
                    "verdict": "FAIL",
                    "detail": f"cannot read brain config: {e}",
                }
            )
            continue
        all_results.extend(verify_brain(cfg, PROJECT_ROOT, registry_hashes, line_magics))

    n_pass = sum(1 for r in all_results if r["verdict"] == "PASS")
    n_fail = sum(1 for r in all_results if r["verdict"] == "FAIL")
    n_missing = sum(1 for r in all_results if r["verdict"] == "MISSING")
    n_skip = sum(1 for r in all_results if r["verdict"] == "SKIP")

    # ASCII markers only - Windows GBK console cannot encode unicode glyphs.
    for r in all_results:
        mark = {"PASS": "PASS", "FAIL": "FAIL", "MISSING": "MISS", "SKIP": "skip"}[r["verdict"]]
        print(f"  [{mark:4}] {r['brain']}  {r['check']}: {r['detail']}")

    print("=" * 78)
    print(f"  PASS={n_pass}  FAIL={n_fail}  MISSING={n_missing}  SKIP={n_skip}")
    if n_missing:
        print(MIGRATION_GUIDANCE)
    if n_fail or n_missing:
        print("[verify_lineage] EXIT 1 - lineage incomplete; gate CLOSED.")
    else:
        print("[verify_lineage] EXIT 0 - every enabled brain carries a full birth certificate.")
    print("=" * 78)

    return 1 if (n_fail or n_missing) else 0


if __name__ == "__main__":
    raise SystemExit(main())
