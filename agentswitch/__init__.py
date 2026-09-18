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
    get_current_user_id,
    login,
)
from .reschedule import plan_reschedule, reschedule

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
    "get_current_user_id",
    "login",
    "plan_reschedule",
    "reschedule",
]
