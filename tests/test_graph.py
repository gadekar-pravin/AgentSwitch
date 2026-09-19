"""Offline tests for the live graph and journal."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agentswitch.graph import (
    GraphError,
    GraphPatch,
    Journal,
    LiveGraph,
    NodeSpec,
    StructuralError,
    TransitionError,
    replay,
)


def _patch(*specs: NodeSpec, reason: str = "test") -> GraphPatch:
    return GraphPatch(add=specs, finish=False, reason=reason)


def _node(node_id: str, *, depends_on: tuple[str, ...] = ()) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        capability="WorkOrder.get",
        arguments={"id": node_id},
        depends_on=depends_on,
    )


def _assert_graph_unchanged(
    graph: LiveGraph,
    graph_export: dict[str, object],
    frontier: int,
    events: tuple[dict[str, object], ...],
) -> None:
    assert graph.export() == graph_export
    assert graph.frontier == frontier
    assert graph.journal.events == events
    assert replay(graph.journal).export() == graph.export()


def test_invalid_patches_are_atomic(monkeypatch):
    """Spec: AI (Codex) Missing, failed, same-patch, and cyclic patches change nothing."""
    graph = LiveGraph()
    graph.apply_patch(_patch(_node("root")))
    baseline = graph.export()

    with pytest.raises(StructuralError, match="missing"):
        graph.apply_patch(_patch(_node("missing-child", depends_on=("absent",))))
    assert graph.export() == baseline

    graph.start("root")
    graph.fail("root", "error", {"message": "bad"})
    failed = graph.export()
    with pytest.raises(StructuralError, match="failed"):
        graph.apply_patch(_patch(_node("failed-child", depends_on=("root",))))
    assert graph.export() == failed

    same_patch = _patch(_node("first"), _node("second", depends_on=("first",)))
    with pytest.raises(StructuralError, match="same-patch"):
        graph.apply_patch(same_patch)
    assert graph.export() == failed

    monkeypatch.setattr("agentswitch.graph.nx.is_directed_acyclic_graph", lambda _: False)
    with pytest.raises(StructuralError, match="cyclic"):
        graph.apply_patch(_patch(_node("cycle-guard")))
    assert graph.export() == failed


def test_parent_failure_blocks_pending_descendants_and_journals_them():
    """Spec: AI (Codex) A failed parent immediately blocks every pending descendant."""
    graph = LiveGraph()
    graph.apply_patch(_patch(_node("parent")))
    graph.apply_patch(_patch(_node("child", depends_on=("parent",))))
    graph.apply_patch(_patch(_node("grandchild", depends_on=("child",))))

    graph.start("parent")
    graph.fail("parent", "error", {"message": "boom"})

    assert graph.node("child").state == "failed"
    assert graph.node("child").failure_reason == "blocked"
    assert graph.node("grandchild").failure_reason == "blocked"
    failures = [event for event in graph.journal.events if event["type"] == "task_failed"]
    assert [(event["node"], event["data"]["reason"]) for event in failures] == [
        ("parent", "error"),
        ("child", "blocked"),
        ("grandchild", "blocked"),
    ]


def test_replay_restores_failures_blocking_and_added_edge():
    """Spec: AI (Codex) Journal replay exactly restores states, outcomes, and dependency edges."""
    graph = LiveGraph()
    graph.apply_patch(_patch(_node("target-read"), _node("failure")), round=1)
    graph.apply_patch(
        _patch(
            _node("blocked", depends_on=("failure",)),
            _node("write"),
        ),
        round=2,
    )
    graph.start("target-read", round=2)
    graph.succeed("target-read", {"record": {"id": "WO-1"}}, round=2)
    graph.add_dependency("target-read", "write", round=2)
    graph.start("failure", round=2)
    graph.fail("failure", "incomplete_scan", {"returned": 1}, round=2)

    rebuilt = replay(graph.journal)

    assert rebuilt.export() == graph.export()
    assert rebuilt.ancestors("write") == ("target-read",)
    assert rebuilt.node("blocked").failure_reason == "blocked"


def test_illegal_transitions_and_ready_node_ordering():
    """Spec: AI (Codex) Only legal transitions run and ready nodes are sorted by id."""
    graph = LiveGraph()
    graph.apply_patch(_patch(_node("z"), _node("a")))
    assert [node.id for node in graph.ready_nodes()] == ["a", "z"]

    with pytest.raises(TransitionError):
        graph.succeed("a", {"ok": True})
    graph.start("a")
    with pytest.raises(TransitionError):
        graph.start("a")
    graph.succeed("a", {"ok": True})
    with pytest.raises(TransitionError):
        graph.fail("a", "error")
    empty_frontier = graph.apply_patch(GraphPatch(add=(), finish=True, reason="done"))
    assert graph.frontier_settled(empty_frontier) is True


def test_journal_uses_injected_clocks():
    """Spec: AI (Codex) Journal events have deterministic sequence and injected timestamps."""
    ticks = iter((10, 20))
    journal = Journal(
        wall_clock=lambda: datetime(2026, 9, 19, tzinfo=timezone.utc),
        monotonic_clock=lambda: next(ticks),
    )
    journal.append("run_started")
    journal.append("run_finished", data={"ok": True})

    assert [event["seq"] for event in journal.events] == [1, 2]
    assert [event["monotonic_ns"] for event in journal.events] == [10, 20]
    assert journal.events[0]["at"] == "2026-09-19T00:00:00Z"


def test_apply_patch_rejected_journal_event_is_atomic():
    """Spec: AI (Codex) A rejected patch event preserves graph, frontier, and journal."""
    graph = LiveGraph()
    baseline = (graph.export(), graph.frontier, graph.journal.events)

    with pytest.raises(GraphError, match="JSON-safe"):
        graph.apply_patch(
            _patch(_node("new")),
            journal_data={"invalid": object()},
        )

    _assert_graph_unchanged(graph, *baseline)


def test_add_dependency_rejected_journal_event_is_atomic():
    """Spec: AI (Codex) A rejected edge event preserves graph, frontier, and journal."""
    graph = LiveGraph()
    graph.apply_patch(_patch(_node("source"), _node("target")))
    graph.start("source")
    graph.succeed("source", {"ok": True})
    baseline = (graph.export(), graph.frontier, graph.journal.events)

    with pytest.raises(ValueError, match="round"):
        graph.add_dependency("source", "target", round=True)

    _assert_graph_unchanged(graph, *baseline)


def test_start_rejected_journal_event_is_atomic():
    """Spec: AI (Codex) A rejected start event preserves graph, frontier, and journal."""
    graph = LiveGraph()
    graph.apply_patch(_patch(_node("task")))
    baseline = (graph.export(), graph.frontier, graph.journal.events)

    with pytest.raises(ValueError, match="round"):
        graph.start("task", round=True)

    _assert_graph_unchanged(graph, *baseline)


def test_succeed_rejected_journal_event_is_atomic():
    """Spec: AI (Codex) A rejected success event preserves graph, frontier, and journal."""
    graph = LiveGraph()
    graph.apply_patch(_patch(_node("task")))
    graph.start("task")
    baseline = (graph.export(), graph.frontier, graph.journal.events)

    with pytest.raises(ValueError, match="round"):
        graph.succeed("task", {"ok": True}, round=True)

    _assert_graph_unchanged(graph, *baseline)


def test_fail_rejected_journal_events_are_atomic():
    """Spec: AI (Codex) Rejected failure events do not fail the node or its descendants."""
    graph = LiveGraph()
    graph.apply_patch(_patch(_node("parent")))
    graph.apply_patch(_patch(_node("child", depends_on=("parent",))))
    graph.start("parent")
    baseline = (graph.export(), graph.frontier, graph.journal.events)

    with pytest.raises(ValueError, match="round"):
        graph.fail("parent", "error", {"message": "boom"}, round=True)

    _assert_graph_unchanged(graph, *baseline)
