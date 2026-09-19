"""Check concurrency evidence in persisted graph-subject run records."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable
from itertools import combinations
from pathlib import Path
from typing import Any

_IGNORED_SUFFIXES = (
    ".score.json",
    ".restore.json",
    ".fixture.json",
    ".action.json",
    ".spans.json",
    ".judge.json",
)
_TERMINAL_EVENTS = {"task_succeeded", "task_failed"}
_SUBJECT_PHASES = {"subject", "write_guard"}


def _identity(record: dict[str, Any]) -> tuple[Any, Any]:
    task = record.get("task")
    task_id = task.get("id") if isinstance(task, dict) else task
    return task_id, record.get("tenant")


def _result(
    record: dict[str, Any],
    status: str,
    failures: list[str],
    *,
    overlapping_call_pairs: int = 0,
    max_running: int = 0,
    max_workers: int | None = None,
) -> dict[str, Any]:
    task, tenant = _identity(record)
    return {
        "status": status,
        "failures": failures,
        "overlapping_call_pairs": overlapping_call_pairs,
        "max_running": max_running,
        "max_workers": max_workers,
        "task": task,
        "tenant": tenant,
    }


def _max_workers(record: dict[str, Any]) -> int | None:
    config = record.get("config")
    if not isinstance(config, dict):
        return None
    candidates = [config]
    values = config.get("values")
    if isinstance(values, dict):
        candidates.insert(0, values)
    for candidate in candidates:
        limits = candidate.get("limits")
        value = limits.get("max_workers") if isinstance(limits, dict) else None
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _subject_calls(record: dict[str, Any]) -> list[dict[str, Any]]:
    calls = record.get("call_log")
    if not isinstance(calls, list):
        return []
    return [
        call
        for call in calls
        if isinstance(call, dict) and call.get("phase") in _SUBJECT_PHASES
    ]


def _agent(record: dict[str, Any]) -> dict[str, Any] | None:
    output = record.get("subject_output")
    agent = output.get("agent") if isinstance(output, dict) else None
    return agent if isinstance(agent, dict) else None


def _node_map(agent: dict[str, Any]) -> dict[str, dict[str, Any]]:
    graph = agent.get("graph")
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    if not isinstance(nodes, list):
        return {}
    return {
        node["id"]: node
        for node in nodes
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }


def _journal(agent: dict[str, Any]) -> list[dict[str, Any]]:
    journal = agent.get("journal")
    if not isinstance(journal, list):
        return []
    events = [event for event in journal if isinstance(event, dict)]
    return sorted(
        events,
        key=lambda event: event.get("seq")
        if isinstance(event.get("seq"), int)
        else sys.maxsize,
    )


def _lifecycle_times(
    journal: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[str, int], int | None]:
    starts: dict[str, int] = {}
    terminals: dict[str, int] = {}
    first_start: int | None = None
    for event in journal:
        event_type = event.get("type")
        node = event.get("node")
        timestamp = event.get("monotonic_ns")
        if not isinstance(node, str) or not isinstance(timestamp, int):
            continue
        if event_type == "task_started":
            starts.setdefault(node, timestamp)
            first_start = timestamp if first_start is None else min(first_start, timestamp)
        elif event_type in _TERMINAL_EVENTS and node in starts:
            terminals.setdefault(node, timestamp)
    return starts, terminals, first_start


def _replay_gates(
    journal: list[dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
    max_workers: int | None,
) -> tuple[int, list[str]]:
    running: set[str] = set()
    maximum = 0
    failures: list[str] = []
    worker_limit_reported = False
    started_answer: str | None = None
    for event in journal:
        node_id = event.get("node")
        if not isinstance(node_id, str):
            continue
        event_type = event.get("type")
        if event_type == "task_started":
            capability = nodes.get(node_id, {}).get("capability")
            if started_answer is not None and node_id != started_answer:
                failures.append(
                    f"node {node_id!r} started after answer node "
                    f"{started_answer!r} started"
                )
            others = sorted(running - {node_id})
            running_reschedules = sorted(
                running_id
                for running_id in running
                if nodes.get(running_id, {}).get("capability")
                == "reschedule_work_order"
            )
            if capability == "reschedule_work_order" and others:
                failures.append(
                    f"reschedule node {node_id!r} started while other nodes were running"
                )
            elif running_reschedules and node_id not in running_reschedules:
                failures.append(
                    f"node {node_id!r} started while reschedule node "
                    f"{running_reschedules[0]!r} was running"
                )
            if capability == "answer" and others:
                failures.append(
                    f"answer node {node_id!r} started while other nodes were running"
                )
            if capability == "answer" and started_answer is None:
                started_answer = node_id
            running.add(node_id)
            maximum = max(maximum, len(running))
            if (
                max_workers is not None
                and maximum > max_workers
                and not worker_limit_reported
            ):
                failures.append(
                    f"max running nodes {maximum} exceeds max_workers {max_workers}"
                )
                worker_limit_reported = True
        elif event_type in _TERMINAL_EVENTS:
            running.discard(node_id)
    return maximum, failures


def _overlap_count(calls: list[dict[str, Any]]) -> int:
    attributed = [
        call
        for call in calls
        if call.get("kind") == "call_tool"
        and isinstance(call.get("node"), str)
        and isinstance(call.get("start_sequence"), int)
        and isinstance(call.get("end_sequence"), int)
    ]
    count = 0
    for first, second in combinations(attributed, 2):
        if first["node"] == second["node"]:
            continue
        earlier, later = sorted(
            (first, second), key=lambda call: call["start_sequence"]
        )
        if (
            earlier["start_sequence"]
            < later["start_sequence"]
            < earlier["end_sequence"]
        ):
            count += 1
    return count


def _expected_arguments(capability: str, arguments: Any) -> Any:
    if not capability.endswith(".list") or not isinstance(arguments, dict):
        return arguments
    return {
        key: value
        for key, value in arguments.items()
        if key not in {"limit", "offset"}
    }


def _check_attribution(
    calls: list[dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
    journal: list[dict[str, Any]],
) -> list[str]:
    failures: list[str] = []
    starts, terminals, first_start = _lifecycle_times(journal)
    for index, call in enumerate(calls, start=1):
        if call.get("kind") not in {"call_tool", "get_tool"}:
            continue
        node_id = call.get("node")
        started = call.get("started_ns")
        finished = call.get("finished_ns")
        start_sequence = call.get("start_sequence")
        end_sequence = call.get("end_sequence")
        if (
            not isinstance(start_sequence, int)
            or not isinstance(end_sequence, int)
            or start_sequence >= end_sequence
        ):
            failures.append(f"call {index} has an invalid sequence interval")
        if node_id is None:
            if first_start is not None and (
                not isinstance(started, int) or started >= first_start
            ):
                failures.append(f"call {index} has a null node after task execution began")
            continue
        if not isinstance(node_id, str) or node_id not in nodes:
            failures.append(f"call {index} names node {node_id!r} not in the graph")
            continue
        node = nodes[node_id]
        capability = node.get("capability")
        tool = call.get("tool")
        if capability == "answer":
            failures.append(f"answer node {node_id!r} has a recorded tool call")
        elif capability == "reschedule_work_order":
            if tool not in {"WorkOrder.get", "WorkOrder.update"}:
                failures.append(
                    f"reschedule node {node_id!r} used disallowed tool {tool!r}"
                )
        elif tool != capability:
            failures.append(
                f"call for node {node_id!r} used tool {tool!r}, expected {capability!r}"
            )
        elif call.get("kind") == "call_tool" and call.get(
            "arguments"
        ) != _expected_arguments(str(capability), node.get("arguments")):
            actual = _expected_arguments(str(capability), call.get("arguments"))
            expected = _expected_arguments(str(capability), node.get("arguments"))
            if actual != expected:
                failures.append(f"call arguments do not match node {node_id!r}")

        node_start = starts.get(node_id)
        if not isinstance(started, int) or node_start is None or started < node_start:
            failures.append(f"call for node {node_id!r} starts outside its journal window")
        node_terminal = terminals.get(node_id)
        if (
            node_terminal is not None
            and (not isinstance(finished, int) or finished > node_terminal)
        ):
            failures.append(f"call for node {node_id!r} finishes outside its journal window")
    return failures


def check_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return concurrency and attribution findings for one persisted run record."""
    max_workers = _max_workers(record)
    subject = record.get("subject")
    if not isinstance(subject, dict) or subject.get("name") not in {
        "graph_subject",
        "graph",
    }:
        return _result(
            record,
            "skipped",
            ["subject is not graph"],
            max_workers=max_workers,
        )
    agent = _agent(record)
    if agent is None:
        return _result(
            record,
            "skipped",
            ["subject_output.agent is unavailable"],
            max_workers=max_workers,
        )

    calls = _subject_calls(record)
    required = {"start_sequence", "end_sequence", "started_ns", "finished_ns", "node"}
    if any(
        call.get("kind") == "call_tool" and not required.issubset(call)
        for call in calls
    ):
        return _result(
            record,
            "inconclusive",
            ["no attribution fields"],
            max_workers=max_workers,
        )

    nodes = _node_map(agent)
    journal = _journal(agent)
    max_running, failures = _replay_gates(journal, nodes, max_workers)
    failures.extend(_check_attribution(calls, nodes, journal))
    overlapping = _overlap_count(calls)
    return _result(
        record,
        "fail" if failures else "pass",
        failures,
        overlapping_call_pairs=overlapping,
        max_running=max_running,
        max_workers=max_workers,
    )


