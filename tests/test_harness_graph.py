"""Offline harness tests for the graph subject and persisted audits."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from agentswitch.config import Config, load_config
from agentswitch.economics import MeteredClient
from agentswitch.executor import GraphAgentError, run_graph_agent
from agentswitch.harness import runner, subjects
from agentswitch.harness.audits import (
    capabilities_registered,
    journal_consistent,
    limits_respected,
    single_subject_write,
    terminal_last,
    write_after_target_read,
)
from agentswitch.harness.recorder import ReadOnlyTools, ScopedWriteTools
from agentswitch.harness.runner import (
    AUDIT_NAMES,
    SCHEMA_VERSION,
    HarnessPersistenceError,
    _restore_fixture,
    _score_run,
    _score_verdict,
    run_tasks,
)
from agentswitch.mcp_client import McpClient
from agentswitch.offline import (
    OfflineMcpTransport,
    offline_llm_client,
    scripted_tool_response,
)
from agentswitch.telemetry import build_spans

TODAY = date(2026, 9, 19)
TARGET = "WO-HARNESS"
RELATED = "WO-RELATED"
USER = "offline-user"


def _schema(
    properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


CATALOGUE = [
    {
        "name": "WorkOrder.get",
        "description": "Offline target read",
        "inputSchema": _schema({"id": {"type": "string"}}, ["id"]),
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    }
]
WRITE_CATALOGUE = [
    CATALOGUE[0],
    {
        "name": "WorkOrder.list",
        "description": "Offline work-order selection",
        "inputSchema": _schema(
            {
                "status": {"type": "string"},
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
            }
        ),
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "WorkOrder.update",
        "description": "Offline planned-date update",
        "inputSchema": _schema(
            {
                "id": {"type": "string"},
                "planned_start_date": {"type": "string"},
                "planned_end_date": {"type": "string"},
            },
            ["id"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
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
RELATED_RECORD = {
    **TARGET_RECORD,
    "id": RELATED,
    "number": RELATED,
}


def _write_task() -> dict[str, Any]:
    return {
        "id": "reschedule_own_draft",
        "request": "This draft work order is past its planned dates. Reschedule what you can.",
        "request_kind": "work_order_lateness",
        "selector": "own_draft_fixture",
        "expected": {
            "outcome": "answered",
            "is_late": False,
            "reschedule_action": "applied",
        },
        "brief_refusal": False,
        "reschedule": True,
        "writes": True,
        "expectation": "Applies an owned draft planned-date update.",
    }


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    return load_config(env_file=tmp_path / "missing.env")


def _addition(
    node_id: str,
    capability: str,
    arguments: dict[str, Any],
    *,
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "capability": capability,
        "arguments": arguments,
        "depends_on": depends_on or [],
    }


def _response(additions: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    return 200, scripted_tool_response(
        "plan_frontier",
        {"add": additions, "finish": False, "reason": "offline harness plan"},
    )


def _answer_addition(
    *,
    outcome: str = "answered",
    refusal_reason: str | None = None,
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    return _addition(
        "answer",
        "answer",
        {
            "outcome": outcome,
            "refusal_reason": refusal_reason,
            "prose": "Offline graph conclusion.",
        },
        depends_on=depends_on,
    )


def _real_agent_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    config = _config(tmp_path, monkeypatch)
    mcp_transport = OfflineMcpTransport(
        CATALOGUE,
        {"WorkOrder": [TARGET_RECORD, RELATED_RECORD]},
    )
    tools = McpClient(
        "https://offline.invalid",
        "offline-token",
        transport=mcp_transport,
    )
    answer = _answer_addition()
    raw_llm, _ = offline_llm_client(
        config,
        [
            _response([_addition("target", "WorkOrder.get", {"id": TARGET})]),
            _response(
                [
                    _addition(
                        "related",
                        "WorkOrder.get",
                        {"id": RELATED},
                        depends_on=["target"],
                    )
                ]
            ),
            _response([answer]),
            _response([answer]),
            _response([answer]),
        ],
    )
    llm = MeteredClient(raw_llm, config, sleep=lambda _: None)
    agent = run_graph_agent(
        tools,
        llm,
        request="Investigate the late work order.",
        target_id=TARGET,
        today=TODAY,
        own_user_id=USER,
        reschedule_requested=False,
        config=config,
        authority={"write": False, "reason": "task_read_only"},
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "task": {
            "id": "offline_graph",
            "request": "Investigate the late work order.",
            "request_kind": "work_order_lateness",
            "selector": "late_open_oldest",
            "expected": {"outcome": "answered"},
            "brief_refusal": False,
            "reschedule": False,
            "writes": False,
        },
        "tenant": "suryodaya",
        "today": TODAY.isoformat(),
        "run_file": "offline.json",
        "config": config.effective_record(),
        "economics": llm.ledger(),
        "selection": {"status": "selected", "target_id": TARGET},
        "subject": {"name": "graph_subject", "label": "offline"},
        "subject_output": {"agent": agent},
        "call_log": [],
        "harness_errors": [],
    }


def _renumber(journal: list[dict[str, Any]]) -> None:
    for sequence, event in enumerate(journal, start=1):
        event["seq"] = sequence


def _outside_seat_task() -> dict[str, Any]:
    return {
        "id": "refuse_outside_seat",
        "request": "Show the stock ledger for this item in every warehouse.",
        "request_kind": "stock_ledger",
        "selector": "none",
        "expected": {
            "outcome": "refused",
            "refusal_reason": ["outside_seat", "unsupported"],
        },
        "brief_refusal": True,
        "reschedule": False,
        "writes": False,
        "expectation": "Refuses because the request is outside the seat.",
    }


class _HarnessTransport:
    def __init__(self, mcp: OfflineMcpTransport) -> None:
        self.mcp = mcp
        self.login_calls = 0

    def __call__(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, bytes]:
        if url.endswith("/api/auth/login"):
            self.login_calls += 1
            return 200, b'{"token":"offline-token"}'
        return self.mcp(url, body, headers, timeout)


def _get_transport(
    url: str, headers: dict[str, str], timeout: float
) -> tuple[int, bytes]:
    del url, headers, timeout
    return 200, json.dumps({"id": USER}).encode()


def _env_file(tmp_path: Path) -> Path:
    path = tmp_path / "offline.env"
    path.write_text(
        "AS_URL_SURYODAYA=https://offline.invalid\n"
        "AS_EMAIL=offline@example.invalid\n"
        "AS_PASSWORD_SURYODAYA=offline-password\n",
        encoding="utf-8",
    )
    return path


def test_score_verdict_keeps_not_applicable_with_passing_audits() -> None:
    """Spec: AI (Codex) Passing audits cannot promote an all-N/A task, but a failing audit fails it."""
    selection = {
        "name": "selection",
        "verdict": "not_applicable",
        "reason": "draft writes not enabled",
        "evidence": {},
    }
    passing = [
        {
            "name": name,
            "verdict": "pass",
            "reason": "ok",
            "evidence": {},
        }
        for name in sorted(AUDIT_NAMES)
    ]

    assert _score_verdict([selection, *passing]) == "not_applicable"
    passing[0]["verdict"] = "fail"
    assert _score_verdict([selection, *passing]) == "fail"


def test_score_run_draft_disabled_stays_not_applicable_with_graph_audits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: AI (Codex) A draft-writes-disabled record stays N/A when its graph audits pass."""
    record = _real_agent_record(tmp_path, monkeypatch)
    record["task"]["writes"] = True
    record["selection"] = {
        "status": "draft_writes_disabled",
        "target_id": None,
    }

    score, _ = _score_run(
        record,
        base_url="https://offline.invalid",
        token="offline-token",
        transport=lambda *_: pytest.fail("draft N/A scoring must not call MCP"),
        secrets=(),
    )

    audits = [item for item in score["verifiers"] if item["name"] in AUDIT_NAMES]
    assert score["verdict"] == "not_applicable"
    assert len(audits) == 6
    assert all(item["verdict"] == "pass" for item in audits)


