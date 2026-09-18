"""Conservative work-order rescheduling within the agreed draft-only scope."""

from __future__ import annotations

from datetime import date
from typing import Any

from .mcp_client import (
    InvalidParams,
    PermissionDenied,
    ToolError,
    ToolNotFound,
    ToolResult,
    TransportError,
    WriteNotAllowed,
)

_DRAFT_BASIS = (
    "Estimate keeps the original planned duration and starts no earlier than today; "
    "it has no capacity, progress, or material basis."
)
_ACTIVE_BASIS = (
    "The planned start is kept because work has begun; the new end allows the full original "
    "planned duration from today because progress is unknown, so it is an upper bound, not a "
    "forecast; it has no capacity or material basis."
)
_INVALID_DATES_BASIS = (
    "No reschedule estimate is available because the planned dates are missing or invalid; "
    "it has no capacity, progress, or material basis."
)
_NO_PLAN_BASIS = (
    "No reschedule estimate is available; it has no capacity, progress, or material basis."
)
_HANDLED_ERRORS = (InvalidParams, PermissionDenied, ToolNotFound, WriteNotAllowed, ToolError)


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def plan_reschedule(work_order: dict[str, Any], *, today: date) -> dict[str, Any]:
    """Return a pure, duration-preserving reschedule decision."""
    start = _parse_date(work_order.get("planned_start_date"))
    end = _parse_date(work_order.get("planned_end_date"))
    if start is None or end is None:
        return {
            "action": "cannot_plan",
            "reason": "missing_or_unparseable_planned_dates",
            "proposed": None,
            "basis": _INVALID_DATES_BASIS,
        }
    if end < start:
        return {
            "action": "cannot_plan",
            "reason": "planned_end_before_start",
            "proposed": None,
            "basis": _INVALID_DATES_BASIS,
        }

    status = work_order.get("status")
    if status in {"completed", "cancelled"}:
        return {
            "action": "not_needed",
            "reason": "terminal_status",
            "proposed": None,
            "basis": "No reschedule estimate is needed for a completed or cancelled work order.",
        }
    duration = end - start
    proposed: dict[str, str] | None = None
    basis: str
    if status in {"draft", "not_started"}:
        basis = _DRAFT_BASIS
        if start < today or end < today:
            proposed = {
                "planned_start_date": today.isoformat(),
                "planned_end_date": (today + duration).isoformat(),
            }
    elif status in {"in_progress", "stopped"}:
        basis = _ACTIVE_BASIS
        if end < today:
            proposed = {
                "planned_start_date": start.isoformat(),
                "planned_end_date": (today + duration).isoformat(),
            }
    else:
        return {
            "action": "cannot_plan",
            "reason": "unknown_status",
            "proposed": None,
            "basis": _NO_PLAN_BASIS,
        }
    if proposed is None:
        return {
            "action": "not_needed",
            "reason": "planned_dates_not_past",
            "proposed": None,
            "basis": "The planned dates are not past, so no reschedule estimate is needed.",
        }
    return {"action": "needed", "reason": None, "proposed": proposed, "basis": basis}


def _record_from_result(result: ToolResult) -> dict[str, Any]:
    if not isinstance(result.structured, dict):
        raise TypeError("WorkOrder.get returned a non-record result")
    return result.structured


def _before(record: dict[str, Any] | None) -> dict[str, Any]:
    record = record or {}
    return {
        "status": record.get("status"),
        "created_by": record.get("created_by"),
        "planned_start_date": record.get("planned_start_date"),
        "planned_end_date": record.get("planned_end_date"),
    }


def _error_reason(error: Exception) -> str:
    if isinstance(error, InvalidParams) and "not found" in error.message.lower():
        return "not_found"
    if isinstance(error, PermissionDenied):
        return "permission_denied"
    if isinstance(error, ToolNotFound):
        return "tool_not_found"
    if isinstance(error, WriteNotAllowed):
        return "write_not_allowed"
    if isinstance(error, ToolError):
        return "tool_error"
    return "source_unavailable"


def _open_cause_notes(causes: Any) -> list[str]:
    if not isinstance(causes, list):
        return []
    codes = sorted(
        {
            cause.get("code")
            for cause in causes
            if isinstance(cause, dict) and isinstance(cause.get("code"), str) and cause["code"]
        }
    )
    if not codes:
        return []
    return [f"Proposed dates do not account for open causes: {', '.join(codes)}."]


