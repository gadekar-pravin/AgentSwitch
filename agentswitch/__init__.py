"""AgentSwitch client package."""

from .mcp_client import (
    ArgumentError,
    AuthError,
    InvalidParams,
    JsonRpcError,
    McpClient,
    McpError,
    PermissionDenied,
    ProtocolError,
    ToolError,
    ToolNotFound,
    ToolResult,
    TransportError,
    WriteNotAllowed,
    from_env,
    login,
)

__all__ = [
    "ArgumentError",
    "AuthError",
    "InvalidParams",
    "JsonRpcError",
    "McpClient",
    "McpError",
    "PermissionDenied",
    "ProtocolError",
    "ToolError",
    "ToolNotFound",
    "ToolResult",
    "TransportError",
    "WriteNotAllowed",
    "from_env",
    "login",
]
