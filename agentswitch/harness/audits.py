"""Independent audits over persisted graph-subject evidence."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any

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


class _Malformed(ValueError):
    """Persisted audit input cannot be interpreted safely."""


class _Broken(ValueError):
    """Persisted evidence clearly violates a graph or journal rule."""


class _BlockingMismatch(_Broken):
    """A failed node's required descendant-blocking event is absent or invalid."""

    def __init__(self, node: str, blocked_by: str, message: str) -> None:
        super().__init__(message)
        self.node = node
        self.blocked_by = blocked_by


class _MissingNodeKey(_Malformed):
    """A persisted graph node omits a required export key."""

    def __init__(self, node: str, key: str) -> None:
        super().__init__(f"persisted node {node!r} is missing key {key!r}")
        self.node = node
        self.key = key


def _result(name: str, verdict: str, reason: str, **evidence: Any) -> dict[str, Any]:
    return {"name": name, "verdict": verdict, "reason": reason, "evidence": evidence}


def _agent(record: dict[str, Any]) -> dict[str, Any]:
    output = record.get("subject_output")
    agent = output.get("agent") if isinstance(output, dict) else None
    if not isinstance(agent, dict):
        raise _Malformed("subject_output.agent must be an object")
    return agent


def _events(record: dict[str, Any]) -> list[dict[str, Any]]:
    journal = _agent(record).get("journal")
    if not isinstance(journal, list):
        raise _Malformed("journal must be a list")
    events: list[dict[str, Any]] = []
    for index, raw in enumerate(journal, start=1):
        if not isinstance(raw, dict):
            raise _Malformed(f"journal event {index} must be an object")
        event_type = raw.get("type")
        if event_type not in _EVENT_TYPES:
            raise _Broken(f"journal event {index} has unknown type {event_type!r}")
        if raw.get("seq") != index:
            raise _Broken(
                f"journal sequence expected {index}, got {raw.get('seq')!r}"
            )
        if not isinstance(raw.get("data"), dict):
            raise _Malformed(f"journal event {index} data must be an object")
        events.append(raw)
    return events


def _empty_replay() -> dict[str, Any]:
    return {"nodes": {}, "edges": set()}


def _add_patch(state: dict[str, Any], event: dict[str, Any]) -> None:
    data = event["data"]
    kind = data.get("kind")
    if kind == "edge":
        edge = data.get("edge")
        if not isinstance(edge, dict):
            raise _Malformed("edge patch must contain an edge object")
        source = edge.get("source")
        target = edge.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            raise _Malformed("edge endpoints must be strings")
        if source not in state["nodes"] or target not in state["nodes"]:
            raise _Broken("edge patch names an unknown node")
        state["edges"].add((source, target))
        return
    if kind != "patch":
        raise _Malformed(f"graph patch has unknown kind {kind!r}")
    patch = data.get("patch")
    additions = patch.get("add") if isinstance(patch, dict) else None
    frontier = data.get("frontier")
    if not isinstance(additions, list):
        raise _Malformed("graph patch additions must be a list")
    if not isinstance(frontier, int) or isinstance(frontier, bool) or frontier < 1:
        raise _Malformed("graph patch frontier must be a positive integer")
    new_ids: set[str] = set()
    for addition in additions:
        if not isinstance(addition, dict):
            raise _Malformed("graph patch addition must be an object")
        node_id = addition.get("id")
        capability = addition.get("capability")
        arguments = addition.get("arguments")
        dependencies = addition.get("depends_on")
        if not isinstance(node_id, str) or not node_id:
            raise _Malformed("graph node id must be a non-empty string")
        if node_id in state["nodes"] or node_id in new_ids:
            raise _Broken(f"graph patch repeats node {node_id!r}")
        if not isinstance(capability, str) or not capability:
            raise _Malformed(f"node {node_id!r} has an invalid capability")
        if not isinstance(arguments, dict):
            raise _Malformed(f"node {node_id!r} arguments must be an object")
        if not isinstance(dependencies, list) or any(
            not isinstance(parent, str) or not parent for parent in dependencies
        ):
            raise _Malformed(f"node {node_id!r} dependencies must be string ids")
        if len(dependencies) != len(set(dependencies)):
            raise _Broken(f"node {node_id!r} repeats a dependency")
        for parent in dependencies:
            if parent not in state["nodes"]:
                raise _Broken(
                    f"node {node_id!r} depends on unknown node {parent!r}"
                )
        new_ids.add(node_id)
        state["nodes"][node_id] = {
            "id": node_id,
            "capability": capability,
            "arguments": deepcopy(arguments),
            "frontier": frontier,
            "state": "pending",
            "failure_reason": None,
            "outcome": None,
            "error_detail": None,
        }
        state["edges"].update((parent, node_id) for parent in dependencies)


