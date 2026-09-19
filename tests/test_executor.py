"""Offline integration tests for the graph executor and graph-agent entry point."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from agentswitch.capabilities import build_manifest
from agentswitch.config import Config, load_config
from agentswitch.economics import MeteredClient
from agentswitch.executor import (
    GraphAgentError,
    eligible_ready_node,
    run_graph_agent,
)
from agentswitch.graph import GraphPatch, LiveGraph, NodeSpec, replay
from agentswitch.mcp_client import McpClient
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
    tools: McpClient,
    llm: MeteredClient,
    config: Config,
    *,
    reschedule_requested: bool = False,
    authority: dict[str, Any] | None = None,
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

    result = _run(tools, llm, config, reschedule_requested=True)

    assert result["reschedule"]["action"] == "escalated"
    assert result["reschedule"]["reason"] == "writes_disabled"
    assert result["reschedule"]["proposed"] == {
        "planned_start_date": "2026-09-19",
        "planned_end_date": "2026-09-21",
    }
    assert "WorkOrder.update" not in _tool_names(transport)


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


def test_write_authority_is_rejected_before_any_mcp_call(tmp_path, monkeypatch):
    """Spec: AI (Codex) Phase-four write authority is rejected before catalogue discovery."""
    config = _config(tmp_path, monkeypatch)
    tools, transport, llm, llm_transport = _clients(config, [])

    with pytest.raises(
        NotImplementedError, match="write authority arrives in phase 6"
    ):
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