@pytest.mark.parametrize("tamper", ["state", "edge", "node"])
def test_journal_consistent_detects_persisted_graph_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    """Spec: AI (Codex) Real executor journals replay, while state, edge, and node tampering fails."""
    record = _real_agent_record(tmp_path, monkeypatch)

    assert journal_consistent(record)["verdict"] == "pass"
    changed = copy.deepcopy(record)
    graph = changed["subject_output"]["agent"]["graph"]
    if tamper == "state":
        graph["nodes"][0]["state"] = "running"
    elif tamper == "edge":
        assert graph["edges"]
        graph["edges"].pop()
    else:
        graph["nodes"].append(
            {
                "id": "extra",
                "capability": "WorkOrder.get",
                "arguments": {"id": "extra"},
                "frontier": 1,
                "state": "pending",
                "failure_reason": None,
                "outcome": None,
                "error_detail": None,
            }
        )

    assert journal_consistent(changed)["verdict"] == "fail"


def test_journal_consistent_detects_frontier_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: AI (Codex) Journal replay detects a changed persisted frontier."""
    record = _real_agent_record(tmp_path, monkeypatch)
    record["subject_output"]["agent"]["graph"]["nodes"][0]["frontier"] += 1

    assert journal_consistent(record)["verdict"] == "fail"


@pytest.mark.parametrize("key", ["error_detail", "failure_reason", "outcome"])
def test_journal_consistent_rejects_missing_nullable_node_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    """Spec: AI (Codex) Persisted nodes must include every nullable export field."""
    record = _real_agent_record(tmp_path, monkeypatch)

    assert journal_consistent(record)["verdict"] == "pass"
    changed = copy.deepcopy(record)
    node = changed["subject_output"]["agent"]["graph"]["nodes"][0]
    del node[key]

    result = journal_consistent(changed)
    assert result["verdict"] == "fail"
    assert result["evidence"] == {"node": node["id"], "key": key}


def _failed_and_blocked_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    config = _config(tmp_path, monkeypatch)
    config = replace(config, limits=replace(config.limits, replan="node"))
    mcp_transport = OfflineMcpTransport(CATALOGUE, {"WorkOrder": [RELATED_RECORD]})
    tools = McpClient(
        "https://offline.invalid",
        "offline-token",
        transport=mcp_transport,
    )
    answer = _answer_addition(
        outcome="refused", refusal_reason="source_unavailable"
    )
    raw_llm, _ = offline_llm_client(
        config,
        [
            _response(
                [
                    _addition("a_success", "WorkOrder.get", {"id": RELATED}),
                    _addition("failed", "WorkOrder.get", {"id": TARGET}),
                ]
            ),
            _response(
                [
                    _addition(
                        "blocked",
                        "WorkOrder.get",
                        {"id": "WO-BLOCKED"},
                        depends_on=["failed"],
                    ),
                ]
            ),
            _response([answer]),
        ],
    )
    llm = MeteredClient(raw_llm, config, sleep=lambda _: None)
    agent = run_graph_agent(
        tools,
        llm,
        request="Investigate the late work order.",
        target_id=TARGET,
        today=TODAY,
        own_user_id=USER,
        reschedule_requested=False,
        config=config,
        authority={"write": False, "reason": "task_read_only"},
    )
    return {
        "config": config.effective_record(),
        "economics": llm.ledger(),
        "subject_output": {"agent": agent},
    }


def _remove_blocked_event(record: dict[str, Any]) -> None:
    journal = record["subject_output"]["agent"]["journal"]
    journal[:] = [
        event
        for event in journal
        if not (event["type"] == "task_failed" and event.get("node") == "blocked")
    ]
    _renumber(journal)


def test_journal_consistent_accepts_real_failed_read_with_blocked_dependent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: AI (Codex) A real failed read and its blocked dependent replay consistently."""
    record = _failed_and_blocked_record(tmp_path, monkeypatch)

    assert journal_consistent(record)["verdict"] == "pass"