def _pending_descendants(state: dict[str, Any], node_id: str) -> tuple[str, ...]:
    children: dict[str, set[str]] = {}
    for source, target in state["edges"]:
        children.setdefault(source, set()).add(target)
    descendants: set[str] = set()
    pending = list(children.get(node_id, ()))
    while pending:
        descendant = pending.pop()
        if descendant in descendants:
            continue
        descendants.add(descendant)
        pending.extend(children.get(descendant, ()))
    return tuple(
        sorted(
            descendant
            for descendant in descendants
            if state["nodes"][descendant]["state"] == "pending"
        )
    )


def _apply_transition(
    state: dict[str, Any], event: dict[str, Any]
) -> tuple[str, ...]:
    event_type = event["type"]
    if event_type == "graph_patched":
        _add_patch(state, event)
        return ()
    if event_type not in {"task_started", "task_succeeded", "task_failed"}:
        return ()
    node_id = event.get("node")
    if not isinstance(node_id, str) or node_id not in state["nodes"]:
        raise _Broken(f"{event_type} names an unknown node {node_id!r}")
    node = state["nodes"][node_id]
    if event_type == "task_started":
        if node["state"] != "pending":
            raise _Broken(f"node {node_id!r} started from state {node['state']!r}")
        node["state"] = "running"
        return ()
    if event_type == "task_succeeded":
        outcome = event["data"].get("outcome")
        if not isinstance(outcome, dict):
            raise _Malformed(f"node {node_id!r} success outcome must be an object")
        if node["state"] != "running":
            raise _Broken(
                f"node {node_id!r} succeeded from state {node['state']!r}"
            )
        node["state"] = "succeeded"
        node["outcome"] = deepcopy(outcome)
        return ()
    reason = event["data"].get("reason")
    if not isinstance(reason, str) or not reason:
        raise _Malformed(f"node {node_id!r} failure reason must be a string")
    allowed_state = "pending" if reason == "blocked" else "running"
    if node["state"] != allowed_state:
        raise _Broken(
            f"node {node_id!r} failed from state {node['state']!r} as {reason!r}"
        )
    node["state"] = "failed"
    node["failure_reason"] = reason
    node["error_detail"] = deepcopy(event["data"].get("detail"))
    if reason == "blocked":
        return ()
    return _pending_descendants(state, node_id)


def _replay(events: list[dict[str, Any]]) -> dict[str, Any]:
    state = _empty_replay()
    index = 0
    while index < len(events):
        event = events[index]
        if (
            event["type"] == "task_failed"
            and event["data"].get("reason") == "blocked"
        ):
            raise _Broken(
                f"node {event.get('node')!r} has an unexpected blocking failure event"
            )
        descendants = _apply_transition(state, event)
        for descendant in descendants:
            index += 1
            if index >= len(events):
                raise _BlockingMismatch(
                    descendant,
                    event["node"],
                    f"node {descendant!r} is missing its blocking failure event",
                )
            blocked = events[index]
            if (
                blocked["type"] != "task_failed"
                or blocked.get("node") != descendant
                or blocked["data"].get("reason") != "blocked"
                or blocked["data"].get("detail")
                != {"blocked_by": event["node"]}
            ):
                raise _BlockingMismatch(
                    descendant,
                    event["node"],
                    f"node {descendant!r} has a missing or mismatched "
                    f"blocking failure event after {event['node']!r}",
                )
            _apply_transition(state, blocked)
        index += 1
    return state


