"""Concurrency coverage for MCP calls, recording, attribution, and offline transport."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agentswitch.attribution import node_context
from agentswitch.harness.recorder import ReadOnlyTools, ScopedWriteTools
from agentswitch.harness.write_verifiers import _subject_write_attempts
from agentswitch.mcp_client import ToolError, ToolResult, WriteNotAllowed
from agentswitch.offline import OfflineMcpTransport, offline_mcp_client

THREADS = 8
READ_TOOL = {
    "name": "WorkOrder.get",
    "inputSchema": {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True},
}
UPDATE_TOOL = {
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
    "annotations": {"readOnlyHint": False},
}
RECORDS = {
    "WorkOrder": [
        {
            "id": f"wo-{index}",
            "status": "draft",
            "created_by": "user-1",
            "planned_start_date": "2026-09-01",
            "planned_end_date": "2026-09-02",
        }
        for index in range(THREADS)
    ]
}


def _is_tool_call(request: dict[str, Any] | None) -> bool:
    return isinstance(request, dict) and request.get("method") == "tools/call"


def test_concurrent_mcp_requests_have_unique_ids_and_matching_responses() -> None:
    """Spec: AI (Codex). Concurrent calls get unique ids and their own responses."""
    barrier = threading.Barrier(THREADS)

    def before_response(request: dict[str, Any] | None) -> None:
        if _is_tool_call(request):
            barrier.wait(timeout=5)

    client, transport = offline_mcp_client(
        [READ_TOOL], RECORDS, before_response=before_response
    )
    client.initialize()
    client.list_tools()

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        results = list(
            pool.map(
                lambda index: client.call_tool("WorkOrder.get", {"id": f"wo-{index}"}),
                range(THREADS),
            )
        )

    calls = [call for call in transport.calls if _is_tool_call(call["request"])]
    request_ids = [call["request"]["id"] for call in calls]
    assert len(set(request_ids)) == THREADS
    assert {result.structured["id"] for result in results} == {
        f"wo-{index}" for index in range(THREADS)
    }


def test_recorder_sequences_cover_overlapping_calls_and_all_observations() -> None:
    """Spec: AI (Codex). Starts and ends are ordered while overlapping reads are retained."""
    barrier = threading.Barrier(THREADS)

    def before_response(request: dict[str, Any] | None) -> None:
        if _is_tool_call(request):
            barrier.wait(timeout=5)

    client, _ = offline_mcp_client([READ_TOOL], RECORDS, before_response=before_response)
    client.initialize()
    client.list_tools()
    tools = ReadOnlyTools(client, phase="subject")

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(
            pool.map(
                lambda index: tools.call_tool("WorkOrder.get", {"id": f"wo-{index}"}),
                range(THREADS),
            )
        )

    sequences = [value for call in tools.calls for value in (call["start_sequence"], call["end_sequence"])]
    assert sorted(sequences) == list(range(1, 2 * THREADS + 1))
    assert all(call["start_sequence"] < call["end_sequence"] for call in tools.calls)
    assert any(
        first["start_sequence"] < second["start_sequence"] < first["end_sequence"]
        for first in tools.calls
        for second in tools.calls
        if first is not second
    )
    assert all(call["started_ns"] <= call["finished_ns"] for call in tools.calls)
    assert set(tools.observations) == {
        ("WorkOrder", f"wo-{index}") for index in range(THREADS)
    }


def test_node_attribution_is_context_local_and_resets_in_reused_thread() -> None:
    """Spec: AI (Codex). Node ids stay local to calls and do not leak between tasks."""
    barrier = threading.Barrier(2)

    def before_response(request: dict[str, Any] | None) -> None:
        if _is_tool_call(request):
            barrier.wait(timeout=5)

    client, _ = offline_mcp_client([READ_TOOL], RECORDS, before_response=before_response)
    client.initialize()
    client.list_tools()
    tools = ReadOnlyTools(client)

    def attributed(node_id: str, record_id: str) -> None:
        with node_context(node_id):
            tools.call_tool("WorkOrder.get", {"id": record_id})

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(attributed, "n1", "wo-0"),
            pool.submit(attributed, "n2", "wo-1"),
        ]
        for future in futures:
            future.result(timeout=5)

    transport = client._transport
    assert isinstance(transport, OfflineMcpTransport)
    transport.before_response = None
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(attributed, "temporary", "wo-2").result(timeout=5)
        pool.submit(tools.call_tool, "WorkOrder.get", {"id": "wo-3"}).result(timeout=5)

    nodes_by_id = {call["arguments"]["id"]: call["node"] for call in tools.calls}
    assert nodes_by_id == {
        "wo-0": "n1",
        "wo-1": "n2",
        "wo-2": "temporary",
        "wo-3": None,
    }


def test_node_attribution_inside_and_outside_context() -> None:
    """Spec: AI (Codex). A direct context is recorded and the following call has no node."""
    client, _ = offline_mcp_client([READ_TOOL], RECORDS)
    client.initialize()
    client.list_tools()
    tools = ReadOnlyTools(client)

    with node_context("n1"):
        tools.call_tool("WorkOrder.get", {"id": "wo-0"})
    tools.call_tool("WorkOrder.get", {"id": "wo-1"})

    assert [call["node"] for call in tools.calls] == ["n1", None]


def test_recorder_captures_phase_at_call_start() -> None:
    """Spec: AI (Codex). A subject call remains observed after the shared phase changes."""
    entered = threading.Event()
    release = threading.Event()

    def before_response(request: dict[str, Any] | None) -> None:
        if _is_tool_call(request):
            entered.set()
            assert release.wait(timeout=5)

    client, _ = offline_mcp_client([READ_TOOL], RECORDS, before_response=before_response)
    client.initialize()
    client.list_tools()
    tools = ReadOnlyTools(client, phase="subject")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.call_tool, "WorkOrder.get", {"id": "wo-0"})
        assert entered.wait(timeout=5)
        tools.phase = "verify"
        release.set()
        future.result(timeout=5)

    assert tools.calls[0]["phase"] == "subject"
    assert ("WorkOrder", "wo-0") in tools.observations


def test_write_guard_phase_override_is_thread_local() -> None:
    """Spec: AI (Codex). A held guard read does not relabel a concurrent plain read."""
    guard_entered = threading.Event()
    release_guard = threading.Event()
    hold_guard = threading.Event()

    def before_response(request: dict[str, Any] | None) -> None:
        if not (_is_tool_call(request) and hold_guard.is_set()):
            return
        arguments = request["params"]["arguments"]
        if arguments == {"id": "wo-0"}:
            guard_entered.set()
            assert release_guard.wait(timeout=5)

    client, _ = offline_mcp_client(
        [READ_TOOL, UPDATE_TOOL], RECORDS, before_response=before_response
    )
    client.initialize()
    client.list_tools()
    tools = ScopedWriteTools(client, target_id="wo-0", own_user_id="user-1", phase="subject")
    tools.call_tool("WorkOrder.get", {"id": "wo-0"})
    hold_guard.set()

    def guarded_update() -> None:
        try:
            tools.call_tool(
                "WorkOrder.update",
                {"id": "wo-0", "planned_end_date": "2026-09-03"},
                allow_write=True,
            )
        except (ToolError, WriteNotAllowed):
            pass

    with ThreadPoolExecutor(max_workers=2) as pool:
        update = pool.submit(guarded_update)
        assert guard_entered.wait(timeout=5)
        assert tools.phase == "subject"
        plain = pool.submit(tools.call_tool, "WorkOrder.get", {"id": "wo-1"})
        plain.result(timeout=5)
        release_guard.set()
        update.result(timeout=5)

    phases = [
        call["phase"]
        for call in tools.calls
        if call["tool"] == "WorkOrder.get"
    ]
    assert phases == ["subject", "write_guard", "subject"]


def test_scoped_write_attempt_is_marked_before_client_call() -> None:
    """Spec: AI (Codex). A write interrupted by BaseException remains selectable."""

    class InterruptingClient:
        tools: ScopedWriteTools | None = None

        def call_tool(
            self,
            name: str,
            arguments: dict[str, Any],
            *,
            allow_write: bool = False,
        ) -> ToolResult:
            if name == "WorkOrder.get":
                return ToolResult(
                    structured={
                        "id": "wo-0",
                        "status": "draft",
                        "created_by": "user-1",
                        "planned_start_date": "2026-09-01",
                        "planned_end_date": "2026-09-02",
                    },
                    text="",
                    is_error=False,
                    raw={},
                )
            assert name == "WorkOrder.update"
            assert allow_write is True
            assert self.tools is not None
            assert self.tools.calls[-1]["write"] is True
            raise KeyboardInterrupt

    client = InterruptingClient()
    tools = ScopedWriteTools(client, target_id="wo-0", own_user_id="user-1", phase="subject")
    client.tools = tools
    tools.call_tool("WorkOrder.get", {"id": "wo-0"})

    try:
        tools.call_tool(
            "WorkOrder.update",
            {"id": "wo-0", "planned_end_date": "2026-09-03"},
            allow_write=True,
        )
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("expected the fake client to interrupt the write")

    entry = tools.calls[-1]
    assert entry["write"] is True
    assert _subject_write_attempts(tools.calls) == [entry]


def test_offline_transport_assigns_unique_concurrent_call_numbers() -> None:
    """Spec: AI (Codex). Offline call numbers are allocated atomically under concurrency."""
    barrier = threading.Barrier(THREADS)
    transport = OfflineMcpTransport([], {}, before_response=lambda request: barrier.wait(timeout=5))

    def invoke(index: int) -> tuple[int, bytes]:
        body = json.dumps(
            {"jsonrpc": "2.0", "id": index, "method": "unknown", "params": {}}
        ).encode()
        return transport("https://offline.invalid/api/mcp", body, {}, 1.0)

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(invoke, range(THREADS)))

    assert sorted(call["call_number"] for call in transport.calls) == list(
        range(1, THREADS + 1)
    )