def test_journal_consistent_rejects_missing_block_event_with_pending_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: AI (Codex) A missing block event fails even when the descendant persists pending."""
    record = _failed_and_blocked_record(tmp_path, monkeypatch)
    _remove_blocked_event(record)
    blocked = next(
        node
        for node in record["subject_output"]["agent"]["graph"]["nodes"]
        if node["id"] == "blocked"
    )
    blocked["state"] = "pending"
    blocked["failure_reason"] = None
    blocked["error_detail"] = None

    result = journal_consistent(record)

    assert result["verdict"] == "fail"
    assert result["evidence"]["node"] == "blocked"


def test_journal_consistent_rejects_missing_block_event_with_blocked_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: AI (Codex) A persisted blocked descendant cannot replace its missing block event."""
    record = _failed_and_blocked_record(tmp_path, monkeypatch)
    _remove_blocked_event(record)

    result = journal_consistent(record)

    assert result["verdict"] == "fail"
    assert result["evidence"]["node"] == "blocked"


def test_journal_consistent_rejects_stray_block_event_for_independent_node() -> None:
    """Spec: AI (Codex) A block event outside the failed node's descendants fails."""
    additions = [
        _addition("parent", "WorkOrder.get", {"id": TARGET}),
        _addition(
            "child",
            "WorkOrder.get",
            {"id": RELATED},
            depends_on=["parent"],
        ),
        _addition("other", "WorkOrder.get", {"id": "WO-OTHER"}),
    ]
    failure_detail = {"error": {"message": "parent failed"}}
    journal = [
        {
            "seq": 1,
            "type": "graph_patched",
            "data": {
                "kind": "patch",
                "patch": {"add": additions},
                "frontier": 1,
            },
        },
        {"seq": 2, "type": "task_started", "node": "parent", "data": {}},
        {
            "seq": 3,
            "type": "task_failed",
            "node": "parent",
            "data": {"reason": "tool_error", "detail": failure_detail},
        },
        {
            "seq": 4,
            "type": "task_failed",
            "node": "child",
            "data": {"reason": "blocked", "detail": {"blocked_by": "parent"}},
        },
        {
            "seq": 5,
            "type": "task_failed",
            "node": "other",
            "data": {"reason": "blocked", "detail": {"blocked_by": "parent"}},
        },
    ]
    graph = {
        "nodes": [
            {
                "id": addition["id"],
                "capability": addition["capability"],
                "arguments": addition["arguments"],
                "frontier": 1,
                "state": "failed",
                "failure_reason": "tool_error"
                if addition["id"] == "parent"
                else "blocked",
                "outcome": None,
                "error_detail": failure_detail
                if addition["id"] == "parent"
                else {"blocked_by": "parent"},
            }
            for addition in additions
        ],
        "edges": [{"source": "parent", "target": "child"}],
    }
    record = {"subject_output": {"agent": {"journal": journal, "graph": graph}}}

    result = journal_consistent(record)

    assert result["verdict"] == "fail"
    assert "other" in result["reason"]