def reschedule(
    client: Any,
    work_order_id: Any,
    *,
    today: date,
    own_user_id: Any,
    causes: Any = None,
) -> dict[str, Any]:
    """Re-read, plan, and conditionally reschedule one owned draft work order."""
    calls: list[dict[str, Any]] = []
    notes = _open_cause_notes(causes)

    def call(name: str, arguments: dict[str, Any], *, allow_write: bool = False) -> ToolResult:
        entry: dict[str, Any] = {"tool": name, "arguments": dict(arguments)}
        calls.append(entry)
        try:
            result = client.call_tool(name, arguments, allow_write=allow_write)
        except Exception as error:
            entry["outcome"] = type(error).__name__
            raise
        entry["outcome"] = "ok"
        return result

    def result(
        action: str,
        reason: str | None,
        record: dict[str, Any] | None,
        proposed: dict[str, str] | None,
        *,
        applied: dict[str, str] | None = None,
        basis: str = _NO_PLAN_BASIS,
    ) -> dict[str, Any]:
        return {
            "action": action,
            "reason": reason,
            "before": _before(record),
            "proposed": proposed,
            "applied": applied,
            "basis": basis,
            "notes": notes,
            "calls": calls,
        }

    try:
        record = _record_from_result(call("WorkOrder.get", {"id": work_order_id}))
    except _HANDLED_ERRORS as error:
        return result("escalated", _error_reason(error), None, None)
    except Exception:
        return result("escalated", "source_unavailable", None, None)

    plan = plan_reschedule(record, today=today)
    proposed = plan["proposed"]
    if plan["action"] in {"not_needed", "cannot_plan"}:
        return result(plan["action"], plan["reason"], record, proposed, basis=plan["basis"])
    status = record.get("status")
    if status != "draft":
        notes.append(
            "not_started updates are refused by the server and cancellation requires an admin; "
            "in_progress and stopped are outside the agreed write scope."
        )
        return result("escalated", "status_not_editable", record, proposed, basis=plan["basis"])
    created_by = record.get("created_by")
    if (
        not isinstance(own_user_id, str)
        or not own_user_id
        or not isinstance(created_by, str)
        or not created_by
        or created_by != own_user_id
    ):
        return result("escalated", "not_own_record", record, proposed, basis=plan["basis"])

    arguments: dict[str, Any] = {"id": work_order_id}
    for field in ("planned_start_date", "planned_end_date"):
        if record.get(field) != proposed[field]:
            arguments[field] = proposed[field]
    write_error: Exception | None = None
    try:
        call("WorkOrder.update", arguments, allow_write=True)
    except Exception as error:
        write_error = error

    try:
        observed = _record_from_result(call("WorkOrder.get", {"id": work_order_id}))
    except Exception:
        if write_error is not None:
            if isinstance(write_error, TransportError) and write_error.outcome_unknown:
                notes.append("The update and confirmation read both failed; the write outcome is unknown.")
                reason = "outcome_unknown"
            else:
                reason = type(write_error).__name__
            return result("write_failed", reason, record, proposed, basis=plan["basis"])
        return result("mismatch", "confirmation_read_failed", record, proposed, basis=plan["basis"])

    applied = {
        "planned_start_date": observed.get("planned_start_date"),
        "planned_end_date": observed.get("planned_end_date"),
    }
    dates_match = applied == proposed
    if write_error is not None:
        outcome_unknown = isinstance(write_error, TransportError) and write_error.outcome_unknown
        if outcome_unknown and dates_match:
            notes.append("The write raised an error, but the confirmation read shows it was applied; outcome was uncertain.")
            return result("applied", None, record, proposed, applied=applied, basis=plan["basis"])
        return result("write_failed", type(write_error).__name__, record, proposed, basis=plan["basis"])
    if dates_match and observed.get("status") == "draft":
        return result("applied", None, record, proposed, applied=applied, basis=plan["basis"])
    return result(
        "mismatch",
        "confirmation_mismatch",
        record,
        proposed,
        applied=applied,
        basis=plan["basis"],
    )


__all__ = ["plan_reschedule", "reschedule"]
