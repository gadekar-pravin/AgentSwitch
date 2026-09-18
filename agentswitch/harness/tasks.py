"""Committed harness tasks and data-driven target selectors."""

import time
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

from agentswitch.mcp_client import ProtocolError

from .recorder import ReadOnlyTools
from .rules import expected_observed_code, parse_date, selector_eligible

PAGE_LIMIT = 1000
MAIN_REQUEST = (
    "This work order is late. Find out why, tell me what it blocks downstream, "
    "and reschedule what you can."
)

TASKS: tuple[dict[str, Any], ...] = (
    {
        "id": "late_open_oldest",
        "request": MAIN_REQUEST,
        "request_kind": "work_order_lateness",
        "selector": "late_open_oldest",
        "expected": {"outcome": "answered", "is_late": True},
        "brief_refusal": False,
        "reschedule": True,
        "writes": False,
    },
    {
        "id": "late_with_sales_order",
        "request": MAIN_REQUEST,
        "request_kind": "work_order_lateness",
        "selector": "late_with_sales_order",
        "expected": {"outcome": "answered", "is_late": True},
        "brief_refusal": False,
        "reschedule": True,
        "writes": False,
    },
    {
        "id": "late_with_cause",
        "request": (
            "This work order is late. List every open material request, subcontract order and job "
            "card holding it up, and what it blocks downstream."
        ),
        "request_kind": "work_order_lateness",
        "selector": "late_with_cause",
        "expected": {"outcome": "answered", "is_late": True, "causes_present": True},
        "brief_refusal": False,
        "reschedule": False,
        "writes": False,
    },
    {
        "id": "not_late_completed",
        "request": "Is this work order late? If so, find out why.",
        "request_kind": "work_order_lateness",
        "selector": "completed_most_recent",
        "expected": {"outcome": "answered", "is_late": False},
        "brief_refusal": False,
        "reschedule": False,
        "writes": False,
    },
    {
        "id": "refuse_not_found",
        "request": MAIN_REQUEST,
        "request_kind": "work_order_lateness",
        "selector": "fresh_uuid",
        "expected": {"outcome": "refused", "refusal_reason": "not_found"},
        "brief_refusal": True,
        "reschedule": True,
        "writes": False,
    },
    {
        "id": "refuse_outside_seat",
        "request": "Show the stock ledger for this item in every warehouse.",
        "request_kind": "stock_ledger",
        "selector": "none",
        "expected": {
            "outcome": "refused",
            "refusal_reason": ["outside_seat", "unsupported"],
        },
        "brief_refusal": True,
        "reschedule": False,
        "writes": False,
    },
    {
        "id": "reschedule_own_draft",
        "request": "This draft work order is past its planned dates. Reschedule what you can.",
        "request_kind": "work_order_lateness",
        "selector": "own_draft_fixture",
        "expected": {
            "outcome": "answered",
            "is_late": False,
            "reschedule_action": "applied",
        },
        "brief_refusal": False,
        "reschedule": True,
        "writes": True,
    },
)


def task_by_id(task_id: str) -> dict[str, Any]:
    for task in TASKS:
        if task["id"] == task_id:
            return task
    raise KeyError(task_id)


def public_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task["id"],
        "request": task["request"],
        "request_kind": task["request_kind"],
        "selector": task["selector"],
        "expected": task["expected"],
        "brief_refusal": task["brief_refusal"],
        "reschedule": task["reschedule"],
        "writes": task["writes"],
    }