def test_journal_consistent_compares_failed_and_blocked_error_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: AI (Codex) Failure details replay exactly, including blocked-node detail."""
    record = _failed_and_blocked_record(tmp_path, monkeypatch)

    assert journal_consistent(record)["verdict"] == "pass"
    changed = copy.deepcopy(record)
    failed = next(
        node
        for node in changed["subject_output"]["agent"]["graph"]["nodes"]
        if node["id"] == "failed"
    )
    failed["error_detail"]["error"]["message"] = "tampered"

    assert journal_consistent(changed)["verdict"] == "fail"


def test_capabilities_registered_fails_for_unoffered_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: AI (Codex) A hand-edited unregistered node capability fails its audit."""
    record = _real_agent_record(tmp_path, monkeypatch)
    record["subject_output"]["agent"]["graph"]["nodes"][0]["capability"] = (
        "Unknown.read"
    )

    assert capabilities_registered(record)["verdict"] == "fail"


def test_limits_respected_fails_for_too_many_nodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: AI (Codex) A graph over its persisted maximum node count fails its audit."""
    record = _real_agent_record(tmp_path, monkeypatch)
    record["config"]["values"]["limits"]["max_nodes"] = 1

    assert limits_respected(record)["verdict"] == "fail"


@pytest.mark.parametrize("kind", ["hard", "soft"])
def test_limits_respected_fails_for_excessive_rejections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Spec: AI (Codex) Rejected patches over a configured repair limit fail the audit."""
    record = _real_agent_record(tmp_path, monkeypatch)
    limit = record["config"]["values"]["limits"][f"{kind}_repairs"]
    record["subject_output"]["agent"]["patches"]["rejected"] = [
        {"raw": {}, "kind": kind, "message": f"rejection {index}"}
        for index in range(limit + 2)
    ]
    record["subject_output"]["agent"]["planner"][f"{kind}_repairs_used"] = limit

    result = limits_respected(record)

    assert result["verdict"] == "fail"
    assert result["evidence"]["repair_rejections"][kind] == limit + 2


def _hard_repair_exhaustion_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    config = _config(tmp_path, monkeypatch)
    mcp_transport = OfflineMcpTransport(CATALOGUE, {"WorkOrder": [TARGET_RECORD]})
    tools = McpClient(
        "https://offline.invalid",
        "offline-token",
        transport=mcp_transport,
    )
    invalid = _response([_addition("bad", "Unknown.get", {})])
    raw_llm, _ = offline_llm_client(
        config,
        [invalid for _ in range(config.limits.hard_repairs + 1)],
    )
    llm = MeteredClient(raw_llm, config, sleep=lambda _: None)

    with pytest.raises(GraphAgentError) as caught:
        run_graph_agent(
            tools,
            llm,
            request="Investigate the late work order.",
            target_id=TARGET,
            today=TODAY,
            own_user_id=USER,
            reschedule_requested=False,
            config=config,
            authority={"write": False, "reason": "task_read_only"},
        )

    assert caught.value.original_type == "PlannerError"
    return {
        "config": config.effective_record(),
        "economics": llm.ledger(),
        "subject_output": {"agent": caught.value.agent},
    }


def test_limits_respected_allows_final_hard_rejection_on_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: AI (Codex) A real failed run may persist the final hard rejection after exhaustion."""
    record = _hard_repair_exhaustion_record(tmp_path, monkeypatch)

    assert limits_respected(record)["verdict"] == "pass"


def test_limits_respected_rejects_extra_hard_rejection_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: AI (Codex) Limit plus one hard rejections cannot pass without planner exhaustion."""
    record = _hard_repair_exhaustion_record(tmp_path, monkeypatch)
    terminal = record["subject_output"]["agent"]["journal"][-1]
    terminal["type"] = "run_finished"
    terminal["data"] = {"outcome": "answered"}

    assert limits_respected(record)["verdict"] == "fail"


def test_terminal_last_fails_when_a_node_starts_after_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: AI (Codex) A hand-edited task start after the answer start fails terminal ordering."""
    record = _real_agent_record(tmp_path, monkeypatch)
    journal = record["subject_output"]["agent"]["journal"]
    answer_index = next(
        index
        for index, event in enumerate(journal)
        if event["type"] == "task_started" and event["node"] == "answer"
    )
    journal[answer_index + 1 : answer_index + 1] = [
        {
            "seq": 0,
            "type": "graph_patched",
            "at": "2026-09-19T00:00:00Z",
            "monotonic_ns": 1,
            "round": 9,
            "node": None,
            "data": {
                "kind": "patch",
                "frontier": 9,
                "patch": {
                    "add": [
                        _addition("late_start", "WorkOrder.get", {"id": TARGET})
                    ],
                    "finish": False,
                    "reason": "tampered",
                },
            },
        },
        {
            "seq": 0,
            "type": "task_started",
            "at": "2026-09-19T00:00:00Z",
            "monotonic_ns": 2,
            "round": 9,
            "node": "late_start",
            "data": {},
        },
    ]
    _renumber(journal)

    assert terminal_last(record)["verdict"] == "fail"


def test_write_after_target_read_fails_for_early_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: AI (Codex) An action receipt before the successful target read fails its audit."""
    record = _real_agent_record(tmp_path, monkeypatch)
    journal = record["subject_output"]["agent"]["journal"]
    journal.insert(
        1,
        {
            "seq": 0,
            "type": "action_started",
            "at": "2026-09-19T00:00:00Z",
            "monotonic_ns": 1,
            "round": 1,
            "node": "reschedule",
            "data": {
                "action": "reschedule_work_order",
                "target_id": TARGET,
            },
        },
    )
    _renumber(journal)

    assert write_after_target_read(record)["verdict"] == "fail"


