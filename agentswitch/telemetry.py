"""Build a safe span tree from a persisted harness run record.

Coverage is ``pre_restore`` because the run record is persisted before fixture
restoration; restore calls therefore cannot appear in its call log.
"""

from __future__ import annotations

from pathlib import PurePath
from typing import Any

_SCHEMA = "agentswitch.spans/1"


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: Any) -> int | float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _bounds(items: list[tuple[int | None, int | None]]) -> tuple[int | None, int | None]:
    starts = [start for start, _ in items if start is not None]
    ends = [end for _, end in items if end is not None]
    return (min(starts) if starts else None, max(ends) if ends else None)


def _span(
    span_id: str,
    parent_id: str | None,
    name: str,
    kind: str,
    start_ns: int | None,
    end_ns: int | None,
    attributes: dict[str, Any],
) -> dict[str, Any]:
    return {
        "span_id": span_id,
        "parent_id": parent_id,
        "name": name,
        "kind": kind,
        "start_ns": start_ns,
        "end_ns": end_ns,
        "attributes": attributes,
    }


def _widen_aggregate_spans(spans: list[dict[str, Any]]) -> None:
    children: dict[str, list[dict[str, Any]]] = {}
    for span in spans:
        parent_id = span["parent_id"]
        if parent_id is not None:
            children.setdefault(parent_id, []).append(span)

    for span in reversed(spans):
        span_id = span["span_id"]
        if not (
            span_id == "run"
            or span_id.startswith(("phase:", "round:", "node:"))
        ):
            continue
        span["start_ns"], span["end_ns"] = _bounds(
            [
                (span["start_ns"], span["end_ns"]),
                *[
                    (child["start_ns"], child["end_ns"])
                    for child in children.get(span_id, [])
                ],
            ]
        )


def _journal(record: dict[str, Any], warnings: list[str]) -> list[dict[str, Any]]:
    subject_output = record.get("subject_output")
    agent = subject_output.get("agent") if isinstance(subject_output, dict) else None
    raw = agent.get("journal") if isinstance(agent, dict) else None
    if raw is None:
        return []
    if not isinstance(raw, list):
        warnings.append("subject journal is not a list; journal spans were skipped")
        return []
    events = [item for item in raw if isinstance(item, dict)]
    if len(events) != len(raw):
        warnings.append("non-object journal events were skipped")
    return events


def _node_capabilities(record: dict[str, Any]) -> dict[str, str]:
    subject_output = record.get("subject_output")
    agent = subject_output.get("agent") if isinstance(subject_output, dict) else None
    graph = agent.get("graph") if isinstance(agent, dict) else None
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    if not isinstance(nodes, list):
        return {}
    result: dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = _text(node.get("id"))
        capability = _text(node.get("capability"))
        if node_id is not None and capability is not None:
            result[node_id] = capability
    return result


def _attempt_attributes(entry: dict[str, Any]) -> dict[str, Any]:
    attributes: dict[str, Any] = {"gen_ai.provider.name": "openrouter"}
    model = entry.get("model")
    if isinstance(model, dict):
        requested = _text(model.get("requested"))
        returned = _text(model.get("returned"))
        if requested is not None:
            attributes["gen_ai.request.model"] = requested
        if returned is not None:
            attributes["gen_ai.response.model"] = returned
    input_tokens = _integer(entry.get("prompt_tokens"))
    output_tokens = _integer(entry.get("completion_tokens"))
    if input_tokens is not None:
        attributes["gen_ai.usage.input_tokens"] = input_tokens
    if output_tokens is not None:
        attributes["gen_ai.usage.output_tokens"] = output_tokens
    finish_reason = entry.get("finish_reason")
    if isinstance(finish_reason, str):
        attributes["gen_ai.response.finish_reasons"] = [finish_reason]
    elif isinstance(finish_reason, list):
        reasons = [item for item in finish_reason if isinstance(item, str)]
        if reasons:
            attributes["gen_ai.response.finish_reasons"] = reasons
    for source, target in (
        ("charged_micro", "agentswitch.charged_micro"),
        ("provider_cost_micro", "agentswitch.provider_cost_micro"),
    ):
        value = _integer(entry.get(source))
        if value is not None:
            attributes[target] = value
    for source, target in (
        ("status", "agentswitch.ledger_status"),
        ("outcome", "agentswitch.outcome"),
        ("error_type", "agentswitch.error_type"),
    ):
        value = _text(entry.get(source))
        if value is not None:
            attributes[target] = value
    return attributes