def _persisted_graph(record: dict[str, Any]) -> dict[str, Any]:
    graph = _agent(record).get("graph")
    if not isinstance(graph, dict):
        raise _Malformed("persisted graph must be an object")
    raw_nodes = graph.get("nodes")
    raw_edges = graph.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise _Malformed("persisted graph must contain node and edge lists")
    nodes: dict[str, dict[str, Any]] = {}
    required_node_keys = (
        "id",
        "capability",
        "arguments",
        "frontier",
        "state",
        "failure_reason",
        "outcome",
        "error_detail",
    )
    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            raise _Malformed("persisted graph node must be an object")
        for key in required_node_keys:
            if key not in raw:
                node = raw.get("id")
                label = node if isinstance(node, str) and node else f"index {index}"
                raise _MissingNodeKey(label, key)
        node_id = raw.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise _Malformed("persisted graph node id must be a non-empty string")
        if node_id in nodes:
            raise _Malformed(f"persisted graph repeats node {node_id!r}")
        if not isinstance(raw.get("capability"), str):
            raise _Malformed(f"persisted node {node_id!r} has no capability")
        if not isinstance(raw.get("arguments"), dict):
            raise _Malformed(f"persisted node {node_id!r} has invalid arguments")
        frontier = raw.get("frontier")
        if not isinstance(frontier, int) or isinstance(frontier, bool) or frontier < 1:
            raise _Malformed(f"persisted node {node_id!r} has invalid frontier")
        if raw.get("state") not in {"pending", "running", "succeeded", "failed"}:
            raise _Malformed(f"persisted node {node_id!r} has invalid state")
        failure_reason = raw.get("failure_reason")
        if failure_reason is not None and not isinstance(failure_reason, str):
            raise _Malformed(f"persisted node {node_id!r} has invalid failure reason")
        outcome = raw.get("outcome")
        if outcome is not None and not isinstance(outcome, dict):
            raise _Malformed(f"persisted node {node_id!r} has invalid outcome")
        nodes[node_id] = {
            key: deepcopy(raw.get(key))
            for key in (
                "id",
                "capability",
                "arguments",
                "frontier",
                "state",
                "failure_reason",
                "outcome",
                "error_detail",
            )
        }
    edges: set[tuple[str, str]] = set()
    for raw in raw_edges:
        if not isinstance(raw, dict):
            raise _Malformed("persisted graph edge must be an object")
        source = raw.get("source")
        target = raw.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            raise _Malformed("persisted graph edge endpoints must be strings")
        if source not in nodes or target not in nodes:
            raise _Malformed("persisted graph edge names an unknown node")
        edge = (source, target)
        if edge in edges:
            raise _Malformed("persisted graph repeats an edge")
        edges.add(edge)
    return {"nodes": nodes, "edges": edges}


def journal_consistent(record: dict[str, Any]) -> dict[str, Any]:
    """Replay the persisted journal and compare it with the persisted final graph."""
    name = "journal_consistent"
    try:
        events = _events(record)
        replayed = _replay(events)
        persisted = _persisted_graph(record)
    except _BlockingMismatch as error:
        return _result(
            name,
            "fail",
            str(error),
            node=error.node,
            blocked_by=error.blocked_by,
        )
    except _Broken as error:
        return _result(name, "fail", str(error))
    except _MissingNodeKey as error:
        return _result(
            name,
            "fail",
            str(error),
            node=error.node,
            key=error.key,
        )
    except _Malformed as error:
        return _result(name, "inconclusive", f"malformed journal or graph: {error}")
    replay_nodes = {
        node_id: {
            key: deepcopy(node.get(key))
            for key in (
                "id",
                "capability",
                "arguments",
                "frontier",
                "state",
                "failure_reason",
                "outcome",
                "error_detail",
            )
        }
        for node_id, node in replayed["nodes"].items()
    }
    if replay_nodes != persisted["nodes"] or replayed["edges"] != persisted["edges"]:
        return _result(
            name,
            "fail",
            "journal replay does not match the persisted graph",
            replay_node_ids=sorted(replay_nodes),
            persisted_node_ids=sorted(persisted["nodes"]),
            replay_edges=sorted(replayed["edges"]),
            persisted_edges=sorted(persisted["edges"]),
        )
    return _result(
        name,
        "pass",
        "journal replay matches the persisted graph",
        nodes=len(replay_nodes),
        edges=len(replayed["edges"]),
    )