def test_single_subject_write_fails_for_two_update_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: AI (Codex) Two subject WorkOrder.update attempts fail the single-write audit."""
    record = _real_agent_record(tmp_path, monkeypatch)
    record["call_log"] = [
        {
            "phase": "subject",
            "tool": "WorkOrder.update",
            "arguments": {"id": TARGET},
            "outcome": outcome,
        }
        for outcome in ("ok", "refused_write")
    ]

    assert single_subject_write(record)["verdict"] == "fail"


def _receipt_audit_record(
    action_receipt: dict[str, Any], journal: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "call_log": [],
        "subject_output": {
            "agent": {
                "action_receipt": action_receipt,
                "journal": journal,
            }
        },
    }


def test_single_subject_write_fails_receipt_without_matching_action() -> None:
    """Spec: AI (Codex) A persisted receipt file must name a started and finished action."""
    record = _receipt_audit_record(
        {
            "file": "run.action.json",
            "node": "reschedule",
            "error": None,
        },
        [],
    )

    assert single_subject_write(record)["verdict"] == "fail"


def test_single_subject_write_accepts_receipt_with_matching_action_events() -> None:
    """Spec: AI (Codex) A receipt-backed action passes when its same node later finishes."""
    record = _receipt_audit_record(
        {
            "file": "run.action.json",
            "node": "reschedule",
            "error": None,
        },
        [
            {
                "seq": 1,
                "type": "action_started",
                "node": "reschedule",
                "data": {"receipt": "run.action.json"},
            },
            {
                "seq": 2,
                "type": "action_finished",
                "node": "reschedule",
                "data": {"status": "applied"},
            },
        ],
    )

    assert single_subject_write(record)["verdict"] == "pass"


def test_single_subject_write_accepts_failed_receipt_without_file() -> None:
    """Spec: AI (Codex) A receipt error with no persisted file is not an audit failure alone."""
    record = _receipt_audit_record(
        {
            "file": None,
            "node": "reschedule",
            "error": {"error": {"message": "disk full"}},
        },
        [],
    )

    assert single_subject_write(record)["verdict"] == "pass"


def test_graph_agent_error_persists_partial_journal_and_execution_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec: AI (Codex) GraphAgentError partial state is persisted and scored as subject failure."""
    config = _config(tmp_path, monkeypatch)
    raw_llm, _ = offline_llm_client(config, [])
    monkeypatch.setattr(runner.llm_client, "from_config", lambda *args, **kwargs: raw_llm)
    partial_agent = {
        "outcome": None,
        "refusal_reason": None,
        "prose": None,
        "reschedule": None,
        "model": "offline/model",
        "usage": {"calls": [], "totals": {}},
        "turns": 0,
        "repairs": [],
        "coverage": {},
        "manifest": {"offered": [], "dropped": []},
        "journal": [
            {
                "seq": 1,
                "type": "run_started",
                "at": "2026-09-19T00:00:00Z",
                "monotonic_ns": 1,
                "round": None,
                "node": None,
                "data": {},
            },
            {
                "seq": 2,
                "type": "run_failed",
                "at": "2026-09-19T00:00:01Z",
                "monotonic_ns": 2,
                "round": 1,
                "node": None,
                "data": {"error_type": "RuntimeError", "message": "offline failure"},
            },
        ],
        "graph": {"directed": True, "multigraph": False, "graph": {}, "nodes": [], "edges": []},
        "patches": {"accepted": [], "rejected": []},
        "authority": {"write": False, "reason": "task_read_only"},
        "action_receipt": {
            "file": None,
            "action": "reschedule_work_order",
            "target_id": None,
            "node": "reschedule",
            "error": {"error": {"message": "offline failure"}},
        },
        "missing": [],
        "planner": {
            "rounds": 0,
            "valid_replies": 0,
            "hard_repairs_used": 0,
            "soft_repairs_used": 0,
            "discarded_additions": 0,
        },
    }

    def fail_graph(*args: Any, **kwargs: Any) -> None:
        raise GraphAgentError(RuntimeError("offline failure"), partial_agent)

    monkeypatch.setattr(subjects, "run_graph_agent", fail_graph)
    mcp = OfflineMcpTransport([], {})
    transport = _HarnessTransport(mcp)
    summaries = run_tasks(
        "suryodaya",
        tasks=(_outside_seat_task(),),
        today=TODAY,
        transport=transport,
        get_transport=_get_transport,
        env_file=_env_file(tmp_path),
        runs_dir=tmp_path / "partial-runs",
        subject="graph",
    )

    with Path(summaries[0]["run_path"]).open(encoding="utf-8") as stream:
        persisted = json.load(stream)
    verifier = next(
        item
        for item in summaries[0]["verifiers"]
        if item["name"] == "subject_execution"
    )
    assert summaries[0]["verdict"] == "fail"
    assert verifier["verdict"] == "fail"
    assert persisted["subject_output"]["agent"]["journal"][-1]["type"] == (
        "run_failed"
    )
    assert persisted["subject_output"]["agent"]["action_receipt"] == (
        partial_agent["action_receipt"]
    )