def _call_attributes(call: dict[str, Any], phase: str) -> dict[str, Any]:
    attributes: dict[str, Any] = {"mcp.phase": phase}
    for source, target in (
        ("kind", "mcp.kind"),
        ("tool", "mcp.tool"),
        ("outcome", "mcp.outcome"),
        ("node", "agentswitch.node"),
    ):
        value = _text(call.get(source))
        if value is not None:
            attributes[target] = value
    elapsed = _number(call.get("elapsed_ms"))
    if elapsed is not None:
        attributes["agentswitch.elapsed_ms"] = elapsed
    return attributes


def _build(record: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    raw_run_file = record.get("run_file")
    run_file = raw_run_file if isinstance(raw_run_file, str) else ""
    if not run_file:
        warnings.append("run_file is missing or invalid")
    try:
        trace_id = PurePath(run_file).stem if run_file else ""
    except (TypeError, ValueError):
        trace_id = ""
        warnings.append("run_file stem could not be derived")

    events = _journal(record, warnings)
    run_started = next(
        (event for event in events if event.get("type") == "run_started"), None
    )
    run_terminal = next(
        (
            event
            for event in reversed(events)
            if event.get("type") in {"run_finished", "run_failed"}
        ),
        None,
    )
    timings = record.get("timings")
    fallback_anchor = timings.get("started_at") if isinstance(timings, dict) else None
    anchor_at = (
        run_started.get("at")
        if isinstance(run_started, dict) and isinstance(run_started.get("at"), str)
        else fallback_anchor
        if isinstance(fallback_anchor, str)
        else None
    )
    anchor_ns = (
        _integer(run_started.get("monotonic_ns"))
        if isinstance(run_started, dict)
        else None
    )

    raw_calls = record.get("call_log")
    if not isinstance(raw_calls, list):
        raw_calls = []
        warnings.append("call_log is not a list; call spans were skipped")
    calls: list[tuple[int, dict[str, Any], str]] = []
    phases: list[str] = []
    for index, item in enumerate(raw_calls, start=1):
        if not isinstance(item, dict):
            warnings.append("non-object call_log entries were skipped")
            continue
        original_phase = _text(item.get("phase")) or "unknown"
        phase = "subject" if original_phase == "write_guard" else original_phase
        calls.append((index, item, phase))
        if phase not in phases:
            phases.append(phase)

    economics = record.get("economics")
    raw_entries = economics.get("entries") if isinstance(economics, dict) else None
    if raw_entries is None:
        raw_entries = []
    elif not isinstance(raw_entries, list):
        raw_entries = []
        warnings.append("economics entries are not a list; attempt spans were skipped")
    entries: list[dict[str, Any]] = []
    for item in raw_entries:
        if isinstance(item, dict):
            entries.append(item)
        else:
            warnings.append("non-object economics entries were skipped")
    if (entries or events) and "subject" not in phases:
        phases.append("subject")

    summary = economics.get("summary") if isinstance(economics, dict) else None
    total_charged = (
        _integer(summary.get("spent_micro")) if isinstance(summary, dict) else None
    )
    task = record.get("task")
    subject = record.get("subject")
    run_attributes: dict[str, Any] = {}
    subject_name = _text(subject.get("name")) if isinstance(subject, dict) else None
    task_id = _text(task.get("id")) if isinstance(task, dict) else None
    tenant = _text(record.get("tenant"))
    if subject_name is not None:
        run_attributes["agentswitch.subject"] = subject_name
    if task_id is not None:
        run_attributes["agentswitch.task"] = task_id
    if tenant is not None:
        run_attributes["agentswitch.tenant"] = tenant
    if total_charged is not None:
        run_attributes["agentswitch.charged_micro"] = total_charged

    spans = [
        _span(
            "run",
            None,
            "agentswitch.run",
            "internal",
            _integer(run_started.get("monotonic_ns"))
            if isinstance(run_started, dict)
            else None,
            _integer(run_terminal.get("monotonic_ns"))
            if isinstance(run_terminal, dict)
            else None,
            run_attributes,
        )
    ]

    call_bounds: dict[str, list[tuple[int | None, int | None]]] = {
        phase: [] for phase in phases
    }
    for _, call, phase in calls:
        call_bounds[phase].append(
            (_integer(call.get("started_ns")), _integer(call.get("finished_ns")))
        )
    for phase in phases:
        start_ns, end_ns = _bounds(call_bounds[phase])
        spans.append(
            _span(
                f"phase:{phase}",
                "run",
                phase,
                "internal",
                start_ns,
                end_ns,
                {},
            )
        )

    attempts_by_round: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    for index, entry in enumerate(entries, start=1):
        round_number = _integer(entry.get("round"))
        if round_number is None or round_number < 1:
            warnings.append("economics entries with invalid rounds were skipped")
            continue
        attempts_by_round.setdefault(round_number, []).append((index, entry))

    starts: dict[str, dict[str, Any]] = {}
    terminals: dict[str, dict[str, Any]] = {}
    node_rounds: dict[str, int] = {}
    for event in events:
        node_id = _text(event.get("node"))
        event_type = event.get("type")
        if node_id is None:
            continue
        if event_type == "task_started" and node_id not in starts:
            starts[node_id] = event
            round_number = _integer(event.get("round"))
            if round_number is not None and round_number >= 1:
                node_rounds[node_id] = round_number
        elif event_type in {"task_succeeded", "task_failed"} and node_id not in terminals:
            terminals[node_id] = event

    all_rounds = sorted(set(attempts_by_round) | set(node_rounds.values()))
    for round_number in all_rounds:
        round_entries = attempts_by_round.get(round_number, [])
        start_ns, end_ns = _bounds(
            [
                (_integer(entry.get("started_ns")), _integer(entry.get("finished_ns")))
                for _, entry in round_entries
            ]
        )
        spans.append(
            _span(
                f"round:{round_number}",
                "phase:subject",
                f"planner round {round_number}",
                "internal",
                start_ns,
                end_ns,
                {},
            )
        )
        used_attempt_ids: set[str] = set()
        for entry_index, entry in round_entries:
            attempt = _integer(entry.get("attempt"))
            suffix = str(attempt) if attempt is not None and attempt >= 1 else str(entry_index)
            span_id = f"round:{round_number}/attempt:{suffix}"
            if span_id in used_attempt_ids:
                span_id = f"{span_id}:{entry_index}"
            used_attempt_ids.add(span_id)
            spans.append(
                _span(
                    span_id,
                    f"round:{round_number}",
                    "openrouter.chat",
                    "client",
                    _integer(entry.get("started_ns")),
                    _integer(entry.get("finished_ns")),
                    _attempt_attributes(entry),
                )
            )

    capabilities = _node_capabilities(record)
    for node_id, start_event in starts.items():
        terminal = terminals.get(node_id)
        attributes: dict[str, Any] = {}
        capability = capabilities.get(node_id)
        if capability is not None:
            attributes["agentswitch.capability"] = capability
        if terminal is not None:
            attributes["agentswitch.state"] = terminal.get("type")
        round_number = node_rounds.get(node_id)
        parent_id = (
            f"round:{round_number}" if round_number in all_rounds else "phase:subject"
        )
        spans.append(
            _span(
                f"node:{node_id}",
                parent_id,
                capability or node_id,
                "internal",
                _integer(start_event.get("monotonic_ns")),
                _integer(terminal.get("monotonic_ns"))
                if terminal is not None
                else None,
                attributes,
            )
        )

    used_call_ids: set[str] = set()
    node_span_ids = {f"node:{node_id}" for node_id in starts}
    for index, call, phase in calls:
        sequence = call.get("start_sequence")
        suffix = (
            str(sequence)
            if isinstance(sequence, (str, int)) and not isinstance(sequence, bool)
            else str(index)
        )
        span_id = f"call:{suffix}"
        if span_id in used_call_ids:
            span_id = f"call:{suffix}:{index}"
        used_call_ids.add(span_id)
        node_id = _text(call.get("node"))
        candidate_parent = f"node:{node_id}" if node_id is not None else ""
        parent_id = (
            candidate_parent
            if phase == "subject" and candidate_parent in node_span_ids
            else f"phase:{phase}"
        )
        spans.append(
            _span(
                span_id,
                parent_id,
                _text(call.get("tool")) or _text(call.get("kind")) or "mcp.call",
                "client",
                _integer(call.get("started_ns")),
                _integer(call.get("finished_ns")),
                _call_attributes(call, phase),
            )
        )

    _widen_aggregate_spans(spans)

    return {
        "schema": _SCHEMA,
        "run_file": run_file,
        "trace_id": trace_id,
        "coverage": "pre_restore",
        "clock": {
            "kind": "monotonic_ns",
            "anchor_at": anchor_at,
            "anchor_ns": anchor_ns,
        },
        "spans": spans,
        "warnings": warnings,
    }


def build_spans(record: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic, content-safe span projection for any dictionary."""
    warnings: list[str] = []
    try:
        return _build(record, warnings)
    except Exception:
        return {
            "schema": _SCHEMA,
            "run_file": "",
            "trace_id": "",
            "coverage": "pre_restore",
            "clock": {"kind": "monotonic_ns", "anchor_at": None, "anchor_ns": None},
            "spans": [
                _span("run", None, "agentswitch.run", "internal", None, None, {})
            ],
            "warnings": [*warnings, "unexpected malformed input; remaining spans were skipped"],
        }


__all__ = ["build_spans"]
