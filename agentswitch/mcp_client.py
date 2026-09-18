"""Synchronous JSON-RPC client for the AgentSwitch MCP endpoint."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

PROTOCOL_VERSION = "2025-11-25"
CLIENT_INFO = {"name": "agentswitch-team04", "version": "0.1.0"}
DEFAULT_TIMEOUT = 60.0
_BODY_EXCERPT_LENGTH = 200
_VALID_TENANTS = {"keystone", "suryodaya"}
_UNKNOWN_TOOL_OUTCOME_WARNING = (
    "; the tools/call request may have reached the server and its outcome is unknown; "
    "it was not retried"
)

Transport = Callable[[str, bytes, dict[str, str], float], tuple[int, bytes]]
GetTransport = Callable[[str, dict[str, str], float], tuple[int, bytes]]


class McpError(Exception):
    """Base class for client errors."""


class AuthError(McpError):
    """Authentication failed or returned no usable token."""


class TransportError(McpError):
    """An HTTP or response-decoding failure occurred."""

    def __init__(
        self,
        message: str,
        *,
        cause_type: str | None = None,
        outcome_unknown: bool = False,
    ) -> None:
        self.cause_type = cause_type
        self.outcome_unknown = outcome_unknown
        super().__init__(message)


class ProtocolError(McpError):
    """The server returned an invalid MCP or JSON-RPC response."""


class JsonRpcError(McpError):
    """A JSON-RPC error returned by the server."""

    def __init__(
        self,
        code: int,
        message: str,
        data: Any = None,
        *,
        method: str,
        tool_name: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.data = data
        self.method = method
        self.tool_name = tool_name
        context = f" for tool {tool_name!r}" if tool_name is not None else ""
        super().__init__(f"JSON-RPC error {code} from {method}{context}: {message}")


class PermissionDenied(JsonRpcError):
    """The authenticated seat may not perform the requested operation."""


class InvalidParams(JsonRpcError):
    """The server rejected the request parameters."""


class ToolNotFound(McpError):
    """A tool is absent from the client's cached catalogue."""


class ArgumentError(McpError):
    """Tool arguments fail top-level catalogue validation."""


class WriteNotAllowed(McpError):
    """A tool call was stopped by the local write guard."""


@dataclass(frozen=True)
class ToolResult:
    """Normalized result of a tools/call request."""

    structured: Any
    text: str
    is_error: bool
    raw: dict[str, Any]


class ToolError(McpError):
    """A tool completed with an MCP-level error result."""

    def __init__(self, tool_name: str, result: ToolResult) -> None:
        self.tool_name = tool_name
        self.result = result
        super().__init__(f"Tool {tool_name!r} returned isError=true")


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler())


def _default_transport(
    url: str,
    body: bytes,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, bytes]:
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def _default_get_transport(
    url: str,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, bytes]:
    request = Request(url, headers=headers, method="GET")
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def _safe_excerpt(body: bytes, sensitive_values: tuple[str, ...] = ()) -> str:
    text = body.decode("utf-8", errors="replace")
    for value in sensitive_values:
        if value:
            text = text.replace(value, "<redacted>")
    text = re.sub(r"(?i)Bearer\s+\S+", "Bearer <redacted>", text)
    text = re.sub(
        r'(?i)("[^"\\]*(?:token|password)[^"\\]*"\s*:\s*)"(?:\\.|[^"\\])*"',
        r'\1"<redacted>"',
        text,
    )
    compact = " ".join(text.split())
    if len(compact) > _BODY_EXCERPT_LENGTH:
        return f"{compact[:_BODY_EXCERPT_LENGTH]}..."
    return compact or "<empty>"


def _perform_transport(
    transport: Transport,
    url: str,
    body: bytes,
    headers: dict[str, str],
    timeout: float,
    *,
    outcome_unknown: bool = False,
) -> tuple[int, bytes]:
    try:
        status, response_body = transport(url, body, headers, timeout)
    except (URLError, TimeoutError, OSError, HTTPException) as error:
        cause_type = type(error).__name__
        message = f"Network failure ({cause_type}) while contacting {url}"
        if outcome_unknown:
            message += _UNKNOWN_TOOL_OUTCOME_WARNING
        raise TransportError(
            message,
            cause_type=cause_type,
            outcome_unknown=outcome_unknown,
        ) from None
    if not isinstance(status, int) or not isinstance(response_body, bytes):
        raise TransportError(
            "Transport must return an integer status and bytes body",
            outcome_unknown=outcome_unknown,
        )
    return status, response_body