def test_deterministic_record_gets_no_graph_audits() -> None:
    """Spec: AI (Codex) A deterministic record without a journal receives no graph audits."""
    record = {
        "schema_version": SCHEMA_VERSION,
        "task": {
            "id": "reschedule_own_draft",
            "writes": True,
            "expected": {"outcome": "answered"},
        },
        "tenant": "suryodaya",
        "today": TODAY.isoformat(),
        "run_file": "deterministic.json",
        "selection": {"status": "draft_writes_disabled", "target_id": None},
        "subject": {"name": "investigate_subject"},
        "subject_output": None,
        "call_log": [],
        "harness_errors": [],
    }

    score, _ = _score_run(
        record,
        base_url="https://offline.invalid",
        token="offline-token",
        transport=lambda *_: pytest.fail("deterministic N/A scoring must not call MCP"),
        secrets=(),
    )

    assert score["verdict"] == "not_applicable"
    assert not ({item["name"] for item in score["verifiers"]} & AUDIT_NAMES)


def test_schema_version_is_two() -> None:
    """Spec: AI (Codex) Harness run and score records use schema version 2.0."""
    assert SCHEMA_VERSION == "2.0"


def test_end_to_end_offline_graph_harness_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec: AI (Codex) The graph write flag still runs read-only tasks without authority."""
    config = _config(tmp_path, monkeypatch)
    raw_llm, _ = offline_llm_client(
        config,
        [
            _response(
                [
                    _answer_addition(
                        outcome="refused",
                        refusal_reason="outside_seat",
                    )
                ]
            )
        ],
    )
    monkeypatch.setattr(runner.llm_client, "from_config", lambda *args, **kwargs: raw_llm)
    mcp = OfflineMcpTransport([], {})
    transport = _HarnessTransport(mcp)

    summaries = run_tasks(
        "suryodaya",
        tasks=(_outside_seat_task(),),
        today=TODAY,
        transport=transport,
        get_transport=_get_transport,
        env_file=_env_file(tmp_path),
        runs_dir=tmp_path / "graph-runs",
        allow_draft_writes=True,
        subject="graph",
    )

    with Path(summaries[0]["run_path"]).open(encoding="utf-8") as stream:
        persisted = json.load(stream)
    assert transport.login_calls == 1
    assert summaries[0]["verdict"] == "pass"
    assert persisted["schema_version"] == "2.0"
    assert persisted["subject"]["name"] == "graph_subject"
    assert persisted["economics"]["summary"]["attempts"] == 1
    assert persisted["subject_output"]["agent"]["authority"] == {
        "write": False,
        "reason": "task_read_only",
    }


def test_real_offline_graph_record_builds_deterministic_nested_spans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec: AI (Codex) Real graph calls, attempts, rounds, and nodes form one span tree."""
    config = _config(tmp_path, monkeypatch)
    mcp_transport = OfflineMcpTransport(
        CATALOGUE,
        {"WorkOrder": [TARGET_RECORD, RELATED_RECORD]},
    )
    tools = ReadOnlyTools(
        McpClient(
            "https://offline.invalid",
            "offline-token",
            transport=mcp_transport,
        ),
        phase="subject",
    )
    answer = _answer_addition(depends_on=["target"])
    raw_llm, _ = offline_llm_client(
        config,
        [
            _response([_addition("target", "WorkOrder.get", {"id": TARGET})]),
            _response([answer]),
            _response([answer]),
            _response([answer]),
            _response([answer]),
        ],
    )
    llm = MeteredClient(raw_llm, config, sleep=lambda _: None)
    agent = run_graph_agent(
        tools,
        llm,
        request="Investigate the late work order.",
        target_id=TARGET,
        today=TODAY,
        own_user_id=USER,
        reschedule_requested=False,
        config=config,
        authority={"write": False, "reason": "task_read_only"},
    )
    record = {
        "run_file": "offline-graph.json",
        "task": {"id": "offline_graph"},
        "tenant": "suryodaya",
        "subject": {"name": "graph_subject"},
        "subject_output": {"agent": agent},
        "economics": llm.ledger(),
        "call_log": tools.calls,
        "timings": {"started_at": "2026-09-19T00:00:00+00:00"},
    }

    first = build_spans(record)
    second = build_spans(copy.deepcopy(record))
    spans = first["spans"]
    call_spans = [span for span in spans if span["span_id"].startswith("call:")]
    ids = {span["span_id"] for span in spans}

    assert first == second
    assert len(call_spans) == len(tools.calls)
    assert all(
        span["parent_id"] is None or span["parent_id"] in ids for span in spans
    )
    assert all(
        span["parent_id"].startswith("round:")
        for span in spans
        if "/attempt:" in span["span_id"]
    )
    node_calls = [
        span
        for span in call_spans
        if span["attributes"].get("agentswitch.node") is not None
    ]
    assert node_calls
    assert all(span["parent_id"] == "node:target" for span in node_calls)


