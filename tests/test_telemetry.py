"""Offline tests for safe span projection."""

from __future__ import annotations

import json

from agentswitch.telemetry import build_spans


def test_spans_never_copy_sensitive_record_content() -> None:
    """Spec: AI (Codex) Span serialization excludes payload and prose sentinel values."""
    sentinel = "SENTINEL-MUST-NOT-APPEAR"
    record = {
        "run_file": "example.json",
        "request": sentinel,
        "answer": {"prose": sentinel},
        "subject_output": {
            "agent": {
                "prose": sentinel,
                "messages": [{"content": sentinel}],
                "journal": [],
            }
        },
        "call_log": [
            {
                "phase": "subject",
                "start_sequence": 1,
                "kind": "call_tool",
                "tool": "WorkOrder.get",
                "arguments": {"secret": sentinel},
                "structuredContent": {"secret": sentinel},
                "started_ns": 10,
                "finished_ns": 20,
                "outcome": "ok",
            }
        ],
        "economics": {
            "entries": [
                {
                    "round": 1,
                    "attempt": 1,
                    "started_ns": 1,
                    "finished_ns": 2,
                    "messages": sentinel,
                    "completion": sentinel,
                }
            ],
            "summary": {"spent_micro": 0},
        },
    }

    rendered = json.dumps(build_spans(record), sort_keys=True)

    assert sentinel not in rendered
    assert "arguments" not in rendered
    assert "structuredContent" not in rendered


def test_malformed_records_return_warnings_without_raising() -> None:
    """Spec: AI (Codex) Malformed optional sections are skipped and reported as warnings."""
    records = (
        {},
        {"run_file": "bad.json", "call_log": {}, "economics": {"entries": {}}},
        {
            "run_file": "bad.json",
            "subject_output": {"agent": {"journal": "not-a-list"}},
            "call_log": [None, {"phase": "subject"}],
            "economics": {"entries": [None, {"round": "one"}]},
        },
    )

    results = [build_spans(record) for record in records]

    assert all(result["schema"] == "agentswitch.spans/1" for result in results)
    assert all(result["warnings"] for result in results)
    assert all(result["spans"][0]["span_id"] == "run" for result in results)


def test_older_ledger_entries_build_attempts_with_unknown_times() -> None:
    """Spec: AI (Codex) Timestamp-free legacy ledger entries produce null attempt bounds."""
    result = build_spans(
        {
            "run_file": "older.json",
            "call_log": [],
            "economics": {"entries": [{"round": 1, "attempt": 1}]},
        }
    )

    attempt = next(
        span for span in result["spans"] if span["span_id"] == "round:1/attempt:1"
    )
    assert attempt["start_ns"] is None
    assert attempt["end_ns"] is None


def test_aggregate_spans_cover_all_timed_descendants() -> None:
    """Spec: AI (Codex) Round, phase, and run bounds include later node work."""
    record = {
        "run_file": "bounded.json",
        "subject_output": {
            "agent": {
                "journal": [
                    {"type": "task_started", "node": "target", "round": 1, "monotonic_ns": 21},
                    {"type": "task_succeeded", "node": "target", "monotonic_ns": 40},
                ]
            }
        },
        "call_log": [
            {
                "phase": "subject",
                "node": "target",
                "started_ns": 22,
                "finished_ns": 39,
            }
        ],
        "economics": {
            "entries": [
                {"round": 1, "attempt": 1, "started_ns": 10, "finished_ns": 20}
            ]
        },
    }

    spans = build_spans(record)["spans"]
    by_id = {span["span_id"]: span for span in spans}

    assert (by_id["round:1"]["start_ns"], by_id["round:1"]["end_ns"]) == (10, 40)
    assert (by_id["phase:subject"]["start_ns"], by_id["phase:subject"]["end_ns"]) == (10, 40)
    assert (by_id["run"]["start_ns"], by_id["run"]["end_ns"]) == (10, 40)
    for span in spans:
        if span["parent_id"] is None or span["start_ns"] is None or span["end_ns"] is None:
            continue
        parent = by_id[span["parent_id"]]
        assert parent["start_ns"] <= span["start_ns"]
        assert span["end_ns"] <= parent["end_ns"]


def test_round_without_attempts_inherits_node_bounds() -> None:
    """Spec: AI (Codex) A node-only round derives its bounds from that node."""
    result = build_spans(
        {
            "run_file": "node-only-round.json",
            "subject_output": {
                "agent": {
                    "journal": [
                        {"type": "task_started", "node": "target", "round": 2, "monotonic_ns": 21},
                        {"type": "task_succeeded", "node": "target", "monotonic_ns": 40},
                    ]
                }
            },
            "call_log": [],
        }
    )

    round_span = next(
        span for span in result["spans"] if span["span_id"] == "round:2"
    )
    assert (round_span["start_ns"], round_span["end_ns"]) == (21, 40)


def test_aggregate_without_timed_descendants_stays_null() -> None:
    """Spec: AI (Codex) An entirely untimed span subtree retains null bounds."""
    result = build_spans(
        {
            "run_file": "untimed.json",
            "call_log": [],
            "economics": {"entries": [{"round": 1, "attempt": 1}]},
        }
    )

    by_id = {span["span_id"]: span for span in result["spans"]}
    for span_id in ("run", "phase:subject", "round:1"):
        assert by_id[span_id]["start_ns"] is None
        assert by_id[span_id]["end_ns"] is None
