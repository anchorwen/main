"""Audit model inventory: map brain configs to model files, find orphans, show live system loading state."""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    config_dir = ROOT / "configs" / "brains"
    model_dir = ROOT / "data" / "models"

    # Read all brain configs
    print("=" * 130)
    print(f"{'Brain ID':42s} {'Type':22s} {'Status':10s} {'Exists':6s} {'Artifact'}")
    print("-" * 130)

    referenced_artifacts = set()

    for f in sorted(config_dir.glob("*.json")):
        if "normalization" in f.name:
            continue
        try:
            with open(f) as fh:
                d = json.load(fh)
        except Exception as e:  # noqa: BLE001
            print(f"{f.name}: ERROR {e}")
            continue

        artifact = d.get("artifact_path", "?")
        exists = (
            "Y" if artifact and Path(artifact).exists() else ("MISSING" if artifact else "NONE")
        )
        referenced_artifacts.add(str(Path(artifact).resolve()) if artifact else "")
        print(
            f"{d.get('brain_id', '?'):42s} {d.get('brain_type', '?'):22s} {d.get('status', '?'):10s} {exists:6s} {artifact}"
        )

    # Find model files NOT referenced by any config
    print()
    print("=" * 130)
    print("ORPHAN MODEL FILES (not referenced by any brain config)")
    print("-" * 130)
    orphan_count = 0
    for root, _dirs, files in os.walk(str(model_dir)):
        for fname in files:
            if fname.endswith((".json", ".txt", ".pkl", ".joblib", ".ubj", ".h5", ".pt", ".xml")):
                fpath = Path(root) / fname
                fpath_str = str(fpath.resolve())
                # Skip result/summary/meta files
                if any(
                    k in fname for k in ("result.json", "training_summary", ".meta.", ".result")
                ):
                    continue
                if fpath_str not in referenced_artifacts:
                    orphan_count += 1
                    size_kb = fpath.stat().st_size / 1024
                    print(f"  [{size_kb:7.0f} KB] {fpath.relative_to(ROOT)}")
    print(f"\n  Total orphans: {orphan_count}")

    # Check old/deprecated models
    print()
    print("=" * 130)
    print("EXPIRED/WORTHLESS MODELS (10d, 20d D1 contracts — known failed)")
    print("-" * 130)
    for root, _dirs, files in os.walk(str(model_dir)):
        for fname in files:
            if ("10d" in fname or "20d" in fname) and not fname.endswith("result.json"):
                fpath = Path(root) / fname
                size_kb = fpath.stat().st_size / 1024
                print(f"  [{size_kb:7.0f} KB] {fpath.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
