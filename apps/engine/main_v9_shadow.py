import json
import sys
from argparse import ArgumentParser
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.engine.bootstrap_v9 import build_v9_shadow_runtime_loop  # noqa: E402
from apps.engine.communication_summary_contract import (  # noqa: E402
    build_summary_mirror_fields_from_operations_summary,
)
from apps.engine.v9_shadow_sse import (  # noqa: E402
    render_sse_message,
)
from apps.engine.v9_shadow_sse import (  # noqa: E402
    run_shadow_session_sse_server as run_shadow_session_sse_server_impl,
)
from apps.engine.v9_shadow_sse import (  # noqa: E402
    stream_session_sse as stream_session_sse_impl,
)
from core.contracts.domain_keys import (  # noqa: E402
    PAYLOAD_KEY_ACTION,
    PAYLOAD_KEY_BRAIN_COUNT,
    PAYLOAD_KEY_COMMUNICATION_LEDGER_PATH,
    PAYLOAD_KEY_COMMUNICATION_RECORD_ID,
    PAYLOAD_KEY_CONVICTION,
    PAYLOAD_KEY_DISPATCH_STATUS,
    PAYLOAD_KEY_FEATURE_FILE,
    PAYLOAD_KEY_FEATURE_SOURCE_TYPE,
    PAYLOAD_KEY_GOVERNANCE_SOURCES,
    PAYLOAD_KEY_LEDGER_PATH,
    PAYLOAD_KEY_MESSAGE_ID,
    PAYLOAD_KEY_MODE,
    PAYLOAD_KEY_OPERATIONS_POSTURE,
    PAYLOAD_KEY_OPERATIONS_SUMMARY,
    PAYLOAD_KEY_POSTURE_SOURCES,
    PAYLOAD_KEY_RECORD_ID,
    PAYLOAD_KEY_RISK_STATUS,
    PAYLOAD_KEY_SAMPLE_DESCRIPTION,
    PAYLOAD_KEY_SCENARIO,
    PAYLOAD_KEY_SIDE,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_SYMBOL,
)
from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES  # noqa: E402


class FeatureInputError(ValueError):
    pass


@dataclass(frozen=True)
class OutputPlan:
    mode: str
    output_path: str | None
    render_strategy: str | None = None
    include_meta: bool = False
    include_compact_stats: bool = False
    include_json_stats: bool = False
    summary_style: str = "compact"
    inferred_from_suffix: bool = False
    naming_policy: str = "explicit"


@dataclass(frozen=True)
class StreamEnvelopePlan:
    include_meta: bool = False
    include_stats: bool = False


@dataclass(frozen=True)
class SessionStreamPlan:
    include_meta: bool = True
    include_stats: bool = False
    event_name_prefix: str = "session"


def build_stub_feature_source() -> dict:
    feature_source = {}
    base_values = {
        "M5_Ret_1": 0.0012,
        "M5_Body_Ratio": 0.42,
        "M5_ATR_14": 2.1,
        "M5_RSI_14": 57.0,
        "M5_MACD": 0.15,
        "M5_Vol_ZScore": 0.35,
        "M5_Macro1_Corr": 0.28,
        "M5_Price_ZScore": 4.41,
        "M15_Ret_1": 0.0016,
        "M15_Body_Ratio": 0.45,
        "M15_ATR_14": 4.0,
        "M15_RSI_14": 58.0,
        "M15_MACD": 0.20,
        "M15_Vol_ZScore": 0.30,
        "M15_Macro1_Corr": 0.29,
        "M15_Price_ZScore": 4.41,
        "M30_Ret_1": 0.0019,
        "M30_Body_Ratio": 0.47,
        "M30_ATR_14": 6.0,
        "M30_RSI_14": 59.0,
        "M30_MACD": 0.25,
        "M30_Vol_ZScore": 0.25,
        "M30_Macro1_Corr": 0.30,
        "M30_Price_ZScore": 4.41,
        "H1_Ret_1": 0.0024,
        "H1_Body_Ratio": 0.48,
        "H1_ATR_14": 8.1,
        "H1_RSI_14": 60.5,
        "H1_MACD": 0.32,
        "H1_Vol_ZScore": 0.20,
        "H1_Macro1_Corr": 0.31,
        "H1_Price_ZScore": 4.41,
        "M5_OU_Theta": 58.0,
        "M15_OU_Theta": 21.0,
        "M30_OU_Theta": 10.0,
        "H1_OU_Theta": 4.7,
        "M5_Hurst": 0.31,
        "M15_Hurst": 0.26,
        "M30_Hurst": 0.23,
        "H1_Hurst": 0.21,
    }

    for name in V9_INSTITUTIONAL_40_FEATURES:
        feature_source[name] = base_values.get(name, 0.0)
    return feature_source