def test_graph_and_deterministic_harness_runs_write_spans_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec: AI (Codex) Both offline harness subjects persist a spans file after scoring."""
    config = _config(tmp_path, monkeypatch)
    raw_llm, _ = offline_llm_client(
        config,
        [
            _response(
                [
                    _answer_addition(
                        outcome="refused",
                        refusal_reason="outside_seat",
                    )
                ]
            )
        ],
    )
    monkeypatch.setattr(runner.llm_client, "from_config", lambda *args, **kwargs: raw_llm)
    transport = _HarnessTransport(OfflineMcpTransport([], {}))

    graph_summary = run_tasks(
        "suryodaya",
        tasks=(_outside_seat_task(),),
        today=TODAY,
        transport=transport,
        get_transport=_get_transport,
        env_file=_env_file(tmp_path),
        runs_dir=tmp_path / "graph-spans-runs",
        subject="graph",
    )[0]
    deterministic_summary = run_tasks(
        "suryodaya",
        tasks=(_outside_seat_task(),),
        today=TODAY,
        transport=transport,
        get_transport=_get_transport,
        env_file=_env_file(tmp_path),
        runs_dir=tmp_path / "deterministic-spans-runs",
        subject="deterministic",
    )[0]

    for summary in (graph_summary, deterministic_summary):
        spans_path = Path(summary["spans_path"])
        assert spans_path.exists()
        assert Path(summary["score_path"]).exists()
        assert json.loads(spans_path.read_text(encoding="utf-8"))["schema"] == (
            "agentswitch.spans/1"
        )


def test_spans_write_failure_raises_after_score_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec: AI (Codex) A spans persistence failure is fatal after score persistence."""
    original_write = runner.write_exclusive

    def fail_spans(path: Path, *args: Any, **kwargs: Any) -> Path:
        if path.name.endswith(".spans.json"):
            raise OSError("offline spans failure")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(runner, "write_exclusive", fail_spans)
    runs_dir = tmp_path / "failed-spans-runs"

    with pytest.raises(HarnessPersistenceError, match="Could not persist spans"):
        run_tasks(
            "suryodaya",
            tasks=(_outside_seat_task(),),
            today=TODAY,
            transport=_HarnessTransport(OfflineMcpTransport([], {})),
            get_transport=_get_transport,
            env_file=_env_file(tmp_path),
            runs_dir=runs_dir,
            subject="deterministic",
        )

    assert len(list(runs_dir.glob("*.score.json"))) == 1
    assert list(runs_dir.glob("*.spans.json")) == []


