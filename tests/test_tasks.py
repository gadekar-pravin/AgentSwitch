"""Offline checks for JSONL-backed harness tasks."""

import json
from pathlib import Path
from typing import Any

import pytest

from agentswitch.harness import __main__ as harness_main
from agentswitch.harness.tasks import TaskFileError, load_tasks, public_task

TASK_IDS = (
    "late_open_oldest",
    "late_with_sales_order",
    "late_with_cause",
    "not_late_completed",
    "refuse_not_found",
    "refuse_outside_seat",
    "reschedule_own_draft",
)
PUBLIC_FIELDS = {
    "id",
    "request",
    "request_kind",
    "selector",
    "expected",
    "brief_refusal",
    "reschedule",
    "writes",
}


def _task() -> dict[str, Any]:
    return {
        "id": "example",
        "request": "Investigate this work order.",
        "request_kind": "work_order_lateness",
        "selector": "none",
        "expected": {"outcome": "answered"},
        "brief_refusal": False,
        "reschedule": False,
        "writes": False,
        "expectation": "Answers from the available evidence.",
    }


def test_committed_tasks_load_in_order_with_public_fields() -> None:
    """Spec: AI (Codex) — committed tasks preserve order and public record shape."""
    tasks = load_tasks()

    assert tuple(task["id"] for task in tasks) == TASK_IDS
    assert len(tasks) == 7
    assert all(set(public_task(task)) == PUBLIC_FIELDS for task in tasks)


def _unknown_selector(rows: list[dict[str, Any]]) -> None:
    rows[0]["selector"] = "unknown"


def _duplicate_id(rows: list[dict[str, Any]]) -> None:
    rows.append(dict(rows[0]))


def _missing_key(rows: list[dict[str, Any]]) -> None:
    del rows[0]["request"]


def _unknown_key(rows: list[dict[str, Any]]) -> None:
    rows[0]["extra"] = True


def _non_bool_writes(rows: list[dict[str, Any]]) -> None:
    rows[0]["writes"] = 0


def _list_outcome(rows: list[dict[str, Any]]) -> None:
    rows[0]["expected"] = {"outcome": []}


def _object_outcome(rows: list[dict[str, Any]]) -> None:
    rows[0]["expected"] = {"outcome": {}}


@pytest.mark.parametrize(
    ("mutation", "raw_text"),
    [
        (_unknown_selector, None),
        (_duplicate_id, None),
        (_missing_key, None),
        (_unknown_key, None),
        (_non_bool_writes, None),
        (_list_outcome, None),
        (_object_outcome, None),
        (None, "{invalid json}"),
    ],
    ids=(
        "unknown-selector",
        "duplicate-id",
        "missing-key",
        "unknown-key",
        "non-bool-writes",
        "list-outcome",
        "object-outcome",
        "invalid-json",
    ),
)
def test_invalid_task_files_raise_task_file_error(
    tmp_path: Path,
    mutation: Any,
    raw_text: str | None,
) -> None:
    """Spec: AI (Codex) — malformed task rows are rejected as configuration errors."""
    rows = [_task()]
    if mutation is not None:
        mutation(rows)
    content = raw_text if raw_text is not None else "\n".join(json.dumps(row) for row in rows)
    path = tmp_path / "tasks.jsonl"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(TaskFileError):
        load_tasks(path)


def test_main_reports_task_file_error_without_running(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Spec: AI (Codex) — invalid task data exits 2 before a harness run starts."""
    error = TaskFileError("line 1: invalid task")
    monkeypatch.setattr(harness_main, "load_tasks", lambda: (_ for _ in ()).throw(error))

    def fail_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("run_tasks must not be called")

    monkeypatch.setattr(harness_main, "run_tasks", fail_run)

    assert harness_main.main() == 2
    assert capsys.readouterr().out == "configuration error: line 1: invalid task\n"


def test_main_reports_real_invalid_task_file_without_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Spec: AI (Codex) — invalid outcome data exits 2 through the real task loader."""
    row = _task()
    row["expected"] = {"outcome": []}
    path = tmp_path / "tasks.jsonl"
    path.write_text(json.dumps(row), encoding="utf-8")
    monkeypatch.setattr(load_tasks, "__defaults__", (path,))

    def fail_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("run_tasks must not be called")

    monkeypatch.setattr(harness_main, "run_tasks", fail_run)

    assert harness_main.main() == 2
    assert capsys.readouterr().out == (
        "configuration error: line 1: expected must be an object with outcome answered or refused\n"
    )