def capabilities_registered(record: dict[str, Any]) -> dict[str, Any]:
    """Require every persisted node capability to have been offered."""
    name = "capabilities_registered"
    try:
        graph = _persisted_graph(record)
        manifest = _agent(record).get("manifest")
        offered = manifest.get("offered") if isinstance(manifest, dict) else None
        if not isinstance(offered, list) or any(
            not isinstance(item, str) for item in offered
        ):
            raise _Malformed("manifest.offered must be a list of strings")
    except _Malformed as error:
        return _result(name, "inconclusive", f"malformed graph or manifest: {error}")
    unknown = sorted(
        {
            node["capability"]
            for node in graph["nodes"].values()
            if node["capability"] not in offered
        }
    )
    if unknown:
        return _result(
            name,
            "fail",
            "graph used capabilities absent from manifest.offered",
            capabilities=unknown,
        )
    return _result(name, "pass", "all graph capabilities were offered")


def _nonnegative_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _Malformed(f"{label} must be a non-negative integer")
    return value


def limits_respected(record: dict[str, Any]) -> dict[str, Any]:
    """Check graph, repair, and model-attempt ceilings from the persisted record."""
    name = "limits_respected"
    try:
        graph = _persisted_graph(record)
        values = record.get("config", {}).get("values")
        if not isinstance(values, dict):
            raise _Malformed("config.values must be an object")
        limits = values.get("limits")
        budgets = values.get("budgets")
        if not isinstance(limits, dict) or not isinstance(budgets, dict):
            raise _Malformed("config limits and budgets must be objects")
        max_nodes = _nonnegative_int(limits.get("max_nodes"), "limits.max_nodes")
        max_new = _nonnegative_int(
            limits.get("max_new_tasks"), "limits.max_new_tasks"
        )
        hard_limit = _nonnegative_int(
            limits.get("hard_repairs"), "limits.hard_repairs"
        )
        soft_limit = _nonnegative_int(
            limits.get("soft_repairs"), "limits.soft_repairs"
        )
        run_limit = _nonnegative_int(
            budgets.get("max_attempts_per_run"),
            "budgets.max_attempts_per_run",
        )
        round_limit = _nonnegative_int(
            budgets.get("max_attempts_per_round"),
            "budgets.max_attempts_per_round",
        )
        agent = _agent(record)
        patches = agent.get("patches")
        accepted = patches.get("accepted") if isinstance(patches, dict) else None
        rejected = patches.get("rejected") if isinstance(patches, dict) else None
        if not isinstance(accepted, list) or not isinstance(rejected, list):
            raise _Malformed("patches accepted and rejected must be lists")
        events = _events(record)
        addition_counts: list[int] = []
        for patch_record in accepted:
            patch = patch_record.get("patch") if isinstance(patch_record, dict) else None
            additions = patch.get("add") if isinstance(patch, dict) else None
            if not isinstance(additions, list):
                raise _Malformed("accepted patch additions must be a list")
            addition_counts.append(len(additions))
        journal_addition_counts = [
            len(event["data"]["patch"]["add"])
            for event in events
            if event["type"] == "graph_patched"
            and event["data"].get("kind") == "patch"
            and isinstance(event["data"].get("patch"), dict)
            and isinstance(event["data"]["patch"].get("add"), list)
        ]
        if addition_counts != journal_addition_counts:
            raise _Malformed("accepted patch summary disagrees with the journal")
        repair_counts = Counter()
        for rejection in rejected:
            kind = rejection.get("kind") if isinstance(rejection, dict) else None
            if kind not in {"hard", "soft"}:
                raise _Malformed("rejected patch kind must be hard or soft")
            repair_counts[kind] += 1
        exhaustion_kind = None
        if (
            events
            and events[-1]["type"] == "run_failed"
            and events[-1]["data"].get("error_type") == "PlannerError"
            and rejected
        ):
            exhaustion_kind = rejected[-1]["kind"]
        planner = agent.get("planner")
        if not isinstance(planner, dict):
            raise _Malformed("planner must be an object")
        hard_used = _nonnegative_int(
            planner.get("hard_repairs_used"), "planner.hard_repairs_used"
        )
        soft_used = _nonnegative_int(
            planner.get("soft_repairs_used"), "planner.soft_repairs_used"
        )
        economics = record.get("economics")
        entries = economics.get("entries") if isinstance(economics, dict) else None
        summary = economics.get("summary") if isinstance(economics, dict) else None
        if not isinstance(entries, list) or not isinstance(summary, dict):
            raise _Malformed("economics entries and summary must be present")
        attempts_by_round: Counter[int] = Counter()
        attempt_numbers: list[int] = []
        attempts = 0
        for entry in entries:
            if not isinstance(entry, dict):
                raise _Malformed("economics entry must be an object")
            round_number = entry.get("round")
            if not isinstance(round_number, int) or isinstance(round_number, bool):
                raise _Malformed("economics entry round must be an integer")
            attempt_number = entry.get("attempt")
            if (
                not isinstance(attempt_number, int)
                or isinstance(attempt_number, bool)
                or attempt_number < 1
            ):
                raise _Malformed("economics entry attempt must be a positive integer")
            attempt_numbers.append(attempt_number)
            if entry.get("status") != "refused":
                attempts += 1
                attempts_by_round[round_number] += 1
        summary_attempts = _nonnegative_int(
            summary.get("attempts"), "economics.summary.attempts"
        )
        if summary_attempts != attempts:
            raise _Malformed("economics summary attempts disagree with ledger entries")
    except (_Broken, _Malformed) as error:
        return _result(name, "inconclusive", f"malformed limit evidence: {error}")

    violations: list[str] = []
    if len(graph["nodes"]) > max_nodes:
        violations.append(f"nodes {len(graph['nodes'])} exceed {max_nodes}")
    if any(count > max_new for count in addition_counts):
        violations.append(f"accepted patch additions exceed {max_new}")
    if hard_used > hard_limit:
        violations.append(f"hard repairs {hard_used} exceed {hard_limit}")
    if soft_used > soft_limit:
        violations.append(f"soft repairs {soft_used} exceed {soft_limit}")
    repair_evidence = {
        "hard": (repair_counts["hard"], hard_used, hard_limit),
        "soft": (repair_counts["soft"], soft_used, soft_limit),
    }
    for kind, (rejected_count, used, limit) in repair_evidence.items():
        allowed_rejections = limit + (kind == exhaustion_kind)
        if rejected_count > allowed_rejections:
            violations.append(
                f"{kind} rejected patches {rejected_count} exceed "
                f"{allowed_rejections}"
            )
        expected_rejections = used + (kind == exhaustion_kind)
        if rejected_count != expected_rejections:
            violations.append(
                f"{kind} repairs used {used} disagree with "
                f"{rejected_count} rejected patches"
            )
    if attempts > run_limit:
        violations.append(f"model attempts {attempts} exceed {run_limit}")
    if any(attempt > round_limit for attempt in attempt_numbers):
        violations.append(f"a model attempt number exceeds {round_limit} in its round")
    excess_rounds = sorted(
        round_number
        for round_number, count in attempts_by_round.items()
        if count > round_limit
    )
    if excess_rounds:
        violations.append(f"model attempts exceed the per-round limit in {excess_rounds}")
    if violations:
        return _result(
            name,
            "fail",
            "; ".join(violations),
            repair_rejections=dict(repair_counts),
            repairs_used={"hard": hard_used, "soft": soft_used},
            repair_limits={"hard": hard_limit, "soft": soft_limit},
            exhaustion_kind=exhaustion_kind,
        )
    return _result(
        name,
        "pass",
        "graph, repair, and model-attempt limits were respected",
        nodes=len(graph["nodes"]),
        attempts=attempts,
    )


