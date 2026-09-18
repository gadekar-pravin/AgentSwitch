"""Read-only MCP adapter, call logging, observations, and safe persistence."""

import json
import os
import re
import time
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from agentswitch.mcp_client import McpClient, ToolResult, WriteNotAllowed

_BEARER_PATTERN = re.compile(r"(?i)Bearer\s+\S+")
_JSON_SECRET_PATTERN = re.compile(
    r'(?i)("[^"\\]*(?:token|password)[^"\\]*"\s*:\s*)"(?:\\.|[^"\\])*"'
)


def _secret_forms(secret: str) -> tuple[str, ...]:
    """Return raw and JSON-escaped spellings that may occur in nested text."""
    forms = {
        secret,
        json.dumps(secret)[1:-1],
        json.dumps(secret, ensure_ascii=False)[1:-1],
    }
    return tuple(sorted((form for form in forms if form), key=len, reverse=True))


class ReadOnlyTools:
    """Expose MCP reads without an allow-write escape hatch and record every access."""

    def __init__(self, client: McpClient, *, phase: str = "select") -> None:
        self.client = client
        self.phase = phase
        self.calls: list[dict[str, Any]] = []
        self.observations: dict[tuple[str, Any], list[dict[str, Any]]] = {}
        self._sequence = 0

    def _entry(self, kind: str, tool: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], int]:
        self._sequence += 1
        entry = {
            "phase": self.phase,
            "sequence": self._sequence,
            "kind": kind,
            "tool": tool,
            "arguments": dict(arguments),
        }
        self.calls.append(entry)
        return entry, time.perf_counter_ns()

    @staticmethod
    def _finish(entry: dict[str, Any], started: int, outcome: str) -> None:
        entry["outcome"] = outcome
        entry["elapsed_ms"] = round((time.perf_counter_ns() - started) / 1_000_000, 3)

    def list_tools(self) -> list[dict[str, Any]]:
        entry, started = self._entry("list_tools", "tools/list", {})
        try:
            tools = self.client.list_tools()
        except Exception as error:
            self._finish(entry, started, type(error).__name__)
            entry["error"] = str(error)
            raise
        self._finish(entry, started, "ok")
        entry["structuredContent"] = {"tools": tools}
        return tools

    def get_tool(self, name: str) -> dict[str, Any]:
        entry, started = self._entry("get_tool", name, {})
        try:
            tool = self.client.get_tool(name)
        except Exception as error:
            self._finish(entry, started, type(error).__name__)
            entry["error"] = str(error)
            raise
        self._finish(entry, started, "ok")
        entry["structuredContent"] = tool
        return tool

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        allow_write: bool = False,
    ) -> ToolResult:
        call_arguments = {} if arguments is None else dict(arguments)
        entry, started = self._entry("call_tool", name, call_arguments)
        if allow_write:
            entry["write"] = True
            error = WriteNotAllowed("The read-only harness adapter refuses write-enabled calls")
            self._finish(entry, started, "refused_write")
            entry["error"] = str(error)
            raise error
        get_tool = getattr(self.client, "get_tool", None)
        if callable(get_tool):
            try:
                tool = get_tool(name)
            except Exception as error:
                self._finish(entry, started, type(error).__name__)
                entry["error"] = str(error)
                raise
            annotations = tool.get("annotations") if isinstance(tool, dict) else None
            if not isinstance(annotations, dict) or annotations.get("readOnlyHint") is not True:
                entry["write"] = True
        try:
            result = self.client.call_tool(name, call_arguments)
        except WriteNotAllowed as error:
            entry["write"] = True
            self._finish(entry, started, "refused_write")
            entry["error"] = str(error)
            raise
        except Exception as error:
            self._finish(entry, started, type(error).__name__)
            entry["error"] = str(error)
            raise
        self._finish(entry, started, "ok")
        entry["structuredContent"] = result.structured
        if self.phase == "subject":
            self._observe(name, result.structured)
        return result

    def _observe(self, tool: str, structured: Any) -> None:
        if tool == "endpoint.manufacturing.finite_schedule":
            if not isinstance(structured, dict):
                return
            result = structured.get("result")
            rows = result.get("orders") if isinstance(result, dict) else None
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        self._add_observation("FiniteScheduleOrder", row.get("work_order_id"), row)
            return
        if "." not in tool:
            return
        entity, operation = tool.split(".", 1)
        if operation == "get" and isinstance(structured, dict):
            self._add_observation(entity, structured.get("id"), structured)
        elif operation == "list" and isinstance(structured, dict):
            rows = structured.get("data")
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        self._add_observation(entity, row.get("id"), row)

    def _add_observation(self, entity: str, identifier: Any, record: dict[str, Any]) -> None:
        if identifier is None:
            return
        self.observations.setdefault((entity, identifier), []).append(record)


