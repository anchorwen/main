"""Tests for generic MT5 execution payload helpers (Phase B)."""

from core.protocol.live_execution_contract import (
    SCHEMA_LIVE_MT5_EXECUTION_PAYLOAD_V2,
    attach_schema_metadata,
    effective_volume,
    execution_route,
    normalize_action,
)


def test_normalize_action_defaults_empty():
    assert normalize_action("") == "open"
    assert normalize_action(None) == "open"


def test_effective_volume_prefers_payload():
    assert effective_volume({"volume": 0.05}, default_volume=0.01) == 0.05
    assert effective_volume({"lots": 0.03}, default_volume=0.01) == 0.03
    assert effective_volume({}, default_volume=0.02) == 0.02
    assert effective_volume({"volume": -1}, default_volume=0.07) == 0.07


def test_execution_route_buckets():
    assert execution_route("open") == "market_open"
    assert execution_route("reverse") == "market_open"
    assert execution_route("close") == "close"
    assert execution_route("modify_sltp") == "modify_sltp"
    assert execution_route("modify") == "modify_sltp"
    assert execution_route("foo") == "unsupported"


def test_attach_schema_metadata():
    out = attach_schema_metadata({"action": "open"})
    assert out["execution_payload_schema"] == SCHEMA_LIVE_MT5_EXECUTION_PAYLOAD_V2
    out2 = attach_schema_metadata({"execution_payload_schema": "custom.v1"})
    assert out2["execution_payload_schema"] == "custom.v1"
