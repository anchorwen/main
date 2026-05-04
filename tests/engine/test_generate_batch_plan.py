"""Batch plan generation contract tests."""

from scripts.training.generate_batch_plan import generate_manifests


def test_generate_manifests_shape():
    plan_meta = {
        "lanes": {
            "sur": {
                "role": "chlg",
                "seeds": [42, 43],
                "feature_contract_id": "feat-sur-v9-institutional-1.0.0",
                "iface_semver": "1.0.0",
            },
        },
    }
    manifests = generate_manifests("g2026.1", ["sur"], plan_meta, "deadbeef")
    assert len(manifests) == 2  # 2 seeds
    for m in manifests:
        assert m["schema_version"] == "crt_model_manifest.v1"
        assert m["lane"] == "sur"
        assert m["role"] == "chlg"
        assert m["generation"] == "g2026.1"
        assert m["train_seed"] in (42, 43)
        assert "model_id" in m
        assert "training_run_id" in m


def test_generate_manifests_model_id_format():
    plan_meta = {
        "lanes": {
            "sur": {
                "role": "chlg",
                "seeds": [42],
                "feature_contract_id": "feat-sur-v9-institutional-1.0.0",
                "iface_semver": "1.0.0",
            },
        },
    }
    manifests = generate_manifests("g2026.1", ["sur"], plan_meta, "deadbeef")
    mid = manifests[0]["model_id"]
    assert mid.startswith("CRT.sur.chlg.g2026.1@feat-sur-v9-institutional-1.0.0")


def test_generate_manifests_mtx_maps_to_lane_id():
    plan_meta = {
        "lanes": {
            "mtx_transformer": {
                "role": "chlg",
                "seeds": [42],
                "feature_contract_id": "feat-mtx-qtransformer-1.0.0",
                "iface_semver": "1.0.0",
            },
        },
    }
    manifests = generate_manifests("g2026.1", ["mtx_transformer"], plan_meta, "deadbeef")
    assert manifests[0]["lane"] == "mtx"
    assert manifests[0]["model_id"].startswith("CRT.mtx.")