def _record_paths(paths: Iterable[str | Path]) -> list[Path]:
    selected: set[Path] = set()
    for raw_path in paths:
        path = Path(raw_path)
        candidates = path.rglob("*.json") if path.is_dir() else (path,)
        for candidate in candidates:
            if not candidate.is_file() or candidate.name.endswith(_IGNORED_SUFFIXES):
                continue
            selected.add(candidate)
    return sorted(selected, key=lambda path: str(path))


def check_files(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    """Load and check run records from files or directories."""
    results: list[dict[str, Any]] = []
    for path in _record_paths(paths):
        try:
            with path.open(encoding="utf-8") as stream:
                record = json.load(stream)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            result = _result({}, "skipped", [f"cannot read record: {type(error).__name__}"])
        else:
            if not isinstance(record, dict):
                result = _result({}, "skipped", ["record is not an object"])
            else:
                result = check_record(record)
        result["file"] = str(path)
        results.append(result)
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check concurrency evidence in persisted graph run records"
    )
    parser.add_argument("paths", metavar="FILE_OR_DIR", nargs="+")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the persisted-record concurrency checker CLI."""
    args = _parser().parse_args(argv)
    paths = [Path(path) for path in args.paths]
    missing = [path for path in paths if not path.exists()]
    if missing:
        for path in missing:
            print(f"input not found: {path}", file=sys.stderr)
        return 2
    results = check_files(paths)
    for result in results:
        print(
            f"{Path(result['file']).name} task={result['task']} "
            f"status={result['status']} overlap_pairs={result['overlapping_call_pairs']} "
            f"max_running={result['max_running']}"
        )
    counts = Counter(result["status"] for result in results)
    concurrent = sum(result["max_running"] >= 2 for result in results)
    print(
        "summary: "
        f"pass={counts['pass']} fail={counts['fail']} "
        f"inconclusive={counts['inconclusive']} skipped={counts['skipped']} "
        f"max_running>=2={concurrent}"
    )
    return 1 if counts["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
