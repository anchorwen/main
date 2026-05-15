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
    # v2 format: CRT.<lane>.<role>.<gen>.<TF>@<feat-contract>.s<seed>
    assert mid.startswith("CRT.sur.chlg.g2026.1.M5@feat-sur-v9-institutional-1.0.0")
    assert manifests[0]["timeframe"] == "M5"


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
    assert manifests[0]["timeframe"] == "M5"


def test_generate_manifests_v2_timeframe_expansion():
    """v2: per-TF manifest entries with timeframe and dataset_override fields."""
    plan_meta = {
        "lanes": {
            "arb": {
                "role": "chlg",
                "feature_contract_id": "feat-arb-v6-ou-sniper-1.0.0",
                "iface_semver": "1.0.0",
                "timeframes": {
                    "M5": {"seeds": [42, 43], "dataset": "data/raw/xauusdc_m5.csv"},
                    "M15": {"seeds": [44], "dataset": "data/raw/xauusdc_m15.csv"},
                },
            },
        },
    }
    manifests = generate_manifests("g2026.2", ["arb"], plan_meta, "deadbeef")
    assert len(manifests) == 3  # 2 M5 + 1 M15
    tfs = {m["timeframe"] for m in manifests}
    assert tfs == {"M5", "M15"}
    datasets = {m["dataset_override"] for m in manifests}
    assert "data/raw/xauusdc_m5.csv" in datasets
    assert "data/raw/xauusdc_m15.csv" in datasets
    # Model IDs include timeframe
    m5_ids = [m["model_id"] for m in manifests if m["timeframe"] == "M5"]
    m15_ids = [m["model_id"] for m in manifests if m["timeframe"] == "M15"]
    assert all("M5@" in mid for mid in m5_ids)
    assert all("M15@" in mid for mid in m15_ids)