def load_feature_source_from_json(path: str) -> dict:
    file_path = Path(path)
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FeatureInputError(f"Invalid feature JSON in {file_path}: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise FeatureInputError(f"Feature file must contain a JSON object: {file_path}")
    features = payload.get("features", payload)
    if not isinstance(features, dict):
        raise FeatureInputError(f"Feature payload 'features' must be a JSON object: {file_path}")
    feature_source = {}
    for name in V9_INSTITUTIONAL_40_FEATURES:
        feature_source[name] = float(features.get(name, 0.0))
    return feature_source


def load_feature_batch_from_json(path: str) -> list[dict]:
    file_path = Path(path)
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FeatureInputError(f"Invalid feature batch JSON in {file_path}: {exc.msg}") from exc
    if not isinstance(payload, list):
        raise FeatureInputError(f"Feature batch file must contain a JSON array: {file_path}")
    batch = []
    for item in payload:
        if not isinstance(item, dict):
            raise FeatureInputError(f"Each feature batch item must be a JSON object: {file_path}")
        name = str(item["name"])
        description = str(item.get("description", ""))
        features = item.get("features", {})
        if not isinstance(features, dict):
            raise FeatureInputError(
                f"Feature batch item 'features' must be a JSON object: {file_path}"
            )
        feature_source = {}
        for feature_name in V9_INSTITUTIONAL_40_FEATURES:
            feature_source[feature_name] = float(features.get(feature_name, 0.0))
        batch.append(
            {
                "name": name,
                "description": description,
                "feature_source": feature_source,
            }
        )
    return batch


def load_feature_samples_from_dir(path: str) -> list[dict]:
    directory = Path(path)
    samples = []
    for file_path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise FeatureInputError(f"Invalid feature JSON in {file_path}: {exc.msg}") from exc
        if isinstance(payload, list):
            continue
        if not isinstance(payload, dict):
            continue
        features = payload.get("features", payload)
        if not isinstance(features, dict):
            raise FeatureInputError(
                f"Feature payload 'features' must be a JSON object: {file_path}"
            )
        feature_source = {}
        for feature_name in V9_INSTITUTIONAL_40_FEATURES:
            feature_source[feature_name] = float(features.get(feature_name, 0.0))
        samples.append(
            {
                "name": str(payload.get("name", file_path.stem)),
                "description": str(
                    payload.get("description", DIR_SAMPLE_DESCRIPTIONS.get(file_path.stem, ""))
                ),
                "feature_file": str(file_path),
                "feature_source": feature_source,
            }
        )
    return samples


def build_long_actionable_stub_feature_source() -> dict:
    feature_source = build_stub_feature_source()
    feature_source["H1_Hurst"] = -1.1
    return feature_source


def build_short_actionable_stub_feature_source() -> dict:
    feature_source = build_stub_feature_source()
    for name in V9_INSTITUTIONAL_40_FEATURES:
        if name.startswith("M15_"):
            feature_source[name] = feature_source[name] * -2.5
    return feature_source


def build_edge_allow_stub_feature_source() -> dict:
    feature_source = build_stub_feature_source()
    feature_source["H1_Hurst"] = -0.85
    return feature_source


def build_edge_deny_stub_feature_source() -> dict:
    feature_source = build_stub_feature_source()
    feature_source["H1_Hurst"] = -0.45
    return feature_source


SCENARIO_REGISTRY = {
    "neutral": {
        "builder": build_stub_feature_source,
        "description": "Default reference sample expected to stay passive.",
    },
    "edge_deny": {
        "builder": build_edge_deny_stub_feature_source,
        "description": "Borderline long setup expected to remain risk-blocked.",
    },
    "edge_allow": {
        "builder": build_edge_allow_stub_feature_source,
        "description": "Borderline long setup expected to become risk-allowed.",
    },
    "long": {
        "builder": build_long_actionable_stub_feature_source,
        "description": "Lower H1_Hurst to trigger an open long decision.",
    },
    "short": {
        "builder": build_short_actionable_stub_feature_source,
        "description": "Invert M15 feature group to trigger an open short decision.",
    },
}

DIR_SAMPLE_DESCRIPTIONS = {
    "v9_shadow_neutral": "Default reference sample expected to remain passive and abstain.",
    "v9_shadow_edge_deny": "Borderline long setup expected to remain risk-blocked.",
    "v9_shadow_edge_allow": "Borderline long setup expected to become risk-allowed.",
    "v9_shadow_long": "Lower H1_Hurst to push the model into an open long decision.",
    "v9_shadow_short": "Invert the M15 feature group to trigger an open short decision.",
}


@dataclass(frozen=True)
class BaselineSuiteSpec:
    key: str
    batch_file: str
    baseline_dir: str


@dataclass(frozen=True)
class FormalBaselineManifest:
    path: str
    version: str
    description: str | None
    suites: list[BaselineSuiteSpec]


FORMAL_BASELINE_MANIFEST_PATH = "D:/cursor/data/replays/v9_shadow_baselines/manifest.json"


SCENARIO_ALIAS_REGISTRY = {
    "neutral_case": "neutral",
    "edge_deny_case": "edge_deny",
    "edge_allow_case": "edge_allow",
    "long_case": "long",
    "short_case": "short",
}


def resolve_batch_scenario_name(name: str) -> str:
    return SCENARIO_ALIAS_REGISTRY.get(name, name.removesuffix("_case"))


def load_formal_baseline_manifest(
    manifest_path: str = FORMAL_BASELINE_MANIFEST_PATH,
) -> FormalBaselineManifest:
    file_path = Path(manifest_path)
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FeatureInputError(
            f"Invalid formal baseline manifest JSON in {file_path}: {exc.msg}"
        ) from exc
    if not isinstance(payload, dict):
        raise FeatureInputError(f"Formal baseline manifest must contain a JSON object: {file_path}")
    version = payload.get("version")
    if version is None:
        raise FeatureInputError(
            f"Formal baseline manifest is missing required field 'version': {file_path}"
        )
    if not isinstance(version, str):
        raise FeatureInputError(
            f"Formal baseline manifest field 'version' must be a string: {file_path}"
        )
    description = payload.get("description")
    if description is not None and not isinstance(description, str):
        raise FeatureInputError(
            f"Formal baseline manifest field 'description' must be a string: {file_path}"
        )
    suites_payload = payload.get("suites")
    if not isinstance(suites_payload, list):
        raise FeatureInputError(
            f"Formal baseline manifest 'suites' must be a JSON array: {file_path}"
        )
    suites = []
    required_fields = ["key", "batch_file", "baseline_dir"]
    for item in suites_payload:
        if not isinstance(item, dict):
            raise FeatureInputError(
                f"Each formal baseline suite must be a JSON object: {file_path}"
            )
        for field_name in required_fields:
            if field_name not in item:
                raise FeatureInputError(
                    f"Formal baseline suite is missing required field '{field_name}': {file_path}"
                )
        suites.append(
            BaselineSuiteSpec(
                key=str(item["key"]),
                batch_file=str(item["batch_file"]),
                baseline_dir=str(item["baseline_dir"]),
            )
        )
    return FormalBaselineManifest(
        path=str(file_path),
        version=version,
        description=description,
        suites=suites,
    )


def load_formal_baseline_suites(
    manifest_path: str = FORMAL_BASELINE_MANIFEST_PATH,
) -> list[BaselineSuiteSpec]:
    return load_formal_baseline_manifest(manifest_path).suites


def run_scenario(scenario: str, feature_source: dict | None = None):
    runtime_loop = build_v9_shadow_runtime_loop()
    builder = SCENARIO_REGISTRY[scenario]["builder"]
    resolved_feature_source = feature_source or builder()  # type: ignore[operator]
    return runtime_loop.run_decision_cycle(
        trigger={"symbol": "XAUUSD"},
        feature_source=resolved_feature_source,
    )


def normalize_dispatch_status(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def build_summary_payload(
    scenario: str,
    result,
    feature_source_type: str = "scenario",
    feature_file: str | None = None,
    sample_description: str | None = None,
    manifest: dict | None = None,
) -> dict:
    mode = (
        result.verdict.mode.value if hasattr(result.verdict.mode, "value") else result.verdict.mode
    )
    # operations_summary is the stable consumer-facing communication snapshot.
    # Other communication fields remain available as raw context or compatibility mirrors.
    operations_summary = None
    if getattr(result, "communication_operations", None) is not None:
        operations_summary = result.communication_operations.get(PAYLOAD_KEY_OPERATIONS_SUMMARY)
    elif getattr(result, "communication_record", None) is not None:
        operations_summary = {
            PAYLOAD_KEY_DISPATCH_STATUS: getattr(result.dispatch_result, "status", None),
            PAYLOAD_KEY_MESSAGE_ID: getattr(result.communication_record, "message_id", None),
            PAYLOAD_KEY_COMMUNICATION_RECORD_ID: getattr(
                result.communication_record, "record_id", None
            ),
            PAYLOAD_KEY_COMMUNICATION_LEDGER_PATH: None
            if result.communication_ledger_path is None
            else str(result.communication_ledger_path),
        }
    return {
        PAYLOAD_KEY_SCENARIO: scenario,
        PAYLOAD_KEY_FEATURE_SOURCE_TYPE: feature_source_type,
        PAYLOAD_KEY_FEATURE_FILE: feature_file,
        PAYLOAD_KEY_SAMPLE_DESCRIPTION: sample_description,
        "manifest": manifest,
        PAYLOAD_KEY_SYMBOL: result.intent.symbol,
        PAYLOAD_KEY_MODE: mode,
        PAYLOAD_KEY_ACTION: result.intent.action.value,
        PAYLOAD_KEY_SIDE: result.intent.side.value,
        PAYLOAD_KEY_CONVICTION: round(result.intent.conviction, 6),
        PAYLOAD_KEY_RISK_STATUS: result.verdict.status.value,
        PAYLOAD_KEY_DISPATCH_STATUS: normalize_dispatch_status(
            result.dispatch_result[PAYLOAD_KEY_STATUS]
        ),
        PAYLOAD_KEY_OPERATIONS_SUMMARY: operations_summary,
        PAYLOAD_KEY_BRAIN_COUNT: len(result.proposals),
        PAYLOAD_KEY_LEDGER_PATH: str(result.ledger_path),
        PAYLOAD_KEY_RECORD_ID: result.record.record_id,
    }


def infer_output_format(output_path: str | None) -> str | None:
    if not output_path:
        return None
    suffix = Path(output_path).suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix == ".csv":
        return "csv"
    if suffix == ".summary":
        return "summary"
    if suffix == ".stats":
        return "stats"
    return None


def build_stats_payload(results: list[dict]) -> dict:
    action_counts = Counter(payload["action"] for payload in results)
    side_counts = Counter(payload["side"] for payload in results)
    risk_counts = Counter(payload["risk_status"] for payload in results)
    dispatch_counts = Counter(
        normalize_dispatch_status(payload["dispatch_status"]) for payload in results
    )
    side_action_counts = Counter((payload["side"], payload["action"]) for payload in results)
    risk_dispatch_counts = Counter(
        (payload["risk_status"], normalize_dispatch_status(payload["dispatch_status"]))
        for payload in results
    )
    convictions = [float(payload["conviction"]) for payload in results]
    scenario_groups: dict[str, Any] = {}
    for payload in results:
        scenario_name = payload["scenario"]
        group = scenario_groups.setdefault(
            scenario_name,
            {
                "total": 0,
                "actions": Counter(),
                "sides": Counter(),
                "risk_statuses": Counter(),
                "dispatch_statuses": Counter(),
                "side_actions": Counter(),
                "risk_dispatches": Counter(),
                "convictions": [],
            },
        )
        group["total"] += 1
        group["actions"][payload["action"]] += 1
        group["sides"][payload["side"]] += 1
        group["risk_statuses"][payload["risk_status"]] += 1
        group["dispatch_statuses"][normalize_dispatch_status(payload["dispatch_status"])] += 1
        group["side_actions"][(payload["side"], payload["action"])] += 1
        group["risk_dispatches"][
            (payload["risk_status"], normalize_dispatch_status(payload["dispatch_status"]))
        ] += 1
        group["convictions"].append(float(payload["conviction"]))

    by_scenario = {}
    for scenario_name, group in sorted(scenario_groups.items()):
        group_convictions = group.pop("convictions")
        side_actions = {
            f"{side}.{action}": value
            for (side, action), value in sorted(group.pop("side_actions").items())
        }
        risk_dispatches = {
            f"{risk_status}.{dispatch_status}": value
            for (risk_status, dispatch_status), value in sorted(
                group.pop("risk_dispatches").items()
            )
        }
        by_scenario[scenario_name] = {
            "total": group["total"],
            "actions": dict(sorted(group["actions"].items())),
            "sides": dict(sorted(group["sides"].items())),
            "risk_statuses": dict(sorted(group["risk_statuses"].items())),
            "dispatch_statuses": dict(sorted(group["dispatch_statuses"].items())),
            "side_actions": side_actions,
            "risk_dispatches": risk_dispatches,
            "conviction": {
                "avg": round(sum(group_convictions) / len(group_convictions), 6),
                "max": round(max(group_convictions), 6),
                "min": round(min(group_convictions), 6),
            },
        }

    return {
        "total": len(results),
        "actions": dict(sorted(action_counts.items())),
        "sides": dict(sorted(side_counts.items())),
        "risk_statuses": dict(sorted(risk_counts.items())),
        "dispatch_statuses": dict(sorted(dispatch_counts.items())),
        "side_actions": {
            f"{side}.{action}": value
            for (side, action), value in sorted(side_action_counts.items())
        },
        "risk_dispatches": {
            f"{risk_status}.{dispatch_status}": value
            for (risk_status, dispatch_status), value in sorted(risk_dispatch_counts.items())
        },
        "conviction": {
            "avg": round(sum(convictions) / len(convictions), 6),
            "max": round(max(convictions), 6),
            "min": round(min(convictions), 6),
        },
        "by_scenario": by_scenario,
    }


def render_plain_stats_text(stats: dict) -> str:
    lines = [f"total={stats['total']}"]
    for metric_name, value in stats["conviction"].items():
        lines.append(f"conviction.{metric_name}={value}")
    for key in [
        "actions",
        "sides",
        "risk_statuses",
        "dispatch_statuses",
        "side_actions",
        "risk_dispatches",
    ]:
        for item_key, value in stats[key].items():
            lines.append(f"{key}.{item_key}={value}")
    for scenario_name, scenario_stats in stats["by_scenario"].items():
        lines.append(f"by_scenario.{scenario_name}.total={scenario_stats['total']}")
        for metric_name, value in scenario_stats["conviction"].items():
            lines.append(f"by_scenario.{scenario_name}.conviction.{metric_name}={value}")
        for key in [
            "actions",
            "sides",
            "risk_statuses",
            "dispatch_statuses",
            "side_actions",
            "risk_dispatches",
        ]:
            for item_key, value in scenario_stats[key].items():
                lines.append(f"by_scenario.{scenario_name}.{key}.{item_key}={value}")
    return "\n".join(lines)


def render_compact_stats_text(stats: dict) -> str:
    return " | ".join(
        [
            f"total={stats['total']}",
            f"conviction.avg={stats['conviction']['avg']}",
            f"actions={stats['actions']}",
            f"sides={stats['sides']}",
            f"risk_statuses={stats['risk_statuses']}",
            f"dispatch_statuses={stats['dispatch_statuses']}",
            f"side_actions={stats['side_actions']}",
            f"risk_dispatches={stats['risk_dispatches']}",
        ]
    )


def render_json_stats(stats: dict) -> str:
    return json.dumps(stats)


def render_stats_output(stats: dict, formatter: str = "plain"):
    if formatter == "compact":
        return render_compact_stats_text(stats)
    if formatter == "json":
        return render_json_stats(stats)
    return render_plain_stats_text(stats)


def render_summary_output(
    payloads: list[dict],
    style: str = "compact",
    include_compact_stats: bool = True,
) -> str:
    summary_body = "\n\n".join(render_summary_text(payload) for payload in payloads)
    if not include_compact_stats:
        return summary_body
    stats_block = render_stats_output(build_stats_payload(payloads), formatter="compact")
    if style == "full":
        return f"{summary_body}\n\n--- compact_stats ---\n{stats_block}"
    return f"{summary_body}\n\n--- compact ---\n{stats_block}"


def render_stats_text(stats: dict) -> str:
    return render_stats_output(stats, formatter="plain")


def render_summary_text(payload: dict) -> str:
    return "\n".join(f"{key}={value}" for key, value in payload.items())


def print_summary(
    scenario: str,
    result,
    feature_source_type: str = "scenario",
    feature_file: str | None = None,
    sample_description: str | None = None,
) -> None:
    payload = build_summary_payload(
        scenario,
        result,
        feature_source_type=feature_source_type,
        feature_file=feature_file,
        sample_description=sample_description,
    )
    print(render_summary_text(payload))


def build_csv_row(payload: dict) -> str:
    columns = [
        "scenario",
        "feature_source_type",
        "feature_file",
        "sample_description",
        "symbol",
        "mode",
        "action",
        "side",
        "conviction",
        "risk_status",
        "dispatch_status",
        "brain_count",
        "ledger_path",
        "record_id",
    ]
    return ",".join(json.dumps(payload.get(column, ""), ensure_ascii=False) for column in columns)


def render_csv(results: list[dict]) -> str:
    columns = [
        "scenario",
        "feature_source_type",
        "feature_file",
        "sample_description",
        "symbol",
        "mode",
        "action",
        "side",
        "conviction",
        "risk_status",
        "dispatch_status",
        "brain_count",
        "ledger_path",
        "record_id",
    ]
    rows = [",".join(columns)]
    rows.extend(build_csv_row(payload) for payload in results)
    return "\n".join(rows)


def print_csv(results: list[dict]) -> None:
    print(render_csv(results))


def write_output(path: str, content: str) -> None:
    file_path = Path(path)
    if file_path.parent != Path(""):
        file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content + ("" if content.endswith("\n") else "\n"), encoding="utf-8")


def render_result_text(title: str, result) -> str:
    lines = [
        title,
        f"Intent: {result.intent}",
        f"Verdict: {result.verdict}",
        f"Dispatch: {result.dispatch_result}",
        f"Record ID: {result.record.record_id}",
        f"Ledger Path: {result.ledger_path}",
        f"Brain Count: {len(result.proposals)}",
    ]
    return "\n".join(lines)


def print_result(title: str, result) -> None:
    print(render_result_text(title, result))


def emit_output(content: str, output_path: str | None = None) -> None:
    if output_path:
        write_output(output_path, content)
        return
    print(content)


def render_regression_baseline(payloads: list[dict]) -> dict:
    preferred_order = {
        "neutral_case": 0,
        "long_case": 1,
        "short_case": 2,
        "neutral": 0,
        "long": 1,
        "short": 2,
    }
    ordered_payloads = sorted(
        payloads,
        key=lambda item: (preferred_order.get(item["scenario"], 999), item["scenario"]),
    )
    normalized_payloads = [
        {key: value for key, value in item.items() if key not in {"record_id", "ledger_path"}}
        for item in ordered_payloads
    ]
    return {
        "results": normalized_payloads,
        "stats": build_stats_payload(normalized_payloads),
    }


def summarize_result_differences(expected: dict | None, actual: dict | None) -> list[dict]:
    if expected == actual:
        return []
    if expected is None or actual is None:
        return [{"field": "__missing__", "expected": expected, "actual": actual}]
    differences = []
    keys = sorted(set(expected) | set(actual))
    for key in keys:
        if expected.get(key) != actual.get(key):
            differences.append(
                {
                    "field": key,
                    "expected": expected.get(key),
                    "actual": actual.get(key),
                }
            )
    return differences


def summarize_stats_differences(expected: dict | None, actual: dict | None) -> list[dict]:
    if expected == actual:
        return []
    differences = []
    keys = sorted(set(expected or {}) | set(actual or {}))
    for key in keys:
        expected_value = (expected or {}).get(key)
        actual_value = (actual or {}).get(key)
        if expected_value != actual_value:
            differences.append(
                {
                    "field": key,
                    "expected": expected_value,
                    "actual": actual_value,
                }
            )
    return differences


def diff_regression_baseline(expected: dict, actual: dict) -> dict:
    changes = []
    expected_results = expected.get("results", [])
    actual_results = actual.get("results", [])
    if expected_results != actual_results:
        expected_by_scenario = {
            item.get("scenario"): item for item in expected_results if isinstance(item, dict)
        }
        actual_by_scenario = {
            item.get("scenario"): item for item in actual_results if isinstance(item, dict)
        }
        ordered_scenarios = sorted(
            set(expected_by_scenario.keys()) | set(actual_by_scenario.keys()) - {None}
        )  # type: ignore[type-var]
        if len(expected_by_scenario) == len(expected_results) and len(actual_by_scenario) == len(
            actual_results
        ):
            for scenario_name in ordered_scenarios:
                expected_item = expected_by_scenario.get(scenario_name)
                actual_item = actual_by_scenario.get(scenario_name)
                if expected_item != actual_item:
                    changes.append(
                        {
                            "section": "results",
                            "scenario": scenario_name,
                            "expected": expected_item,
                            "actual": actual_item,
                            "fields": summarize_result_differences(expected_item, actual_item),
                        }
                    )
        else:
            max_len = max(len(expected_results), len(actual_results))
            for index in range(max_len):
                expected_item = expected_results[index] if index < len(expected_results) else None
                actual_item = actual_results[index] if index < len(actual_results) else None
                if expected_item != actual_item:
                    changes.append(
                        {
                            "section": "results",
                            "index": index,
                            "expected": expected_item,
                            "actual": actual_item,
                            "fields": summarize_result_differences(expected_item, actual_item),
                        }
                    )
    expected_stats = expected.get("stats")
    actual_stats = actual.get("stats")
    if expected_stats != actual_stats:
        changes.append(
            {
                "section": "stats",
                "expected": expected_stats,
                "actual": actual_stats,
                "fields": summarize_stats_differences(expected_stats, actual_stats),
            }
        )
    return {
        "matches": not changes,
        "change_count": len(changes),
        "changes": changes,
    }


def render_regression_diff_text(diff: dict) -> str:
    if diff["matches"]:
        return "regression.matches=true\nchange_count=0"
    lines = ["regression.matches=false", f"change_count={diff['change_count']}"]
    for index, change in enumerate(diff["changes"]):
        lines.append(f"changes[{index}].section={change['section']}")
        if "index" in change:
            lines.append(f"changes[{index}].index={change['index']}")
        if "scenario" in change:
            lines.append(f"changes[{index}].scenario={change['scenario']}")
        for field_index, field_change in enumerate(change.get("fields", [])):
            lines.append(
                f"changes[{index}].fields[{field_index}].field" f"={field_change['field']}"
            )
            expected_json = json.dumps(field_change["expected"], ensure_ascii=False, sort_keys=True)
            lines.append(f"changes[{index}].fields[{field_index}].expected={expected_json}")
            actual_json = json.dumps(field_change["actual"], ensure_ascii=False, sort_keys=True)
            lines.append(f"changes[{index}].fields[{field_index}].actual={actual_json}")
        expected_json = json.dumps(change["expected"], ensure_ascii=False, sort_keys=True)
        lines.append(f"changes[{index}].expected={expected_json}")
        actual_json = json.dumps(change["actual"], ensure_ascii=False, sort_keys=True)
        lines.append(f"changes[{index}].actual={actual_json}")
    return "\n".join(lines)


def render_batch_regression_diff_text(batch_diff: dict) -> str:
    if batch_diff["matches"]:
        return "batch_regression.matches=true\nmissing_count=0\ndiff_count=0"
    lines = [
        "batch_regression.matches=false",
        f"missing_count={len(batch_diff['missing'])}",
        f"diff_count={len(batch_diff['diffs'])}",
    ]
    for index, path in enumerate(batch_diff["missing"]):
        lines.append(f"missing[{index}]={path}")
    for index, item in enumerate(batch_diff["diffs"]):
        lines.append(f"diffs[{index}].name={item['name']}")
        lines.append(f"diffs[{index}].path={item['path']}")
        nested_text = render_regression_diff_text(item["diff"])
        for line in nested_text.splitlines():
            lines.append(f"diffs[{index}].{line}")
    return "\n".join(lines)


def build_formal_baseline_gate_summary(result: dict) -> dict:
    failing_samples = []
    failing_fields = []
    for suite in result["suite_results"]:
        diff = suite["diff"]
        for path in diff["missing"]:
            sample_name = Path(path).name.removesuffix(".baseline.json")
            failing_samples.append(
                {
                    "suite": suite["key"],
                    "sample": sample_name,
                    "reason": "missing_baseline",
                    "path": path,
                }
            )
        for item in diff["diffs"]:
            sample_name = item["name"]
            failing_samples.append(
                {
                    "suite": suite["key"],
                    "sample": sample_name,
                    "reason": "diff",
                    "path": item["path"],
                }
            )
            for change in item["diff"].get("changes", []):
                change_target = change.get("scenario", change.get("index", "unknown"))
                for field_change in change.get("fields", []):
                    failing_fields.append(
                        {
                            "suite": suite["key"],
                            "sample": sample_name,
                            "section": change["section"],
                            "target": change_target,
                            "field": field_change["field"],
                            "expected": field_change["expected"],
                            "actual": field_change["actual"],
                        }
                    )
    return {
        "suite_failure_count": len(result["summary"]["failed_suites"]),
        "sample_failure_count": len(failing_samples),
        "field_change_count": len(failing_fields),
        "failing_samples": failing_samples,
        "failing_fields": failing_fields,
    }


def build_formal_suite_semantic_rules() -> dict[str, dict[str, dict]]:
    return {
        "neutral_stability": {
            "neutral_case": {
                "action": "abstain",
                "side": "flat",
                "risk_status": "deny",
                "dispatch_status": "skipped",
            },
        },
        "actionable_decisions": {
            "long_case": {
                "action": "abstain",
                "side": "flat",
                "risk_status": "deny",
                "dispatch_status": "skipped",
            },
            "short_case": {
                "action": "open",
                "side": "short",
                "risk_status": "allow",
                "dispatch_status": "protocol_validated",
            },
        },
        "risk_boundary": {
            "edge_deny_case": {
                "action": "abstain",
                "side": "flat",
                "risk_status": "deny",
                "dispatch_status": "skipped",
            },
            "edge_allow_case": {
                "action": "abstain",
                "side": "flat",
                "risk_status": "deny",
                "dispatch_status": "skipped",
            },
        },
    }


def build_formal_suite_semantic_summary(suite_results: list[dict]) -> dict:
    rules = build_formal_suite_semantic_rules()
    failures = []
    for suite_result in suite_results:
        suite_key = suite_result["key"]
        expected_by_sample = rules.get(suite_key, {})
        for payload in suite_result["payloads"]:
            sample_name = payload["scenario"]
            expected_fields = expected_by_sample.get(sample_name, {})
            for field_name, expected_value in expected_fields.items():
                actual_value = payload.get(field_name)
                if actual_value != expected_value:
                    failures.append(
                        {
                            "suite": suite_key,
                            "sample": sample_name,
                            "field": field_name,
                            "expected": expected_value,
                            "actual": actual_value,
                        }
                    )
    return {
        "matches": not failures,
        "failure_count": len(failures),
        "failures": failures,
    }


def render_formal_suite_semantic_text(result: dict) -> str:
    semantic = result["semantic"]
    lines = [
        f"formal_semantics.matches={'true' if semantic['matches'] else 'false'}",
        f"formal_semantics.failure_count={semantic['failure_count']}",
    ]
    for index, item in enumerate(semantic["failures"]):
        lines.append(f"formal_semantics.failures[{index}].suite={item['suite']}")
        lines.append(f"formal_semantics.failures[{index}].sample={item['sample']}")
        lines.append(f"formal_semantics.failures[{index}].field={item['field']}")
        expected_json = json.dumps(item["expected"], ensure_ascii=False, sort_keys=True)
        lines.append(f"formal_semantics.failures[{index}].expected={expected_json}")
        actual_json = json.dumps(item["actual"], ensure_ascii=False, sort_keys=True)
        lines.append(f"formal_semantics.failures[{index}].actual={actual_json}")
    return "\n".join(lines)


def build_formal_manifest_meta(manifest: FormalBaselineManifest) -> dict:
    return {
        "path": manifest.path,
        "version": manifest.version,
        "description": manifest.description,
    }


def strip_manifest_from_regression_baseline(payload: dict) -> dict:
    stripped = dict(payload)
    stripped.pop("manifest", None)
    return stripped


def build_formal_semantic_payloads(batch: list[dict], manifest_meta: dict | None) -> list[dict]:
    return [
        build_summary_payload(
            item["name"],
            run_scenario(resolve_batch_scenario_name(item["name"])),
            feature_source_type="batch_file",
            feature_file=None,
            sample_description=item["description"],
            manifest=manifest_meta,
        )
        for item in batch
    ]


def check_formal_baseline_suites(manifest_path: str = FORMAL_BASELINE_MANIFEST_PATH) -> dict:
    manifest = load_formal_baseline_manifest(manifest_path)
    manifest_meta = build_formal_manifest_meta(manifest)
    suite_results: list[dict[str, Any]] = []
    for suite in manifest.suites:
        batch = load_feature_batch_from_json(suite.batch_file)
        semantic_payloads = build_formal_semantic_payloads(batch, manifest_meta)
        diff = check_batch_regression_baselines(suite.baseline_dir, batch, manifest=manifest_meta)
        suite_results.append(
            {
                "key": suite.key,
                "batch_file": suite.batch_file,
                "baseline_dir": suite.baseline_dir,
                "payloads": semantic_payloads,
                "diff": diff,
            }
        )
    failed_suites = [item["key"] for item in suite_results if not item["diff"]["matches"]]
    result: dict[str, Any] = {
        "matches": all(item["diff"]["matches"] for item in suite_results),
        "manifest": manifest_meta,
        "suite_results": suite_results,
        "summary": {
            "failed_suites": failed_suites,
            "total_missing": sum(len(item["diff"]["missing"]) for item in suite_results),
            "total_diffs": sum(len(item["diff"]["diffs"]) for item in suite_results),
        },
    }
    result["gate"] = build_formal_baseline_gate_summary(result)
    result["semantic"] = build_formal_suite_semantic_summary(suite_results)
    return result


def assert_formal_baseline_gate(manifest_path: str = FORMAL_BASELINE_MANIFEST_PATH) -> dict:
    result = check_formal_baseline_suites(manifest_path)
    if not result["matches"]:
        raise AssertionError(render_formal_baseline_suite_text(result))
    return result


def assert_formal_suite_semantics(manifest_path: str = FORMAL_BASELINE_MANIFEST_PATH) -> dict:
    result = check_formal_baseline_suites(manifest_path)
    if not result["semantic"]["matches"]:
        raise AssertionError(render_formal_suite_semantic_text(result))
    return result


def rebuild_formal_baseline_suites(manifest_path: str = FORMAL_BASELINE_MANIFEST_PATH) -> dict:
    manifest = load_formal_baseline_manifest(manifest_path)
    manifest_meta = build_formal_manifest_meta(manifest)
    suite_results = []
    written_paths = []
    for suite in manifest.suites:
        batch = load_feature_batch_from_json(suite.batch_file)
        suite_written_paths = write_batch_regression_baselines(
            suite.baseline_dir, batch, manifest=manifest_meta
        )
        suite_results.append(
            {
                "key": suite.key,
                "batch_file": suite.batch_file,
                "baseline_dir": suite.baseline_dir,
                "written_count": len(suite_written_paths),
                "written_paths": suite_written_paths,
            }
        )
        written_paths.extend(suite_written_paths)
    return {
        "manifest": manifest_meta,
        "suite_results": suite_results,
        "summary": {
            "suite_count": len(suite_results),
            "total_written": len(written_paths),
        },
        "written_paths": written_paths,
    }


def render_formal_baseline_suite_text(result: dict) -> str:
    lines = [
        f"formal_baselines.matches={'true' if result['matches'] else 'false'}",
        f"manifest.path={result['manifest']['path']}",
        f"manifest.version={result['manifest']['version']}",
        f"manifest.description={result['manifest']['description']}",
        f"suite_count={len(result['suite_results'])}",
        f"failed_suite_count={len(result['summary']['failed_suites'])}",
        f"failed_suites={json.dumps(result['summary']['failed_suites'], ensure_ascii=False)}",
        f"total_missing={result['summary']['total_missing']}",
        f"total_diffs={result['summary']['total_diffs']}",
        f"gate.sample_failure_count={result['gate']['sample_failure_count']}",
        f"gate.field_change_count={result['gate']['field_change_count']}",
        f"formal_semantics.matches={'true' if result['semantic']['matches'] else 'false'}",
        f"formal_semantics.failure_count={result['semantic']['failure_count']}",
    ]
    for index, suite in enumerate(result["suite_results"]):
        diff = suite["diff"]
        lines.append(f"suites[{index}].key={suite['key']}")
        lines.append(f"suites[{index}].batch_file={suite['batch_file']}")
        lines.append(f"suites[{index}].baseline_dir={suite['baseline_dir']}")
        lines.append(f"suites[{index}].matches={'true' if diff['matches'] else 'false'}")
        lines.append(f"suites[{index}].missing_count={len(diff['missing'])}")
        lines.append(f"suites[{index}].diff_count={len(diff['diffs'])}")
    for index, item in enumerate(result["gate"]["failing_samples"]):
        lines.append(f"gate.failing_samples[{index}].suite={item['suite']}")
        lines.append(f"gate.failing_samples[{index}].sample={item['sample']}")
        lines.append(f"gate.failing_samples[{index}].reason={item['reason']}")
        lines.append(f"gate.failing_samples[{index}].path={item['path']}")
    for index, item in enumerate(result["gate"]["failing_fields"]):
        lines.append(f"gate.failing_fields[{index}].suite={item['suite']}")
        lines.append(f"gate.failing_fields[{index}].sample={item['sample']}")
        lines.append(f"gate.failing_fields[{index}].section={item['section']}")
        lines.append(f"gate.failing_fields[{index}].target={item['target']}")
        lines.append(f"gate.failing_fields[{index}].field={item['field']}")
        expected_json = json.dumps(item["expected"], ensure_ascii=False, sort_keys=True)
        lines.append(f"gate.failing_fields[{index}].expected={expected_json}")
        actual_json = json.dumps(item["actual"], ensure_ascii=False, sort_keys=True)
        lines.append(f"gate.failing_fields[{index}].actual={actual_json}")
    for index, item in enumerate(result["semantic"]["failures"]):
        lines.append(f"formal_semantics.failures[{index}].suite={item['suite']}")
        lines.append(f"formal_semantics.failures[{index}].sample={item['sample']}")
        lines.append(f"formal_semantics.failures[{index}].field={item['field']}")
        expected_json = json.dumps(item["expected"], ensure_ascii=False, sort_keys=True)
        lines.append(f"formal_semantics.failures[{index}].expected={expected_json}")
        actual_json = json.dumps(item["actual"], ensure_ascii=False, sort_keys=True)
        lines.append(f"formal_semantics.failures[{index}].actual={actual_json}")
    return "\n".join(lines)


def render_formal_baseline_suite_json(result: dict) -> str:
    rendered = {
        "meta": {
            "output_mode": "json",
            "suite_count": len(result["suite_results"]),
            "manifest": result["manifest"],
        },
        "matches": result["matches"],
        "summary": result["summary"],
        "gate": result["gate"],
        "semantic": result["semantic"],
        "suites": [
            {
                "key": suite["key"],
                "batch_file": suite["batch_file"],
                "baseline_dir": suite["baseline_dir"],
                "matches": suite["diff"]["matches"],
                "missing_count": len(suite["diff"]["missing"]),
                "diff_count": len(suite["diff"]["diffs"]),
                "payloads": suite["payloads"],
                "diff": suite["diff"],
            }
            for suite in result["suite_results"]
        ],
    }
    return json.dumps(rendered)


def render_formal_baseline_rebuild_text(result: dict) -> str:
    lines = [
        "formal_baselines.rebuilt=true",
        f"manifest.path={result['manifest']['path']}",
        f"manifest.version={result['manifest']['version']}",
        f"manifest.description={result['manifest']['description']}",
        f"suite_count={result['summary']['suite_count']}",
        f"total_written={result['summary']['total_written']}",
    ]
    for index, suite in enumerate(result["suite_results"]):
        lines.append(f"suites[{index}].key={suite['key']}")
        lines.append(f"suites[{index}].batch_file={suite['batch_file']}")
        lines.append(f"suites[{index}].baseline_dir={suite['baseline_dir']}")
        lines.append(f"suites[{index}].written_count={suite['written_count']}")
    return "\n".join(lines)


def render_formal_baseline_rebuild_json(result: dict) -> str:
    rendered = {
        "meta": {
            "output_mode": "json",
            "suite_count": result["summary"]["suite_count"],
            "manifest": result["manifest"],
        },
        "rebuilt": True,
        "summary": result["summary"],
        "suites": [
            {
                "key": suite["key"],
                "batch_file": suite["batch_file"],
                "baseline_dir": suite["baseline_dir"],
                "written_count": suite["written_count"],
                "written_paths": suite["written_paths"],
            }
            for suite in result["suite_results"]
        ],
        "written_paths": result["written_paths"],
    }
    return json.dumps(rendered)


def write_regression_baseline(path: str, payloads: list[dict]) -> None:
    write_output(
        path,
        json.dumps(
            render_regression_baseline(
                [strip_manifest_from_regression_baseline(payload) for payload in payloads]
            ),
            ensure_ascii=False,
            indent=2,
        ),
    )


def write_batch_regression_baselines(
    output_dir: str, batch: list[dict], manifest: dict | None = None
) -> list[str]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    written_paths = []
    for item in batch:
        scenario_name = resolve_batch_scenario_name(item["name"])
        baseline_path = directory / f"{item['name']}.baseline.json"
        payload = build_summary_payload(
            item["name"],
            run_scenario(scenario_name, feature_source=item["feature_source"]),
            feature_source_type="batch_file",
            feature_file=None,
            sample_description=item["description"],
            manifest=manifest,
        )
        write_regression_baseline(str(baseline_path), [payload])
        written_paths.append(str(baseline_path))
    return written_paths


def check_batch_regression_baselines(
    baseline_dir: str, batch: list[dict], manifest: dict | None = None
) -> dict:
    directory = Path(baseline_dir)
    diffs = []
    missing = []
    for item in batch:
        scenario_name = resolve_batch_scenario_name(item["name"])
        baseline_path = directory / f"{item['name']}.baseline.json"
        if not baseline_path.exists():
            missing.append(str(baseline_path))
            continue
        payload = build_summary_payload(
            item["name"],
            run_scenario(scenario_name, feature_source=item["feature_source"]),
            feature_source_type="batch_file",
            feature_file=None,
            sample_description=item["description"],
            manifest=manifest,
        )
        diff = check_regression_baseline(
            str(baseline_path), [strip_manifest_from_regression_baseline(payload)]
        )
        if not diff["matches"]:
            diffs.append(
                {
                    "name": item["name"],
                    "path": str(baseline_path),
                    "diff": diff,
                }
            )
    return {
        "matches": not diffs and not missing,
        "missing": missing,
        "diffs": diffs,
    }


def check_regression_baseline(path: str, payloads: list[dict]) -> dict:
    expected = json.loads(Path(path).read_text(encoding="utf-8"))
    actual = render_regression_baseline(payloads)
    if expected == actual:
        return {"matches": True, "change_count": 0, "changes": []}
    return diff_regression_baseline(expected, actual)


def load_formal_manifest_meta_from_batch_file(batch_file: str | None) -> dict | None:
    if batch_file is None:
        return None
    try:
        manifest = load_formal_baseline_manifest()
    except FeatureInputError:
        return None
    normalized_batch_file = Path(batch_file).resolve(strict=False).as_posix().lower()
    for suite in manifest.suites:
        suite_batch_file = Path(suite.batch_file).resolve(strict=False).as_posix().lower()
        if suite_batch_file == normalized_batch_file:
            return build_formal_manifest_meta(manifest)
    return None


def build_stream_meta(
    payloads: list[dict], output_mode: str, source_type: str | None = None
) -> dict:
    manifests = [payload["manifest"] for payload in payloads if payload.get("manifest") is not None]
    manifest = (
        manifests[0] if manifests and all(item == manifests[0] for item in manifests) else None
    )
    resolved_source_type = source_type or derive_source_type(payloads)
    return {
        "output_mode": output_mode,
        "generated_at": datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds") + "Z",
        "source_type": resolved_source_type,
        "scenario_count": len({payload["scenario"] for payload in payloads}),
        "result_count": len(payloads),
        "manifest": manifest,
    }


def build_output_extension_fields(payload: dict, result) -> dict:
    communication_operations = getattr(result, "communication_operations", None)
    if communication_operations is None:
        return payload
    operations_summary = payload.get(PAYLOAD_KEY_OPERATIONS_SUMMARY)
    if operations_summary is None:
        operations_summary = communication_operations.get(PAYLOAD_KEY_OPERATIONS_SUMMARY)
    return {
        **payload,
        PAYLOAD_KEY_OPERATIONS_SUMMARY: operations_summary,
        PAYLOAD_KEY_OPERATIONS_POSTURE: communication_operations.get(
            PAYLOAD_KEY_OPERATIONS_POSTURE
        ),
        PAYLOAD_KEY_POSTURE_SOURCES: communication_operations.get(PAYLOAD_KEY_POSTURE_SOURCES),
        PAYLOAD_KEY_GOVERNANCE_SOURCES: communication_operations.get(
            PAYLOAD_KEY_GOVERNANCE_SOURCES
        ),
    }


def apply_stable_output_contract(payload: dict, result=None) -> dict:
    normalized_payload = (
        payload if result is None else build_output_extension_fields(payload, result)
    )
    return build_summary_mirror_fields_from_operations_summary(normalized_payload)


def extend_payloads_for_output(payloads: list[dict], results: list) -> list[dict]:
    if len(payloads) != len(results):
        return [apply_stable_output_contract(payload) for payload in payloads]
    return [
        apply_stable_output_contract(payload, result)
        for payload, result in zip(payloads, results, strict=False)
    ]


def render_json_output(
    payloads: list[dict],
    include_stats: bool = False,
    output_mode: str = "json",
    include_meta: bool = False,
    source_type: str | None = None,
) -> str:
    base_payload = payloads[0] if len(payloads) == 1 else payloads
    if not include_stats and not include_meta:
        return json.dumps(base_payload)

    rendered: dict[str, Any] = {}
    if include_meta:
        rendered["meta"] = build_stream_meta(
            payloads, output_mode=output_mode, source_type=source_type
        )
    rendered["results"] = base_payload
    if include_stats:
        rendered["stats"] = json.loads(
            render_stats_output(build_stats_payload(payloads), formatter="json")
        )
    return json.dumps(rendered)


def build_stream_envelope(
    payloads: list[dict],
    plan: StreamEnvelopePlan | None = None,
    output_mode: str = "sse",
) -> dict:
    resolved_plan = plan or StreamEnvelopePlan()
    envelope = {
        "event": "decision.batch.completed" if len(payloads) > 1 else "decision.completed",
        "results": payloads[0] if len(payloads) == 1 else payloads,
    }
    if resolved_plan.include_meta:
        envelope["meta"] = build_stream_meta(
            payloads,
            output_mode=output_mode,
        )
    if resolved_plan.include_stats:
        envelope["stats"] = build_stats_payload(payloads)
    return envelope


def build_session_event(
    session_id: str,
    event_type: str,
    data: dict,
) -> dict:
    return {
        "session_id": session_id,
        "event": event_type,
        "data": data,
    }


class ShadowSessionManager:
    def __init__(self, stream_plan: SessionStreamPlan | None = None):
        self._stream_plan = stream_plan or SessionStreamPlan()

    def stream_run(self, args):
        session_id = f"shadow_{datetime.now(UTC).replace(tzinfo=None).strftime('%Y%m%d%H%M%S')}"
        yield build_session_event(
            session_id,
            f"{self._stream_plan.event_name_prefix}.progress",
            {
                "stage": "started",
                "message": "shadow replay started",
            },
        )
        try:
            prepared = prepare_results(args)
            if len(prepared) == 3:
                payloads, _, results = prepared
            else:
                payloads, _ = prepared
                results = build_results_from_payloads(payloads)
            yield build_session_event(
                session_id,
                f"{self._stream_plan.event_name_prefix}.progress",
                {
                    "stage": "results_ready",
                    "message": "shadow replay results prepared",
                    "result_count": len(payloads),
                },
            )
            completed = build_stream_envelope(
                extend_payloads_for_output(payloads, results),
                plan=StreamEnvelopePlan(
                    include_meta=self._stream_plan.include_meta,
                    include_stats=self._stream_plan.include_stats,
                ),
                output_mode="session_stream",
            )
            completed["event"] = f"{self._stream_plan.event_name_prefix}.completed"
            yield build_session_event(session_id, completed["event"], completed)
        except Exception as exc:
            yield build_session_event(
                session_id,
                f"{self._stream_plan.event_name_prefix}.error",
                {
                    "message": str(exc),
                    "error_type": exc.__class__.__name__,
                },
            )


def render_sse_event(
    payloads: list[dict],
    plan: StreamEnvelopePlan | None = None,
    event_name: str | None = None,
) -> str:
    envelope = build_stream_envelope(payloads, plan=plan, output_mode="sse")
    resolved_event_name = event_name or envelope["event"]
    return render_sse_message(resolved_event_name, envelope)


def stream_session_sse(args, stream_plan: SessionStreamPlan | None = None):
    # Business-facing wrapper: binds the generic SSE stream helper
    # to this module's ShadowSessionManager implementation.
    yield from stream_session_sse_impl(ShadowSessionManager, args, stream_plan=stream_plan)


def render_output_content(plan: OutputPlan, payloads: list[dict], default_text: str) -> str:
    normalized_payloads = [apply_stable_output_contract(payload) for payload in payloads]
    if plan.mode == "json":
        return render_json_output(
            normalized_payloads,
            include_stats=plan.include_json_stats,
            output_mode=plan.mode,
            include_meta=(plan.include_meta or plan.include_json_stats),
            source_type=derive_source_type(normalized_payloads),
        )
    if plan.mode == "csv":
        return render_csv(normalized_payloads)
    if plan.mode == "summary":
        return render_summary_output(
            normalized_payloads,
            style=plan.summary_style,
            include_compact_stats=plan.include_compact_stats,
        )
    if plan.mode == "stats":
        return render_stats_output(
            build_stats_payload(normalized_payloads), formatter=plan.render_strategy or "plain"
        )
    return default_text


def derive_source_type(payloads: list[dict]) -> str:
    source_types = {payload["feature_source_type"] for payload in payloads}
    if len(source_types) == 1:
        return next(iter(source_types))
    return "mixed"


def run_shadow_session_sse_server(host: str = "127.0.0.1", port: int = 8765):
    # Business-facing wrapper: binds the generic SSE HTTP server helper
    # to this module's SessionStreamPlan and ShadowSessionManager types.
    return run_shadow_session_sse_server_impl(ShadowSessionManager, SessionStreamPlan, host, port)


def collect_requested_output_modes(args) -> list[str]:
    explicit_modes = [
        (args.summary, "summary"),
        (args.json, "json"),
        (args.csv, "csv"),
        (args.stats, "stats"),
    ]
    enabled_modes = [name for enabled, name in explicit_modes if enabled]
    if enabled_modes:
        return enabled_modes
    inferred_mode = infer_output_format(args.out)
    return [inferred_mode or "default"]


def build_output_plan(
    args,
    mode: str,
    output_path: str | None,
    *,
    inferred_from_suffix: bool,
    naming_policy: str,
) -> OutputPlan:
    include_json_stats = args.json_with_stats and mode == "json"
    return OutputPlan(
        mode=mode,
        output_path=output_path,
        render_strategy=resolve_stats_format(args, mode),
        include_meta=resolve_json_include_meta(args, mode, include_json_stats),
        include_compact_stats=(mode == "summary"),
        include_json_stats=include_json_stats,
        summary_style=resolve_summary_style(args, mode),
        inferred_from_suffix=inferred_from_suffix,
        naming_policy=naming_policy,
    )


def build_output_plans(args) -> list[OutputPlan]:
    modes = collect_requested_output_modes(args)
    if args.out_multi_base:
        if len(modes) == 1 and modes[0] == "default":
            raise SystemExit(
                "Use one or more of --summary, --json, --csv, or --stats with --out-multi-base."
            )
        base_path = Path(args.out_multi_base)
        base_dir = base_path.parent
        base_name = base_path.name
        return [
            build_output_plan(
                args,
                mode,
                str(base_dir / f"{base_name}.{mode_to_extension(mode)}"),
                inferred_from_suffix=False,
                naming_policy="multi_base",
            )
            for mode in ["summary", "json", "csv", "stats"]
            if mode in modes
        ]
    if args.out_multi:
        if len(modes) == 1 and modes[0] == "default":
            raise SystemExit(
                "Use one or more of --summary, --json, --csv, or --stats with --out-multi."
            )
        return [
            build_output_plan(
                args,
                mode,
                args.out_multi[mode],
                inferred_from_suffix=False,
                naming_policy="multi_explicit",
            )
            for mode in ["summary", "json", "csv", "stats"]
            if args.out_multi.get(mode) and mode in modes
        ]
    if len(modes) > 1:
        raise SystemExit(
            "Use only one of --summary, --json, --csv, or --stats unless using --out-multi."
        )
    inferred = (
        not any([args.summary, args.json, args.csv, args.stats])
        and infer_output_format(args.out) is not None
    )
    return [
        build_output_plan(
            args,
            modes[0],
            args.out,
            inferred_from_suffix=inferred,
            naming_policy="single",
        )
    ]


def mode_to_extension(mode: str) -> str:
    return {
        "summary": "summary",
        "json": "json",
        "csv": "csv",
        "stats": "stats",
        "default": "txt",
    }[mode]


def dispatch_outputs(
    plans: list[OutputPlan],
    payloads: list[dict],
    default_text: str,
) -> None:
    for plan in plans:
        content = render_output_content(plan, payloads, default_text)
        emit_output(content, plan.output_path)


def parse_mode_override_alias(
    value: str | None, mode: str, allowed_values: set[str], option_name: str
) -> dict[str, str]:
    if value is None:
        return {}
    if value not in allowed_values:
        allowed_text = ", ".join(sorted(allowed_values))
        raise SystemExit(f"{option_name} must be one of: {allowed_text}.")
    return {mode: value}


def build_mode_overrides(args) -> dict[str, object]:
    summary = parse_mode_override_alias(
        args.summary_style_summary,
        "summary",
        {"compact", "full"},
        "--summary-style-summary",
    )
    stats = parse_mode_override_alias(
        args.stats_format_stats,
        "stats",
        {"plain", "compact", "json"},
        "--stats-format-stats",
    )
    json_meta = parse_mode_override_alias(
        args.json_include_meta_json,
        "json",
        {"true", "false"},
        "--json-include-meta-json",
    )

    return {
        "summary_style": summary,
        "stats_format": stats,
        "json_include_meta": {mode: value == "true" for mode, value in json_meta.items()},
    }


def resolve_summary_style(args, mode: str) -> str:
    if mode != "summary":
        return "compact"
    return args.mode_overrides["summary_style"].get(mode, args.summary_style)


def resolve_stats_format(args, mode: str) -> str:
    return args.mode_overrides["stats_format"].get(
        mode, args.stats_format if mode == "stats" else "plain"
    )


def resolve_json_include_meta(args, mode: str, include_json_stats: bool) -> bool:
    if mode != "json":
        return False
    return (
        args.mode_overrides["json_include_meta"].get(mode, args.json_include_meta)
        or include_json_stats
    )


def parse_out_multi(values: list[str] | None) -> dict[str, str]:
    result = {}
    for value in values or []:
        mode, separator, path = value.partition("=")
        if separator == "" or mode not in {"summary", "json", "csv", "stats"} or not path:
            raise SystemExit(
                "Each --out-multi value must be one of summary=PATH, json=PATH,"
                " csv=PATH, or stats=PATH."
            )
        result[mode] = path
    return result


def resolve_output_mode(args) -> str | None:
    explicit_modes = [
        (args.summary, "summary"),
        (args.json, "json"),
        (args.csv, "csv"),
        (args.stats, "stats"),
    ]
    enabled_modes = [name for enabled, name in explicit_modes if enabled]
    if len(enabled_modes) > 1:
        raise SystemExit("Use only one of --summary, --json, --csv, or --stats.")
    if enabled_modes:
        return enabled_modes[0]
    inferred_mode = infer_output_format(args.out)
    return inferred_mode or "default"


def parse_args():
    parser = ArgumentParser(description="Run V9 shadow scenarios")
    parser.add_argument(
        "--scenario",
        dest="scenario_flag",
        choices=["all", *SCENARIO_REGISTRY.keys()],
        help="Scenario to run",
    )
    parser.add_argument(
        "--list-scenarios",
        action="store_true",
        help="List available scenarios and exit",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print concise key=value summaries for each replay result",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print replay results as JSON",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="Print replay results as CSV",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print aggregated replay statistics",
    )
    parser.add_argument(
        "--feature-file",
        help=(
            "Load one feature sample JSON file; supports flat feature maps or"
            " {name,description,features}"
        ),
    )
    parser.add_argument(
        "--feature-batch-file",
        help="Load a JSON array of named feature samples for batch replay",
    )
    parser.add_argument(
        "--feature-dir",
        help="Load all single-sample JSON files from a directory for batch replay",
    )
    parser.add_argument(
        "--out",
        help="Write rendered output to a file path",
    )
    parser.add_argument(
        "--out-multi",
        action="append",
        help="Write multiple formats at once, e.g. --out-multi json=path --out-multi csv=path",
    )
    parser.add_argument(
        "--out-multi-base",
        help=(
            "Write multiple formats at once using one base path, e.g."
            " reports/replay -> replay.summary/json/csv/stats"
        ),
    )
    parser.add_argument(
        "--json-with-stats",
        action="store_true",
        help="When rendering JSON, include a top-level stats node",
    )
    parser.add_argument(
        "--summary-style",
        choices=["compact", "full"],
        default="compact",
        help="Choose summary rendering style; default: compact",
    )
    parser.add_argument(
        "--stats-format",
        choices=["plain", "compact", "json"],
        default="plain",
        help="Choose stats rendering format; default: plain",
    )
    parser.add_argument(
        "--json-include-meta",
        action="store_true",
        help="When rendering JSON, include a top-level meta node even without stats",
    )
    parser.add_argument(
        "--summary-style-summary",
        choices=["compact", "full"],
        help="Recommended shortcut for summary override in multi-output mode",
    )
    parser.add_argument(
        "--stats-format-stats",
        choices=["plain", "compact", "json"],
        help="Recommended shortcut for stats override in multi-output mode",
    )
    parser.add_argument(
        "--json-include-meta-json",
        choices=["true", "false"],
        help="Recommended shortcut for JSON meta override in multi-output mode",
    )
    parser.add_argument(
        "--serve-session-sse",
        action="store_true",
        help="Run a minimal HTTP SSE server exposing /engine/v9-shadow/stream",
    )
    parser.add_argument(
        "--serve-host",
        default="127.0.0.1",
        help="Host for the minimal HTTP SSE server; default: 127.0.0.1",
    )
    parser.add_argument(
        "--serve-port",
        type=int,
        default=8765,
        help="Port for the minimal HTTP SSE server; default: 8765",
    )
    parser.add_argument(
        "--write-baseline",
        help="Write replay baseline JSON containing results and stats",
    )
    parser.add_argument(
        "--check-baseline",
        help="Compare replay output against a baseline JSON file",
    )
    parser.add_argument(
        "--write-batch-baselines",
        help="Write one baseline JSON per batch sample into a directory",
    )
    parser.add_argument(
        "--check-batch-baselines",
        help="Compare batch replay output against per-sample baselines in a directory",
    )
    parser.add_argument(
        "--formal-baseline-manifest",
        default=FORMAL_BASELINE_MANIFEST_PATH,
        help="Path to the formal baseline manifest JSON file",
    )
    parser.add_argument(
        "--rebuild-formal-baselines",
        action="store_true",
        help="Rebuild all formal baseline suites under the standard replay directories",
    )
    parser.add_argument(
        "--check-formal-baselines",
        action="store_true",
        help="Check all formal baseline suites under the standard replay directories",
    )
    parser.epilog = (
        "Examples:\n"
        "  Single JSON with stats:\n"
        "    --feature-batch-file data.json --json --json-with-stats\n"
        "  Single summary with full footer:\n"
        "    --feature-dir snapshots --summary --summary-style full\n"
        "  Multi-output with recommended shortcuts:\n"
        "    --feature-batch-file data.json --summary --json --stats"
        " --out-multi-base reports/replay \\\n"
        "      --summary-style-summary full --stats-format-stats json \\\n"
        "      --json-include-meta-json true\n"
        "  Multi-output with explicit paths:\n"
        "    --feature-batch-file data.json --summary --json --stats \\\n"
        "      --out-multi summary=reports/replay.summary \\\n"
        "      --out-multi json=reports/replay.json \\\n"
        "      --out-multi stats=reports/replay.stats\n"
        "  Legacy-compatible by-mode overrides have been removed;"
        " use the recommended shortcuts above.\n"
    )
    args = parser.parse_args()
    args.out_multi = parse_out_multi(args.out_multi)
    args.mode_overrides = build_mode_overrides(args)
    return args


def prepare_batch_results(
    feature_batch_file: str | None = None,
    feature_dir: str | None = None,
    feature_file: str | None = None,
) -> tuple[list[dict], str] | None:
    if feature_batch_file:
        batch = load_feature_batch_from_json(feature_batch_file)
        manifest_meta = load_formal_manifest_meta_from_batch_file(feature_batch_file)
        results = [
            (
                item["name"],
                run_scenario(
                    resolve_batch_scenario_name(item["name"]), feature_source=item["feature_source"]
                ),
            )
            for item in batch
        ]
        payloads = [
            build_summary_payload(
                item["name"],
                result,
                feature_source_type="batch_file",
                feature_file=feature_batch_file,
                sample_description=item["description"],
                manifest=manifest_meta,
            )
            for item, (_, result) in zip(batch, results, strict=False)
        ]
        default_output = "\n\n".join(
            render_result_text(f"=== V9 Shadow Batch Sample {name} ===", result)
            for name, result in results
        )
        return payloads, default_output

    if feature_dir:
        samples = load_feature_samples_from_dir(feature_dir)
        results = [
            (
                item["name"],
                run_scenario("neutral", feature_source=item["feature_source"]),
            )
            for item in samples
        ]
        payloads = [
            build_summary_payload(
                item["name"],
                result,
                feature_source_type="dir_file",
                feature_file=item["feature_file"],
                sample_description=item["description"],
            )
            for item, (_, result) in zip(samples, results, strict=False)
        ]
        default_output = "\n\n".join(
            render_result_text(f"=== V9 Shadow Directory Sample {name} ===", result)
            for name, result in results
        )
        return payloads, default_output

    if feature_file is not None:
        feature_source = load_feature_source_from_json(feature_file)
        scenario_names = ["neutral", "long", "short"]
        results = [
            (name, run_scenario(name, feature_source=feature_source)) for name in scenario_names
        ]
        payloads = [
            build_summary_payload(
                name,
                result,
                feature_source_type="file",
                feature_file=feature_file,
            )
            for name, result in results
        ]
        default_output = "\n\n".join(
            render_result_text(
                f"=== V9 Shadow "
                f"{'Actionable ' if name in {'long', 'short'} else ''}"
                f"{name.title()} Scenario ===",
                result,
            )
            for name, result in results
        )
        return payloads, default_output

    return None


def prepare_single_results(
    scenario: str, feature_file: str | None = None
) -> tuple[list[dict], str]:
    feature_source = load_feature_source_from_json(feature_file) if feature_file else None
    feature_source_type = "file" if feature_file else "scenario"
    result = run_scenario(scenario, feature_source=feature_source)
    payload = build_summary_payload(
        scenario,
        result,
        feature_source_type=feature_source_type,
        feature_file=feature_file,
    )
    default_output = render_result_text(f"=== V9 Shadow {scenario.title()} Scenario ===", result)
    return [payload], default_output


def prepare_results(args) -> tuple[list[dict], str]:
    scenario = args.scenario_flag or "all"
    batch_result = prepare_batch_results(
        feature_batch_file=args.feature_batch_file,
        feature_dir=args.feature_dir,
        feature_file=args.feature_file if scenario == "all" else None,
    )
    if batch_result is not None:
        return batch_result

    if scenario == "all":
        results = [(name, run_scenario(name)) for name in ["neutral", "long", "short"]]
        payloads = [
            build_summary_payload(name, result, feature_source_type="scenario", feature_file=None)
            for name, result in results
        ]
        default_output = "\n\n".join(
            render_result_text(
                f"=== V9 Shadow "
                f"{'Actionable ' if name in {'long', 'short'} else ''}"
                f"{name.title()} Scenario ===",
                result,
            )
            for name, result in results
        )
        return payloads, default_output

    return prepare_single_results(scenario, feature_file=args.feature_file)


def build_results_from_payloads(payloads: list[dict]) -> list:
    communication_mirror_keys = (
        PAYLOAD_KEY_OPERATIONS_SUMMARY,
        PAYLOAD_KEY_OPERATIONS_POSTURE,
        PAYLOAD_KEY_POSTURE_SOURCES,
        PAYLOAD_KEY_GOVERNANCE_SOURCES,
    )
    return [
        SimpleNamespace(
            communication_operations={
                PAYLOAD_KEY_OPERATIONS_SUMMARY: payload.get(PAYLOAD_KEY_OPERATIONS_SUMMARY),
                PAYLOAD_KEY_OPERATIONS_POSTURE: payload.get(PAYLOAD_KEY_OPERATIONS_POSTURE),
                PAYLOAD_KEY_POSTURE_SOURCES: payload.get(PAYLOAD_KEY_POSTURE_SOURCES),
                PAYLOAD_KEY_GOVERNANCE_SOURCES: payload.get(PAYLOAD_KEY_GOVERNANCE_SOURCES),
            }
            if any(payload.get(key) is not None for key in communication_mirror_keys)
            else None
        )
        for payload in payloads
    ]


def execute_outputs(
    args, payloads: list[dict], default_output: str, results: list | None = None
) -> None:
    output_plans = build_output_plans(args)
    resolved_results = results if results is not None else build_results_from_payloads(payloads)
    extended_payloads = extend_payloads_for_output(payloads, resolved_results)
    dispatch_outputs(output_plans, extended_payloads, default_output)


def execute_regression_actions(args, payloads: list[dict]) -> bool:
    if args.rebuild_formal_baselines:
        result = rebuild_formal_baseline_suites(args.formal_baseline_manifest)
        print(
            render_formal_baseline_rebuild_json(result)
            if args.json
            else render_formal_baseline_rebuild_text(result)
        )
        return True
    if args.check_formal_baselines:
        result = check_formal_baseline_suites(args.formal_baseline_manifest)
        print(
            render_formal_baseline_suite_json(result)
            if args.json
            else render_formal_baseline_suite_text(result)
        )
        if not result["matches"]:
            raise SystemExit(1)
        return True
    if args.write_batch_baselines:
        if not args.feature_batch_file:
            raise SystemExit("--write-batch-baselines requires --feature-batch-file.")
        batch = load_feature_batch_from_json(args.feature_batch_file)
        written_paths = write_batch_regression_baselines(args.write_batch_baselines, batch)
        print("\n".join(written_paths))
        return True
    if args.check_batch_baselines:
        if not args.feature_batch_file:
            raise SystemExit("--check-batch-baselines requires --feature-batch-file.")
        batch = load_feature_batch_from_json(args.feature_batch_file)
        diff = check_batch_regression_baselines(args.check_batch_baselines, batch)
        print(render_batch_regression_diff_text(diff))
        if not diff["matches"]:
            raise SystemExit(1)
        return True
    if args.write_baseline:
        write_regression_baseline(args.write_baseline, payloads)
        return True
    if args.check_baseline:
        diff = check_regression_baseline(args.check_baseline, payloads)
        print(render_regression_diff_text(diff))
        if not diff["matches"]:
            raise SystemExit(1)
        return True
    return False


def main():
    args = parse_args()

    try:
        if args.serve_session_sse:
            server = run_shadow_session_sse_server(args.serve_host, args.serve_port)
            print(
                f"shadow_session_sse_server=http://{args.serve_host}:{args.serve_port}/engine/v9-shadow/stream"
            )
            server.serve_forever()
            return

        if args.list_scenarios:
            print("Available scenarios:")
            print("- all: run every scenario")
            for name, config in SCENARIO_REGISTRY.items():
                print(f"- {name}: {config['description']}")
            return

        feature_inputs = [args.feature_file, args.feature_batch_file, args.feature_dir]
        if sum(value is not None for value in feature_inputs) > 1:
            raise SystemExit(
                "Use only one of --feature-file, --feature-batch-file, or --feature-dir."
            )

        prepared = prepare_results(args)
        if len(prepared) == 3:
            payloads, default_output, results = prepared
        else:
            payloads, default_output = prepared
            results = None
        if execute_regression_actions(args, payloads):
            return
        execute_outputs(args, payloads, default_output, results)
    except FeatureInputError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
