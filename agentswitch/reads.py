"""Shared MCP read workers for AgentSwitch agents."""

from __future__ import annotations

import copy
import json
from datetime import date
from typing import Any

from .answer import Store, build_raw, is_hashable
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

RESULT_CHARACTER_LIMIT = 60_000


def project_value(value: Any, *, entity: str | None = None) -> Any:
    if isinstance(value, list):
        return [project_value(item, entity=entity) for item in value]
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
        projected[key] = project_value(item)
    return projected


def render_list_result(
    rows: list[dict[str, Any]],
    *,
    entity: str,
    total: int,
    complete: bool,
    character_limit: int = RESULT_CHARACTER_LIMIT,
) -> dict[str, Any]:
    projected = [project_value(row, entity=entity) for row in rows]
    result: dict[str, Any] = {
        "data": projected,
        "total": total,
        "returned": len(projected),
        "complete": complete,
    }
    if len(json.dumps(result, ensure_ascii=False, default=str)) <= character_limit:
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
        if len(json.dumps(candidate, ensure_ascii=False, default=str)) > character_limit:
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


def render_get_result(
    structured: dict[str, Any],
    *,
    entity: str,
    character_limit: int = RESULT_CHARACTER_LIMIT,
) -> dict[str, Any]:
    projected = project_value(structured, entity=entity)
    result = {"ok": True, "record": projected}
    serialized = json.dumps(result, ensure_ascii=False, default=str)
    if len(serialized) <= character_limit:
        return result
    excerpt = serialized[: max(0, character_limit - 500)]
    while True:
        truncated = {
            "ok": True,
            "record_excerpt": excerpt,
            "truncated": True,
            "full_record_held_by_code": True,
            "message": "Display truncated; code holds the full record for classification.",
        }
        excess = len(json.dumps(truncated, ensure_ascii=False, default=str)) - character_limit
        if excess <= 0 or not excerpt:
            return truncated
        excerpt = excerpt[: max(0, len(excerpt) - excess)]


def render_endpoint_result(
    structured: Any,
    *,
    character_limit: int = RESULT_CHARACTER_LIMIT,
) -> Any:
    projected = project_value(structured)
    if len(json.dumps(projected, ensure_ascii=False, default=str)) <= character_limit:
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
        if len(json.dumps(candidate, ensure_ascii=False, default=str)) > character_limit:
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


def error_type(error: Exception) -> str:
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


def error_result(error: Exception) -> dict[str, Any]:
    detail: dict[str, Any] = {"type": error_type(error), "message": str(error)}
    if isinstance(error, TransportError):
        detail["outcome_unknown"] = error.outcome_unknown
    return {"ok": False, "error": detail}


def call_list(
    tools: Any,
    store: Store,
    tool: str,
    filters: dict[str, Any],
    *,
    character_limit: int = RESULT_CHARACTER_LIMIT,
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
    return render_list_result(
        rows,
        entity=tool.rsplit(".", 1)[0],
        total=total,
        complete=complete,
        character_limit=character_limit,
    )


def call_read(
    tools: Any,
    store: Store,
    tool: str,
    arguments: dict[str, Any],
    *,
    character_limit: int = RESULT_CHARACTER_LIMIT,
) -> dict[str, Any]:
    if tool.endswith(".list"):
        filters = {key: value for key, value in arguments.items() if key not in {"limit", "offset"}}
        return {
            "ok": True,
            **call_list(
                tools,
                store,
                tool,
                filters,
                character_limit=character_limit,
            ),
        }
    result = tools.call_tool(tool, arguments, allow_write=False)
    structured = result.structured
    if tool.endswith(".get"):
        if not isinstance(structured, dict):
            raise ProtocolError(f"{tool} structured result must be a record object")
        store.add_get(tool, arguments, structured, model_read=True)
        return render_get_result(
            structured,
            entity=tool.rsplit(".", 1)[0],
            character_limit=character_limit,
        )
    if (
        not isinstance(structured, dict)
        or structured.get("status") != "ok"
        or not isinstance(structured.get("result"), dict)
    ):
        raise ProtocolError(
            f"{tool} structured result must contain status 'ok' and a result object"
        )
    store.endpoints.setdefault(tool, []).append(copy.deepcopy(structured))
    store.endpoint_calls.append({"tool": tool, "arguments": copy.deepcopy(arguments)})
    return {
        "ok": True,
        "result": render_endpoint_result(
            structured,
            character_limit=character_limit,
        ),
    }


class RecordingProxy:
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


def guarded_reschedule(
    tools: Any,
    store: Store,
    target_id: str,
    *,
    today: date,
    own_user_id: str | None,
    allow_write: bool,
) -> dict[str, Any]:
    store.pin_target_before_reschedule(target_id)
    causes = build_raw(store, target_id, today=today)["causes"]
    return reschedule(
        RecordingProxy(tools, store),
        target_id,
        today=today,
        own_user_id=own_user_id,
        causes=causes,
        allow_write=allow_write,
    )


__all__ = [
    "RESULT_CHARACTER_LIMIT",
    "RecordingProxy",
    "call_list",
    "call_read",
    "error_result",
    "error_type",
    "guarded_reschedule",
    "project_value",
    "render_endpoint_result",
    "render_get_result",
    "render_list_result",
]
