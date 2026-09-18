"""Independent scoring rules for the read-only harness."""

from datetime import date
from typing import Any

OPEN_WORK_ORDER_STATES = {"not_started", "in_progress", "stopped"}
POTENTIAL_CONSUMER_STATES = {"draft", "not_started", "in_progress", "stopped"}

CAUSE_RULES: dict[str, dict[str, Any]] = {
    "work_order_stopped": {"basis": "observed", "evidence": {"WorkOrder": 1}},
    "material_request_open": {"basis": "observed", "evidence": {"MaterialRequest": 1}},
    "material_request_draft": {"basis": "possible", "evidence": {"MaterialRequest": 1}},
    "subcontract_awaiting_receipt": {
        "basis": "observed",
        "evidence": {"SubcontractOrder": 1},
    },
    "subcontract_not_completed": {
        "basis": "observed",
        "evidence": {"SubcontractOrder": 1},
    },
    "subcontract_draft": {"basis": "possible", "evidence": {"SubcontractOrder": 1}},
    "job_card_overdue": {"basis": "observed", "evidence": {"JobCard": 1}},
    "job_card_open": {"basis": "context", "evidence": {"JobCard": 1}},
    "recorded_downtime": {"basis": "possible", "evidence": {"DowntimeEntry": 1}},
    "eco_possible_hold": {
        "basis": "possible",
        "evidence": {"EngineeringChangeOrder": 1},
    },
    "eco_affects_order": {
        "basis": "context",
        "evidence": {"EngineeringChangeOrder": 1},
    },
    "quality_rejected": {
        "basis": "observed",
        "evidence": {"QualityInspection": 1, "WorkOrder": 1},
    },
    "quality_inspection_pending": {
        "basis": "possible",
        "evidence": {"QualityInspection": 1, "WorkOrder": 1},
    },
    "quality_conditional": {
        "basis": "context",
        "evidence": {"QualityInspection": 1, "WorkOrder": 1},
    },
    "workstation_unavailable": {"basis": "possible", "evidence": {"Workstation": 1}},
    "schedule_verdict": {"basis": "context", "evidence": {"FiniteScheduleOrder": 1}},
}

REQUIRED_CAUSE_ENTITIES = {"MaterialRequest", "SubcontractOrder", "JobCard"}
WORK_ORDER_CLAIM_FIELDS = (
    "id",
    "status",
    "planned_start_date",
    "planned_end_date",
    "sales_order_id",
    "item_id",
    "bom_id",
)


def parse_date(value: Any) -> date | None:
    """Parse the date portion of an ISO-like platform value."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def lateness(work_order: dict[str, Any], today: date) -> dict[str, bool | int | None]:
    """Apply the status-first lateness rule."""
    status = work_order.get("status")
    if status in {"draft", "completed", "cancelled"}:
        return {"is_late": False, "days_late": None}
    if status not in OPEN_WORK_ORDER_STATES:
        return {"is_late": None, "days_late": None}
    planned_end = parse_date(work_order.get("planned_end_date"))
    if planned_end is None:
        return {"is_late": None, "days_late": None}
    if planned_end < today:
        return {"is_late": True, "days_late": (today - planned_end).days}
    return {"is_late": False, "days_late": None}


def expected_reschedule_plan(work_order: dict[str, Any], today: date) -> dict[str, Any]:
    """Independently derive the duration-preserving reschedule proposal."""
    start = parse_date(work_order.get("planned_start_date"))
    end = parse_date(work_order.get("planned_end_date"))
    if start is None or end is None:
        return {"decision": "cannot_plan", "proposed": None}
    if end < start:
        return {"decision": "cannot_plan", "proposed": None}
    status = work_order.get("status")
    if status in {"completed", "cancelled"}:
        return {"decision": "not_needed", "proposed": None}
    duration = end - start
    if status in {"draft", "not_started"}:
        needed = start < today or end < today
        proposed_start = today
    elif status in {"in_progress", "stopped"}:
        needed = end < today
        proposed_start = start
    else:
        return {"decision": "cannot_plan", "proposed": None}
    if not needed:
        return {"decision": "not_needed", "proposed": None}
    return {
        "decision": "needed",
        "proposed": {
            "planned_start_date": proposed_start.isoformat(),
            "planned_end_date": (today + duration).isoformat(),
        },
    }


def overdue(record: dict[str, Any], entity: str, today: date) -> bool:
    """Compute a cause's date-derived overdue value."""
    field = {
        "MaterialRequest": "required_by_date",
        "SubcontractOrder": "expected_delivery_date",
        "JobCard": "planned_end",
    }.get(entity)
    if field is None:
        return False
    value = parse_date(record.get(field))
    return value is not None and value < today


