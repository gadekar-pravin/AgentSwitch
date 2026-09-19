"""Live task graph and replayable in-memory journal."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

import networkx as nx

NodeState = Literal["pending", "running", "succeeded", "failed"]
EventType = Literal[
    "run_started",
    "graph_patched",
    "task_started",
    "task_succeeded",
    "task_failed",
    "action_started",
    "action_finished",
    "run_finished",
    "run_failed",
]

_EVENT_TYPES = {
    "run_started",
    "graph_patched",
    "task_started",
    "task_succeeded",
    "task_failed",
    "action_started",
    "action_finished",
    "run_finished",
    "run_failed",
}


class GraphError(ValueError):
    """Base class for graph validation and state errors."""


class StructuralError(GraphError):
    """A graph patch or dependency would violate graph structure."""


class TransitionError(GraphError):
    """A node state transition is illegal."""


@dataclass(frozen=True)
class NodeSpec:
    id: str
    capability: str
    arguments: dict[str, Any]
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class GraphPatch:
    add: tuple[NodeSpec, ...]
    finish: bool
    reason: str


@dataclass(frozen=True)
class NodeSnapshot:
    id: str
    capability: str
    arguments: dict[str, Any]
    frontier: int
    state: NodeState
    failure_reason: str | None
    outcome: dict[str, Any] | None
    error_detail: Any | None


def _json_copy(value: Any, label: str) -> Any:
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
        return json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise GraphError(f"{label} must be JSON-safe: {error}") from None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Journal:
    """Append-only, JSON-safe event journal with injectable clocks."""

    def __init__(
        self,
        *,
        wall_clock: Callable[[], datetime] = _utc_now,
        monotonic_clock: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._events: list[dict[str, Any]] = []

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple(deepcopy(self._events))

    def append(
        self,
        event_type: EventType,
        *,
        round: int | None = None,
        node: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        events = self._prepare_batch(((event_type, round, node, data),))
        self._commit(events)
        return deepcopy(events[0])

    def _prepare_batch(
        self,
        specifications: Iterable[
            tuple[EventType, int | None, str | None, Mapping[str, Any] | None]
        ],
    ) -> list[dict[str, Any]]:
        prepared = []
        for event_type, round, node, data in specifications:
            prepared.append(
                self._prepare(
                    event_type,
                    seq=len(self._events) + len(prepared) + 1,
                    round=round,
                    node=node,
                    data=data,
                )
            )
        return prepared

    def _prepare(
        self,
        event_type: EventType,
        *,
        seq: int,
        round: int | None,
        node: str | None,
        data: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if event_type not in _EVENT_TYPES:
            raise ValueError(f"unknown journal event type {event_type!r}")
        if round is not None and (
            not isinstance(round, int) or isinstance(round, bool)
        ):
            raise ValueError("journal round must be an integer or None")
        if node is not None and (not isinstance(node, str) or not node):
            raise ValueError("journal node must be a non-empty string or None")
        at = self._wall_clock()
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("journal wall clock must return a timezone-aware datetime")
        event = {
            "seq": seq,
            "type": event_type,
            "at": at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "monotonic_ns": self._monotonic_clock(),
            "round": round,
            "node": node,
            "data": _json_copy(dict(data or {}), "journal data"),
        }
        return event

    def _commit(self, events: Iterable[dict[str, Any]]) -> None:
        self._events.extend(events)


def _patch_data(patch: GraphPatch) -> dict[str, Any]:
    return {
        "add": [
            {
                "id": spec.id,
                "capability": spec.capability,
                "arguments": _json_copy(spec.arguments, "node arguments"),
                "depends_on": list(spec.depends_on),
            }
            for spec in patch.add
        ],
        "finish": patch.finish,
        "reason": patch.reason,
    }


class LiveGraph:
    """A directed acyclic task graph whose mutations are journalled."""

    def __init__(self, journal: Journal | None = None, *, _record_events: bool = True) -> None:
        self._graph = nx.DiGraph()
        self.journal = journal if journal is not None else Journal()
        self._record_events = _record_events
        self._frontier = 0
        self._frontiers: set[int] = set()

    @property
    def frontier(self) -> int:
        return self._frontier

    def __len__(self) -> int:
        return len(self._graph)

    def node(self, node_id: str) -> NodeSnapshot:
        try:
            attributes = self._graph.nodes[node_id]
        except KeyError:
            raise GraphError(f"unknown node {node_id!r}") from None
        return NodeSnapshot(
            id=node_id,
            capability=attributes["capability"],
            arguments=deepcopy(attributes["arguments"]),
            frontier=attributes["frontier"],
            state=attributes["state"],
            failure_reason=attributes["failure_reason"],
            outcome=deepcopy(attributes["outcome"]),
            error_detail=deepcopy(attributes["error_detail"]),
        )

    def nodes(self) -> tuple[NodeSnapshot, ...]:
        return tuple(self.node(node_id) for node_id in sorted(self._graph.nodes))

    def apply_patch(
        self,
        patch: GraphPatch,
        *,
        round: int | None = None,
        journal_data: Mapping[str, Any] | None = None,
    ) -> int:
        """Atomically add one planner frontier and return its number."""
        if not isinstance(patch, GraphPatch):
            raise StructuralError("patch must be a GraphPatch")
        if not isinstance(patch.finish, bool) or not isinstance(patch.reason, str):
            raise StructuralError("patch finish and reason have invalid types")
        if not isinstance(patch.add, tuple):
            raise StructuralError("patch add must be a tuple")

        existing = set(self._graph.nodes)
        new_ids: set[str] = set()
        for spec in patch.add:
            if not isinstance(spec, NodeSpec):
                raise StructuralError("patch additions must be NodeSpec values")
            if not isinstance(spec.id, str) or not spec.id:
                raise StructuralError("node id must be a non-empty string")
            if spec.id in existing or spec.id in new_ids:
                raise StructuralError(f"node id {spec.id!r} is not new and unique")
            new_ids.add(spec.id)

        prepared: list[tuple[NodeSpec, dict[str, Any]]] = []
        for spec in patch.add:
            if not isinstance(spec.capability, str) or not spec.capability:
                raise StructuralError(f"node {spec.id!r} capability must be a non-empty string")
            if not isinstance(spec.arguments, dict):
                raise StructuralError(f"node {spec.id!r} arguments must be an object")
            try:
                arguments = _json_copy(spec.arguments, f"node {spec.id!r} arguments")
            except GraphError as error:
                raise StructuralError(str(error)) from None
            if not isinstance(spec.depends_on, tuple) or any(
                not isinstance(parent, str) or not parent for parent in spec.depends_on
            ):
                raise StructuralError(f"node {spec.id!r} dependencies must be string ids")
            if len(set(spec.depends_on)) != len(spec.depends_on):
                raise StructuralError(f"node {spec.id!r} has duplicate dependencies")
            for parent in spec.depends_on:
                if parent in new_ids:
                    raise StructuralError(
                        f"node {spec.id!r} depends on same-patch node {parent!r}"
                    )
                if parent not in existing:
                    raise StructuralError(
                        f"node {spec.id!r} depends on missing node {parent!r}"
                    )
                if self._graph.nodes[parent]["state"] == "failed":
                    raise StructuralError(
                        f"node {spec.id!r} depends on failed node {parent!r}"
                    )
            prepared.append((spec, arguments))

        frontier = self._frontier + 1
        candidate = self._graph.copy()
        for spec, arguments in prepared:
            candidate.add_node(
                spec.id,
                capability=spec.capability,
                arguments=arguments,
                frontier=frontier,
                state="pending",
                failure_reason=None,
                outcome=None,
                error_detail=None,
            )
            candidate.add_edges_from((parent, spec.id) for parent in spec.depends_on)
        if not nx.is_directed_acyclic_graph(candidate):
            raise StructuralError("patch would make the graph cyclic")

        event_data = {
            "kind": "patch",
            "frontier": frontier,
            "patch": _patch_data(patch),
        }
        for key, value in dict(journal_data or {}).items():
            if key in event_data:
                raise GraphError(f"journal metadata may not replace {key!r}")
            event_data[key] = value
        _json_copy(event_data, "graph patch journal data")

        events = []
        if self._record_events:
            events = self.journal._prepare_batch(
                (("graph_patched", round, None, event_data),)
            )
        self._graph = candidate
        self._frontier = frontier
        self._frontiers.add(frontier)
        self.journal._commit(events)
        return frontier

    def start(self, node_id: str, *, round: int | None = None) -> None:
        self._check_transition(node_id, "pending", "running")
        candidate = self._graph.copy()
        candidate.nodes[node_id]["state"] = "running"
        events = []
        if self._record_events:
            events = self.journal._prepare_batch(
                (("task_started", round, node_id, None),)
            )
        self._graph = candidate
        self.journal._commit(events)

    def succeed(
        self,
        node_id: str,
        outcome: dict[str, Any],
        *,
        round: int | None = None,
    ) -> None:
        if not isinstance(outcome, dict):
            raise TransitionError("successful node outcome must be an object")
        copied = _json_copy(outcome, "node outcome")
        self._check_transition(node_id, "running", "succeeded")
        candidate = self._graph.copy()
        attributes = candidate.nodes[node_id]
        attributes["state"] = "succeeded"
        attributes["outcome"] = copied
        events = []
        if self._record_events:
            events = self.journal._prepare_batch(
                (("task_succeeded", round, node_id, {"outcome": copied}),)
            )
        self._graph = candidate
        self.journal._commit(events)

    def fail(
        self,
        node_id: str,
        reason: str,
        detail: Any | None = None,
        *,
        round: int | None = None,
    ) -> None:
        if not isinstance(reason, str) or not reason:
            raise TransitionError("failure reason must be a non-empty string")
        copied_detail = _json_copy(detail, "error detail")
        self._check_transition(node_id, "running", "failed")
        candidate = self._graph.copy()
        attributes = candidate.nodes[node_id]
        attributes["state"] = "failed"
        attributes["failure_reason"] = reason
        attributes["error_detail"] = copied_detail
        event_specs = [
            (
                "task_failed",
                round,
                node_id,
                {"reason": reason, "detail": copied_detail},
            )
        ]
        for descendant in sorted(nx.descendants(candidate, node_id)):
            descendant_data = candidate.nodes[descendant]
            if descendant_data["state"] != "pending":
                continue
            descendant_data["state"] = "failed"
            descendant_data["failure_reason"] = "blocked"
            descendant_data["error_detail"] = {"blocked_by": node_id}
            event_specs.append(
                (
                    "task_failed",
                    round,
                    descendant,
                    {
                        "reason": "blocked",
                        "detail": {"blocked_by": node_id},
                    },
                )
            )
        events = []
        if self._record_events:
            events = self.journal._prepare_batch(event_specs)
        self._graph = candidate
        self.journal._commit(events)

    def _check_transition(self, node_id: str, before: NodeState, after: NodeState) -> None:
        if node_id not in self._graph:
            raise TransitionError(f"unknown node {node_id!r}")
        state = self._graph.nodes[node_id]["state"]
        if state != before:
            raise TransitionError(
                f"node {node_id!r} cannot transition {state!r} -> {after!r}"
            )

    def ready_nodes(self) -> tuple[NodeSnapshot, ...]:
        ready = []
        for node_id in sorted(self._graph.nodes):
            if self._graph.nodes[node_id]["state"] != "pending":
                continue
            if all(
                self._graph.nodes[parent]["state"] == "succeeded"
                for parent in self._graph.predecessors(node_id)
            ):
                ready.append(self.node(node_id))
        return tuple(ready)

    def frontier_settled(self, frontier: int) -> bool:
        if frontier not in self._frontiers:
            raise GraphError(f"unknown frontier {frontier}")
        states = [
            data["state"]
            for _, data in self._graph.nodes(data=True)
            if data["frontier"] == frontier
        ]
        return not any(state in {"pending", "running"} for state in states)

    def has_pending_or_running(self) -> bool:
        return any(
            data["state"] in {"pending", "running"}
            for _, data in self._graph.nodes(data=True)
        )

    def ancestors(self, node_id: str) -> tuple[str, ...]:
        if node_id not in self._graph:
            raise GraphError(f"unknown node {node_id!r}")
        return tuple(sorted(nx.ancestors(self._graph, node_id)))

    def add_dependency(
        self,
        source_id: str,
        target_id: str,
        *,
        round: int | None = None,
    ) -> None:
        """Add a succeeded-to-pending edge while preserving acyclicity."""
        if source_id not in self._graph or target_id not in self._graph:
            raise StructuralError("dependency edge names an unknown node")
        if self._graph.nodes[source_id]["state"] != "succeeded":
            raise StructuralError("dependency source must be succeeded")
        if self._graph.nodes[target_id]["state"] != "pending":
            raise StructuralError("dependency target must be pending")
        if self._graph.has_edge(source_id, target_id):
            return
        candidate = self._graph.copy()
        candidate.add_edge(source_id, target_id)
        if not nx.is_directed_acyclic_graph(candidate):
            raise StructuralError("dependency edge would make the graph cyclic")
        events = []
        if self._record_events:
            events = self.journal._prepare_batch(
                (
                    (
                        "graph_patched",
                        round,
                        target_id,
                        {
                            "kind": "edge",
                            "edge": {"source": source_id, "target": target_id},
                        },
                    ),
                )
            )
        self._graph = candidate
        self.journal._commit(events)

    def export(self) -> dict[str, Any]:
        return export_graph(self._graph)


def export_graph(graph: nx.DiGraph) -> dict[str, Any]:
    """Return networkx node-link data with a stable edge key."""
    return _json_copy(nx.node_link_data(graph, edges="edges"), "graph export")


def replay(events: Journal | Iterable[Mapping[str, Any]]) -> LiveGraph:
    """Rebuild a live graph from its journal events."""
    source = events.events if isinstance(events, Journal) else tuple(events)
    rebuilt = LiveGraph(_record_events=False)
    expected_seq = 1
    for raw_event in source:
        event = dict(raw_event)
        if event.get("seq") != expected_seq:
            raise GraphError(f"journal sequence expected {expected_seq}")
        expected_seq += 1
        event_type = event.get("type")
        node_id = event.get("node")
        data = event.get("data")
        if not isinstance(data, dict):
            raise GraphError("journal event data must be an object")
        if event_type == "graph_patched":
            if data.get("kind") == "edge":
                edge = data.get("edge")
                if not isinstance(edge, dict):
                    raise GraphError("edge journal event is malformed")
                rebuilt.add_dependency(edge.get("source"), edge.get("target"))
                continue
            patch_data = data.get("patch")
            if not isinstance(patch_data, dict):
                raise GraphError("patch journal event is malformed")
            additions = patch_data.get("add")
            if not isinstance(additions, list):
                raise GraphError("patch additions must be a list")
            patch = GraphPatch(
                add=tuple(
                    NodeSpec(
                        id=item["id"],
                        capability=item["capability"],
                        arguments=item["arguments"],
                        depends_on=tuple(item["depends_on"]),
                    )
                    for item in additions
                ),
                finish=patch_data["finish"],
                reason=patch_data["reason"],
            )
            rebuilt.apply_patch(patch)
        elif event_type == "task_started":
            rebuilt.start(node_id)
        elif event_type == "task_succeeded":
            rebuilt.succeed(node_id, data["outcome"])
        elif event_type == "task_failed":
            if node_id not in rebuilt._graph:
                raise GraphError(f"journal failure names unknown node {node_id!r}")
            attributes = rebuilt._graph.nodes[node_id]
            if attributes["state"] == "failed" and attributes["failure_reason"] == "blocked":
                continue
            if attributes["state"] != "running":
                raise GraphError(f"journal failure for {node_id!r} has no running node")
            rebuilt.fail(node_id, data["reason"], data.get("detail"))
    return rebuilt


__all__ = [
    "GraphError",
    "GraphPatch",
    "Journal",
    "LiveGraph",
    "NodeSnapshot",
    "NodeSpec",
    "StructuralError",
    "TransitionError",
    "export_graph",
    "replay",
]
