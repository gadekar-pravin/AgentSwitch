"""LLM-directed, MCP-observed production investigation agent."""

from __future__ import annotations

import argparse
import copy
import json
from datetime import date
from pathlib import Path
from typing import Any

from . import config, economics, llm_client, mcp_client
from .answer import (
    Store,
    build_raw,
    coverage,
    is_hashable,
    project_answer,
    read_requirements,
    refusal,
)
from .investigate import PAGE_LIMIT
from .mcp_client import (
    ArgumentError,
    InvalidParams,
    PermissionDenied,
    ProtocolError,
    ToolError,
    ToolNotFound,
    TransportError,
    WriteNotAllowed,
)
from .reschedule import reschedule

ALLOWED_MCP_TOOLS = (
    "WorkOrder.get",
    "WorkOrder.list",
    "MaterialRequest.list",
    "SubcontractOrder.list",
    "JobCard.list",
    "DowntimeEntry.list",
    "QualityInspection.list",
    "EngineeringChangeOrder.list",
    "BOM.get",
    "BOM.list",
    "Workstation.list",
    "SalesOrder.get",
    "Item.get",
    "endpoint.manufacturing.finite_schedule",
)
LIST_TOOL_FILTERS = {
    "WorkOrder.list": ("bom_id", "status", "item_id", "sales_order_id"),
    "MaterialRequest.list": ("work_order_id",),
    "SubcontractOrder.list": ("work_order_id",),
    "JobCard.list": ("work_order_id",),
    "DowntimeEntry.list": ("work_order_id", "job_card_id"),
    "QualityInspection.list": ("reference_type", "reference_id"),
    "EngineeringChangeOrder.list": (),
    "BOM.list": (),
    "Workstation.list": (),
}
_RESULT_CHARACTER_LIMIT = 60_000
_REFUSAL_REASONS = {"not_found", "outside_seat", "unsupported", "source_unavailable"}

SYSTEM_PROMPT = """You are the Production-seat agent for the manufacturing app. Today is {today}; the supplied target work-order id is {target_id}.
Use the native tools to investigate the user's request. WorkOrder is the target and possible consumers; MaterialRequest, SubcontractOrder, JobCard, DowntimeEntry, QualityInspection, EngineeringChangeOrder, BOM, Workstation, SalesOrder, Item, and finite_schedule provide cause or downstream evidence. Read paginated sources completely; code paginates list calls, so send only filters you mean. A read counts only with exactly the filters needed (target-linked or none); add no placeholder values. List filters are optional; send only the filters shown and only with real values; unfiltered reads are expected for ECOs, BOMs and workstations. Code computes lateness, causes, and downstream claims only from records you read. Status comes first: draft, completed, and cancelled work orders are never late. Shared-BOM material links identify only potential downstream consumers.
Refuse with not_found only when the target does not exist; outside_seat when needed data or actions are absent from the seat catalogue; unsupported when the request cannot be supported by available data; source_unavailable when an offered source fails. The full seat catalogue is: {catalogue}.
reschedule_work_order is the only way to change anything; code chooses dates and decides whether writing is allowed. Call finish with short prose when done. For a complete lateness answer, read the target, linked cause sources, all ECOs/BOMs/workstations, the linked BOM and sales order, each consumer BOM's work orders, and finite_schedule."""


class AgentIncomplete(Exception):
    """The model exhausted its call budget without completing the run."""

    def __init__(self, message: str, agent: dict[str, Any] | None = None) -> None:
        self.agent = agent or {}
        self.transcript = self.agent.get("transcript", [])
        super().__init__(message)


class AgentError(Exception):
    """An unexpected run failure carrying the agent's auditable partial state."""

    def __init__(self, error: Exception, agent: dict[str, Any]) -> None:
        self.original_type = type(error).__name__
        self.original_error = error
        self.agent = agent
        self.transcript = agent.get("transcript", [])
        super().__init__(f"{self.original_type}: {error}")


def _native_name(name: str) -> str:
    if "__" in name:
        raise ValueError(f"MCP tool name {name!r} cannot be mapped reversibly")
    mapped = name.replace(".", "__")
    if len(mapped) > 64:
        raise ValueError(f"MCP tool name {name!r} exceeds the native-function name limit")
    return mapped


def _strip_defaults(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_defaults(item) for key, item in value.items() if key != "default"}
    if isinstance(value, list):
        return [_strip_defaults(item) for item in value]
    return value


