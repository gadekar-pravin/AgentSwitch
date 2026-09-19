"""Tests for the persisted graph-run concurrency checker."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from agentswitch.harness.concurrency_check import check_files, check_record, main


def _record() -> dict:
    nodes = [
        {
            "id": "work_order",
            "capability": "WorkOrder.get",
            "arguments": {"id": "WO-1"},
            "state": "succeeded",
            "frontier": 1,
        },
        {
            "id": "bom",
            "capability": "BOM.list",
            "arguments": {"work_order_id": "WO-1"},
            "state": "succeeded",
            "frontier": 1,
        },
        {
            "id": "answer",
            "capability": "answer",
            "arguments": {},
            "state": "succeeded",
            "frontier": 2,
        },
    ]
    journal = [
        _event(1, "run_started", None, 50),
        _event(2, "task_started", "work_order", 100),
        _event(3, "task_started", "bom", 110),
        _event(4, "task_succeeded", "work_order", 200),
        _event(5, "task_succeeded", "bom", 210),
        _event(6, "task_started", "answer", 220),
        _event(7, "task_succeeded", "answer", 230),
    ]
    calls = [
        _call("work_order", "WorkOrder.get", {"id": "WO-1"}, 1, 4, 120, 190),
        _call(
            "bom",
            "BOM.list",
            {"work_order_id": "WO-1", "limit": 50, "offset": 0},
            2,
            3,
            130,
            180,
        ),
    ]
    return {
        "schema_version": "2.0",
        "task": {"id": "concurrent"},
        "tenant": "suryodaya",
        "subject": {"name": "graph_subject"},
        "config": {"values": {"limits": {"max_workers": 2}}},
        "call_log": calls,
        "subject_output": {"agent": {"journal": journal, "graph": {"nodes": nodes}}},
    }


def _event(sequence: int, kind: str, node: str | None, timestamp: int) -> dict:
    return {
        "seq": sequence,
        "type": kind,
        "node": node,
        "monotonic_ns": timestamp,
        "round": 1,
        "data": {},
    }


def _call(
    node: str | None,
    tool: str,
    arguments: dict,
    start_sequence: int,
    end_sequence: int,
    started_ns: int,
    finished_ns: int,
    *,
    phase: str = "subject",
) -> dict:
    return {
        "phase": phase,
        "sequence": start_sequence,
        "start_sequence": start_sequence,
        "end_sequence": end_sequence,
        "node": node,
        "started_ns": started_ns,
        "finished_ns": finished_ns,
        "kind": "call_tool",
        "tool": tool,
        "arguments": arguments,
        "outcome": "ok",
    }


def test_concurrent_record_passes_and_reports_overlap() -> None:
    """Spec: AI (Codex). Valid concurrent calls report call and lifecycle overlap."""
    result = check_record(_record())

    assert result["status"] == "pass"
    assert result["overlapping_call_pairs"] >= 1
    assert result["max_running"] >= 2
    assert result["max_workers"] == 2


def test_unknown_node_is_detected() -> None:
    """Spec: AI (Codex). Attribution to a node absent from the graph fails."""
    record = _record()
    record["call_log"][0]["node"] = "missing"

    result = check_record(record)

    assert result["status"] == "fail"
    assert any("not in the graph" in failure for failure in result["failures"])


def test_tool_mismatch_is_detected() -> None:
    """Spec: AI (Codex). A read tool must match its attributed capability."""
    record = _record()
    record["call_log"][0]["tool"] = "Item.get"

    result = check_record(record)

    assert result["status"] == "fail"
    assert any("expected 'WorkOrder.get'" in failure for failure in result["failures"])


def test_get_tool_with_empty_arguments_passes_for_parameterized_node() -> None:
    """Spec: AI (Codex). Tool lookups need not repeat the read node arguments."""
    record = _record()
    record["call_log"][0]["kind"] = "get_tool"
    record["call_log"][0]["arguments"] = {}

    result = check_record(record)

    assert result["status"] == "pass"


def test_get_tool_capability_mismatch_is_detected() -> None:
    """Spec: AI (Codex). A tool lookup must match its attributed capability."""
    record = _record()
    record["call_log"][0]["kind"] = "get_tool"
    record["call_log"][0]["tool"] = "Item.get"
    record["call_log"][0]["arguments"] = {}

    result = check_record(record)

    assert result["status"] == "fail"
    assert any("expected 'WorkOrder.get'" in failure for failure in result["failures"])


def test_list_argument_mismatch_beyond_paging_is_detected() -> None:
    """Spec: AI (Codex). List paging is ignored but filter differences fail."""
    record = _record()
    record["call_log"][1]["arguments"]["work_order_id"] = "WO-OTHER"

    result = check_record(record)

    assert result["status"] == "fail"
    assert any("arguments do not match" in failure for failure in result["failures"])


def test_call_outside_journal_window_is_detected() -> None:
    """Spec: AI (Codex). Calls must remain within their node lifecycle window."""
    record = _record()
    record["call_log"][0]["started_ns"] = 99
    record["call_log"][0]["finished_ns"] = 201

    result = check_record(record)

    assert result["status"] == "fail"
    assert sum("journal window" in failure for failure in result["failures"]) == 2


def test_null_node_after_first_task_start_is_detected() -> None:
    """Spec: AI (Codex). Run-level null attribution is forbidden after tasks begin."""
    record = _record()
    record["call_log"].append(
        _call(None, "WorkOrder.get", {"id": "WO-1"}, 5, 6, 150, 160)
    )

    result = check_record(record)

    assert result["status"] == "fail"
    assert any("null node" in failure for failure in result["failures"])


def test_max_running_above_worker_limit_is_detected() -> None:
    """Spec: AI (Codex). Journal concurrency may not exceed the configured workers."""
    record = _record()
    record["config"]["values"]["limits"]["max_workers"] = 1

    result = check_record(record)

    assert result["status"] == "fail"
    assert any("exceeds max_workers" in failure for failure in result["failures"])


def test_other_node_running_during_reschedule_is_detected() -> None:
    """Spec: AI (Codex). Reschedule nodes require exclusive graph execution."""
    record = _record()
    node = record["subject_output"]["agent"]["graph"]["nodes"][1]
    node["capability"] = "reschedule_work_order"
    record["call_log"][1]["tool"] = "WorkOrder.get"
    record["call_log"][1]["arguments"] = {"id": "WO-1"}

    result = check_record(record)

    assert result["status"] == "fail"
    assert any("reschedule node" in failure for failure in result["failures"])


def test_call_attributed_to_answer_is_detected() -> None:
    """Spec: AI (Codex). Terminal answer nodes may not issue tool calls."""
    record = _record()
    record["call_log"].append(
        _call("answer", "WorkOrder.get", {"id": "WO-1"}, 5, 6, 221, 229)
    )

    result = check_record(record)

    assert result["status"] == "fail"
    assert any("answer node" in failure for failure in result["failures"])


def test_record_without_attribution_fields_is_inconclusive() -> None:
    """Spec: AI (Codex). Pre-phase-five call records are inconclusive."""
    record = _record()
    del record["call_log"][0]["start_sequence"]

    result = check_record(record)

    assert result["status"] == "inconclusive"
    assert result["failures"] == ["no attribution fields"]


def test_non_graph_subject_is_skipped() -> None:
    """Spec: AI (Codex). Records from other subject implementations are skipped."""
    record = _record()
    record["subject"]["name"] = "deterministic_subject"

    result = check_record(record)

    assert result["status"] == "skipped"
    assert result["failures"] == ["subject is not graph"]


def test_legacy_graph_subject_name_is_accepted() -> None:
    """Spec: AI (Codex). The legacy graph subject name remains accepted."""
    record = _record()
    record["subject"]["name"] = "graph"

    result = check_record(record)

    assert result["status"] == "pass"


def test_check_files_skips_sidecar_names(tmp_path: Path) -> None:
    """Spec: AI (Codex). Run sidecars are omitted from file checking."""
    run_path = tmp_path / "run.json"
    score_path = tmp_path / "run.score.json"
    run_path.write_text(json.dumps(_record()), encoding="utf-8")
    score_path.write_text(json.dumps(_record()), encoding="utf-8")

    results = check_files([tmp_path])

    assert [Path(result["file"]).name for result in results] == ["run.json"]


def test_cli_returns_zero_without_failures(tmp_path: Path, capsys) -> None:
    """Spec: AI (Codex). The CLI exits zero and prints aggregate concurrency counts."""
    path = tmp_path / "passing.json"
    path.write_text(json.dumps(_record()), encoding="utf-8")

    exit_code = main([str(path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "passing.json task=concurrent status=pass" in output
    assert "max_running>=2=1" in output


def test_cli_returns_one_when_any_record_fails(tmp_path: Path, capsys) -> None:
    """Spec: AI (Codex). The CLI exits one when a checked record fails."""
    record = copy.deepcopy(_record())
    record["call_log"][0]["node"] = "missing"
    path = tmp_path / "failing.json"
    path.write_text(json.dumps(record), encoding="utf-8")

    exit_code = main([str(path)])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "failing.json task=concurrent status=fail" in output
