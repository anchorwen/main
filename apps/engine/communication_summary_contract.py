def build_summary_mirror_fields_from_operations_summary(payload: dict, *, stable_fields: tuple[str, ...] | None = None) -> dict:
    operations_summary = payload.get("operations_summary")
    if not isinstance(operations_summary, dict):
        if stable_fields is None:
            return payload
        return {
            field_name: payload.get(field_name)
            for field_name in stable_fields
            if field_name in payload
        }

    normalized_payload = {
        "operations_summary": operations_summary,
        "operations_posture": operations_summary.get("posture"),
        "posture_sources": {
            "operations_posture_source": operations_summary.get("posture_source"),
        },
    }

    governance_summary_source = operations_summary.get("governance_summary_source")
    execution_projection_source = operations_summary.get("execution_projection_source")
    if governance_summary_source is not None or execution_projection_source is not None:
        normalized_payload["governance_sources"] = {
            "summary_source": governance_summary_source,
            "execution_projection_source": execution_projection_source,
        }
    elif "governance_sources" in payload:
        normalized_payload["governance_sources"] = payload.get("governance_sources")

    if stable_fields is None:
        return {
            **payload,
            **normalized_payload,
        }
    return normalized_payload