def terminal_last(record: dict[str, Any]) -> dict[str, Any]:
    """Require the terminal answer node to start only after all other work settles."""
    name = "terminal_last"
    try:
        events = _events(record)
        state = _empty_replay()
        terminal_started = False
        for event in events:
            if event["type"] == "task_started":
                node_id = event.get("node")
                node = state["nodes"].get(node_id)
                if node is None:
                    raise _Broken(f"task_started names unknown node {node_id!r}")
                if terminal_started:
                    return _result(
                        name,
                        "fail",
                        "a task started after the terminal answer node",
                        node=node_id,
                    )
                if node["capability"] == "answer":
                    active = sorted(
                        other_id
                        for other_id, other in state["nodes"].items()
                        if other_id != node_id
                        and other["state"] in {"pending", "running"}
                    )
                    if active:
                        return _result(
                            name,
                            "fail",
                            "the terminal answer started while other nodes were active",
                            active=active,
                        )
                    terminal_started = True
            _apply_transition(state, event)
    except _Broken as error:
        return _result(name, "inconclusive", f"malformed journal: {error}")
    except _Malformed as error:
        return _result(name, "inconclusive", f"malformed journal: {error}")
    if not terminal_started:
        return _result(
            name,
            "pass",
            "no terminal node started",
            detail="no terminal node started",
        )
    return _result(name, "pass", "the terminal answer node started last")