def build_tool_menu(
    tools: Any,
) -> tuple[list[dict[str, Any]], dict[str, str], list[str]]:
    """Build the safe native-function menu from the live seat catalogue."""
    catalogue = tools.list_tools()
    catalogue_names = [
        item.get("name") for item in catalogue if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]
    allowed = set(ALLOWED_MCP_TOOLS)
    functions: list[dict[str, Any]] = []
    native_to_mcp: dict[str, str] = {}
    for tool in catalogue:
        if not isinstance(tool, dict):
            continue
        original = tool.get("name")
        if not isinstance(original, str) or original not in allowed:
            continue
        annotations = tool.get("annotations")
        if (
            not isinstance(annotations, dict)
            or annotations.get("readOnlyHint") is not True
            or annotations.get("destructiveHint") is True
        ):
            continue
        native = _native_name(original)
        if native in native_to_mcp:
            raise ValueError(f"Native tool-name collision for {original!r}")
        schema = _strip_defaults(copy.deepcopy(tool.get("inputSchema", {})))
        if not isinstance(schema, dict):
            raise ProtocolError(f"Tool {original!r} has no valid inputSchema")
        if original.endswith(".list"):
            catalogue_properties = schema.get("properties")
            if not isinstance(catalogue_properties, dict):
                catalogue_properties = {}
            schema = {
                "type": "object",
                "properties": {
                    name: catalogue_properties[name]
                    for name in LIST_TOOL_FILTERS[original]
                    if name in catalogue_properties
                },
                "additionalProperties": False,
            }
        description = tool.get("description")
        functions.append(
            {
                "type": "function",
                "function": {
                    "name": native,
                    "description": description.strip() if isinstance(description, str) else "",
                    "parameters": schema,
                },
            }
        )
        native_to_mcp[native] = original

    functions.extend(
        [
            {
                "type": "function",
                "function": {
                    "name": "reschedule_work_order",
                    "description": "Guardedly propose or apply a reschedule for the supplied target only.",
                    "parameters": {
                        "type": "object",
                        "properties": {"work_order_id": {"type": "string"}},
                        "required": ["work_order_id"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "finish",
                    "description": "Finish with an answer or a principled refusal.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "outcome": {"type": "string", "enum": ["answered", "refused"]},
                            "refusal_reason": {
                                "anyOf": [
                                    {
                                        "type": "string",
                                        "enum": sorted(_REFUSAL_REASONS),
                                    },
                                    {"type": "null"},
                                ]
                            },
                            "prose": {"type": "string"},
                        },
                        "required": ["outcome", "refusal_reason", "prose"],
                        "additionalProperties": False,
                    },
                },
            },
        ]
    )
    return functions, native_to_mcp, catalogue_names


def _list_filter_schemas_by_native(
    functions: list[dict[str, Any]], native_to_mcp: dict[str, str]
) -> dict[str, dict[str, Any]]:
    exposed: dict[str, dict[str, Any]] = {}
    for item in functions:
        function = item.get("function")
        if not isinstance(function, dict):
            continue
        native = function.get("name")
        if not isinstance(native, str):
            continue
        original = native_to_mcp.get(native)
        if original is None or not original.endswith(".list"):
            continue
        parameters = function.get("parameters")
        properties = parameters.get("properties") if isinstance(parameters, dict) else None
        if not isinstance(properties, dict):
            raise ProtocolError(f"Tool {original!r} has no valid exposed filter schema")
        exposed[native] = copy.deepcopy(properties)
    return exposed


def _declared_enum(schema: Any) -> list[Any] | None:
    if not isinstance(schema, dict):
        return None
    values = schema.get("enum")
    if isinstance(values, list):
        return values
    for keyword in ("anyOf", "oneOf", "allOf"):
        branches = schema.get(keyword)
        if not isinstance(branches, list):
            continue
        for branch in branches:
            declared = _declared_enum(branch)
            if declared is not None:
                return declared
    return None


def _project_value(value: Any, *, entity: str | None = None) -> Any:
    if isinstance(value, list):
        return [_project_value(item, entity=entity) for item in value]
    if not isinstance(value, dict):
        return value
    projected: dict[str, Any] = {}
    for key, item in value.items():
        if key.startswith("_"):
            continue
        if entity == "BOM" and key == "operations":
            continue
        if entity == "BOM" and key == "materials":
            projected[key] = (
                [material.get("item_id") for material in item if isinstance(material, dict)]
                if isinstance(item, list)
                else []
            )
            continue
        projected[key] = _project_value(item)
    return projected


