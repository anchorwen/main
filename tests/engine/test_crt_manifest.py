"""CRT manifest builder contracts."""

from scripts.training.crt_manifest import (
    CRTManifestV1,
    build_manifest,
    build_model_id,
)


def test_build_model_id_shape():
    mid = build_model_id(
        lane="sur",
        role="chlg",
        generation="g2026.1",
        feature_contract_id="feat-v9-institutional-1.0.0",
    )
    assert mid.startswith("CRT.sur.chlg.g2026.1@feat-v9-institutional-1.0.0")


def test_manifest_roundtrip_json_mode():
    m = build_manifest(
        lane="mtx",
        role="chlg",
        generation="g2026.1",
        feature_contract_id="feat-tick9-seq-0.2.0",
        dataset_slice_id="2025Q4_xau_v2",
        iface_semver="1.0.0",
        trainer_version="test-0.0.1",
        git_commit="deadbeef",
        train_started_at_utc="2026-04-30T12:00:00Z",
        train_seed=99,
        metrics={"auc": 0.71},
        risk_notes=["test"],
    )
    raw = m.model_dump(mode="json")
    m2 = CRTManifestV1.model_validate(raw)
    assert m2.model_id == m.model_id
    assert m2.train_seed == 99
