"""Tests-scoped autouse fixtures — global test-domain isolation (DQAF-20260806-002).

Physical webhook blindfold: any ``LiveAlertHub`` constructed anywhere in the
test suite can auto-wire real DingTalk/Slack channels from environment
variables.  A single test that trips a critical path would leak a CRITICAL
alert into the live ops group.

This autouse fixture deletes the alert webhook env vars for every test,
so the test domain can never connect to the production channel even if a
test constructs an alert hub directly (belt-and-suspenders on top of the
per-module stub in tests/contracts/test_phantom_contract.py).
"""

from __future__ import annotations

import pytest

_ALERT_WEBHOOK_ENV_VARS = (
    "QUANTOS_DINGTALK_WEBHOOK_URL",
    "QUANTOS_DINGTALK_SECRET",
    "QUANTOS_SLACK_WEBHOOK_URL",
    "SLACK_WEBHOOK_URL",
)


@pytest.fixture(autouse=True)
def _blind_alert_webhooks(monkeypatch):
    """Physically remove alert webhook credentials from the test environment."""
    for var in _ALERT_WEBHOOK_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
