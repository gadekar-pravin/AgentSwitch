"""Read-only MCP adapter, call logging, observations, and safe persistence."""

import json
import os
import re
import time
from collections.abc import Mapping
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

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        call_arguments = {} if arguments is None else dict(arguments)
        entry, started = self._entry("call_tool", name, call_arguments)
        try:
            result = self.client.call_tool(name, call_arguments)
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
    "observations_from_calls",
    "redact",
    "safe_json",
    "write_exclusive",
]