def _render_list_result(
    rows: list[dict[str, Any]],
    *,
    entity: str,
    total: int,
    complete: bool,
) -> dict[str, Any]:
    projected = [_project_value(row, entity=entity) for row in rows]
    result: dict[str, Any] = {
        "data": projected,
        "total": total,
        "returned": len(projected),
        "complete": complete,
    }
    if len(json.dumps(result, ensure_ascii=False, default=str)) <= _RESULT_CHARACTER_LIMIT:
        return result
    kept: list[Any] = []
    for row in projected:
        candidate = {
            "data": [*kept, row],
            "total": total,
            "returned": len(kept) + 1,
            "complete": complete,
            "truncated": True,
            "full_returned": len(projected),
            "code_holds_all_rows": complete,
        }
        if len(json.dumps(candidate, ensure_ascii=False, default=str)) > _RESULT_CHARACTER_LIMIT:
            break
        kept.append(row)
    return {
        "data": kept,
        "total": total,
        "returned": len(kept),
        "complete": complete,
        "truncated": True,
        "full_returned": len(projected),
        "code_holds_all_rows": complete,
    }


def _render_get_result(structured: dict[str, Any], *, entity: str) -> dict[str, Any]:
    projected = _project_value(structured, entity=entity)
    result = {"ok": True, "record": projected}
    serialized = json.dumps(result, ensure_ascii=False, default=str)
    if len(serialized) <= _RESULT_CHARACTER_LIMIT:
        return result
    excerpt = serialized[: _RESULT_CHARACTER_LIMIT - 500]
    while True:
        truncated = {
            "ok": True,
            "record_excerpt": excerpt,
            "truncated": True,
            "full_record_held_by_code": True,
            "message": "Display truncated; code holds the full record for classification.",
        }
        excess = (
            len(json.dumps(truncated, ensure_ascii=False, default=str))
            - _RESULT_CHARACTER_LIMIT
        )
        if excess <= 0:
            return truncated
        excerpt = excerpt[: max(0, len(excerpt) - excess)]


def _render_endpoint_result(structured: Any) -> Any:
    projected = _project_value(structured)
    if len(json.dumps(projected, ensure_ascii=False, default=str)) <= _RESULT_CHARACTER_LIMIT:
        return projected
    if not isinstance(projected, dict):
        return {"truncated": True, "complete": True, "result": None}
    inner = projected.get("result")
    rows = inner.get("orders") if isinstance(inner, dict) else None
    if not isinstance(rows, list):
        return {"truncated": True, "complete": True, "result": None}
    kept: list[Any] = []
    for row in rows:
        candidate = copy.deepcopy(projected)
        candidate_inner = candidate["result"]
        candidate_inner["orders"] = [*kept, row]
        candidate_inner.update(
            {
                "total": len(rows),
                "returned": len(kept) + 1,
                "complete": True,
                "truncated": True,
            }
        )
        if len(json.dumps(candidate, ensure_ascii=False, default=str)) > _RESULT_CHARACTER_LIMIT:
            break
        kept.append(row)
    rendered = copy.deepcopy(projected)
    rendered_inner = rendered["result"]
    rendered_inner["orders"] = kept
    rendered_inner.update(
        {
            "total": len(rows),
            "returned": len(kept),
            "complete": True,
            "truncated": True,
        }
    )
    return rendered


def _error_type(error: Exception) -> str:
    if isinstance(error, InvalidParams):
        return "not_found" if "not found" in error.message.lower() else "invalid_params"
    if isinstance(error, ArgumentError):
        return "invalid_params"
    if isinstance(error, PermissionDenied):
        return "permission_denied"
    if isinstance(error, WriteNotAllowed):
        return "write_not_allowed"
    if isinstance(error, TransportError):
        return "transport"
    if isinstance(error, (ToolError, ToolNotFound, ProtocolError)):
        return "tool_error"
    return "tool_error"


def _error_result(error: Exception) -> dict[str, Any]:
    detail: dict[str, Any] = {"type": _error_type(error), "message": str(error)}
    if isinstance(error, TransportError):
        detail["outcome_unknown"] = error.outcome_unknown
    return {"ok": False, "error": detail}


