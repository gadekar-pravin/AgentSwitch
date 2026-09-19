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
from agentswitch.harness.runner import (
    AUDIT_NAMES,
    SCHEMA_VERSION,
    HarnessConfigurationError,
    _run_tasks,
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
        authority={"write": False, "reason": "phase_4_no_write_authority"},
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
        authority={"write": False, "reason": "phase_4_no_write_authority"},
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
            authority={"write": False, "reason": "phase_4_no_write_authority"},
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


def test_graph_write_flag_is_rejected_before_login() -> None:
    """Spec: AI (Codex) Graph draft-write authority is a configuration error before login."""
    calls = 0

    def forbidden_login(*args: Any, **kwargs: Any) -> tuple[int, bytes]:
        nonlocal calls
        calls += 1
        pytest.fail("login transport must not be called")

    with pytest.raises(
        HarnessConfigurationError,
        match="the graph subject has no write authority until phase 6",
    ):
        _run_tasks(
            "suryodaya",
            failed_restores=[],
            tasks=(_outside_seat_task(),),
            transport=forbidden_login,
            allow_draft_writes=True,
            subject="graph",
        )

    assert calls == 0


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
        "authority": {"write": False, "reason": "phase_4_no_write_authority"},
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
    """Spec: AI (Codex) Offline run_tasks dispatches graph, meters it, persists it, and passes."""
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
        "reason": "phase_4_no_write_authority",
    }