def write_after_target_read(record: dict[str, Any]) -> dict[str, Any]:
    """Require every action receipt to follow a successful model target read."""
    name = "write_after_target_read"
    try:
        events = _events(record)
        target_id = record.get("selection", {}).get("target_id")
        if target_id is not None and not isinstance(target_id, str):
            raise _Malformed("selection target_id must be a string or null")
        state = _empty_replay()
        succeeded: set[str] = set()
        action_count = 0
        for event in events:
            if event["type"] == "action_started":
                action_count += 1
                matching = any(
                    state["nodes"][node_id]["capability"] == "WorkOrder.get"
                    and state["nodes"][node_id]["arguments"] == {"id": target_id}
                    for node_id in succeeded
                )
                if not matching:
                    return _result(
                        name,
                        "fail",
                        "action started before a successful target WorkOrder.get",
                        action_node=event.get("node"),
                        target_id=target_id,
                    )
            _apply_transition(state, event)
            if event["type"] == "task_succeeded":
                node_id = event.get("node")
                if isinstance(node_id, str):
                    succeeded.add(node_id)
    except (_Broken, _Malformed) as error:
        return _result(name, "inconclusive", f"malformed journal: {error}")
    if action_count == 0:
        return _result(
            name,
            "pass",
            "no action started",
            detail="no action started",
        )
    return _result(name, "pass", "every action followed a successful target read")


def single_subject_write(record: dict[str, Any]) -> dict[str, Any]:
    """Limit subject updates and require every started action to finish."""
    name = "single_subject_write"
    try:
        calls = record.get("call_log")
        if not isinstance(calls, list) or any(not isinstance(call, dict) for call in calls):
            raise _Malformed("call_log must be a list of objects")
        updates = [
            call
            for call in calls
            if call.get("phase") == "subject"
            and call.get("tool") == "WorkOrder.update"
        ]
        events = _events(record)
        starts = [
            (index, event.get("node"))
            for index, event in enumerate(events)
            if event["type"] == "action_started"
        ]
        finishes = [
            (index, event.get("node"))
            for index, event in enumerate(events)
            if event["type"] == "action_finished"
        ]
        if any(not isinstance(node_id, str) for _, node_id in starts + finishes):
            raise _Malformed("action events must name a node")
    except (_Broken, _Malformed) as error:
        return _result(name, "inconclusive", f"malformed write evidence: {error}")
    if len(updates) > 1:
        return _result(
            name,
            "fail",
            "more than one subject WorkOrder.update attempt was recorded",
            attempts=len(updates),
        )
    unfinished = [
        node_id
        for start_index, node_id in starts
        if not any(
            finish_index > start_index and finish_node == node_id
            for finish_index, finish_node in finishes
        )
    ]
    if unfinished:
        return _result(
            name,
            "fail",
            "an action_started event has no matching later action_finished",
            nodes=unfinished,
        )
    return _result(
        name,
        "pass",
        "subject write count and action completion are valid",
        update_attempts=len(updates),
    )


__all__ = [
    "capabilities_registered",
    "journal_consistent",
    "limits_respected",
    "single_subject_write",
    "terminal_last",
    "write_after_target_read",
]
