from core.features.adapters.v9_feature_adapter import V9FeatureAdapter
from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES


def test_v9_feature_schema_dimension():
    assert len(V9_INSTITUTIONAL_40_FEATURES) == 40


def test_v9_feature_adapter_builds_vector():
    adapter = V9FeatureAdapter()
    feature_source = {name: float(index) for index, name in enumerate(V9_INSTITUTIONAL_40_FEATURES)}

    raw = adapter.build_raw_vector(feature_source)
    assert raw.shape == (40,)
    assert float(raw[0]) == 0.0
    assert float(raw[-1]) == 39.0