def _call_list(
    tools: Any,
    store: Store,
    tool: str,
    filters: dict[str, Any],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    seen_ids: set[Any] = set()
    offset = 0
    total = 0
    complete = False
    try:
        while True:
            arguments = dict(filters)
            arguments.update({"limit": PAGE_LIMIT, "offset": offset})
            result = tools.call_tool(tool, arguments, allow_write=False)
            envelope = result.structured
            if not isinstance(envelope, dict):
                raise ProtocolError(f"{tool} structured result must be a list envelope object")
            page = envelope.get("data")
            total = envelope.get("total")
            if (
                not isinstance(page, list)
                or any(not isinstance(row, dict) for row in page)
                or not isinstance(total, int)
                or isinstance(total, bool)
                or total < 0
            ):
                raise ProtocolError(f"{tool} returned an invalid list envelope")
            if not page and len(rows) < total:
                break
            added = 0
            for row in page:
                identifier = row.get("id")
                hashable_identifier = identifier is not None and is_hashable(identifier)
                if hashable_identifier and identifier in seen_ids:
                    continue
                if hashable_identifier:
                    seen_ids.add(identifier)
                rows.append(row)
                added += 1
            if len(rows) >= total:
                complete = True
                break
            if added == 0:
                break
            new_offset = offset + len(page)
            if new_offset <= offset:
                break
            offset = new_offset
    except Exception:
        store.add_list(tool, filters, rows, complete=False)
        raise
    store.add_list(tool, filters, rows, complete=complete)
    return _render_list_result(
        rows,
        entity=tool.rsplit(".", 1)[0],
        total=total,
        complete=complete,
    )


def _call_read(
    tools: Any,
    store: Store,
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if tool.endswith(".list"):
        filters = {key: value for key, value in arguments.items() if key not in {"limit", "offset"}}
        return {"ok": True, **_call_list(tools, store, tool, filters)}
    result = tools.call_tool(tool, arguments, allow_write=False)
    structured = result.structured
    if tool.endswith(".get"):
        if not isinstance(structured, dict):
            raise ProtocolError(f"{tool} structured result must be a record object")
        store.add_get(tool, arguments, structured, model_read=True)
        return _render_get_result(structured, entity=tool.rsplit(".", 1)[0])
    if (
        not isinstance(structured, dict)
        or structured.get("status") != "ok"
        or not isinstance(structured.get("result"), dict)
    ):
        raise ProtocolError(
            f"{tool} structured result must contain status 'ok' and a result object"
        )
    store.endpoints.setdefault(tool, []).append(copy.deepcopy(structured))
    store.endpoint_calls.append(
        {"tool": tool, "arguments": copy.deepcopy(arguments)}
    )
    return {"ok": True, "result": _render_endpoint_result(structured)}


def _missing_read_calls(store: Store, target_id: str | None) -> list[str]:
    calls: list[str] = []
    for _name, tool, arguments, read in read_requirements(store, target_id):
        if read:
            continue
        native = _native_name(tool)
        if tool == "endpoint.manufacturing.finite_schedule" and not arguments:
            calls.append(f"{native} (call with its required arguments)")
            continue
        rendered = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        calls.append(f"{native} {rendered}")
    return calls


class _RecordingProxy:
    def __init__(self, tools: Any, store: Store) -> None:
        self.tools = tools
        self.store = store

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        allow_write: bool = False,
    ) -> Any:
        call_arguments = {} if arguments is None else dict(arguments)
        result = self.tools.call_tool(name, call_arguments, allow_write=allow_write)
        if name.endswith(".get") and isinstance(result.structured, dict):
            self.store.add_get(name, call_arguments, result.structured, model_read=False)
        return result


def _decode_arguments(function: dict[str, Any]) -> dict[str, Any]:
    raw = function.get("arguments", "{}")
    if isinstance(raw, dict):
        arguments = raw
    elif isinstance(raw, str):
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(f"arguments are not valid JSON: {error.msg}") from None
    else:
        raise ValueError("arguments must be a JSON object")
    if not isinstance(arguments, dict):
        raise ValueError("arguments must decode to an object")
    return arguments


def _usage_summary(calls: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, int | float] = {}
    for call in calls:
        usage = call.get("usage")
        if not isinstance(usage, dict):
            continue
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value
    return {"calls": calls, "totals": totals}


_MISSING = object()


def _received(value: Any) -> str:
    if value is _MISSING:
        return "missing value"
    if value is None:
        return "null value null"
    if isinstance(value, bool):
        kind = "boolean"
    elif isinstance(value, str):
        kind = "string"
    elif isinstance(value, int):
        kind = "integer"
    elif isinstance(value, float):
        kind = "number"
    elif isinstance(value, dict):
        kind = "object"
    elif isinstance(value, list):
        kind = "array"
    else:
        kind = type(value).__name__
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        rendered = repr(value)
    return f"{kind} value {rendered}"


def _invalid_finish_message(
    key: str,
    value: Any,
    *,
    problem: str,
    allowed: str,
) -> str:
    return (
        f"Invalid finish argument {key!r}: received {_received(value)}; {problem}. "
        f"Allowed: {allowed}. For outcome 'answered', refusal_reason must be null; "
        "a missing refusal_reason key or the literal string 'null' is treated as null."
    )


def _invalid_list_filter_message(
    original: str,
    arguments: dict[str, Any],
    schemas: dict[str, Any],
) -> str | None:
    unsupported = [key for key in arguments if key not in schemas]
    if unsupported:
        allowed_display = ", ".join(sorted(schemas)) or "(none)"
        unsupported_display = ", ".join(sorted(repr(key) for key in unsupported))
        return (
            f"Invalid filters for {original}: unsupported key(s) {unsupported_display}. "
            f"Allowed filters: {allowed_display}."
        )
    for key, value in arguments.items():
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return (
                    f"Invalid filter {key!r} for {original}: the value is empty or "
                    "whitespace-only; omit the filter or provide a real value."
                )
            if stripped.startswith(":"):
                return (
                    f"Invalid filter {key!r} for {original}: {value!r} starts with ':' "
                    "and is a placeholder, not a real filter value."
                )
        if isinstance(key, str) and key.endswith("_id") and not isinstance(value, str):
            return (
                f"Invalid filter {key!r} for {original}: received {_received(value)}; "
                "filters ending in '_id' require a string value."
            )
        if key in {"status", "reference_type"}:
            declared = _declared_enum(schemas[key])
            if declared is not None and value not in declared:
                rendered = json.dumps(declared, ensure_ascii=False, default=str)
                return (
                    f"Invalid filter {key!r} for {original}: received {_received(value)}; "
                    f"the catalogue schema allows only {rendered}."
                )
    return None


def run_agent(
    tools: Any,
    llm: Any,
    *,
    request: str,
    target_id: str | None,
    today: date,
    own_user_id: str | None,
    allow_write: bool,
    max_turns: int = 30,
) -> dict[str, Any]:
    """Run the bounded native-tool loop and classify only agent-read records."""
    if max_turns < 1:
        raise ValueError("max_turns must be positive")
    transcript: list[dict[str, Any]] = []
    store = Store()
    usage_calls: list[dict[str, Any]] = []
    repairs: list[str] = []
    reschedule_result: dict[str, Any] | None = None
    reschedule_failure: dict[str, Any] | None = None
    reschedule_attempted = False
    reschedule_invoked = False
    last_model = getattr(llm, "model", None)
    warned = False
    completed_turns = 0
    contradiction_repair_issued = False
    coverage_repair_issued = False

    def partial_state() -> dict[str, Any]:
        return {
            "transcript": copy.deepcopy(transcript),
            "usage": _usage_summary(usage_calls),
            "model": last_model,
            "turns": completed_turns,
            "repairs": list(repairs),
            "coverage": coverage(store, target_id, rescheduled=reschedule_invoked),
        }

    def tool_message(
        tool_call: Any, index: int, turn: int, result: dict[str, Any]
    ) -> dict[str, Any]:
        call_id = tool_call.get("id") if isinstance(tool_call, dict) else None
        function = tool_call.get("function") if isinstance(tool_call, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        return {
            "role": "tool",
            "tool_call_id": call_id if isinstance(call_id, str) else f"turn-{turn}-{index}",
            "name": name if isinstance(name, str) else "unknown",
            "content": json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str),
        }

    def execute_non_finish(tool_call: Any) -> dict[str, Any]:
        nonlocal reschedule_attempted, reschedule_failure, reschedule_invoked, reschedule_result
        function = tool_call.get("function") if isinstance(tool_call, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        try:
            if not isinstance(name, str):
                raise ValueError("tool call has no function name")
            arguments = _decode_arguments(function)
            if name in native_to_mcp:
                original = native_to_mcp[name]
                if original.endswith(".list"):
                    filter_error = _invalid_list_filter_message(
                        original, arguments, exposed_list_filter_schemas[name]
                    )
                    if filter_error is not None:
                        return {
                            "ok": False,
                            "error": {
                                "type": "invalid_params",
                                "message": filter_error,
                            },
                        }
                return _call_read(tools, store, original, arguments)
            if name != "reschedule_work_order":
                return {
                    "ok": False,
                    "error": {
                        "type": "unknown_function",
                        "message": f"Unknown function {name!r}",
                    },
                }
            supplied_id = arguments.get("work_order_id")
            if set(arguments) != {"work_order_id"} or not isinstance(supplied_id, str):
                return {
                    "ok": False,
                    "error": {
                        "type": "invalid_params",
                        "message": "work_order_id must be the only argument",
                    },
                }
            if target_id is None or supplied_id != target_id:
                return {
                    "ok": False,
                    "error": {
                        "type": "wrong_target",
                        "message": "Only the supplied target work order may be rescheduled",
                    },
                }
            if not store.got("WorkOrder.get", target_id, model_only=True):
                return {
                    "ok": False,
                    "error": {
                        "type": "target_read_required",
                        "message": (
                            "Read the target with WorkOrder.get before calling "
                            "reschedule_work_order"
                        ),
                    },
                }
            reschedule_invoked = True
            if reschedule_failure is not None:
                return copy.deepcopy(reschedule_failure)
            if not reschedule_attempted:
                reschedule_attempted = True
                try:
                    store.pin_target_before_reschedule(target_id)
                    causes = build_raw(store, target_id, today=today)["causes"]
                    reschedule_result = reschedule(
                        _RecordingProxy(tools, store),
                        target_id,
                        today=today,
                        own_user_id=own_user_id,
                        causes=causes,
                        allow_write=allow_write,
                    )
                except Exception as error:
                    reschedule_failure = _error_result(error)
                    return copy.deepcopy(reschedule_failure)
            if reschedule_result is None:
                raise RuntimeError("reschedule attempt produced no result")
            return {
                "ok": True,
                **{
                    key: reschedule_result.get(key)
                    for key in ("action", "reason", "proposed", "applied", "notes")
                },
            }
        except ValueError as error:
            return {
                "ok": False,
                "error": {"type": "invalid_arguments", "message": str(error)},
            }
        except Exception as error:
            return _error_result(error)

    def evaluate_finish(tool_call: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
        nonlocal contradiction_repair_issued, coverage_repair_issued
        function = tool_call.get("function") if isinstance(tool_call, dict) else None
        if not isinstance(function, dict):
            return (
                {
                    "ok": False,
                    "error": {
                        "type": "invalid_params",
                        "message": _invalid_finish_message(
                            "function",
                            function if function is not None else _MISSING,
                            problem="a finish call requires a function object",
                            allowed="an object with outcome, optional refusal_reason, and prose",
                        ),
                    },
                },
                None,
            )
        try:
            arguments = _decode_arguments(function)
        except ValueError as error:
            raw_arguments = function.get("arguments", _MISSING)
            return (
                {
                    "ok": False,
                    "error": {
                        "type": "invalid_params",
                        "message": _invalid_finish_message(
                            "arguments",
                            raw_arguments,
                            problem=str(error),
                            allowed="a JSON object with outcome, optional refusal_reason, and prose",
                        ),
                    },
                },
                None,
            )

        allowed_keys = {"outcome", "refusal_reason", "prose"}
        unexpected = [key for key in arguments if key not in allowed_keys]
        if unexpected:
            key = sorted(unexpected, key=repr)[0]
            return (
                {
                    "ok": False,
                    "error": {
                        "type": "invalid_params",
                        "message": _invalid_finish_message(
                            str(key),
                            arguments[key],
                            problem="the key is not allowed",
                            allowed="only the keys 'outcome', 'refusal_reason', and 'prose'",
                        ),
                    },
                },
                None,
            )
        outcome = arguments.get("outcome")
        if not isinstance(outcome, str) or outcome not in {"answered", "refused"}:
            return (
                {
                    "ok": False,
                    "error": {
                        "type": "invalid_params",
                        "message": _invalid_finish_message(
                            "outcome",
                            arguments.get("outcome", _MISSING),
                            problem="outcome must be a permitted string",
                            allowed="'answered' or 'refused'",
                        ),
                    },
                },
                None,
            )
        raw_refusal_reason = arguments.get("refusal_reason", _MISSING)
        if outcome == "answered":
            if (
                raw_refusal_reason is _MISSING
                or raw_refusal_reason is None
                or raw_refusal_reason == "null"
            ):
                refusal_reason = None
            else:
                return (
                    {
                        "ok": False,
                        "error": {
                            "type": "invalid_params",
                            "message": _invalid_finish_message(
                                "refusal_reason",
                                raw_refusal_reason,
                                problem="answered outcomes cannot carry a refusal reason",
                                allowed="null, an omitted key, or the literal string 'null'",
                            ),
                        },
                    },
                    None,
                )
        else:
            refusal_reason = raw_refusal_reason
            if not (
                isinstance(refusal_reason, str)
                and refusal_reason in _REFUSAL_REASONS
            ):
                allowed_reasons = ", ".join(repr(reason) for reason in sorted(_REFUSAL_REASONS))
                return (
                    {
                        "ok": False,
                        "error": {
                            "type": "invalid_params",
                            "message": _invalid_finish_message(
                                "refusal_reason",
                                raw_refusal_reason,
                                problem="refused outcomes require a permitted refusal reason string",
                                allowed=allowed_reasons,
                            ),
                        },
                    },
                    None,
                )
        prose = arguments.get("prose", _MISSING)
        if not isinstance(prose, str):
            return (
                {
                    "ok": False,
                    "error": {
                        "type": "invalid_params",
                        "message": _invalid_finish_message(
                            "prose",
                            prose,
                            problem="prose must be a string",
                            allowed="any string value",
                        ),
                    },
                },
                None,
            )

        target_read = target_id is not None and store.got(
            "WorkOrder.get", target_id, model_only=True
        )
        contradiction: str | None = None
        if outcome == "answered" and target_id is None:
            contradiction = "There is no supplied target id to answer about."
        elif outcome == "answered" and not target_read:
            contradiction = "An answered outcome requires a successful target WorkOrder.get."
        elif outcome == "refused" and refusal_reason == "not_found" and target_read:
            contradiction = "The target WorkOrder.get succeeded, so not_found is inconsistent."
        if contradiction is not None and not contradiction_repair_issued:
            contradiction_repair_issued = True
            repair = f"Repair the finish call. {contradiction}"
            repairs.append(repair)
            return (
                {
                    "ok": False,
                    "error": {"type": "repair_required", "message": repair},
                },
                None,
            )
        if outcome == "answered" and (target_id is None or not target_read):
            return (
                {
                    "ok": False,
                    "error": {
                        "type": "finish_conflict",
                        "message": contradiction or "A target read is required",
                    },
                },
                None,
            )

        coverage_result = coverage(store, target_id, rescheduled=reschedule_invoked)
        missing = [key for key, status in coverage_result.items() if status == "missing"]
        if outcome == "answered" and missing and not coverage_repair_issued:
            coverage_repair_issued = True
            repair = (
                "Repair the finish call. Missing answer reads: "
                + "; ".join(_missing_read_calls(store, target_id))
                + ". Only these exact arguments count; extra filters make a read non-covering"
            )
            if coverage_result["reschedule_work_order"] == "not_invoked":
                repair += ". If the request asks to reschedule, call reschedule_work_order"
            repairs.append(repair)
            return (
                {
                    "ok": False,
                    "error": {"type": "repair_required", "message": repair},
                },
                None,
            )
        accepted = {
            "outcome": outcome,
            "refusal_reason": refusal_reason if outcome == "refused" else None,
            "prose": prose,
            "coverage": coverage_result,
        }
        return {"ok": True, "accepted": True}, accepted

    try:
        menu, native_to_mcp, catalogue_names = build_tool_menu(tools)
        exposed_list_filter_schemas = _list_filter_schemas_by_native(
            menu, native_to_mcp
        )
        system = SYSTEM_PROMPT.format(
            today=today.isoformat(),
            target_id=target_id if target_id is not None else "none",
            catalogue=", ".join(catalogue_names),
        )
        transcript.extend(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": request},
            ]
        )
        for turn in range(1, max_turns + 1):
            remaining = max_turns - turn + 1
            if remaining == 2 and not warned:
                transcript.append(
                    {
                        "role": "system",
                        "content": "Two model calls remain. Complete essential reads and call finish.",
                    }
                )
                warned = True
            response = llm.chat(transcript, tools=menu, tool_choice="auto")
            message = response.get("message")
            last_model = response.get("model") or last_model
            usage_calls.append(
                {
                    "turn": turn,
                    "usage": copy.deepcopy(response.get("usage", {})),
                    "provider": response.get("provider"),
                    "model": response.get("model"),
                    "finish_reason": response.get("finish_reason"),
                }
            )
            completed_turns = turn
            if not isinstance(message, dict):
                raise AgentIncomplete("The model returned no assistant message", partial_state())
            transcript.append(copy.deepcopy(message))
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list) or not tool_calls:
                content = {
                    "ok": False,
                    "error": {
                        "type": "finish_required",
                        "message": "Call finish to complete the run.",
                    },
                }
                transcript.append(
                    {"role": "user", "content": json.dumps(content, separators=(",", ":"))}
                )
                continue

            indexed_calls = list(enumerate(tool_calls))
            finish_calls = [
                (index, call)
                for index, call in indexed_calls
                if isinstance(call, dict)
                and isinstance(call.get("function"), dict)
                and call["function"].get("name") == "finish"
            ]
            accepted: dict[str, Any] | None = None
            for index, tool_call in indexed_calls:
                if any(index == finish_index for finish_index, _ in finish_calls):
                    continue
                result = execute_non_finish(tool_call)
                transcript.append(tool_message(tool_call, index, turn, result))
            if len(finish_calls) > 1:
                for index, tool_call in finish_calls:
                    result = {
                        "ok": False,
                        "error": {
                            "type": "invalid_finish_batch",
                            "message": _invalid_finish_message(
                                "finish_call_count",
                                len(finish_calls),
                                problem="a turn must contain exactly one finish call",
                                allowed="the integer value 1",
                            ),
                        },
                    }
                    transcript.append(tool_message(tool_call, index, turn, result))
            elif finish_calls:
                index, tool_call = finish_calls[0]
                result, accepted = evaluate_finish(tool_call)
                transcript.append(tool_message(tool_call, index, turn, result))

            if accepted is not None:
                final: dict[str, Any] = {
                    "outcome": accepted["outcome"],
                    "refusal_reason": accepted["refusal_reason"],
                    "prose": accepted["prose"],
                    "reschedule": reschedule_result,
                    **partial_state(),
                    "coverage": accepted["coverage"],
                }
                if accepted["outcome"] == "answered":
                    assert target_id is not None
                    final["raw"] = build_raw(store, target_id, today=today)
                return final

        raise AgentIncomplete(
            "The agent exhausted max_turns without a valid finish call", partial_state()
        )
    except AgentIncomplete:
        raise
    except AgentError:
        raise
    except Exception as error:
        raise AgentError(error, partial_state()) from error


def _date_argument(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the AgentSwitch Production LLM agent")
    parser.add_argument("--tenant", required=True, choices=("suryodaya", "keystone"))
    parser.add_argument("--work-order", required=True)
    parser.add_argument("--request")
    parser.add_argument("--today", type=_date_argument, default=date.today())
    parser.add_argument("--allow-draft-writes", action="store_true")
    parser.add_argument(
        "--config",
        type=Path,
        default=config.DEFAULT_CONFIG_PATH,
        help="runtime TOML configuration path",
    )
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.allow_draft_writes and args.today != date.today():
        parser.error("--allow-draft-writes requires --today to equal the system date")
    try:
        resolved_config = config.load_config(args.config, env_file=".env")
    except config.ConfigError as error:
        parser.error(str(error))
    try:
        raw_llm = llm_client.from_config(resolved_config, env_file=".env")
    except ValueError as error:
        parser.error(str(error))
    client = mcp_client.from_env(args.tenant)
    llm = economics.MeteredClient(raw_llm, resolved_config)
    request = args.request or (
        "This work order is late. Find out why, tell me what it blocks downstream, "
        "and reschedule what you can."
    )
    result = run_agent(
        client,
        llm,
        request=request,
        target_id=args.work_order,
        today=args.today,
        own_user_id=client.current_user_id(),
        allow_write=args.allow_draft_writes,
    )
    if result["outcome"] == "answered":
        answer = project_answer(result["raw"], args.work_order, result.get("reschedule"))
        answer["prose"] = result["prose"]
    else:
        answer = refusal(result["refusal_reason"], args.work_order)
        answer["prose"] = result["prose"]
    print(result["prose"])
    print(json.dumps(answer, indent=2, ensure_ascii=False, default=str))
    totals = result["usage"]["totals"]
    print(
        "usage: "
        f"prompt_tokens={totals.get('prompt_tokens', 0)} "
        f"completion_tokens={totals.get('completion_tokens', 0)} "
        f"total_tokens={totals.get('total_tokens', 0)} cost={totals.get('cost', 0)}"
    )
    summary = llm.ledger()["summary"]
    print(
        "economics: "
        f"budget_usd={summary['budget_micro'] / 1_000_000:.6f} "
        f"spent_usd={summary['spent_micro'] / 1_000_000:.6f} "
        f"remaining_usd={summary['remaining_micro'] / 1_000_000:.6f} "
        f"attempts={summary['attempts']} refused={summary['refused']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ALLOWED_MCP_TOOLS",
    "LIST_TOOL_FILTERS",
    "AgentError",
    "AgentIncomplete",
    "SYSTEM_PROMPT",
    "build_tool_menu",
    "run_agent",
]
