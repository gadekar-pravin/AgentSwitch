"""Offline infrastructure transport tests."""

from __future__ import annotations

import json

import pytest

from agentswitch.answer import Store
from agentswitch.config import load_config
from agentswitch.economics import MeteredClient
from agentswitch.llm_client import OpenRouterClient
from agentswitch.mcp_client import InvalidParams, McpClient, ToolError
from agentswitch.offline import (
    OfflineMcpTransport,
    ScriptedLlmTransport,
    offline_mcp_client,
    scripted_tool_response,
)
from agentswitch.reads import call_list

CATALOGUE = [
    {
        "name": "WorkOrder.get",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "WorkOrder.list",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "WorkOrder.update",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "planned_start_date": {"type": "string"},
                "planned_end_date": {"type": "string"},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        "annotations": {},
    },
]

RECORDS = {
    "WorkOrder": [
        {"id": "WO-1", "status": "late", "item": "A"},
        {"id": "WO-2", "status": "late", "item": "B"},
        {"id": "WO-3", "status": "done", "item": "C"},
    ]
}


def test_real_mcp_client_lists_gets_pages_and_refuses_write():
    """Spec: AI (Codex) Real MCP parsing works over fixed reads and refuses offline writes."""
    client, transport = offline_mcp_client(CATALOGUE, RECORDS)

    assert [tool["name"] for tool in client.list_tools()] == [
        "WorkOrder.get",
        "WorkOrder.list",
        "WorkOrder.update",
    ]
    assert client.call_tool("WorkOrder.get", {"id": "WO-1"}).structured["item"] == "A"
    with pytest.raises(InvalidParams, match="not found"):
        client.call_tool("WorkOrder.get", {"id": "missing"})

    first = client.call_tool(
        "WorkOrder.list", {"status": "late", "limit": 1, "offset": 0}
    ).structured
    second = client.call_tool(
        "WorkOrder.list", {"status": "late", "limit": 1, "offset": 1}
    ).structured
    assert first == {"data": [RECORDS["WorkOrder"][0]], "total": 2}
    assert second == {"data": [RECORDS["WorkOrder"][1]], "total": 2}

    with pytest.raises(ToolError, match="isError=true"):
        client.call_tool("WorkOrder.update", {"id": "WO-1"}, allow_write=True)
    assert transport.calls


def test_offline_work_order_update_requires_opt_in_and_mutates_dates():
    """Spec: AI (Codex) Writable offline tools update stored planned dates only after opt-in."""
    arguments = {
        "id": "WO-1",
        "planned_start_date": "2026-09-19",
        "planned_end_date": "2026-09-21",
    }
    read_only, _ = offline_mcp_client(CATALOGUE, RECORDS)
    writable, _ = offline_mcp_client(
        CATALOGUE,
        RECORDS,
        writable_tools={"WorkOrder.update"},
    )

    with pytest.raises(ToolError, match="isError=true"):
        read_only.call_tool("WorkOrder.update", arguments, allow_write=True)

    updated = writable.call_tool(
        "WorkOrder.update", arguments, allow_write=True
    ).structured

    assert updated["planned_start_date"] == "2026-09-19"
    assert updated["planned_end_date"] == "2026-09-21"
    persisted = writable.call_tool("WorkOrder.get", {"id": "WO-1"}).structured
    assert persisted == updated


def test_empty_page_fault_produces_incomplete_scan():
    """Spec: AI (Codex) An injected empty page before total leaves a list scan incomplete."""
    records = {"WorkOrder": [{"id": f"WO-{number}"} for number in range(1001)]}
    transport = OfflineMcpTransport(CATALOGUE, records, faults={5: "empty_page"})
    client = McpClient("https://offline.invalid", "offline", transport=transport)
    store = Store()

    result = call_list(client, store, "WorkOrder.list", {})

    assert result["returned"] == 1000
    assert result["total"] == 1001
    assert result["complete"] is False
    assert store.list_calls == [
        {"tool": "WorkOrder.list", "filters": {}, "complete": False}
    ]


@pytest.mark.parametrize("faults", [None, {4: "empty_page"}])
def test_write_tool_is_refused_before_endpoint_or_empty_page(faults):
    """Spec: AI (Codex) Writes cannot succeed through endpoint fixtures or page faults."""
    client, _ = offline_mcp_client(
        CATALOGUE,
        RECORDS,
        endpoints={"WorkOrder.update": {"id": "WO-1", "updated": True}},
        faults=faults,
    )

    with pytest.raises(ToolError, match="isError=true"):
        client.call_tool("WorkOrder.update", {"id": "WO-1"}, allow_write=True)


def test_tool_missing_from_offline_catalogue_is_refused():
    """Spec: AI (Codex) The offline transport refuses tools absent from its catalogue."""
    transport = OfflineMcpTransport(CATALOGUE, RECORDS)
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "Unknown.list", "arguments": {}},
    }

    status, body = transport(
        "https://offline.invalid/api/mcp",
        json.dumps(request).encode(),
        {},
        1.0,
    )

    payload = json.loads(body)
    assert status == 200
    assert payload["result"]["isError"] is True
    assert "not explicitly read-only" in payload["result"]["content"][0]["text"]


def test_real_metered_client_charges_scripted_tool_call(monkeypatch, tmp_path):
    """Spec: AI (Codex) Real model parsing returns a tool call and charges usage.cost."""
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    config = load_config(env_file=tmp_path / "missing.env")
    response = scripted_tool_response(
        "plan_frontier",
        {"add": [], "finish": True, "reason": "done"},
        cost=0.000007,
        model=config.models.agent,
    )
    transport = ScriptedLlmTransport([(200, response)])
    raw = OpenRouterClient(
        "offline-key",
        config.models.agent,
        reasoning_effort=config.models.reasoning_effort,
        seed=config.models.seed,
        max_tokens=config.models.max_tokens,
        timeout=float(config.models.timeout_seconds),
        transport=transport,
    )
    client = MeteredClient(raw, config, sleep=lambda _: None)

    result = client.chat(
        [{"role": "user", "content": "plan"}],
        tools=[{"type": "function", "function": {"name": "plan_frontier"}}],
        tool_choice={"type": "function", "function": {"name": "plan_frontier"}},
    )

    assert result["message"]["tool_calls"][0]["function"]["name"] == "plan_frontier"
    assert client.ledger()["summary"]["spent_micro"] == 7
    assert transport.calls[0]["body"]["tool_choice"]["function"]["name"] == "plan_frontier"


def test_scripted_llm_fails_clearly_when_exhausted():
    """Spec: AI (Codex) An exhausted offline model script raises a clear error."""
    transport = ScriptedLlmTransport([])

    with pytest.raises(RuntimeError, match="script exhausted"):
        transport("https://offline.invalid", b"{}", {}, 1.0)