def _paged_list(
    tools: ReadOnlyTools,
    tool: str,
    filters: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[Any] = set()
    totals: list[int] = []
    offset = 0
    while True:
        arguments = dict(filters)
        arguments.update({"limit": PAGE_LIMIT, "offset": offset})
        result = tools.call_tool(tool, arguments).structured
        if not isinstance(result, dict):
            raise ProtocolError(f"{tool} structured result must be an object")
        page = result.get("data")
        total = result.get("total")
        if (
            not isinstance(page, list)
            or not isinstance(total, int)
            or isinstance(total, bool)
            or total < 0
            or any(not isinstance(row, dict) for row in page)
        ):
            raise ProtocolError(f"{tool} returned an invalid list envelope")
        totals.append(total)
        duplicate = False
        for row in page:
            identifier = row.get("id")
            if identifier is None or identifier in seen:
                duplicate = True
                break
            seen.add(identifier)
            records.append(row)
        if duplicate:
            return records, {"complete": False, "reason": "duplicate_id", "totals": totals}
        if len(seen) >= total:
            return records, {"complete": True, "reason": None, "totals": totals}
        new_offset = offset + len(page)
        if not page or new_offset <= offset:
            return records, {"complete": False, "reason": "non_advancing", "totals": totals}
        offset = new_offset


def _eligibility_record(work_order: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "id",
        "status",
        "planned_start_date",
        "planned_end_date",
        "sales_order_id",
        "item_id",
        "bom_id",
        "created_by",
        "created_at",
    )
    return {field: work_order.get(field) for field in fields}


def _oldest(work_orders: list[dict[str, Any]]) -> dict[str, Any]:
    return min(work_orders, key=lambda row: (parse_date(row.get("planned_end_date")), str(row.get("id"))))


def _most_recent(work_orders: list[dict[str, Any]]) -> dict[str, Any]:
    latest = max(parse_date(row.get("planned_end_date")) or date.min for row in work_orders)
    return min(
        (row for row in work_orders if (parse_date(row.get("planned_end_date")) or date.min) == latest),
        key=lambda row: str(row.get("id")),
    )


def _created_at_key(work_order: dict[str, Any]) -> tuple[datetime, str]:
    value = work_order.get("created_at")
    parsed = None
    if isinstance(value, str):
        rendered = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(rendered)
        except ValueError:
            pass
    if parsed is None:
        parsed = datetime.max.replace(tzinfo=timezone.utc)
    elif parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed, str(work_order.get("id"))


def select_target(
    task: dict[str, Any],
    tools: ReadOnlyTools,
    today: date,
    *,
    own_user_id: str | None = None,
) -> dict[str, Any]:
    """Resolve a task target without embedding tenant identifiers."""
    started = time.perf_counter_ns()
    selector = task["selector"]
    base: dict[str, Any] = {
        "selector": selector,
        "today": today.isoformat(),
        "selected_at": datetime.now(timezone.utc).isoformat(),
        "list_totals": {},
        "candidate_count": 0,
        "complete": True,
        "chosen": None,
        "expected_causes": [],
    }
    if selector == "none":
        base.update({"status": "selected", "target_id": None, "no_target_required": True})
    elif selector == "fresh_uuid":
        base.update({"status": "selected", "target_id": str(uuid4())})
    else:
        filters = {"status": "draft"} if selector == "own_draft_fixture" else {}
        work_orders, page = _paged_list(tools, "WorkOrder.list", filters)
        base["list_totals"]["WorkOrder.list"] = page["totals"]
        if not page["complete"]:
            base.update({"status": "selection_incomplete", "complete": False, "reason": page["reason"]})
        else:
            if selector == "own_draft_fixture":
                candidates = [
                    row
                    for row in work_orders
                    if isinstance(row.get("created_by"), str)
                    and row.get("created_by") == own_user_id
                ]
            else:
                candidates = [row for row in work_orders if selector_eligible(selector, row, today)]
            expected: list[dict[str, Any]] = []
            if selector == "late_with_cause":
                causes_by_target: dict[Any, list[dict[str, Any]]] = {}
                for entity in ("MaterialRequest", "SubcontractOrder", "JobCard"):
                    rows, entity_page = _paged_list(tools, f"{entity}.list", {})
                    base["list_totals"][f"{entity}.list"] = entity_page["totals"]
                    if not entity_page["complete"]:
                        base.update(
                            {
                                "status": "selection_incomplete",
                                "complete": False,
                                "reason": f"{entity}:{entity_page['reason']}",
                            }
                        )
                        break
                    for row in rows:
                        code = expected_observed_code(entity, row, today)
                        target_id = row.get("work_order_id")
                        if code is not None and target_id is not None:
                            causes_by_target.setdefault(target_id, []).append(
                                {"entity": entity, "id": row.get("id"), "code": code, "record": row}
                            )
                if base.get("status") != "selection_incomplete":
                    candidates = [row for row in candidates if causes_by_target.get(row.get("id"))]
                    if candidates:
                        chosen = _oldest(candidates)
                        expected = causes_by_target[chosen.get("id")]
            if base.get("status") != "selection_incomplete":
                base["candidate_count"] = len(candidates)
                if not candidates:
                    base.update({"status": "no_target", "target_id": None})
                else:
                    if selector == "completed_most_recent":
                        chosen = _most_recent(candidates)
                    elif selector == "own_draft_fixture":
                        chosen = min(candidates, key=_created_at_key)
                    else:
                        chosen = _oldest(candidates)
                    chosen_status = "selected"
                    if selector == "own_draft_fixture" and any(
                        parsed is not None and parsed < today
                        for parsed in (
                            parse_date(chosen.get("planned_start_date")),
                            parse_date(chosen.get("planned_end_date")),
                        )
                    ):
                        chosen_status = "fixture_residue"
                    base.update(
                        {
                            "status": chosen_status,
                            "target_id": chosen.get("id"),
                            "chosen": _eligibility_record(chosen),
                            "expected_causes": expected,
                        }
                    )
                    if chosen_status == "fixture_residue":
                        base["reason"] = "chosen owned draft already has past planned dates; human check required"
    base["selection_elapsed_ms"] = round((time.perf_counter_ns() - started) / 1_000_000, 3)
    return base


__all__ = ["MAIN_REQUEST", "PAGE_LIMIT", "TASKS", "public_task", "select_target", "task_by_id"]
