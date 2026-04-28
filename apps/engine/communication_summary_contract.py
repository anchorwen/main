from core.deployment.domain_keys import (
    PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE,
    PAYLOAD_KEY_GOVERNANCE_SOURCES,
    PAYLOAD_KEY_GOVERNANCE_SUMMARY_SOURCE,
    PAYLOAD_KEY_OPERATIONS_POSTURE,
    PAYLOAD_KEY_OPERATIONS_POSTURE_SOURCE,
    PAYLOAD_KEY_OPERATIONS_SUMMARY,
    PAYLOAD_KEY_POSTURE,
    PAYLOAD_KEY_POSTURE_SOURCES,
    PAYLOAD_KEY_POSTURE_SOURCE,
    PAYLOAD_KEY_SUMMARY_SOURCE,
)


def build_summary_mirror_fields_from_operations_summary(payload: dict, *, stable_fields: tuple[str, ...] | None = None) -> dict:
    operations_summary = payload.get(PAYLOAD_KEY_OPERATIONS_SUMMARY)
    if not isinstance(operations_summary, dict):
        if stable_fields is None:
            return payload
        return {
            field_name: payload.get(field_name)
            for field_name in stable_fields
            if field_name in payload
        }

    normalized_payload = {
        PAYLOAD_KEY_OPERATIONS_SUMMARY: operations_summary,
        PAYLOAD_KEY_OPERATIONS_POSTURE: operations_summary.get(PAYLOAD_KEY_POSTURE),
        PAYLOAD_KEY_POSTURE_SOURCES: {
            PAYLOAD_KEY_OPERATIONS_POSTURE_SOURCE: operations_summary.get(PAYLOAD_KEY_POSTURE_SOURCE),
        },
    }

    governance_summary_source = operations_summary.get(PAYLOAD_KEY_GOVERNANCE_SUMMARY_SOURCE)
    execution_projection_source = operations_summary.get(PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE)
    if governance_summary_source is not None or execution_projection_source is not None:
        normalized_payload[PAYLOAD_KEY_GOVERNANCE_SOURCES] = {
            PAYLOAD_KEY_SUMMARY_SOURCE: governance_summary_source,
            PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE: execution_projection_source,
        }
    elif PAYLOAD_KEY_GOVERNANCE_SOURCES in payload:
        normalized_payload[PAYLOAD_KEY_GOVERNANCE_SOURCES] = payload.get(PAYLOAD_KEY_GOVERNANCE_SOURCES)

    if stable_fields is None:
        return {
            **payload,
            **normalized_payload,
        }
    return normalized_payload