def json_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without treating booleans as numbers."""
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if (
        isinstance(left, (int, float))
        and not isinstance(left, bool)
        and isinstance(right, (int, float))
        and not isinstance(right, bool)
    ):
        return left == right
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, list) or isinstance(right, list):
        return (
            isinstance(left, list)
            and isinstance(right, list)
            and len(left) == len(right)
            and all(json_equal(left_item, right_item) for left_item, right_item in zip(left, right))
        )
    if isinstance(left, dict) or isinstance(right, dict):
        return (
            isinstance(left, dict)
            and isinstance(right, dict)
            and left.keys() == right.keys()
            and all(json_equal(left[key], right[key]) for key in left)
        )
    return False


def projected_records(entity: str, record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return comparison projections, including one projection per ECO link."""
    if entity != "EngineeringChangeOrder":
        return [record]
    affected = record.get("affected_work_orders")
    links = affected if isinstance(affected, list) else []
    projections: list[dict[str, Any]] = []
    for link in links:
        if not isinstance(link, dict):
            continue
        projection = dict(record)
        projection["work_order_id"] = link.get("work_order_id")
        projection["action"] = link.get("action")
        projections.append(projection)
    return projections or [dict(record)]


def fields_match(
    entity: str,
    record: dict[str, Any],
    claimed_fields: dict[str, Any],
    today: date,
) -> bool:
    """Compare verbatim fields while recomputing supported derived values."""
    return bool(matching_projections(entity, record, claimed_fields, today))


def matching_projections(
    entity: str,
    record: dict[str, Any],
    claimed_fields: dict[str, Any],
    today: date,
) -> list[dict[str, Any]]:
    """Return projections on which every claimed field holds together."""
    matches: list[dict[str, Any]] = []
    for projection in projected_records(entity, record):
        if all(
            json_equal(
                overdue(record, entity, today) if field == "overdue" else projection.get(field),
                claimed,
            )
            for field, claimed in claimed_fields.items()
        ):
            matches.append(projection)
    return matches


def expected_observed_code(entity: str, record: dict[str, Any], today: date) -> str | None:
    """Return the required observed-basis code used by the cause-completeness task."""
    status = record.get("status")
    if entity == "MaterialRequest" and status in {"submitted", "partially_ordered", "ordered"}:
        return "material_request_open"
    if entity == "SubcontractOrder":
        if status in {"submitted", "materials_sent", "in_progress"}:
            return "subcontract_awaiting_receipt"
        if status in {"received", "quality_check"}:
            return "subcontract_not_completed"
    if entity == "JobCard" and status in {"open", "in_progress"}:
        return "job_card_overdue" if overdue(record, entity, today) else None
    if (
        entity == "QualityInspection"
        and status == "completed"
        and record.get("overall_result") == "rejected"
    ):
        return "quality_rejected"
    return None


def selector_eligible(selector: str, work_order: dict[str, Any], today: date) -> bool:
    """Re-evaluate the work-order portion of a selector."""
    status = work_order.get("status")
    if selector == "completed_most_recent":
        return status == "completed"
    if selector in {"late_open_oldest", "late_with_sales_order", "late_with_cause"}:
        eligible = status in OPEN_WORK_ORDER_STATES and lateness(work_order, today)["is_late"] is True
        if selector == "late_with_sales_order":
            return eligible and isinstance(work_order.get("sales_order_id"), str)
        return eligible
    return True


__all__ = [
    "CAUSE_RULES",
    "OPEN_WORK_ORDER_STATES",
    "POTENTIAL_CONSUMER_STATES",
    "REQUIRED_CAUSE_ENTITIES",
    "expected_observed_code",
    "expected_reschedule_plan",
    "fields_match",
    "json_equal",
    "lateness",
    "matching_projections",
    "overdue",
    "parse_date",
    "projected_records",
    "selector_eligible",
    "WORK_ORDER_CLAIM_FIELDS",
]