def _decode_json(body: bytes, status: int, sensitive_values: tuple[str, ...] = ()) -> Any:
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        excerpt = _safe_excerpt(body, sensitive_values)
        cause_type = type(error).__name__
        raise TransportError(
            f"HTTP {status} returned non-JSON body ({cause_type}); excerpt: {excerpt}",
            cause_type=cause_type,
        ) from None


def login(
    base_url: str,
    email: str,
    password: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    transport: Transport | None = None,
) -> str:
    """Authenticate and return a bearer token."""
    url = f"{base_url.rstrip('/')}/api/auth/login"
    request_body = json.dumps({"email": email, "password": password}, separators=(",", ":")).encode()
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    status, response_body = _perform_transport(
        transport or _default_transport,
        url,
        request_body,
        headers,
        timeout,
    )
    if status == 401:
        raise AuthError("Login was rejected with HTTP 401")
    if status != 200:
        excerpt = _safe_excerpt(response_body, (password,))
        raise TransportError(f"Unexpected HTTP status {status} from login; body excerpt: {excerpt}")

    try:
        payload = json.loads(response_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        excerpt = _safe_excerpt(response_body, (password,))
        cause_type = type(error).__name__
        raise TransportError(
            f"HTTP 200 returned non-JSON login body ({cause_type}); body excerpt: {excerpt}",
            cause_type=cause_type,
        ) from None
    if not isinstance(payload, dict) or not isinstance(payload.get("token"), str) or not payload["token"]:
        raise AuthError("Login response did not contain a token")
    return payload["token"]


def get_current_user_id(
    base_url: str,
    token: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    transport: GetTransport | None = None,
) -> str:
    """Return the authenticated user's id from the REST identity endpoint."""
    url = f"{base_url.rstrip('/')}/api/auth/me"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    try:
        status, response_body = (transport or _default_get_transport)(url, headers, timeout)
    except (URLError, TimeoutError, OSError, HTTPException) as error:
        cause_type = type(error).__name__
        raise TransportError(
            f"Network failure ({cause_type}) while contacting the current-user endpoint",
            cause_type=cause_type,
        ) from None
    if not isinstance(status, int) or not isinstance(response_body, bytes):
        raise TransportError("GET transport must return an integer status and bytes body")
    if status == 401:
        raise AuthError("Current-user request was rejected with HTTP 401")
    if status != 200:
        excerpt = _safe_excerpt(response_body, (token,))
        raise TransportError(
            f"Unexpected HTTP status {status} from current-user endpoint; body excerpt: {excerpt}"
        )
    payload = _decode_json(response_body, status, (token,))
    if not isinstance(payload, dict):
        raise ProtocolError("Current-user response must be an object")
    identifier = payload.get("id")
    if not isinstance(identifier, str) or not identifier.strip():
        raise ProtocolError("Current-user response did not contain a non-empty string id")
    return identifier


class McpClient:
    """Synchronous MCP client that automatically initializes before tool methods."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Transport | None = None,
        get_transport: GetTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self._transport = transport or _default_transport
        self._get_transport = get_transport
        self._next_request_id = 1
        self._initialize_result: dict[str, Any] | None = None
        self._tools: list[dict[str, Any]] | None = None
        self._tools_by_name: dict[str, dict[str, Any]] | None = None

    def current_user_id(self) -> str:
        """Return the authenticated user's non-empty id."""
        return get_current_user_id(
            self.base_url,
            self._token,
            timeout=self.timeout,
            transport=self._get_transport,
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self.base_url!r})"

    def initialize(self) -> dict[str, Any]:
        """Perform the MCP handshake once and return its result."""
        if self._initialize_result is not None:
            return self._initialize_result

        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        )
        if not isinstance(result, dict):
            raise ProtocolError("initialize result must be an object")
        if result.get("protocolVersion") != PROTOCOL_VERSION:
            raise ProtocolError("Server negotiated an unsupported protocol version")
        self._send_notification("notifications/initialized")
        self._initialize_result = result
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        """Return and cache this client's complete seat-scoped tool catalogue."""
        self.initialize()
        if self._tools is not None:
            return list(self._tools)

        result = self._request("tools/list", {})
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            raise ProtocolError("tools/list result must contain a tools list")
        next_cursor = result.get("nextCursor")
        if next_cursor is not None and next_cursor != "":
            raise ProtocolError("tools/list returned a nextCursor; incomplete catalogues are unsupported")

        tools = result["tools"]
        by_name: dict[str, dict[str, Any]] = {}
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                raise ProtocolError("tools/list contained a tool without a string name")
            name = tool["name"]
            if name in by_name:
                raise ProtocolError("tools/list contained a duplicate tool name")
            by_name[name] = tool
        self._tools = tools
        self._tools_by_name = by_name
        return list(tools)

    def get_tool(self, name: str) -> dict[str, Any]:
        """Return one tool definition from the cached catalogue."""
        self.list_tools()
        if self._tools_by_name is None or name not in self._tools_by_name:
            raise ToolNotFound(f"Tool {name!r} is not in the seat-scoped catalogue")
        return self._tools_by_name[name]

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        allow_write: bool = False,
    ) -> ToolResult:
        """Validate and call a tool, requiring opt-in for possible writes."""
        tool = self.get_tool(name)
        call_arguments = {} if arguments is None else arguments
        self._validate_arguments(tool, call_arguments)
        self._check_write_guard(tool, allow_write)

        result = self._request(
            "tools/call",
            {"name": name, "arguments": call_arguments},
            tool_name=name,
        )
        if not isinstance(result, dict):
            raise ProtocolError("tools/call result must be an object")
        tool_result = self._make_tool_result(result)
        if result.get("isError") is True:
            raise ToolError(name, tool_result)
        return tool_result

    def _validate_arguments(self, tool: dict[str, Any], arguments: object) -> None:
        name = tool["name"]
        if not isinstance(arguments, dict):
            raise ArgumentError(f"Arguments for tool {name!r} must be a dict")

        schema = tool.get("inputSchema")
        if not isinstance(schema, dict):
            raise ProtocolError(f"Tool {name!r} has no valid inputSchema")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise ProtocolError(f"Tool {name!r} has a malformed inputSchema")

        extra = sorted((key for key in arguments if key not in properties), key=str)
        if extra:
            rendered = ", ".join(repr(key) for key in extra)
            raise ArgumentError(f"Tool {name!r} received unknown argument keys: {rendered}")
        if any(not isinstance(key, str) for key in required):
            raise ProtocolError(f"Tool {name!r} has non-string required argument names")
        missing = sorted(key for key in required if key not in arguments)
        if missing:
            raise ArgumentError(f"Tool {name!r} is missing required argument keys: {', '.join(missing)}")

    @staticmethod
    def _check_write_guard(tool: dict[str, Any], allow_write: bool) -> None:
        name = tool["name"]
        annotations = tool.get("annotations")
        if not isinstance(annotations, dict):
            annotations = {}
        if annotations.get("destructiveHint") is True:
            raise WriteNotAllowed(f"Tool {name!r} is marked destructive and is always refused")
        if annotations.get("readOnlyHint") is not True and not allow_write:
            raise WriteNotAllowed(
                f"Tool {name!r} is not explicitly read-only; pass allow_write=True to permit the call"
            )

    @staticmethod
    def _make_tool_result(result: dict[str, Any]) -> ToolResult:
        content = result.get("content", [])
        if not isinstance(content, list):
            raise ProtocolError("tools/call content must be a list")
        text = "".join(
            item["text"]
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
        )

        if "structuredContent" in result:
            structured = result["structuredContent"]
        else:
            try:
                structured = json.loads(text) if text else None
            except json.JSONDecodeError:
                structured = None
        return ToolResult(
            structured=structured,
            text=text,
            is_error=result.get("isError") is True,
            raw=result,
        )

    def _request(self, method: str, params: dict[str, Any], tool_name: str | None = None) -> Any:
        request_id = self._next_request_id
        self._next_request_id += 1
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            status, response_body = self._post(payload)
            response = _decode_json(response_body, status, (self._token,))
        except TransportError as error:
            if method != "tools/call":
                raise
            raise TransportError(
                f"{error}{_UNKNOWN_TOOL_OUTCOME_WARNING}",
                cause_type=error.cause_type,
                outcome_unknown=True,
            ) from None
        if not isinstance(response, dict):
            raise ProtocolError("JSON-RPC response must be an object")
        if response.get("jsonrpc") != "2.0":
            raise ProtocolError("JSON-RPC response has a missing or invalid jsonrpc version")
        response_id = response.get("id")
        if not isinstance(response_id, int) or isinstance(response_id, bool) or response_id != request_id:
            raise ProtocolError(f"JSON-RPC response id does not match request id {request_id}")
        has_result = "result" in response
        has_error = "error" in response
        if has_result == has_error:
            raise ProtocolError("JSON-RPC response must contain exactly one of result or error")
        if has_error:
            self._raise_jsonrpc_error(response["error"], method, tool_name)
        return response["result"]

    def _send_notification(self, method: str) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        status, response_body = self._post(payload, notification=True)
        if response_body.strip():
            excerpt = _safe_excerpt(response_body, (self._token,))
            raise ProtocolError(
                f"Notification {method} returned a non-empty HTTP {status} body; excerpt: {excerpt}"
            )

    def _post(
        self,
        payload: dict[str, Any],
        *,
        notification: bool = False,
    ) -> tuple[int, bytes]:
        url = f"{self.base_url}/api/mcp"
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        status, response_body = _perform_transport(
            self._transport,
            url,
            body,
            headers,
            self.timeout,
        )
        if status == 401:
            raise AuthError("MCP request was rejected with HTTP 401")
        expected_statuses = {200, 202} if notification else {200}
        if status not in expected_statuses:
            excerpt = _safe_excerpt(response_body, (self._token,))
            raise TransportError(f"Unexpected HTTP status {status}; body excerpt: {excerpt}")
        return status, response_body

    def _raise_jsonrpc_error(self, error: Any, method: str, tool_name: str | None) -> None:
        if not isinstance(error, dict):
            raise ProtocolError("JSON-RPC error must be an object")
        code = error.get("code")
        message = error.get("message")
        if not isinstance(code, int) or isinstance(code, bool) or not isinstance(message, str):
            raise ProtocolError("JSON-RPC error must contain an integer code and string message")
        message = message.replace(self._token, "<redacted>") if self._token else message
        data = error.get("data")
        error_type: type[JsonRpcError] = JsonRpcError
        if code == -32001 and isinstance(data, dict) and data.get("code") == "permission_denied":
            error_type = PermissionDenied
        elif code == -32602:
            error_type = InvalidParams
        raise error_type(code, message, data, method=method, tool_name=tool_name)