def test_end_to_end_offline_graph_harness_writes_receipt_and_restores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec: AI (Codex) An authorized graph run receipts one update and restores its fixture."""
    config = _config(tmp_path, monkeypatch)
    raw_llm, _ = offline_llm_client(
        config,
        [
            _response([_addition("target", "WorkOrder.get", {"id": TARGET})]),
            _response(
                [
                    _addition(
                        "reschedule",
                        "reschedule_work_order",
                        {"work_order_id": TARGET},
                    )
                ]
            ),
            _response(
                [
                    _answer_addition(
                        outcome="refused",
                        refusal_reason="unsupported",
                        depends_on=["reschedule"],
                    )
                ]
            ),
        ],
    )
    monkeypatch.setattr(runner.llm_client, "from_config", lambda *args, **kwargs: raw_llm)
    original = {
        **TARGET_RECORD,
        "planned_start_date": "2026-09-20",
        "planned_end_date": "2026-09-22",
        "created_at": "2026-09-01T00:00:00Z",
    }
    mcp = OfflineMcpTransport(
        WRITE_CATALOGUE,
        {"WorkOrder": [original]},
        writable_tools={"WorkOrder.update"},
    )
    transport = _HarnessTransport(mcp)
    runs_dir = tmp_path / "writable-graph-runs"

    summaries = run_tasks(
        "suryodaya",
        tasks=(_write_task(),),
        today=TODAY,
        transport=transport,
        get_transport=_get_transport,
        env_file=_env_file(tmp_path),
        runs_dir=runs_dir,
        allow_draft_writes=True,
        subject="graph",
    )

    run_path = Path(summaries[0]["run_path"])
    score_path = Path(summaries[0]["score_path"])
    persisted = json.loads(run_path.read_text(encoding="utf-8"))
    score = json.loads(score_path.read_text(encoding="utf-8"))
    fixture_files = list(runs_dir.glob("*.fixture.json"))
    action_files = list(runs_dir.glob("*.action.json"))
    restore_files = list(runs_dir.glob("*.restore.json"))

    assert len(fixture_files) == 1
    assert len(action_files) == 1
    assert len(restore_files) == 1
    action = json.loads(action_files[0].read_text(encoding="utf-8"))
    assert set(action) == {"action", "target_id", "node", "round", "run_file"}
    assert action["action"] == "reschedule_work_order"
    assert action["target_id"] == TARGET
    assert action["run_file"] == run_path.name
    authority = persisted["subject_output"]["agent"]["authority"]
    assert authority == {
        "write": True,
        "reason": "draft_write_fixture_ready",
        "target_id": TARGET,
        "fixture": fixture_files[0].name,
    }
    receipt = persisted["subject_output"]["agent"]["action_receipt"]
    assert receipt["file"] == action_files[0].name
    assert persisted["harness_errors"] == []
    updates = [
        call
        for call in persisted["call_log"]
        if call.get("phase") == "subject"
        and call.get("tool") == "WorkOrder.update"
    ]
    assert len(updates) == 1
    assert summaries[0]["restore"]["status"] == "restored"
    assert mcp.records["WorkOrder"][0]["planned_start_date"] == (
        original["planned_start_date"]
    )
    assert mcp.records["WorkOrder"][0]["planned_end_date"] == (
        original["planned_end_date"]
    )
    verifiers = {item["name"]: item for item in score["verifiers"]}
    for name in (
        "writes_in_scope",
        "write_after_target_read",
        "single_subject_write",
    ):
        assert verifiers[name]["verdict"] == "pass"


def test_action_receipt_writer_refuses_a_second_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec: AI (Codex) The per-run receipt closure creates at most one action file."""
    config = _config(tmp_path, monkeypatch)
    raw_llm, _ = offline_llm_client(config, [])
    monkeypatch.setattr(runner.llm_client, "from_config", lambda *args, **kwargs: raw_llm)
    captured: list[Any] = []

    def capture_receipt(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args
        captured.append(kwargs["receipt"])
        raise RuntimeError("stop after capturing receipt")

    monkeypatch.setattr(runner, "graph_subject", capture_receipt)
    original = {
        **TARGET_RECORD,
        "planned_start_date": "2026-09-20",
        "planned_end_date": "2026-09-22",
        "created_at": "2026-09-01T00:00:00Z",
    }
    mcp = OfflineMcpTransport(
        WRITE_CATALOGUE,
        {"WorkOrder": [original]},
        writable_tools={"WorkOrder.update"},
    )
    runs_dir = tmp_path / "receipt-runs"

    run_tasks(
        "suryodaya",
        tasks=(_write_task(),),
        today=TODAY,
        transport=_HarnessTransport(mcp),
        get_transport=_get_transport,
        env_file=_env_file(tmp_path),
        runs_dir=runs_dir,
        allow_draft_writes=True,
        subject="graph",
    )

    assert len(captured) == 1
    payload = {
        "action": "reschedule_work_order",
        "target_id": TARGET,
        "node": "reschedule",
        "round": 2,
    }
    first = captured[0](payload)
    assert first.endswith(".action.json")
    with pytest.raises(RuntimeError, match="already written"):
        captured[0](payload)
    assert [path.name for path in runs_dir.glob("*.action.json")] == [first]


def test_restore_accepts_interrupted_subject_update_state(
    tmp_path: Path,
) -> None:
    """Spec: AI (Codex) Restore accepts dates from a dispatched update lacking an outcome."""
    original_dates = {
        "planned_start_date": "2026-09-20",
        "planned_end_date": "2026-09-22",
    }
    fixture_dates = {
        "planned_start_date": "2026-09-09",
        "planned_end_date": "2026-09-16",
    }
    subject_dates = {
        "planned_start_date": "2026-09-19",
        "planned_end_date": "2026-09-26",
    }
    record = {
        **TARGET_RECORD,
        **fixture_dates,
    }
    client = McpClient(
        "https://offline.invalid",
        "offline-token",
        transport=OfflineMcpTransport(
            WRITE_CATALOGUE,
            {"WorkOrder": [record]},
            writable_tools={"WorkOrder.update"},
        ),
    )
    tools = ScopedWriteTools(
        client,
        target_id=TARGET,
        own_user_id=USER,
        phase="subject",
    )
    tools.call_tool("WorkOrder.get", {"id": TARGET})
    tools.call_tool(
        "WorkOrder.update",
        {"id": TARGET, **subject_dates},
        allow_write=True,
    )
    interrupted = next(
        call
        for call in tools.calls
        if call.get("phase") == "subject"
        and call.get("tool") == "WorkOrder.update"
    )
    del interrupted["outcome"]
    fixture_path = tmp_path / "interrupted.fixture.json"
    fixture_path.write_text(
        json.dumps(
            {
                "pre_fixture": {**record, **original_dates},
                "intended_mutation": {"id": TARGET, **fixture_dates},
            }
        ),
        encoding="utf-8",
    )

    restored = _restore_fixture(
        tools,
        fixture_path=fixture_path,
        restore_path=tmp_path / "interrupted.restore.json",
        target_id=TARGET,
        own_user_id=USER,
        secrets=(),
        write_attempted=True,
        restore_required=True,
    )

    assert restored["status"] == "restored"
    produced = restored["compared_states"]["run_produced"]
    assert any(
        state.get("source") == "subject_update"
        and state.get("outcome") == "interrupted"
        and state.get("dates") == subject_dates
        for state in produced
    )
    current = tools.call_tool("WorkOrder.get", {"id": TARGET}).structured
    assert {
        "planned_start_date": current["planned_start_date"],
        "planned_end_date": current["planned_end_date"],
    } == original_dates
