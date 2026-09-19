"""Explicit offline transports for exercising real client infrastructure."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any
from urllib.error import URLError

from .config import Config
from .llm_client import OpenRouterClient
from .mcp_client import PROTOCOL_VERSION, McpClient


class OfflineMcpTransport:
    """Serve a fixed MCP catalogue and records without network access."""

    def __init__(
        self,
        catalogue: Sequence[dict[str, Any]],
        records: Mapping[str, Sequence[dict[str, Any]]],
        *,
        endpoints: Mapping[str, dict[str, Any]] | None = None,
        faults: Mapping[int, str] | None = None,
        before_response: Callable[[dict[str, Any] | None], None] | None = None,
    ) -> None:
        self.catalogue = deepcopy(list(catalogue))
        self.records = {key: deepcopy(list(value)) for key, value in records.items()}
        self.endpoints = deepcopy(dict(endpoints or {}))
        self.faults = dict(faults or {})
        self.before_response = before_response
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def __call__(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, bytes]:
        del headers
        try:
            request = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            request = None
        with self._lock:
            call_number = len(self.calls) + 1
            self.calls.append(
                {
                    "call_number": call_number,
                    "url": url,
                    "request": deepcopy(request),
                    "timeout": timeout,
                }
            )
        fault = self.faults.get(call_number)
        if fault == "transport":
            raise URLError(f"offline transport fault on call {call_number}")
        if self.before_response is not None:
            self.before_response(request if isinstance(request, dict) else None)
        if not isinstance(request, dict):
            return self._error(None, -32700, "Parse error")

        request_id = request.get("id")
        method = request.get("method")
        if method == "notifications/initialized":
            return 202, b""
        if method == "initialize":
            return self._result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "agentswitch-offline", "version": "1"},
                },
            )
        if method == "tools/list":
            return self._result(request_id, {"tools": deepcopy(self.catalogue)})
        if method != "tools/call":
            return self._error(request_id, -32601, f"Method not found: {method}")

        params = request.get("params")
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "Invalid tools/call parameters")
        name = params.get("name")
        arguments = params.get("arguments")
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return self._error(request_id, -32602, "Invalid tools/call parameters")
        tool = next(
            (
                item
                for item in self.catalogue
                if isinstance(item, dict) and item.get("name") == name
            ),
            None,
        )
        annotations = tool.get("annotations") if tool is not None else None
        if (
            not isinstance(annotations, dict)
            or annotations.get("readOnlyHint") is not True
            or annotations.get("destructiveHint") is True
        ):
            return self._tool_error(
                request_id,
                f"offline transport refuses tool {name!r}; it is not explicitly read-only",
            )
        if fault == "empty_page":
            total = len(self._matching_records(name, arguments))
            return self._tool_result(request_id, {"data": [], "total": total})
        if name.endswith(".get"):
            return self._get(request_id, name, arguments)
        if name.endswith(".list"):
            return self._list(request_id, name, arguments)
        if name in self.endpoints:
            return self._tool_result(request_id, deepcopy(self.endpoints[name]))
        return self._tool_error(request_id, f"offline transport has no fixture for tool {name!r}")

    def _records_for(self, tool_name: str) -> list[dict[str, Any]]:
        entity = tool_name.rsplit(".", 1)[0]
        return self.records.get(tool_name, self.records.get(entity, []))

    def _matching_records(
        self, tool_name: str, arguments: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        filters = {
            key: value
            for key, value in arguments.items()
            if key not in {"limit", "offset"}
        }
        return [
            deepcopy(record)
            for record in self._records_for(tool_name)
            if all(record.get(key) == value for key, value in filters.items())
        ]

    def _get(
        self, request_id: Any, name: str, arguments: Mapping[str, Any]
    ) -> tuple[int, bytes]:
        identifier = arguments.get("id")
        for record in self._records_for(name):
            if record.get("id") == identifier:
                return self._tool_result(request_id, deepcopy(record))
        return self._error(request_id, -32602, f"{name} id {identifier!r} not found")

    def _list(
        self, request_id: Any, name: str, arguments: Mapping[str, Any]
    ) -> tuple[int, bytes]:
        matches = self._matching_records(name, arguments)
        limit = arguments.get("limit", len(matches))
        offset = arguments.get("offset", 0)
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 0
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
        ):
            return self._error(request_id, -32602, "limit and offset must be non-negative integers")
        return self._tool_result(
            request_id,
            {"data": matches[offset : offset + limit], "total": len(matches)},
        )

    @staticmethod
    def _result(request_id: Any, result: Any) -> tuple[int, bytes]:
        return 200, json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": result},
            separators=(",", ":"),
        ).encode()

    @classmethod
    def _tool_result(cls, request_id: Any, structured: Any) -> tuple[int, bytes]:
        return cls._result(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(structured, separators=(",", ":")),
                    }
                ],
                "structuredContent": structured,
                "isError": False,
            },
        )

    @classmethod
    def _tool_error(cls, request_id: Any, message: str) -> tuple[int, bytes]:
        return cls._result(
            request_id,
            {
                "content": [{"type": "text", "text": message}],
                "isError": True,
            },
        )

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> tuple[int, bytes]:
        return 200, json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            },
            separators=(",", ":"),
        ).encode()


def offline_mcp_client(
    catalogue: Sequence[dict[str, Any]],
    records: Mapping[str, Sequence[dict[str, Any]]],
    *,
    endpoints: Mapping[str, dict[str, Any]] | None = None,
    faults: Mapping[int, str] | None = None,
    before_response: Callable[[dict[str, Any] | None], None] | None = None,
) -> tuple[McpClient, OfflineMcpTransport]:
    """Build an authenticated-looking MCP client and expose its transport."""
    transport = OfflineMcpTransport(
        catalogue,
        records,
        endpoints=endpoints,
        faults=faults,
        before_response=before_response,
    )
    return McpClient("https://offline.invalid", "offline-token", transport=transport), transport


class ScriptedLlmTransport:
    """Return scripted HTTP responses while recording decoded request bodies."""

    def __init__(self, responses: Sequence[tuple[int, dict[str, Any] | bytes]]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, bytes]:
        del headers
        try:
            request = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("offline LLM received invalid JSON") from error
        self.calls.append({"url": url, "body": request, "timeout": timeout})
        if not self._responses:
            raise RuntimeError("offline LLM script exhausted")
        status, response = self._responses.pop(0)
        encoded = response if isinstance(response, bytes) else json.dumps(response).encode()
        return status, encoded


def scripted_tool_response(
    name: str,
    arguments: dict[str, Any],
    *,
    cost: int | float = 0.0,
    model: str = "offline/model",
    call_id: str = "call_offline_1",
    prompt_tokens: int = 1,
    completion_tokens: int = 1,
) -> dict[str, Any]:
    """Build one OpenRouter-shaped assistant tool-call response with usage."""
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments, separators=(",", ":")),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "model": model,
        "provider": "offline",
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost": cost,
        },
    }


def offline_llm_client(
    config: Config,
    responses: Sequence[tuple[int, dict[str, Any] | bytes]],
) -> tuple[OpenRouterClient, ScriptedLlmTransport]:
    """Build a real OpenRouter client using an explicit offline script."""
    transport = ScriptedLlmTransport(responses)
    client = OpenRouterClient(
        "offline-key",
        config.models.agent,
        reasoning_effort=config.models.reasoning_effort,
        seed=config.models.seed,
        max_tokens=config.models.max_tokens,
        timeout=float(config.models.timeout_seconds),
        transport=transport,
    )
    return client, transport


__all__ = [
    "OfflineMcpTransport",
    "ScriptedLlmTransport",
    "offline_llm_client",
    "offline_mcp_client",
    "scripted_tool_response",
]
