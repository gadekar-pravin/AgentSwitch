"""Offline tests for shared MCP read workers."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from agentswitch.answer import Store
from agentswitch.reads import call_list, call_read, guarded_reschedule, render_list_result


class ScriptedTools:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def call_tool(self, name, arguments, *, allow_write=False):
        self.calls.append((name, dict(arguments), allow_write))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(structured=outcome)


def test_complete_multi_page_list_scan_deduplicates_and_stores_rows():
    """Spec: AI (Codex) Complete scans deduplicate page overlap and store all unique rows."""
    tools = ScriptedTools(
        [
            {"data": [{"id": "a"}, {"id": "b"}], "total": 3},
            {"data": [{"id": "b"}, {"id": "c"}], "total": 3},
        ]
    )
    store = Store()

    result = call_list(tools, store, "WorkOrder.list", {"bom_id": "bom-1"})

    assert result == {
        "data": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        "total": 3,
        "returned": 3,
        "complete": True,
    }
    assert store.rows("WorkOrder") == [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    assert store.list_calls == [
        {"tool": "WorkOrder.list", "filters": {"bom_id": "bom-1"}, "complete": True}
    ]
    assert [call[1]["offset"] for call in tools.calls] == [0, 2]


def test_empty_page_before_total_keeps_partial_rows_and_old_loop_success_shape():
    """Spec: AI (Codex) Early empty pages preserve rows and return ok with complete false."""
    tools = ScriptedTools(
        [
            {"data": [{"id": "a"}], "total": 3},
            {"data": [], "total": 3},
        ]
    )
    store = Store()

    result = call_read(tools, store, "WorkOrder.list", {"bom_id": "bom-1"})

    assert result == {
        "ok": True,
        "data": [{"id": "a"}],
        "total": 3,
        "returned": 1,
        "complete": False,
    }
    assert store.rows("WorkOrder") == [{"id": "a"}]
    assert store.list_calls[-1]["complete"] is False


def test_exception_mid_scan_stores_partial_rows_and_reraises():
    """Spec: AI (Codex) Mid-scan exceptions store partial evidence as incomplete and re-raise."""
    failure = RuntimeError("page failed")
    tools = ScriptedTools(
        [
            {"data": [{"id": "a"}], "total": 2},
            failure,
        ]
    )
    store = Store()

    with pytest.raises(RuntimeError, match="page failed") as caught:
        call_list(tools, store, "WorkOrder.list", {})

    assert caught.value is failure
    assert store.rows("WorkOrder") == [{"id": "a"}]
    assert store.list_calls == [
        {"tool": "WorkOrder.list", "filters": {}, "complete": False}
    ]


def test_renderer_small_character_limit_truncates_and_flags_result():
    """Spec: AI (Codex) A small renderer limit truncates projected rows and flags the result."""
    rows = [{"id": f"row-{index}", "description": "x" * 80} for index in range(3)]

    result = render_list_result(
        rows,
        entity="WorkOrder",
        total=3,
        complete=True,
        character_limit=180,
    )

    assert result["truncated"] is True
    assert result["full_returned"] == 3
    assert result["returned"] < 3
    assert result["code_holds_all_rows"] is True


def test_guarded_reschedule_pins_before_guard_read_and_records_non_model_read():
    """Spec: AI (Codex) Guarded reschedule pins first and records its guard get as non-model."""
    store = Store()
    target = {
        "id": "wo-1",
        "status": "draft",
        "created_by": "user-1",
        "planned_start_date": "2026-09-01",
        "planned_end_date": "2026-09-03",
    }
    store.add_get("WorkOrder.get", {"id": "wo-1"}, target, model_read=True)

    class GuardTools:
        def __init__(self):
            self.pinned_at_call = None

        def call_tool(self, name, arguments, *, allow_write=False):
            self.pinned_at_call = store.target_snapshot_pinned
            assert name == "WorkOrder.get"
            assert arguments == {"id": "wo-1"}
            assert allow_write is False
            return SimpleNamespace(structured=target)

    tools = GuardTools()

    result = guarded_reschedule(
        tools,
        store,
        "wo-1",
        today=date(2026, 9, 19),
        own_user_id="user-1",
        allow_write=False,
    )

    assert tools.pinned_at_call is True
    assert result["action"] == "escalated"
    assert result["reason"] == "writes_disabled"
    assert store.successful_gets[-1]["model_read"] is False
    assert store.successful_gets[-1]["arguments"] == {"id": "wo-1"}
