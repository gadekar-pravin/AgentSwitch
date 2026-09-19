"""Offline integration tests for the graph executor and graph-agent entry point."""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from agentswitch import executor as executor_module
from agentswitch.answer import Store
from agentswitch.capabilities import build_manifest
from agentswitch.config import Config, load_config
from agentswitch.economics import MeteredClient
from agentswitch.executor import (
    GraphAgentError,
    eligible_ready_node,
    run_graph_agent,
)
from agentswitch.graph import GraphPatch, LiveGraph, NodeSpec, replay
from agentswitch.harness.audits import journal_consistent
from agentswitch.harness.recorder import ReadOnlyTools
from agentswitch.mcp_client import McpClient, ToolResult, TransportError
from agentswitch.offline import (
    OfflineMcpTransport,
    offline_llm_client,
    scripted_tool_response,
)

TODAY = date(2026, 9, 19)
TARGET = "WO-LATE"
USER = "user-1"
REQUEST = "This work order is late. Find out why and reschedule what you can."


def _schema(
    properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _tool(
    name: str,
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"Offline {name}",
        "inputSchema": _schema(properties, required),
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    }


PAGING = {
    "limit": {"type": "integer", "minimum": 0},
    "offset": {"type": "integer", "minimum": 0},
}
CATALOGUE = [
    _tool("WorkOrder.get", {"id": {"type": "string"}}, required=["id"]),
    _tool(
        "WorkOrder.list",
        {
            "bom_id": {"type": "string"},
            "status": {"type": "string"},
            "item_id": {"type": "string"},
            "sales_order_id": {"type": "string"},
            **PAGING,
        },
    ),
    _tool("MaterialRequest.list", {"work_order_id": {"type": "string"}, **PAGING}),
    _tool("SubcontractOrder.list", {"work_order_id": {"type": "string"}, **PAGING}),
    _tool("JobCard.list", {"work_order_id": {"type": "string"}, **PAGING}),
    _tool(
        "DowntimeEntry.list",
        {
            "work_order_id": {"type": "string"},
            "job_card_id": {"type": "string"},
            **PAGING,
        },
    ),
    _tool(
        "QualityInspection.list",
        {
            "reference_type": {
                "type": "string",
                "enum": ["WorkOrder", "JobCard"],
            },
            "reference_id": {"type": "string"},
            **PAGING,
        },
    ),
    _tool("EngineeringChangeOrder.list", PAGING),
    _tool("BOM.get", {"id": {"type": "string"}}, required=["id"]),
    _tool("BOM.list", PAGING),
    _tool("Workstation.list", PAGING),
    _tool("SalesOrder.get", {"id": {"type": "string"}}, required=["id"]),
    _tool("Item.get", {"id": {"type": "string"}}, required=["id"]),
    _tool(
        "endpoint.manufacturing.finite_schedule",
        {"horizon_days": {"type": "integer", "minimum": 1, "maximum": 90}},
        required=["horizon_days"],
    ),
]

TARGET_RECORD = {
    "id": TARGET,
    "number": TARGET,
    "status": "draft",
    "created_by": USER,
    "planned_start_date": "2026-09-01",
    "planned_end_date": "2026-09-03",
    "qty": 10,
    "produced_qty": 0,
}


def _records(**changes: Any) -> dict[str, list[dict[str, Any]]]:
    records = {
        "WorkOrder": [dict(TARGET_RECORD)],
        "MaterialRequest": [],
        "SubcontractOrder": [],
        "JobCard": [],
        "DowntimeEntry": [],
        "QualityInspection": [],
        "EngineeringChangeOrder": [],
        "BOM": [],
        "Workstation": [],
        "SalesOrder": [],
        "Item": [],
    }
    records.update(changes)
    return records


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    return load_config(env_file=tmp_path / "does-not-exist.env")


def _addition(
    node_id: str,
    capability: str,
    arguments: dict[str, Any],
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "capability": capability,
        "arguments": arguments,
        "depends_on": depends_on or [],
    }


def _patch(
    additions: list[dict[str, Any]], *, reason: str = "offline plan"
) -> dict[str, Any]:
    return {"add": additions, "finish": False, "reason": reason}


def _answer(
    *, outcome: str = "answered", refusal_reason: str | None = None
) -> dict[str, Any]:
    return _addition(
        "answer",
        "answer",
        {
            "outcome": outcome,
            "refusal_reason": refusal_reason,
            "prose": "Offline conclusion.",
        },
    )


def _response(patch: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return 200, scripted_tool_response("plan_frontier", patch)


def _clients(
    config: Config,
    responses: list[tuple[int, dict[str, Any]]],
    *,
    records: dict[str, list[dict[str, Any]]] | None = None,
    faults: dict[int, str] | None = None,
    before_response: Any | None = None,
) -> tuple[McpClient, OfflineMcpTransport, MeteredClient, Any]:
    mcp_transport = OfflineMcpTransport(
        CATALOGUE,
        records or _records(),
        endpoints={
            "endpoint.manufacturing.finite_schedule": {
                "status": "ok",
                "result": {
                    "orders": [
                        {
                            "work_order_id": TARGET,
                            "verdict": "late",
                            "days_late": 16,
                        }
                    ]
                },
            }
        },
        faults=faults,
        before_response=before_response,
    )
    tools = McpClient(
        "https://offline.invalid", "offline-token", transport=mcp_transport
    )
    raw_llm, llm_transport = offline_llm_client(config, responses)
    return (
        tools,
        mcp_transport,
        MeteredClient(raw_llm, config, sleep=lambda _: None),
        llm_transport,
    )


def _run(
    tools: Any,
    llm: MeteredClient,
    config: Config,
    *,
    reschedule_requested: bool = False,
    authority: dict[str, Any] | None = None,
    receipt: Any | None = None,
) -> dict[str, Any]:
    return run_graph_agent(
        tools,
        llm,
        request=REQUEST,
        target_id=TARGET,
        today=TODAY,
        own_user_id=USER,
        reschedule_requested=reschedule_requested,
        config=config,
        authority=authority or {"write": False},
        receipt=receipt,
    )


def _target_patch(*, node_id: str = "target") -> dict[str, Any]:
    return _patch([_addition(node_id, "WorkOrder.get", {"id": TARGET})])


def _complete_read_patch() -> dict[str, Any]:
    linked = {"work_order_id": TARGET}
    return _patch(
        [
            _addition("01_target", "WorkOrder.get", {"id": TARGET}),
            _addition("02_material", "MaterialRequest.list", linked),
            _addition("03_subcontract", "SubcontractOrder.list", linked),
            _addition("04_jobs", "JobCard.list", linked),
            _addition("05_downtime", "DowntimeEntry.list", linked),
            _addition(
                "06_quality",
                "QualityInspection.list",
                {"reference_type": "WorkOrder", "reference_id": TARGET},
            ),
            _addition("07_changes", "EngineeringChangeOrder.list", {}),
            _addition("08_boms", "BOM.list", {}),
            _addition("09_workstations", "Workstation.list", {}),
            _addition(
                "10_schedule",
                "endpoint.manufacturing.finite_schedule",
                {"horizon_days": 30},
            ),
        ]
    )


def _tool_names(transport: OfflineMcpTransport) -> list[str]:
    names = []
    for call in transport.calls:
        request = call.get("request")
        params = request.get("params") if isinstance(request, dict) else None
        if request.get("method") == "tools/call" and isinstance(params, dict):
            names.append(params.get("name"))
    return names


def _planner_payload(llm_transport: Any, call: int) -> dict[str, Any]:
    content = llm_transport.calls[call]["body"]["messages"][1]["content"]
    return json.loads(content)


def _called_tool(request: dict[str, Any] | None) -> str | None:
    if not isinstance(request, dict) or request.get("method") != "tools/call":
        return None
    params = request.get("params")
    return params.get("name") if isinstance(params, dict) else None


class _WritableTools:
    """Small in-memory tool client for exercising the real guarded write path."""

    def __init__(
        self,
        *,
        barrier: threading.Barrier | None = None,
        barrier_tools: set[str] | None = None,
        update_outcome_unknown: bool = False,
        confirmation_fails: bool = False,
    ) -> None:
        self.work_order = copy.deepcopy(TARGET_RECORD)
        self.barrier = barrier
        self.barrier_tools = barrier_tools or set()
        self.update_outcome_unknown = update_outcome_unknown
        self.confirmation_fails = confirmation_fails
        self.calls: list[dict[str, Any]] = []
        self.order: list[str] = []
        self.update_attempts = 0

    def list_tools(self) -> list[dict[str, Any]]:
        return copy.deepcopy(CATALOGUE)

    @staticmethod
    def _result(structured: Any) -> ToolResult:
        return ToolResult(
            structured=copy.deepcopy(structured),
            text="",
            is_error=False,
            raw={},
        )

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        allow_write: bool = False,
    ) -> ToolResult:
        call_arguments = dict(arguments or {})
        self.calls.append(
            {
                "name": name,
                "arguments": call_arguments,
                "allow_write": allow_write,
            }
        )
        self.order.append(name)
        if name in self.barrier_tools:
            assert self.barrier is not None
            self.barrier.wait(timeout=5)
        if name == "WorkOrder.get":
            if self.confirmation_fails and self.update_attempts:
                raise TransportError("confirmation failed")
            return self._result(self.work_order)
        if name == "WorkOrder.update":
            assert allow_write is True
            self.update_attempts += 1
            if self.update_outcome_unknown:
                raise TransportError("update failed", outcome_unknown=True)
            self.work_order.update(
                {
                    key: value
                    for key, value in call_arguments.items()
                    if key in {"planned_start_date", "planned_end_date"}
                }
            )
            return self._result(self.work_order)
        if name.endswith(".get"):
            return self._result({"id": call_arguments["id"]})
        raise AssertionError(f"unexpected fake tool call {name!r}")


def test_store_merge_appends_deep_copies_without_changing_target_snapshot():
    """Spec: AI (Codex). Store fragments merge in order without sharing mutable data."""
    destination = Store(
        target_before_reschedule={"id": TARGET, "status": "draft"},
        target_snapshot_pinned=True,
    )
    destination.add_record("WorkOrder", {"id": TARGET, "value": "old"})
    fragment = Store()
    fragment.add_get(
        "WorkOrder.get",
        {"id": TARGET},
        {"id": TARGET, "value": "new"},
        model_read=True,
    )
    fragment.add_list(
        "WorkOrder.list", {}, [{"id": TARGET, "value": "list"}], complete=True
    )
    fragment.endpoints["endpoint.test"] = [{"result": {"value": 1}}]
    fragment.endpoint_calls.append({"tool": "endpoint.test", "arguments": {}})

    destination.merge(fragment)
    fragment.records["WorkOrder"][TARGET][0]["value"] = "mutated"
    fragment.list_calls[0]["filters"]["later"] = True
    fragment.endpoints["endpoint.test"][0]["result"]["value"] = 2

    assert [
        row["value"] for row in destination.records["WorkOrder"][TARGET]
    ] == ["old", "new", "list"]
    assert destination.list_calls == [
        {"tool": "WorkOrder.list", "filters": {}, "complete": True}
    ]
    assert destination.endpoints["endpoint.test"] == [{"result": {"value": 1}}]
    assert destination.endpoint_calls == [
        {"tool": "endpoint.test", "arguments": {}}
    ]
    assert destination.successful_gets[0]["record"]["value"] == "new"
    assert destination.target_snapshot_pinned is True
    assert destination.target_before_reschedule == {
        "id": TARGET,
        "status": "draft",
    }

    pinned_fragment = Store(target_snapshot_pinned=True)
    with pytest.raises(ValueError, match="pinned target snapshot"):
        destination.merge(pinned_fragment)


def test_frontier_reads_overlap_and_keep_node_attribution(tmp_path, monkeypatch):
    """Spec: AI (Codex). Two frontier reads overlap and retain their own node ids."""
    config = _config(tmp_path, monkeypatch)
    config = replace(config, limits=replace(config.limits, max_workers=2))
    barrier = threading.Barrier(2)

    def before_response(request: dict[str, Any] | None) -> None:
        if _called_tool(request) in {"WorkOrder.get", "Item.get"}:
            barrier.wait(timeout=5)

    responses = [
        _response(
            _patch(
                [
                    _addition("a_target", "WorkOrder.get", {"id": TARGET}),
                    _addition("b_item", "Item.get", {"id": "ITEM-1"}),
                ]
            )
        ),
        _response(
            _patch([_answer(outcome="refused", refusal_reason="unsupported")])
        ),
    ]
    client, _, llm, _ = _clients(
        config,
        responses,
        records=_records(Item=[{"id": "ITEM-1", "name": "Widget"}]),
        before_response=before_response,
    )
    tools = ReadOnlyTools(client, phase="subject")

    result = _run(tools, llm, config)

    read_events = [
        event
        for event in result["journal"]
        if event["node"] in {"a_target", "b_item"}
        and event["type"] in {"task_started", "task_succeeded", "task_failed"}
    ]
    assert [event["type"] for event in read_events[:2]] == [
        "task_started",
        "task_started",
    ]
    assert {call["node"] for call in tools.calls if call["kind"] == "call_tool"} == {
        "a_target",
        "b_item",
    }


def test_frontier_never_exceeds_worker_cap(tmp_path, monkeypatch):
    """Spec: AI (Codex). A three-read frontier runs at most two reads at once."""
    config = _config(tmp_path, monkeypatch)
    config = replace(config, limits=replace(config.limits, max_workers=2))
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = 0
    maximum = 0
    arrivals = 0

    def before_response(request: dict[str, Any] | None) -> None:
        nonlocal active, maximum, arrivals
        if _called_tool(request) not in {"WorkOrder.get", "Item.get", "BOM.get"}:
            return
        with lock:
            active += 1
            maximum = max(maximum, active)
            arrival = arrivals
            arrivals += 1
        if arrival < 2:
            barrier.wait(timeout=5)
        with lock:
            active -= 1

    responses = [
        _response(
            _patch(
                [
                    _addition("a_target", "WorkOrder.get", {"id": TARGET}),
                    _addition("b_item", "Item.get", {"id": "ITEM-1"}),
                    _addition("c_bom", "BOM.get", {"id": "BOM-1"}),
                ]
            )
        ),
        _response(
            _patch([_answer(outcome="refused", refusal_reason="unsupported")])
        ),
    ]
    tools, _, llm, _ = _clients(
        config,
        responses,
        records=_records(
            Item=[{"id": "ITEM-1"}], BOM=[{"id": "BOM-1"}]
        ),
        before_response=before_response,
    )

    result = _run(tools, llm, config)

    assert result["outcome"] == "refused"
    assert maximum == 2
    running_count = 0
    replay_maximum = 0
    for event in result["journal"]:
        if event["type"] == "task_started":
            running_count += 1
            replay_maximum = max(replay_maximum, running_count)
        elif event["type"] in {"task_succeeded", "task_failed"}:
            running_count -= 1
    assert replay_maximum == 2


def test_evidence_merge_is_deterministic_across_completion_orders(
    tmp_path, monkeypatch
):
    """Spec: AI (Codex). Node-id merge order fixes record versions and answered raw."""
    created_stores: list[Store] = []
    original_store = executor_module.Store
    original_call_read = executor_module.reads.call_read
    coordination: dict[str, Any] = {}

    class TrackingStore(original_store):
        def __init__(self) -> None:
            super().__init__()
            created_stores.append(self)

    def differentiated_read(
        tools: Any,
        fragment: Store,
        tool: str,
        arguments: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        outcome = original_call_read(tools, fragment, tool, arguments, **kwargs)
        label = "get-version" if tool == "WorkOrder.get" else "list-version"
        for versions in fragment.records.get("WorkOrder", {}).values():
            for version in versions:
                version["merge_marker"] = label
        if tool == "WorkOrder.get" and isinstance(outcome.get("record"), dict):
            outcome["record"]["merge_marker"] = label
        for row in outcome.get("data", []):
            row["merge_marker"] = label
        finished = coordination.get(f"{tool}.finished")
        if isinstance(finished, threading.Event):
            finished.set()
        return outcome

    monkeypatch.setattr(executor_module, "Store", TrackingStore)
    monkeypatch.setattr(executor_module.reads, "call_read", differentiated_read)

    def one_run(order: str, workers: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        config = _config(tmp_path, monkeypatch)
        config = replace(config, limits=replace(config.limits, max_workers=workers))
        get_entered = threading.Event()
        list_entered = threading.Event()
        get_finished = threading.Event()
        list_finished = threading.Event()
        coordination.clear()
        coordination.update(
            {
                "WorkOrder.get.finished": get_finished,
                "WorkOrder.list.finished": list_finished,
            }
        )

        def before_response(request: dict[str, Any] | None) -> None:
            tool = _called_tool(request)
            if tool == "WorkOrder.get":
                get_entered.set()
                if order == "list-first":
                    assert list_finished.wait(timeout=5)
            elif tool == "WorkOrder.list":
                list_entered.set()
                if order == "get-first":
                    assert get_finished.wait(timeout=5)

        responses = [
            _response(
                _patch(
                    [
                        _addition("a_get", "WorkOrder.get", {"id": TARGET}),
                        _addition("z_list", "WorkOrder.list", {}),
                    ]
                )
            ),
            _response(_patch([_answer()])),
            _response(_patch([_answer()])),
            _response(_patch([_answer()])),
        ]
        before = len(created_stores)
        tools, _, llm, _ = _clients(
            config,
            responses,
            before_response=before_response if workers > 1 else None,
        )
        result = _run(tools, llm, config)
        shared = created_stores[before]
        return copy.deepcopy(shared.records["WorkOrder"][TARGET]), result["raw"]

    high_first = one_run("list-first", 2)
    low_first = one_run("get-first", 2)
    serial = one_run("serial", 1)

    assert high_first == low_first == serial
    assert [row["merge_marker"] for row in high_first[0]] == [
        "get-version",
        "list-version",
    ]
    assert high_first[1]["work_order"]["merge_marker"] == "list-version"


def test_incomplete_concurrent_read_merges_partial_evidence(tmp_path, monkeypatch):
    """Spec: AI (Codex). An incomplete concurrent scan retains its partial rows."""
    config = _config(tmp_path, monkeypatch)
    config = replace(config, limits=replace(config.limits, max_workers=2))
    barrier = threading.Barrier(2)
    original_call_read = executor_module.reads.call_read

    def before_response(request: dict[str, Any] | None) -> None:
        if _called_tool(request) in {"WorkOrder.get", "MaterialRequest.list"}:
            barrier.wait(timeout=5)

    def incomplete_read(
        tools: Any,
        fragment: Store,
        tool: str,
        arguments: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if tool != "MaterialRequest.list":
            return original_call_read(tools, fragment, tool, arguments, **kwargs)
        result = tools.call_tool(
            tool, {**arguments, "limit": 1, "offset": 0}, allow_write=False
        )
        rows = result.structured["data"]
        fragment.add_list(tool, arguments, rows, complete=False)
        return {
            "ok": True,
            "data": rows,
            "total": 2,
            "returned": len(rows),
            "complete": False,
        }

    monkeypatch.setattr(executor_module.reads, "call_read", incomplete_read)
    responses = [
        _response(
            _patch(
                [
                    _addition("a_target", "WorkOrder.get", {"id": TARGET}),
                    _addition(
                        "b_material",
                        "MaterialRequest.list",
                        {"work_order_id": TARGET},
                    ),
                ]
            )
        ),
        _response(_patch([_answer()])),
        _response(_patch([_answer()])),
        _response(_patch([_answer()])),
    ]
    tools, _, llm, _ = _clients(
        config,
        responses,
        records=_records(
            MaterialRequest=[
                {"id": "MR-1", "work_order_id": TARGET, "status": "pending"},
                {"id": "MR-2", "work_order_id": TARGET, "status": "pending"},
            ]
        ),
        before_response=before_response,
    )

    result = _run(tools, llm, config)

    node = next(
        item for item in result["graph"]["nodes"] if item["id"] == "b_material"
    )
    assert node["failure_reason"] == "incomplete_scan"
    assert "MR-1" in json.dumps(result["raw"])
    assert result["coverage"]["MaterialRequest.list"] == "missing"


def test_exclusive_waits_for_frontier_reads_to_finish(tmp_path, monkeypatch):
    """Spec: AI (Codex). Reschedule starts only after all ready reads are committed."""
    config = _config(tmp_path, monkeypatch)
    config = replace(config, limits=replace(config.limits, max_workers=2))
    barrier = threading.Barrier(2)

    def before_response(request: dict[str, Any] | None) -> None:
        if _called_tool(request) in {"Item.get", "BOM.get"}:
            barrier.wait(timeout=5)

    responses = [
        _response(_target_patch()),
        _response(
            _patch(
                [
                    _addition("a_item", "Item.get", {"id": "ITEM-1"}),
                    _addition("b_bom", "BOM.get", {"id": "BOM-1"}),
                    _addition(
                        "z_reschedule",
                        "reschedule_work_order",
                        {"work_order_id": TARGET},
                    ),
                ]
            )
        ),
        _response(
            _patch([_answer(outcome="refused", refusal_reason="unsupported")])
        ),
    ]
    tools, _, llm, _ = _clients(
        config,
        responses,
        records=_records(
            Item=[{"id": "ITEM-1"}], BOM=[{"id": "BOM-1"}]
        ),
        before_response=before_response,
    )

    result = _run(tools, llm, config, reschedule_requested=True)
    events = result["journal"]
    read_terminal = [
        event["seq"]
        for event in events
        if event["node"] in {"a_item", "b_bom"}
        and event["type"] in {"task_succeeded", "task_failed"}
    ]
    reschedule_started = next(
        event["seq"]
        for event in events
        if event["type"] == "task_started" and event["node"] == "z_reschedule"
    )
    action_started = next(
        event["seq"]
        for event in events
        if event["type"] == "action_started" and event["node"] == "z_reschedule"
    )
    action_finished = next(
        event["seq"]
        for event in events
        if event["type"] == "action_finished" and event["node"] == "z_reschedule"
    )

    assert max(read_terminal) < reschedule_started < action_started
    assert not any(
        event["type"] == "task_started"
        and event["node"] in {"a_item", "b_bom"}
        and action_started < event["seq"] < action_finished
        for event in events
    )


def test_authorized_reschedule_is_exclusive_after_parallel_frontier_reads(
    tmp_path, monkeypatch
):
    """Spec: AI (Codex). Parallel reads settle before an authorized reschedule runs alone."""
    config = _config(tmp_path, monkeypatch)
    config = replace(
        config,
        limits=replace(config.limits, max_workers=4, replan="frontier"),
    )
    barrier_tools = {"Item.get", "BOM.get", "SalesOrder.get"}
    barrier = threading.Barrier(len(barrier_tools))
    responses = [
        _response(_target_patch()),
        _response(
            _patch(
                [
                    _addition("a_item", "Item.get", {"id": "ITEM-1"}),
                    _addition("b_bom", "BOM.get", {"id": "BOM-1"}),
                    _addition(
                        "c_sales", "SalesOrder.get", {"id": "SO-1"}
                    ),
                    _addition(
                        "z_reschedule",
                        "reschedule_work_order",
                        {"work_order_id": TARGET},
                    ),
                ]
            )
        ),
        _response(_patch([_answer(outcome="refused", refusal_reason="unsupported")])),
    ]
    _unused, _, llm, _ = _clients(config, responses)
    tools = _WritableTools(barrier=barrier, barrier_tools=barrier_tools)

    result = _run(
        tools,
        llm,
        config,
        reschedule_requested=True,
        authority={"write": True},
        receipt=lambda _payload: "parallel.action.json",
    )

    events = result["journal"]
    read_nodes = {"a_item", "b_bom", "c_sales"}
    read_terminal = [
        event["seq"]
        for event in events
        if event["node"] in read_nodes
        and event["type"] in {"task_succeeded", "task_failed"}
    ]
    reschedule_started = next(
        event["seq"]
        for event in events
        if event["type"] == "task_started" and event["node"] == "z_reschedule"
    )
    reschedule_terminal = next(
        event["seq"]
        for event in events
        if event["node"] == "z_reschedule"
        and event["type"] in {"task_succeeded", "task_failed"}
    )
    assert len(read_terminal) == len(read_nodes)
    assert max(read_terminal) < reschedule_started
    assert not any(
        event["type"] == "task_started"
        and reschedule_started < event["seq"] < reschedule_terminal
        for event in events
    )
    assert tools.update_attempts == 1


def test_terminal_gate_is_preserved_with_four_workers(tmp_path, monkeypatch):
    """Spec: AI (Codex). Terminal work starts after every frontier read is terminal."""
    config = _config(tmp_path, monkeypatch)
    config = replace(config, limits=replace(config.limits, max_workers=4))
    barrier = threading.Barrier(3)

    def before_response(request: dict[str, Any] | None) -> None:
        if _called_tool(request) in {"WorkOrder.get", "Item.get", "BOM.get"}:
            barrier.wait(timeout=5)

    responses = [
        _response(
            _patch(
                [
                    _addition("a_target", "WorkOrder.get", {"id": TARGET}),
                    _addition("b_item", "Item.get", {"id": "ITEM-1"}),
                    _addition("c_bom", "BOM.get", {"id": "BOM-1"}),
                ]
            )
        ),
        _response(
            _patch([_answer(outcome="refused", refusal_reason="unsupported")])
        ),
    ]
    tools, _, llm, _ = _clients(
        config,
        responses,
        records=_records(
            Item=[{"id": "ITEM-1"}], BOM=[{"id": "BOM-1"}]
        ),
        before_response=before_response,
    )

    result = _run(tools, llm, config)
    answer_started = next(
        event["seq"]
        for event in result["journal"]
        if event["type"] == "task_started" and event["node"] == "answer"
    )
    read_terminal = [
        event["seq"]
        for event in result["journal"]
        if event["node"] in {"a_target", "b_item", "c_bom"}
        and event["type"] in {"task_succeeded", "task_failed"}
    ]
    assert max(read_terminal) < answer_started


def test_node_replan_runs_one_node_and_replans_after_each(tmp_path, monkeypatch):
    """Spec: AI (Codex). Node mode stays serial and replans after each completed node."""
    config = _config(tmp_path, monkeypatch)
    config = replace(
        config,
        limits=replace(config.limits, max_workers=4, replan="node"),
    )
    lock = threading.Lock()
    active = 0
    maximum = 0

    def before_response(request: dict[str, Any] | None) -> None:
        nonlocal active, maximum
        if _called_tool(request) not in {"WorkOrder.get", "Item.get"}:
            return
        with lock:
            active += 1
            maximum = max(maximum, active)
            active -= 1

    responses = [
        _response(
            _patch(
                [
                    _addition("a_target", "WorkOrder.get", {"id": TARGET}),
                    _addition("b_item", "Item.get", {"id": "ITEM-1"}),
                ]
            )
        ),
        _response(_patch([], reason="run remaining node")),
        _response(
            _patch([_answer(outcome="refused", refusal_reason="unsupported")])
        ),
    ]
    tools, _, llm, llm_transport = _clients(
        config,
        responses,
        records=_records(Item=[{"id": "ITEM-1"}]),
        before_response=before_response,
    )

    result = _run(tools, llm, config)

    assert result["outcome"] == "refused"
    assert maximum == 1
    assert len(llm_transport.calls) == 3
    second_nodes = {
        node["id"]: node["state"] for node in _planner_payload(llm_transport, 1)["nodes"]
    }
    third_nodes = {
        node["id"]: node["state"] for node in _planner_payload(llm_transport, 2)["nodes"]
    }
    assert second_nodes == {"a_target": "succeeded", "b_item": "pending"}
    assert third_nodes == {"a_target": "succeeded", "b_item": "succeeded"}


def test_fatal_worker_error_drains_other_reads_before_run_failed(
    tmp_path, monkeypatch
):
    """Spec: AI (Codex). Fatal worker errors drain and commit held reads before failure."""
    config = _config(tmp_path, monkeypatch)
    config = replace(config, limits=replace(config.limits, max_workers=2))
    crash_started = threading.Event()
    held_completed = threading.Event()
    original_call_read = executor_module.reads.call_read

    def before_response(request: dict[str, Any] | None) -> None:
        if _called_tool(request) == "WorkOrder.get":
            assert crash_started.wait(timeout=5)

    def crashing_read(
        tools: Any,
        fragment: Store,
        tool: str,
        arguments: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if tool == "Item.get":
            crash_started.set()
            raise RuntimeError("unexpected worker crash")
        outcome = original_call_read(tools, fragment, tool, arguments, **kwargs)
        if tool == "WorkOrder.get":
            held_completed.set()
        return outcome

    monkeypatch.setattr(executor_module.reads, "call_read", crashing_read)
    responses = [
        _response(
            _patch(
                [
                    _addition("a_held", "WorkOrder.get", {"id": TARGET}),
                    _addition("z_crash", "Item.get", {"id": "ITEM-1"}),
                ]
            )
        )
    ]
    tools, _, llm, _ = _clients(
        config,
        responses,
        records=_records(Item=[{"id": "ITEM-1"}]),
        before_response=before_response,
    )

    with pytest.raises(GraphAgentError) as caught:
        _run(tools, llm, config)

    assert caught.value.original_type == "RuntimeError"
    assert str(caught.value.original_error) == "unexpected worker crash"
    assert held_completed.is_set()
    assert caught.value.agent["journal"][-1]["type"] == "run_failed"
    held_terminal = next(
        event["seq"]
        for event in caught.value.agent["journal"]
        if event["node"] == "a_held"
        and event["type"] in {"task_succeeded", "task_failed"}
    )
    assert held_terminal < caught.value.agent["journal"][-1]["seq"]


def test_rejected_read_outcome_settles_every_started_node_before_run_failed(
    tmp_path, monkeypatch
):
    """Spec: AI (Codex). A rejected read outcome fails its node and drains peers."""
    config = _config(tmp_path, monkeypatch)
    config = replace(config, limits=replace(config.limits, max_workers=2))
    barrier = threading.Barrier(2)
    original_call_read = executor_module.reads.call_read

    def before_response(request: dict[str, Any] | None) -> None:
        if _called_tool(request) in {"WorkOrder.get", "Item.get"}:
            barrier.wait(timeout=5)

    def non_finite_read(
        tools: Any,
        fragment: Store,
        tool: str,
        arguments: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        outcome = original_call_read(tools, fragment, tool, arguments, **kwargs)
        if tool == "Item.get":
            outcome["record"]["invalid_number"] = float("nan")
        return outcome

    monkeypatch.setattr(executor_module.reads, "call_read", non_finite_read)
    responses = [
        _response(
            _patch(
                [
                    _addition("a_invalid", "Item.get", {"id": "ITEM-1"}),
                    _addition("z_peer", "WorkOrder.get", {"id": TARGET}),
                ]
            )
        )
    ]
    tools, _, llm, _ = _clients(
        config,
        responses,
        records=_records(Item=[{"id": "ITEM-1"}]),
        before_response=before_response,
    )

    with pytest.raises(GraphAgentError) as caught:
        _run(tools, llm, config)

    assert caught.value.original_type == "GraphError"
    assert "node outcome must be JSON-safe" in str(caught.value.original_error)
    agent = caught.value.agent
    terminal_counts = {
        node_id: sum(
            event["node"] == node_id
            and event["type"] in {"task_succeeded", "task_failed"}
            for event in agent["journal"]
        )
        for node_id in ("a_invalid", "z_peer")
    }
    assert terminal_counts == {"a_invalid": 1, "z_peer": 1}
    assert {node["id"]: node["state"] for node in agent["graph"]["nodes"]} == {
        "a_invalid": "failed",
        "z_peer": "succeeded",
    }
    assert agent["journal"][-1]["type"] == "run_failed"
    record = {"subject_output": {"agent": agent}}
    assert journal_consistent(record)["verdict"] == "pass"


def test_accepted_same_patch_discard_is_reported_to_next_round(
    tmp_path, monkeypatch
):
    """Spec: AI (Codex) An accepted patch tells the next round to repropose its discarded child."""
    config = _config(tmp_path, monkeypatch)
    responses = [
        _response(
            _patch(
                [
                    _addition("a", "WorkOrder.get", {"id": TARGET}),
                    _addition(
                        "b",
                        "MaterialRequest.list",
                        {"work_order_id": TARGET},
                        depends_on=["a"],
                    ),
                ]
            )
        ),
        _response(
            _patch(
                [
                    _addition(
                        "b",
                        "MaterialRequest.list",
                        {"work_order_id": TARGET},
                        depends_on=["a"],
                    )
                ]
            )
        ),
        _response(
            _patch([_answer(outcome="refused", refusal_reason="unsupported")])
        ),
    ]
    tools, _, llm, llm_transport = _clients(config, responses)

    _run(tools, llm, config)

    messages = _planner_payload(llm_transport, 1)["repair_messages"]
    assert len(messages) == 1
    assert "discarded addition 'b'" in messages[0]
    assert "depends on same-patch node 'a'; propose again next round" in messages[0]


def test_accepted_duplicate_discard_reports_covering_node_to_next_round(
    tmp_path, monkeypatch
):
    """Spec: AI (Codex) Accepted duplicate feedback includes its covering node, state, and outcome."""
    config = _config(tmp_path, monkeypatch)
    responses = [
        _response(_target_patch(node_id="target")),
        _response(
            _patch(
                [
                    _addition("again", "WorkOrder.get", {"id": TARGET}),
                    _addition(
                        "material",
                        "MaterialRequest.list",
                        {"work_order_id": TARGET},
                    ),
                ]
            )
        ),
        _response(
            _patch([_answer(outcome="refused", refusal_reason="unsupported")])
        ),
    ]
    tools, _, llm, llm_transport = _clients(config, responses)

    _run(tools, llm, config)

    messages = _planner_payload(llm_transport, 2)["repair_messages"]
    assert len(messages) == 1
    assert "discarded addition 'again'" in messages[0]
    assert "covering node 'target' is in state 'succeeded'" in messages[0]
    assert "outcome projection" in messages[0]
    assert TARGET in messages[0]


def test_exclusive_start_gate_blocks_both_directions():
    """Spec: AI (Codex) Exclusive work and other running work mutually block new starts."""
    manifest = build_manifest(CATALOGUE)

    regular_running = LiveGraph()
    regular_running.apply_patch(
        GraphPatch(
            add=(
                NodeSpec("regular", "WorkOrder.get", {"id": TARGET}),
                NodeSpec(
                    "exclusive",
                    "reschedule_work_order",
                    {"work_order_id": TARGET},
                ),
            ),
            finish=False,
            reason="gate test",
        )
    )
    regular_running.start("regular")
    assert eligible_ready_node(regular_running, manifest) is None

    exclusive_running = LiveGraph()
    exclusive_running.apply_patch(
        GraphPatch(
            add=(
                NodeSpec(
                    "exclusive",
                    "reschedule_work_order",
                    {"work_order_id": TARGET},
                ),
                NodeSpec("regular", "WorkOrder.get", {"id": TARGET}),
            ),
            finish=False,
            reason="gate test",
        )
    )
    exclusive_running.start("exclusive")
    assert eligible_ready_node(exclusive_running, manifest) is None


def test_reschedule_without_write_authority_returns_proposal_and_never_updates(
    tmp_path, monkeypatch
):
    """Spec: AI (Codex) Read-only authority returns a dated proposal without an update call."""
    config = _config(tmp_path, monkeypatch)
    responses = [
        _response(_target_patch()),
        _response(
            _patch(
                [
                    _addition(
                        "reschedule",
                        "reschedule_work_order",
                        {"work_order_id": TARGET},
                    )
                ]
            )
        ),
        _response(_patch([_answer(outcome="refused", refusal_reason="unsupported")])),
    ]
    tools, transport, llm, _ = _clients(config, responses)

    def unexpected_receipt(_payload: dict[str, Any]) -> str:
        pytest.fail("read-only reschedule must not request an action receipt")

    result = _run(
        tools,
        llm,
        config,
        reschedule_requested=True,
        receipt=unexpected_receipt,
    )

    assert result["reschedule"]["action"] == "escalated"
    assert result["reschedule"]["reason"] == "writes_disabled"
    assert result["reschedule"]["proposed"] == {
        "planned_start_date": "2026-09-19",
        "planned_end_date": "2026-09-21",
    }
    assert "WorkOrder.update" not in _tool_names(transport)
    assert result["action_receipt"] is None
    action_finished = next(
        event
        for event in result["journal"]
        if event["type"] == "action_finished"
    )
    assert action_finished["data"]["status"] == "escalated"


def test_write_authority_applies_once_after_receipt_and_records_journal_order(
    tmp_path, monkeypatch
):
    """Spec: AI (Codex). An authorized write persists its receipt before one guarded update."""
    config = _config(tmp_path, monkeypatch)
    responses = [
        _response(_target_patch()),
        _response(
            _patch(
                [
                    _addition(
                        "reschedule",
                        "reschedule_work_order",
                        {"work_order_id": TARGET},
                    )
                ]
            )
        ),
        _response(_patch([_answer(outcome="refused", refusal_reason="unsupported")])),
    ]
    _unused, _, llm, _ = _clients(config, responses)
    tools = _WritableTools()
    receipt_payloads: list[dict[str, Any]] = []

    def receipt(payload: dict[str, Any]) -> str:
        receipt_payloads.append(copy.deepcopy(payload))
        tools.order.append("receipt")
        return "run.action.json"

    result = _run(
        tools,
        llm,
        config,
        reschedule_requested=True,
        authority={"write": True, "source": "test"},
        receipt=receipt,
    )

    events = result["journal"]

    def event_seq(event_type: str, node: str) -> int:
        return next(
            event["seq"]
            for event in events
            if event["type"] == event_type and event["node"] == node
        )

    target_succeeded = event_seq("task_succeeded", "target")
    reschedule_started = event_seq("task_started", "reschedule")
    action_started = event_seq("action_started", "reschedule")
    action_finished = event_seq("action_finished", "reschedule")
    reschedule_succeeded = event_seq("task_succeeded", "reschedule")
    assert (
        target_succeeded
        < reschedule_started
        < action_started
        < action_finished
        < reschedule_succeeded
    )
    started = next(
        event
        for event in events
        if event["type"] == "action_started" and event["node"] == "reschedule"
    )
    finished = next(
        event
        for event in events
        if event["type"] == "action_finished" and event["node"] == "reschedule"
    )
    assert started["data"]["receipt"] == "run.action.json"
    assert finished["data"]["status"] == "applied"
    assert result["reschedule"]["action"] == "applied"
    assert result["action_receipt"] == {
        "file": "run.action.json",
        "action": "reschedule_work_order",
        "target_id": TARGET,
        "node": "reschedule",
        "error": None,
    }
    assert receipt_payloads == [
        {
            "action": "reschedule_work_order",
            "target_id": TARGET,
            "node": "reschedule",
            "round": 2,
        }
    ]
    updates = [call for call in tools.calls if call["name"] == "WorkOrder.update"]
    assert len(updates) == 1
    assert tools.order.index("receipt") < tools.order.index("WorkOrder.update")


def test_receipt_failure_stops_authorized_write_and_fails_run(tmp_path, monkeypatch):
    """Spec: AI (Codex). A receipt error fails the node before action start or update."""
    config = _config(tmp_path, monkeypatch)
    responses = [
        _response(_target_patch()),
        _response(
            _patch(
                [
                    _addition(
                        "reschedule",
                        "reschedule_work_order",
                        {"work_order_id": TARGET},
                    )
                ]
            )
        ),
    ]
    _unused, _, llm, _ = _clients(config, responses)
    tools = _WritableTools()

    def receipt(_payload: dict[str, Any]) -> str:
        raise OSError("receipt disk full")

    with pytest.raises(GraphAgentError) as caught:
        _run(
            tools,
            llm,
            config,
            reschedule_requested=True,
            authority={"write": True},
            receipt=receipt,
        )

    agent = caught.value.agent
    assert not any(call["name"] == "WorkOrder.update" for call in tools.calls)
    assert not any(
        event["type"] == "action_started" for event in agent["journal"]
    )
    failure = next(
        event
        for event in agent["journal"]
        if event["type"] == "task_failed" and event["node"] == "reschedule"
    )
    assert failure["data"]["reason"] == "receipt_failed"
    assert agent["action_receipt"]["file"] is None
    assert agent["action_receipt"]["error"]["error"]["message"] == (
        "receipt disk full"
    )


def test_unknown_write_status_succeeds_node_without_retry(tmp_path, monkeypatch):
    """Spec: AI (Codex). An uncertain update plus failed confirmation reports unknown once."""
    config = _config(tmp_path, monkeypatch)
    responses = [
        _response(_target_patch()),
        _response(
            _patch(
                [
                    _addition(
                        "reschedule",
                        "reschedule_work_order",
                        {"work_order_id": TARGET},
                    )
                ]
            )
        ),
        _response(_patch([_answer(outcome="refused", refusal_reason="unsupported")])),
    ]
    _unused, _, llm, _ = _clients(config, responses)
    tools = _WritableTools(
        update_outcome_unknown=True,
        confirmation_fails=True,
    )

    result = _run(
        tools,
        llm,
        config,
        reschedule_requested=True,
        authority={"write": True},
        receipt=lambda _payload: "run.action.json",
    )

    finished = next(
        event
        for event in result["journal"]
        if event["type"] == "action_finished"
    )
    node = next(
        item for item in result["graph"]["nodes"] if item["id"] == "reschedule"
    )
    assert finished["data"]["status"] == "unknown"
    assert finished["data"]["result"]["action"] == "write_failed"
    assert finished["data"]["result"]["reason"] == "outcome_unknown"
    assert node["state"] == "succeeded"
    assert tools.update_attempts == 1


def test_missing_read_soft_repairs_are_exhausted_into_unknowns(tmp_path, monkeypatch):
    """Spec: AI (Codex) Exact missing reads consume soft repairs then appear as unknowns."""
    config = _config(tmp_path, monkeypatch)
    answer_patch = _patch([_answer()])
    responses = [
        _response(_target_patch()),
        _response(answer_patch),
        _response(answer_patch),
        _response(answer_patch),
    ]
    tools, _, llm, _ = _clients(config, responses)

    result = _run(tools, llm, config)

    exact = f'MaterialRequest.list {{"work_order_id":"{TARGET}"}}'
    assert result["outcome"] == "answered"
    assert len(result["repairs"]) == config.limits.soft_repairs
    assert all(exact in message for message in result["repairs"])
    assert exact in result["missing"]
    assert any(
        "MaterialRequest.list was not read completely" in unknown
        for unknown in result["raw"]["unknowns"]
    )


def test_incomplete_list_scan_fails_node_keeps_rows_and_stays_missing(
    tmp_path, monkeypatch
):
    """Spec: AI (Codex) An early empty page fails the node but retains partial rows as incomplete."""
    config = _config(tmp_path, monkeypatch)
    material_rows = [
        {"id": f"MR-{index}", "work_order_id": TARGET, "status": "pending"}
        for index in range(1001)
    ]
    first = _patch(
        [
            _addition("a_target", "WorkOrder.get", {"id": TARGET}),
            _addition(
                "b_material",
                "MaterialRequest.list",
                {"work_order_id": TARGET},
            ),
        ]
    )
    answer_patch = _patch([_answer()])
    responses = [
        _response(first),
        _response(answer_patch),
        _response(answer_patch),
        _response(answer_patch),
    ]
    tools, _, llm, _ = _clients(
        config,
        responses,
        records=_records(MaterialRequest=material_rows),
        faults={6: "empty_page"},
    )

    result = _run(tools, llm, config)

    node = next(item for item in result["graph"]["nodes"] if item["id"] == "b_material")
    assert node["state"] == "failed"
    assert node["failure_reason"] == "incomplete_scan"
    assert node["error_detail"]["data"]
    assert node["error_detail"]["full_returned"] == 1000
    assert result["coverage"]["MaterialRequest.list"] == "missing"
    assert any("MaterialRequest.list" in item for item in result["missing"])


def test_graph_list_reads_use_configured_page_size_and_page_to_completion(
    tmp_path, monkeypatch
):
    """Spec: AI (Codex) Graph list reads use the configured limit on every page."""
    config = _config(tmp_path, monkeypatch)
    config = replace(config, limits=replace(config.limits, page_size=2))
    material_rows = [
        {"id": f"MR-{index}", "work_order_id": TARGET, "status": "pending"}
        for index in range(5)
    ]
    first = _patch(
        [
            _addition("a_target", "WorkOrder.get", {"id": TARGET}),
            _addition(
                "b_material",
                "MaterialRequest.list",
                {"work_order_id": TARGET},
            ),
        ]
    )
    answer_patch = _patch([_answer()])
    responses = [
        _response(first),
        _response(answer_patch),
        _response(answer_patch),
        _response(answer_patch),
    ]
    tools, transport, llm, _ = _clients(
        config,
        responses,
        records=_records(MaterialRequest=material_rows),
    )

    result = _run(tools, llm, config)

    node = next(item for item in result["graph"]["nodes"] if item["id"] == "b_material")
    list_arguments = [
        call["request"]["params"]["arguments"]
        for call in transport.calls
        if call["request"].get("method") == "tools/call"
        and call["request"]["params"].get("name") == "MaterialRequest.list"
    ]
    assert node["state"] == "succeeded"
    assert node["outcome"]["complete"] is True
    assert node["outcome"]["returned"] == 5
    assert [arguments["limit"] for arguments in list_arguments] == [2, 2, 2]
    assert [arguments["offset"] for arguments in list_arguments] == [0, 2, 4]


def test_terminal_waits_until_pending_read_finishes_in_node_replan(
    tmp_path, monkeypatch
):
    """Spec: AI (Codex) A terminal proposal is repaired until every pending read has finished."""
    config = _config(tmp_path, monkeypatch)
    config = replace(config, limits=replace(config.limits, replan="node"))
    first = _patch(
        [
            _addition("a_target", "WorkOrder.get", {"id": TARGET}),
            _addition("z_changes", "EngineeringChangeOrder.list", {}),
        ]
    )
    responses = [
        _response(first),
        _response(
            _patch([_answer(outcome="refused", refusal_reason="unsupported")])
        ),
        _response(_patch([], reason="let pending work run")),
        _response(
            _patch([_answer(outcome="refused", refusal_reason="unsupported")])
        ),
    ]
    tools, _, llm, _ = _clients(config, responses)

    result = _run(tools, llm, config)

    events = result["journal"]
    read_finished = next(
        event["seq"]
        for event in events
        if event["type"] == "task_succeeded" and event["node"] == "z_changes"
    )
    answer_started = next(
        event["seq"]
        for event in events
        if event["type"] == "task_started" and event["node"] == "answer"
    )
    assert read_finished < answer_started
    assert any("no node is pending or running" in item for item in result["repairs"])


def test_budget_refusal_is_failed_run_before_llm_transport(tmp_path, monkeypatch):
    """Spec: AI (Codex) An unaffordable planner attempt fails before transport dispatch."""
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    source = Path("config/agentswitch.toml").read_text(encoding="utf-8")
    config_path = tmp_path / "small-budget.toml"
    config_path.write_text(
        source.replace("run_usd = 0.25", "run_usd = 0.000001"),
        encoding="utf-8",
    )
    config = load_config(config_path, env_file=tmp_path / "does-not-exist.env")
    tools, _, llm, llm_transport = _clients(
        config, [_response(_target_patch())]
    )

    with pytest.raises(GraphAgentError) as caught:
        _run(tools, llm, config)

    assert caught.value.original_type == "BudgetExceeded"
    assert llm_transport.calls == []
    assert caught.value.agent["outcome"] is None
    assert caught.value.agent["journal"][-1]["type"] == "run_failed"


def test_full_late_order_run_builds_replayable_answer_and_proposal(
    tmp_path, monkeypatch
):
    """Spec: AI (Codex) Complete reads, proposal, and answer produce a replayable successful run."""
    config = _config(tmp_path, monkeypatch)
    responses = [
        _response(_complete_read_patch()),
        _response(
            _patch(
                [
                    _addition(
                        "reschedule",
                        "reschedule_work_order",
                        {"work_order_id": TARGET},
                    )
                ]
            )
        ),
        _response(_patch([_answer()])),
    ]
    tools, _, llm, _ = _clients(config, responses)

    result = _run(tools, llm, config, reschedule_requested=True)

    assert result["outcome"] == "answered"
    assert result["raw"]["work_order_id"] == TARGET
    assert result["reschedule"]["reason"] == "writes_disabled"
    assert result["journal"][-1]["type"] == "run_finished"
    assert replay(result["journal"]).export() == result["graph"]
    assert {"source": "01_target", "target": "reschedule"} in result["graph"]["edges"]


def test_not_found_refusal_has_no_raw_answer(tmp_path, monkeypatch):
    """Spec: AI (Codex) A not-found refusal finishes without constructing classified raw claims."""
    config = _config(tmp_path, monkeypatch)
    responses = [
        _response(_patch([_answer(outcome="refused", refusal_reason="not_found")]))
    ]
    tools, _, llm, _ = _clients(config, responses)

    result = _run(tools, llm, config)

    assert result["outcome"] == "refused"
    assert result["refusal_reason"] == "not_found"
    assert "raw" not in result


def test_failed_target_read_not_found_detail_reaches_next_planner_request(
    tmp_path, monkeypatch
):
    """Spec: AI (Codex) A failed target read gives the next planner its not-found detail."""
    config = _config(tmp_path, monkeypatch)
    responses = [
        _response(_target_patch()),
        _response(
            _patch(
                [
                    {
                        **_answer(outcome="refused", refusal_reason="not_found"),
                        "depends_on": ["target"],
                    }
                ]
            )
        ),
    ]
    tools, _, llm, llm_transport = _clients(
        config,
        responses,
        records=_records(WorkOrder=[]),
    )

    result = _run(tools, llm, config)
    target = _planner_payload(llm_transport, 1)["nodes"][0]

    assert result["refusal_reason"] == "not_found"
    assert result["planner"]["hard_repairs_used"] == 0
    assert {"source": "target", "target": "answer"} not in result["graph"]["edges"]
    assert target["state"] == "failed"
    assert '"type":"not_found"' in target["error_projection"]
    assert f"WorkOrder.get id '{TARGET}' not found" in target["error_projection"]


def test_failed_parent_blocks_child_without_starting_it(tmp_path, monkeypatch):
    """Spec: AI (Codex) A failed parent immediately blocks its pending child before any MCP call."""
    config = _config(tmp_path, monkeypatch)
    config = replace(config, limits=replace(config.limits, replan="node"))
    responses = [
        _response(
            _patch(
                [
                    _addition("a_target", "WorkOrder.get", {"id": TARGET}),
                    _addition("z_missing", "BOM.get", {"id": "missing"}),
                ]
            )
        ),
        _response(
            _patch(
                [
                    _addition(
                        "child",
                        "MaterialRequest.list",
                        {"work_order_id": TARGET},
                        depends_on=["z_missing"],
                    )
                ]
            )
        ),
        _response(
            _patch([_answer(outcome="refused", refusal_reason="source_unavailable")])
        ),
    ]
    tools, transport, llm, _ = _clients(config, responses)

    result = _run(tools, llm, config)

    child = next(item for item in result["graph"]["nodes"] if item["id"] == "child")
    assert child["state"] == "failed"
    assert child["failure_reason"] == "blocked"
    assert not any(
        event["type"] == "task_started" and event["node"] == "child"
        for event in result["journal"]
    )
    assert "MaterialRequest.list" not in _tool_names(transport)


def test_reschedule_dependency_is_added_when_planner_omits_it(tmp_path, monkeypatch):
    """Spec: AI (Codex) The latest successful target read is added as reschedule ancestry."""
    config = _config(tmp_path, monkeypatch)
    responses = [
        _response(_target_patch(node_id="target_read")),
        _response(
            _patch(
                [
                    _addition(
                        "reschedule",
                        "reschedule_work_order",
                        {"work_order_id": TARGET},
                    )
                ]
            )
        ),
        _response(_patch([_answer(outcome="refused", refusal_reason="unsupported")])),
    ]
    tools, _, llm, _ = _clients(config, responses)

    result = _run(tools, llm, config, reschedule_requested=True)

    assert {"source": "target_read", "target": "reschedule"} in result["graph"]["edges"]
    assert any(
        event["type"] == "graph_patched"
        and event["data"].get("kind") == "edge"
        for event in result["journal"]
    )


def test_second_reschedule_node_fails_before_receipt_or_update(tmp_path, monkeypatch):
    """Spec: AI (Codex). The executor latch blocks a second reschedule independently of planning."""
    config = _config(tmp_path, monkeypatch)
    _unused, _, llm, _ = _clients(config, [])
    tools = _WritableTools()
    receipt_payloads: list[dict[str, Any]] = []
    patches = [
        GraphPatch(
            add=(NodeSpec("target", "WorkOrder.get", {"id": TARGET}),),
            finish=False,
            reason="target",
        ),
        GraphPatch(
            add=(
                NodeSpec(
                    "reschedule_one",
                    "reschedule_work_order",
                    {"work_order_id": TARGET},
                ),
            ),
            finish=False,
            reason="first write",
        ),
        GraphPatch(
            add=(
                NodeSpec(
                    "reschedule_two",
                    "reschedule_work_order",
                    {"work_order_id": TARGET},
                ),
            ),
            finish=False,
            reason="forced second write",
        ),
        GraphPatch(
            add=(
                NodeSpec(
                    "answer",
                    "answer",
                    {
                        "outcome": "refused",
                        "refusal_reason": "unsupported",
                        "prose": "Done.",
                    },
                ),
            ),
            finish=False,
            reason="finish",
        ),
    ]

    def bypass_planner(_client: Any, **kwargs: Any) -> Any:
        return executor_module.planner.PlannerDecision(
            patch=patches.pop(0),
            state=kwargs["state"],
        )

    monkeypatch.setattr(executor_module.planner, "plan_frontier", bypass_planner)

    def receipt(payload: dict[str, Any]) -> str:
        receipt_payloads.append(copy.deepcopy(payload))
        return "once.action.json"

    result = _run(
        tools,
        llm,
        config,
        reschedule_requested=True,
        authority={"write": True},
        receipt=receipt,
    )

    second = next(
        node
        for node in result["graph"]["nodes"]
        if node["id"] == "reschedule_two"
    )
    second_events = [
        event
        for event in result["journal"]
        if event["node"] == "reschedule_two"
    ]
    assert second["state"] == "failed"
    assert second["failure_reason"] == "reschedule_already_attempted"
    assert [event["type"] for event in second_events] == [
        "task_started",
        "task_failed",
    ]
    assert second_events[-1]["data"]["detail"] == {"target_id": TARGET}
    assert len(receipt_payloads) == 1
    assert tools.update_attempts == 1


def test_write_authority_without_receipt_fails_before_any_mcp_call(
    tmp_path, monkeypatch
):
    """Spec: AI (Codex). Write authority without a receipt writer fails before discovery."""
    config = _config(tmp_path, monkeypatch)
    tools, transport, llm, llm_transport = _clients(config, [])

    with pytest.raises(ValueError, match="requires an action receipt writer"):
        _run(tools, llm, config, authority={"write": True})

    assert transport.calls == []
    assert llm_transport.calls == []


def test_hard_repair_exhaustion_raises_with_partial_graph_state(
    tmp_path, monkeypatch
):
    """Spec: AI (Codex) Exhausted hard repairs fail with journal, graph, and planner counts."""
    config = _config(tmp_path, monkeypatch)
    invalid = _response(
        _patch([_addition("bad", "Unknown.get", {})], reason="invalid")
    )
    responses = [invalid for _ in range(config.limits.hard_repairs + 1)]
    tools, _, llm, _ = _clients(config, responses)

    with pytest.raises(GraphAgentError) as caught:
        _run(tools, llm, config)

    agent = caught.value.agent
    assert caught.value.original_type == "PlannerError"
    assert agent["journal"][-1]["type"] == "run_failed"
    assert agent["planner"]["hard_repairs_used"] == config.limits.hard_repairs
    assert agent["planner"]["valid_replies"] == config.limits.hard_repairs + 1
    assert len(agent["patches"]["rejected"]) == config.limits.hard_repairs + 1
