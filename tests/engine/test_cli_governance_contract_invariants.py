"""CLI governance summary contract invariants."""

import json

from apps.engine.cli import main
from core.contracts.domain_keys import (
    PAYLOAD_KEY_GOVERNANCE_FOCUS,
    PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT,
)
from core.deployment.governance_summary import count_governance_warnings


def _assert_governance_shape(payload: dict):
    focus = payload[PAYLOAD_KEY_GOVERNANCE_FOCUS]
    warning_count = payload[PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT]
    assert isinstance(focus, list)
    assert all(isinstance(item, dict) for item in focus)
    assert isinstance(warning_count, int)
    assert warning_count >= 0
    assert warning_count == count_governance_warnings(focus)


def test_cli_governance_contract_invariants(tmp_path, capsys):
    commands = [
        ["--base-dir", str(tmp_path), "compliance-audit"],
        ["--base-dir", str(tmp_path), "compliance-matrix"],
        ["--base-dir", str(tmp_path), "final-audit"],
        ["--base-dir", str(tmp_path), "ops-maturity"],
        ["--base-dir", str(tmp_path), "postmortem-report", "--incident-id", "cli-inv"],
        ["--base-dir", str(tmp_path), "release-registry", "summary"],
    ]
    for argv in commands:
        rc = main(argv)
        out = json.loads(capsys.readouterr().out)
        if "summary" in out:
            _assert_governance_shape(out["summary"])
        else:
            _assert_governance_shape(out)
        assert rc in (0, 1)
