import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

existing = os.environ.get("PYTHONPATH", "")
parts = [str(ROOT)]
if existing:
    parts.append(existing)
os.environ["PYTHONPATH"] = os.pathsep.join(parts)

COMMUNICATION_V9_SHADOW_REGRESSION = [
    "tests/engine/test_communication_ops_cli.py",
    "tests/engine/test_v9_shadow_smoke.py",
    "tests/engine/test_communication_operations_service.py",
    "tests/engine/test_communication_replay_service.py",
    "tests/engine/test_communication_replay_executor.py",
    "tests/engine/test_communication_chain.py",
    "tests/engine/test_communication_dispatcher.py",
    "tests/engine/test_communication_inspection_service.py",
    "tests/engine/test_communication_record_reader.py",
    "tests/engine/test_communication_replay_gate.py",
    "tests/engine/test_runtime_loop_communication_integration.py",
    "tests/engine/test_v9_shadow_contracts.py",
    "tests/engine/test_v9_shadow_integration.py",
    "tests/engine/test_v9_shadow_sse_utils.py",
]

FAST_COMMUNICATION_V9_SHADOW_CONTRACTS = [
    "tests/engine/test_v9_shadow_contracts.py",
    "tests/engine/test_v9_shadow_sse_utils.py",
]

GOVERNANCE_CONTRACTS = [
    "tests/engine/test_governance_summary.py",
    "tests/engine/test_governance_contract_invariants.py",
    "tests/engine/test_cli_governance_contract_invariants.py",
]

# Engine tests that are contract/preflight guards — fast by design, exempt from slow marking
ENGINE_FAST_GATE = [
    "tests/engine/test_runtime_contract_guard.py",
    "tests/engine/test_live_read_only_preflight.py",
]

# All engine tests are slow by default, except these explicitly fast suites
ENGINE_SLOW_EXEMPTIONS = (
    set(FAST_COMMUNICATION_V9_SHADOW_CONTRACTS) | set(GOVERNANCE_CONTRACTS) | set(ENGINE_FAST_GATE)
)


def pytest_addoption(parser):
    parser.addoption(
        "--communication-v9-shadow-regression",
        action="store_true",
        default=False,
        help="Run the staged communication/v9-shadow regression suite.",
    )
    parser.addoption(
        "--fast-communication-v9-shadow-contracts",
        action="store_true",
        default=False,
        help="Run the fast communication/v9-shadow contract regression suite.",
    )
    parser.addoption(
        "--governance-contracts",
        action="store_true",
        default=False,
        help="Run governance summary contract and invariant suites.",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "fast_contracts: fast communication/v9-shadow contract suite"
    )
    config.addinivalue_line(
        "markers", "staged_regression: staged communication/v9-shadow regression suite"
    )
    config.addinivalue_line("markers", "governance_contracts: governance summary contract suite")
    config.addinivalue_line(
        "markers",
        "slow: integration/E2E tests that perform file I/O, MT5 simulation, or state loading",
    )

    if config.getoption("--governance-contracts"):
        config.args[:] = GOVERNANCE_CONTRACTS
        return
    if config.getoption("--fast-communication-v9-shadow-contracts"):
        config.args[:] = FAST_COMMUNICATION_V9_SHADOW_CONTRACTS
        return
    if config.getoption("--communication-v9-shadow-regression"):
        config.args[:] = COMMUNICATION_V9_SHADOW_REGRESSION


def pytest_collection_modifyitems(config, items):
    fast_paths = set(FAST_COMMUNICATION_V9_SHADOW_CONTRACTS)
    staged_paths = set(COMMUNICATION_V9_SHADOW_REGRESSION)
    governance_paths = set(GOVERNANCE_CONTRACTS)

    for item in items:
        normalized_path = item.nodeid.split("::", 1)[0].replace("\\", "/")
        if normalized_path in fast_paths:
            item.add_marker(pytest.mark.fast_contracts)
        if normalized_path in staged_paths:
            item.add_marker(pytest.mark.staged_regression)
        if normalized_path in governance_paths:
            item.add_marker(pytest.mark.governance_contracts)
        # Auto-mark engine tests as slow, except explicitly fast suites
        # (fast_contracts, governance_contracts, contract guard, preflight)
        if (
            normalized_path.startswith("tests/engine/")
            and normalized_path not in ENGINE_SLOW_EXEMPTIONS
        ):
            item.add_marker(pytest.mark.slow)