def _read_env_file(env_file: str) -> dict[str, str]:
    try:
        lines = Path(env_file).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}

    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def from_env(tenant: str | None = None, *, env_file: str = ".env") -> McpClient:
    """Log in from environment settings and return an uninitialized client."""
    file_values: dict[str, str] | None = None

    def setting(name: str) -> str:
        nonlocal file_values
        if name in os.environ:
            value = os.environ[name]
        else:
            if file_values is None:
                file_values = _read_env_file(env_file)
            if name not in file_values:
                raise ValueError(f"Missing required configuration variable {name}")
            value = file_values[name]
        if not value:
            raise ValueError(f"Missing required configuration variable {name}")
        return value

    selected_tenant = tenant if tenant is not None else setting("AS_TENANT")
    selected_tenant = selected_tenant.strip().lower()
    if selected_tenant not in _VALID_TENANTS:
        choices = ", ".join(sorted(_VALID_TENANTS))
        raise ValueError(f"Invalid tenant {selected_tenant!r}; expected one of: {choices}")

    suffix = selected_tenant.upper()
    base_url = setting(f"AS_URL_{suffix}")
    email = setting("AS_EMAIL")
    password = setting(f"AS_PASSWORD_{suffix}")
    token = login(base_url, email, password)
    return McpClient(base_url, token)