class ScopedWriteTools(ReadOnlyTools):
    """Permit only date updates to one owned draft, guarded by an immediate read."""

    def __init__(
        self,
        client: McpClient,
        *,
        target_id: Any,
        own_user_id: Any,
        phase: str = "select",
        allowed_current_date_states: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(client, phase=phase)
        self.target_id = target_id
        self.own_user_id = own_user_id
        self.allowed_current_date_states = allowed_current_date_states
        self.last_guard_dates: dict[str, Any] | None = None

    @staticmethod
    def _iso_date(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            return False
        return parsed.isoformat() == value

    def _refuse(
        self,
        name: str,
        arguments: dict[str, Any],
        message: str,
        *,
        refusal_kind: str | None = None,
    ) -> None:
        entry, started = self._entry("call_tool", name, arguments)
        entry["write"] = True
        if refusal_kind is not None:
            entry["refusal_kind"] = refusal_kind
        error = WriteNotAllowed(message)
        self._finish(entry, started, "refused_write")
        entry["error"] = str(error)
        raise error

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        allow_write: bool = False,
    ) -> ToolResult:
        call_arguments = {} if arguments is None else dict(arguments)
        is_update = name == "WorkOrder.update"
        if not allow_write and not is_update:
            return super().call_tool(name, call_arguments)
        if not allow_write:
            self._refuse(name, call_arguments, "WorkOrder.update requires explicit write permission")
        allowed_keys = {"id", "planned_start_date", "planned_end_date"}
        date_keys = {"planned_start_date", "planned_end_date"} & call_arguments.keys()
        valid_scope = (
            is_update
            and call_arguments.get("id") == self.target_id
            and set(call_arguments).issubset(allowed_keys)
            and bool(date_keys)
            and all(self._iso_date(call_arguments[key]) for key in date_keys)
        )
        if not valid_scope:
            self._refuse(name, call_arguments, "Write is outside the scoped draft date update")

        original_phase = self.phase
        subject_dates: dict[str, Any] | None = None
        if original_phase == "subject":
            for call in reversed(self.calls):
                observed = call.get("structuredContent")
                if (
                    call.get("phase") == "subject"
                    and call.get("tool") == "WorkOrder.get"
                    and call.get("arguments") == {"id": self.target_id}
                    and call.get("outcome") == "ok"
                    and isinstance(observed, dict)
                ):
                    subject_dates = {
                        "planned_start_date": observed.get("planned_start_date"),
                        "planned_end_date": observed.get("planned_end_date"),
                    }
                    break
            if subject_dates is None:
                self._refuse(
                    name,
                    call_arguments,
                    "Subject must successfully read the target before updating it",
                )

        self.phase = "write_guard" if original_phase == "subject" else original_phase
        try:
            try:
                guard = super().call_tool("WorkOrder.get", {"id": self.target_id}).structured
            except Exception:
                guard = None
        finally:
            self.phase = original_phase
        self.last_guard_dates = (
            {
                "planned_start_date": guard.get("planned_start_date"),
                "planned_end_date": guard.get("planned_end_date"),
            }
            if isinstance(guard, dict)
            else None
        )
        owned = (
            isinstance(self.own_user_id, str)
            and bool(self.own_user_id)
            and isinstance(guard, dict)
            and guard.get("status") == "draft"
            and isinstance(guard.get("created_by"), str)
            and bool(guard.get("created_by"))
            and guard.get("created_by") == self.own_user_id
        )
        if not owned:
            self._refuse(name, call_arguments, "Guard read did not confirm an owned draft work order")
        allowed_date_states = (
            [subject_dates] if original_phase == "subject" else self.allowed_current_date_states
        )
        if allowed_date_states is not None and not any(
            self.last_guard_dates == state for state in allowed_date_states
        ):
            self._refuse(
                name,
                call_arguments,
                "Guard read found a planned date mismatch with the allowed current states",
                refusal_kind="date_mismatch",
            )

        entry, started = self._entry("call_tool", name, call_arguments)
        entry["write"] = True
        try:
            result = self.client.call_tool(name, call_arguments, allow_write=True)
        except WriteNotAllowed as error:
            self._finish(entry, started, "refused_write")
            entry["error"] = str(error)
            raise
        except Exception as error:
            self._finish(entry, started, type(error).__name__)
            entry["error"] = str(error)
            raise
        self._finish(entry, started, "ok")
        entry["structuredContent"] = result.structured
        return result


def observations_from_calls(
    calls: list[dict[str, Any]],
) -> dict[tuple[str, Any], list[dict[str, Any]]]:
    """Rebuild subject observations from the persisted harness log."""
    observations: dict[tuple[str, Any], list[dict[str, Any]]] = {}

    def add(entity: str, identifier: Any, record: dict[str, Any]) -> None:
        if identifier is not None:
            observations.setdefault((entity, identifier), []).append(record)

    for call in calls:
        if call.get("phase") != "subject" or call.get("outcome") != "ok":
            continue
        tool = call.get("tool")
        structured = call.get("structuredContent")
        if tool == "endpoint.manufacturing.finite_schedule" and isinstance(structured, dict):
            result = structured.get("result")
            rows = result.get("orders") if isinstance(result, dict) else None
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        add("FiniteScheduleOrder", row.get("work_order_id"), row)
            continue
        if not isinstance(tool, str) or "." not in tool:
            continue
        entity, operation = tool.split(".", 1)
        if operation == "get" and isinstance(structured, dict):
            add(entity, structured.get("id"), structured)
        elif operation == "list" and isinstance(structured, dict):
            rows = structured.get("data")
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        add(entity, row.get("id"), row)
    return observations


def redact(value: Any, secrets: tuple[str, ...]) -> Any:
    """Recursively redact explicit secrets plus bearer and JSON secret patterns."""
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            for form in _secret_forms(secret):
                redacted = redacted.replace(form, "<redacted>")
        redacted = _BEARER_PATTERN.sub("Bearer <redacted>", redacted)
        return _JSON_SECRET_PATTERN.sub(r'\1"<redacted>"', redacted)
    if isinstance(value, Mapping):
        return {str(redact(key, secrets)): redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [redact(item, secrets) for item in value]
    return value


def safe_json(value: Any, secrets: tuple[str, ...]) -> str:
    """Serialize a redacted object and reject any remaining explicit secret."""
    rendered = json.dumps(redact(value, secrets), indent=2, sort_keys=True, ensure_ascii=False)
    for secret in secrets:
        forms = _secret_forms(secret)
        nested_forms = {
            json.dumps(form, ensure_ascii=False)[1:-1]
            for form in forms
        }
        if any(form in rendered for form in (*forms, *nested_forms) if form):
            raise ValueError("Refusing to persist a record containing an unredacted secret")
    return f"{rendered}\n"


def write_exclusive(
    path: Path,
    value: Any,
    secrets: tuple[str, ...],
    *,
    filename_field: str | None = None,
) -> Path:
    """Durably create a JSON file, suffixing collisions without overwriting."""
    candidate = path
    counter = 2
    while True:
        try:
            stream = candidate.open("x", encoding="utf-8")
        except FileExistsError:
            candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
            counter += 1
            continue
        try:
            with stream:
                persisted_value = value
                if filename_field is not None:
                    if not isinstance(value, Mapping):
                        raise TypeError("filename_field requires a mapping value")
                    persisted_value = dict(value)
                    persisted_value[filename_field] = candidate.name
                rendered = safe_json(persisted_value, secrets)
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
            return candidate
        except Exception:
            candidate.unlink(missing_ok=True)
            raise


__all__ = [
    "ReadOnlyTools",
    "ScopedWriteTools",
    "observations_from_calls",
    "redact",
    "safe_json",
    "write_exclusive",
]
