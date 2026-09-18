"""Committed harness tasks and data-driven target selectors."""

import json
import time
from collections.abc import Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentswitch.mcp_client import ProtocolError

from .recorder import ReadOnlyTools
from .rules import expected_observed_code, parse_date, selector_eligible

PAGE_LIMIT = 1000
TASKS_PATH = Path(__file__).with_name("tasks.jsonl")
REQUEST_KINDS = frozenset({"work_order_lateness", "stock_ledger"})
_TASK_KEYS = frozenset(
    {
        "id",
        "request",
        "request_kind",
        "selector",
        "expected",
        "brief_refusal",
        "reschedule",
        "writes",
        "expectation",
    }
)


class TaskFileError(ValueError):
    """The harness task data file is unreadable or invalid."""


def _validate_task(task: Any, line_number: int) -> dict[str, Any]:
    if not isinstance(task, dict):
        raise TaskFileError(f"line {line_number}: task must be an object")
    keys = set(task)
    if keys != _TASK_KEYS:
        missing = sorted(_TASK_KEYS - keys)
        unknown = sorted(keys - _TASK_KEYS)
        details = []
        if missing:
            details.append(f"missing keys: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown keys: {', '.join(unknown)}")
        raise TaskFileError(f"line {line_number}: {'; '.join(details)}")
    for field in ("id", "request", "request_kind", "selector", "expectation"):
        if not isinstance(task[field], str) or not task[field]:
            raise TaskFileError(f"line {line_number}: {field} must be a non-empty string")
    for field in ("brief_refusal", "reschedule", "writes"):
        if not isinstance(task[field], bool):
            raise TaskFileError(f"line {line_number}: {field} must be a boolean")
    expected = task["expected"]
    outcome = expected.get("outcome") if isinstance(expected, dict) else None
    if not isinstance(outcome, str) or outcome not in {"answered", "refused"}:
        raise TaskFileError(
            f"line {line_number}: expected must be an object with outcome answered or refused"
        )
    if task["selector"] not in SELECTORS:
        raise TaskFileError(f"line {line_number}: unknown selector {task['selector']!r}")
    if task["request_kind"] not in REQUEST_KINDS:
        raise TaskFileError(f"line {line_number}: unknown request_kind {task['request_kind']!r}")
    return task


def load_tasks(path: Path = TASKS_PATH) -> tuple[dict[str, Any], ...]:
    """Load and validate harness tasks from a JSON Lines file."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise TaskFileError(f"could not read task file {path}: {error}") from None

    tasks: list[dict[str, Any]] = []
    ids: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError as error:
            raise TaskFileError(f"line {line_number}: invalid JSON: {error.msg}") from None
        task = _validate_task(decoded, line_number)
        task_id = task["id"]
        if task_id in ids:
            raise TaskFileError(f"line {line_number}: duplicate id {task_id!r}")
        ids.add(task_id)
        tasks.append(task)
    if not tasks:
        raise TaskFileError("task file must contain at least one task")
    return tuple(tasks)


def task_by_id(tasks: Sequence[dict[str, Any]], task_id: str) -> dict[str, Any]:
    for task in tasks:
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
    first_total: int | None = None
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
        if first_total is None:
            first_total = total
        elif total != first_total:
            return records, {"complete": False, "reason": "total_changed", "totals": totals}
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


SELECTORS = frozenset(
    {
        "none",
        "fresh_uuid",
        "late_open_oldest",
        "late_with_sales_order",
        "late_with_cause",
        "completed_most_recent",
        "own_draft_fixture",
    }
)


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


__all__ = [
    "PAGE_LIMIT",
    "REQUEST_KINDS",
    "SELECTORS",
    "TASKS_PATH",
    "TaskFileError",
    "load_tasks",
    "public_task",
    "select_target",
    "task_by_id",
]
