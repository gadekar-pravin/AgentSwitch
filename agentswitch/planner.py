"""Frontier planning, validation, repairs, and deterministic answer criticism."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Literal

from .answer import Store, read_requirements
from .capabilities import CapabilityArgumentError, Manifest, validate
from .config import LimitsConfig
from .graph import GraphPatch, LiveGraph, NodeSnapshot, NodeSpec

RepairKind = Literal["hard", "soft"]

_REFUSAL_REASONS = frozenset(
    {"not_found", "outside_seat", "unsupported", "source_unavailable"}
)
_ACTIVE_OR_COMPLETE = frozenset({"pending", "running", "succeeded"})

PLAN_FRONTIER: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "plan_frontier",
        "description": "Propose the next independent graph nodes, or the terminal answer node.",
        "parameters": {
            "type": "object",
            "properties": {
                "add": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "capability": {"type": "string"},
                            "arguments": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["id", "capability", "arguments", "depends_on"],
                        "additionalProperties": False,
                    },
                },
                "finish": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["add", "finish", "reason"],
            "additionalProperties": False,
        },
    },
}

PLAN_FRONTIER_CHOICE: dict[str, Any] = {
    "type": "function",
    "function": {"name": "plan_frontier"},
}

SYSTEM_MESSAGE = """You are the Production-seat planner for the manufacturing app.
Return exactly one plan_frontier function call. Each call proposes one graph patch; code, not you, executes it.
Use only the canonical capabilities listed in the manifest. Arguments must exactly match that capability's schema.
Every depends_on id must already exist in the graph. Never depend on a node proposed in the same patch; propose that child again in the next round after its parent exists.
The answer capability takes no dependencies: propose it with an empty depends_on list once everything needed has settled. It must be the only addition in its patch, and may be proposed only when no node is pending or running. The patch finish flag does not finish the run; only a succeeded answer node does.
A failed read may be proposed again. List reads are paged to completion by code, so provide only real filters. Never use empty or :placeholder filter values.
A failed read's error type identifies the fitting refusal: not_found on the target read means the target does not exist; transport or permission failures mean the source is unavailable.
Use reschedule_work_order only for the supplied target and at most once. It returns a proposal because writes are not permitted.
Code computes lateness, causes, and downstream claims only from records read. Status comes first: draft, completed, and cancelled work orders are never late. Shared-BOM material links identify only potential downstream consumers.
Refuse with not_found only when the target does not exist; outside_seat when needed data or actions are absent from the seat catalogue; unsupported when the request cannot be supported by available data; source_unavailable when an offered source fails.
For a complete lateness answer, read the target, linked cause sources, all ECOs/BOMs/workstations, the linked BOM and sales order, each consumer BOM's work orders, and finite_schedule."""


@dataclass(frozen=True)
class RejectedAttempt:
    """One rejected model reply retained for the run record."""

    raw: Any
    kind: RepairKind
    message: str


@dataclass(frozen=True)
class PlannerState:
    """Immutable repair counters and rejection history for one run."""

    hard_repairs_left: int
    soft_repairs_left: int
    rejected_attempts: tuple[RejectedAttempt, ...] = ()

    @classmethod
    def from_limits(cls, limits: LimitsConfig) -> PlannerState:
        return cls(
            hard_repairs_left=limits.hard_repairs,
            soft_repairs_left=limits.soft_repairs,
        )


@dataclass(frozen=True)
class DiscardedAddition:
    """A valid addition omitted from an otherwise usable patch."""

    node_id: str
    reason: str
    covering_node_id: str | None = None
    covering_state: str | None = None
    covering_outcome: str | None = None


@dataclass(frozen=True)
class PlannerDecision:
    """The accepted patch or a repair response to feed into the next round."""

    patch: GraphPatch | None
    state: PlannerState
    discarded: tuple[DiscardedAddition, ...] = ()
    repair_kind: RepairKind | None = None
    message: str | None = None
    missing: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.patch is not None


@dataclass(frozen=True)
class ParsedReply:
    """Strictly decoded plan_frontier arguments."""

    raw: dict[str, Any]
    patch: GraphPatch


@dataclass(frozen=True)
class PatchReview:
    """Pure validation result before repair-budget policy is applied."""

    patch: GraphPatch | None
    discarded: tuple[DiscardedAddition, ...] = ()
    hard_error: str | None = None
    duplicates_only: bool = False


@dataclass(frozen=True)
class CriticResult:
    """Deterministic contradiction and evidence-readiness result."""

    hard_error: str | None = None
    missing: tuple[str, ...] = ()


class PlannerReplyError(ValueError):
    """The provider reply did not contain one well-shaped plan_frontier call."""

    def __init__(self, message: str, *, raw: Any) -> None:
        self.raw = copy.deepcopy(raw)
        super().__init__(message)


class PlannerError(RuntimeError):
    """A repair budget was exhausted; the executor must fail the run visibly."""

    def __init__(
        self,
        message: str,
        *,
        kind: RepairKind,
        raw: Any,
        state: PlannerState,
    ) -> None:
        attempt = RejectedAttempt(copy.deepcopy(raw), kind, message)
        self.kind = kind
        self.raw = copy.deepcopy(raw)
        self.state = replace(
            state,
            rejected_attempts=(*state.rejected_attempts, attempt),
        )
        super().__init__(message)

    def details(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": str(self),
            "raw": copy.deepcopy(self.raw),
            "hard_repairs_left": self.state.hard_repairs_left,
            "soft_repairs_left": self.state.soft_repairs_left,
        }


def _compact(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _compact_in_order(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _truncated_json(value: Any, limit: int) -> str:
    if limit <= 0:
        return ""
    rendered = _compact(value)
    if len(rendered) <= limit:
        return rendered
    marker = _compact({"preview": "", "truncated": True})
    short_marker = _compact({"truncated": True})
    if limit < len(short_marker):
        return "…" if limit >= 1 else ""
    if limit <= len(marker):
        return short_marker
    excerpt = rendered[: limit - len(marker)]
    while True:
        candidate = _compact({"preview": excerpt, "truncated": True})
        if len(candidate) <= limit or not excerpt:
            return candidate
        excerpt = excerpt[: len(excerpt) - (len(candidate) - limit)]


def _row_count(outcome: dict[str, Any] | None) -> int | None:
    if not isinstance(outcome, dict):
        return None
    for key in ("data", "rows"):
        rows = outcome.get(key)
        if isinstance(rows, list):
            return len(rows)
    returned = outcome.get("returned")
    if isinstance(returned, int) and not isinstance(returned, bool):
        return returned
    return None


def _failed_projection_parts(
    node: NodeSnapshot,
) -> tuple[dict[str, Any], str | None, dict[str, Any]]:
    """Split failure detail into protected metadata, message, and other detail."""
    metadata: dict[str, Any] = {"failure_reason": node.failure_reason}
    detail = copy.deepcopy(node.error_detail)
    message = None

    if isinstance(detail, dict):
        error = detail.get("error")
        if isinstance(error, dict):
            error = dict(error)
            error_type = error.pop("type", None)
            if error_type is not None:
                metadata["type"] = error_type
            if "outcome_unknown" in error:
                metadata["outcome_unknown"] = error.pop("outcome_unknown")
            raw_message = error.pop("message", None)
            if raw_message is not None:
                message = str(raw_message)
            detail = dict(detail)
            detail.pop("error")
            if error:
                detail["error"] = error

        if "blocked_by" in detail:
            metadata["blocked_by"] = detail.pop("blocked_by")

        if node.failure_reason == "incomplete_scan":
            total = detail.pop("total", None)
            if total is not None:
                metadata["total"] = total
            rows = _row_count(detail)
            if rows is not None:
                metadata["rows"] = rows
            if "complete" in detail:
                metadata["complete"] = detail.pop("complete")
    elif detail is not None:
        detail = {"value": detail}

    return metadata, message, detail if isinstance(detail, dict) else {}


def _failed_projection(
    node: NodeSnapshot, limit: int, *, include_detail: bool = True
) -> str:
    """Render a failure with diagnostic metadata protected from long messages."""
    if limit <= 0:
        return ""
    metadata, message, detail = _failed_projection_parts(node)
    full = dict(metadata)
    if message is not None:
        full["message"] = message
    if detail:
        full["detail"] = detail
    rendered = _compact_in_order(full)
    if include_detail and len(rendered) <= limit:
        return rendered

    base = {**metadata, "truncated": True}
    rendered_base = _compact_in_order(base)
    if len(rendered_base) > limit:
        return _truncated_json(metadata, limit)
    if not include_detail:
        return rendered_base

    content = dict(metadata)
    if message is not None:
        with_message = {**content, "message": message, "truncated": True}
        if len(_compact_in_order(with_message)) > limit:
            low = 0
            high = len(message)
            best = rendered_base
            while low <= high:
                middle = (low + high) // 2
                candidate = _compact_in_order(
                    {**content, "message": message[:middle], "truncated": True}
                )
                if len(candidate) <= limit:
                    best = candidate
                    low = middle + 1
                else:
                    high = middle - 1
            return best
        content["message"] = message

    best = _compact_in_order({**content, "truncated": True})
    if detail:
        low = 1
        high = len(_compact(detail))
        while low <= high:
            middle = (low + high) // 2
            candidate = _compact_in_order(
                {
                    **content,
                    "detail": _truncated_json(detail, middle),
                    "truncated": True,
                }
            )
            if len(candidate) <= limit:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
    return best


def _summary_projection(node: NodeSnapshot, limit: int) -> str:
    if node.state == "failed":
        return _failed_projection(node, limit, include_detail=False)
    summary: dict[str, Any] = {
        "capability": node.capability,
        "id": node.id,
        "state": node.state,
    }
    projected = node.error_detail if node.state == "failed" else node.outcome
    rows = _row_count(projected)
    if rows is not None:
        summary["rows"] = rows
    return _truncated_json(summary, limit)


def _node_projection(node: NodeSnapshot, limit: int) -> str:
    if node.state == "failed":
        return _failed_projection(node, limit)
    return _truncated_json(node.outcome, limit)


def _node_projections(
    nodes: tuple[NodeSnapshot, ...], limits: LimitsConfig
) -> dict[str, str]:
    projections = {
        node.id: _node_projection(node, limits.projection_chars)
        for node in nodes
    }
    total = sum(len(value) for value in projections.values())
    if total <= limits.projection_total_chars:
        return projections

    oldest_first = sorted(nodes, key=lambda node: (node.frontier, node.id))
    newest = max(oldest_first, key=lambda node: (node.frontier, node.id), default=None)
    collapsed: list[NodeSnapshot] = []
    for node in oldest_first:
        if newest is not None and node.id == newest.id:
            continue
        summary = _summary_projection(node, limits.projection_chars)
        current = projections[node.id]
        if len(summary) < len(current):
            projections[node.id] = summary
            total -= len(current) - len(summary)
            collapsed.append(node)
        if total <= limits.projection_total_chars:
            break

    if total > limits.projection_total_chars and newest is not None:
        current = projections[newest.id]
        allowed = max(0, len(current) - (total - limits.projection_total_chars))
        shortened = _node_projection(newest, allowed)
        projections[newest.id] = shortened
        total -= len(current) - len(shortened)

    for node in collapsed:
        if total <= limits.projection_total_chars:
            break
        current = projections[node.id]
        allowed = max(0, len(current) - (total - limits.projection_total_chars))
        shortened = _summary_projection(node, allowed)
        projections[node.id] = shortened
        total -= len(current) - len(shortened)

    if total > limits.projection_total_chars:
        for node in oldest_first:
            if total <= limits.projection_total_chars:
                break
            current = projections[node.id]
            allowed = max(
                0, len(current) - (total - limits.projection_total_chars)
            )
            shortened = _node_projection(node, allowed)
            projections[node.id] = shortened
            total -= len(current) - len(shortened)
    return projections


def _dependency_map(graph: LiveGraph) -> dict[str, list[str]]:
    dependencies = {node.id: [] for node in graph.nodes()}
    for edge in graph.export().get("edges", []):
        if not isinstance(edge, dict):
            continue
        source = edge.get("source")
        target = edge.get("target")
        if isinstance(source, str) and isinstance(target, str) and target in dependencies:
            dependencies[target].append(source)
    for parents in dependencies.values():
        parents.sort()
    return dependencies


def missing_read_calls(store: Store, target_id: str | None) -> list[str]:
    """Return exact missing read calls using canonical capability names."""
    calls: list[str] = []
    for _label, capability, arguments, read in read_requirements(store, target_id):
        if read:
            continue
        if capability == "endpoint.manufacturing.finite_schedule" and not arguments:
            calls.append(f"{capability} (call with its required arguments)")
        else:
            calls.append(f"{capability} {_compact(arguments)}")
    return calls


def build_messages(
    *,
    request: str,
    target_id: str | None,
    today: date,
    manifest: Manifest,
    graph: LiveGraph,
    store: Store,
    limits: LimitsConfig,
    state: PlannerState,
    reschedule_requested: bool,
    reschedule_attempted: bool,
    repair_messages: tuple[str, ...] = (),
) -> list[dict[str, str]]:
    """Build the deterministic, bounded two-message planner prompt for one round."""
    nodes = graph.nodes()
    projections = _node_projections(nodes, limits)
    dependencies = _dependency_map(graph)
    node_table = []
    for node in nodes:
        row = {
            "arguments": node.arguments,
            "capability": node.capability,
            "depends_on": dependencies[node.id],
            "failure_reason": node.failure_reason,
            "id": node.id,
            "state": node.state,
        }
        projection_key = (
            "error_projection" if node.state == "failed" else "outcome_projection"
        )
        row[projection_key] = projections[node.id]
        node_table.append(row)
    capabilities = sorted(manifest.capabilities, key=lambda capability: capability.name)
    manifest_projection = [
        {
            "argument_schema": capability.schema,
            "description": capability.description,
            "name": capability.name,
        }
        for capability in capabilities
    ]
    missing = missing_read_calls(store, target_id)
    if reschedule_requested and not reschedule_attempted:
        missing.append("reschedule_work_order not yet called")
    user_payload = {
        "evidence_checklist": missing,
        "limits_remaining": {
            "hard_repairs": state.hard_repairs_left,
            "new_nodes_this_patch": limits.max_new_tasks,
            "nodes": max(0, limits.max_nodes - len(graph)),
            "soft_repairs": state.soft_repairs_left,
        },
        "manifest": manifest_projection,
        "nodes": node_table,
        "repair_messages": list(repair_messages),
        "request": request,
        "reschedule_requested": reschedule_requested,
        "target_id": target_id if target_id is not None else "none",
        "today": today.isoformat(),
        "write_authority": (
            "writes are not permitted; reschedule_work_order returns a proposal only"
        ),
    }
    return [
        {"role": "system", "content": SYSTEM_MESSAGE},
        {"role": "user", "content": _compact(user_payload)},
    ]


def _shape_error(condition: bool, message: str, raw: Any) -> None:
    if condition:
        raise PlannerReplyError(message, raw=raw)


def _decode_patch_arguments(raw_arguments: Any) -> dict[str, Any]:
    if isinstance(raw_arguments, str):
        try:
            decoded = json.loads(raw_arguments)
        except json.JSONDecodeError as error:
            raise PlannerReplyError(
                f"plan_frontier arguments are not valid JSON: {error.msg}",
                raw={"arguments": raw_arguments},
            ) from None
    elif isinstance(raw_arguments, dict):
        decoded = copy.deepcopy(raw_arguments)
    else:
        raise PlannerReplyError(
            "plan_frontier arguments must be a JSON string or object",
            raw={"arguments": copy.deepcopy(raw_arguments)},
        )
    _shape_error(not isinstance(decoded, dict), "plan_frontier arguments must decode to an object", decoded)
    return decoded


def parse_reply(response: dict[str, Any]) -> ParsedReply:
    """Require exactly one strictly shaped plan_frontier tool call."""
    message = response.get("message") if isinstance(response, dict) else None
    if not isinstance(message, dict):
        raise PlannerReplyError("planner response has no assistant message object", raw=response)
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        raise PlannerReplyError("planner response has no tool call", raw=message)
    if len(tool_calls) != 1:
        raise PlannerReplyError(
            f"planner response has {len(tool_calls)} tool calls; expected exactly one",
            raw=tool_calls,
        )
    call = tool_calls[0]
    if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
        raise PlannerReplyError("planner tool call has no function object", raw=call)
    function = call["function"]
    name = function.get("name")
    if name != "plan_frontier":
        raise PlannerReplyError(
            f"planner called {name!r}; expected 'plan_frontier'", raw=call
        )
    decoded = _decode_patch_arguments(function.get("arguments"))
    expected = {"add", "finish", "reason"}
    missing = expected - decoded.keys()
    extra = decoded.keys() - expected
    if missing:
        raise PlannerReplyError(
            f"plan_frontier arguments missing key {sorted(missing)[0]!r}", raw=decoded
        )
    if extra:
        raise PlannerReplyError(
            f"plan_frontier arguments have extra key {sorted(extra)[0]!r}", raw=decoded
        )
    additions = decoded["add"]
    _shape_error(not isinstance(additions, list), "plan_frontier 'add' must be an array", decoded)
    _shape_error(type(decoded["finish"]) is not bool, "plan_frontier 'finish' must be a boolean", decoded)
    _shape_error(not isinstance(decoded["reason"], str), "plan_frontier 'reason' must be a string", decoded)
    specs: list[NodeSpec] = []
    item_keys = {"id", "capability", "arguments", "depends_on"}
    for index, item in enumerate(additions):
        _shape_error(not isinstance(item, dict), f"plan_frontier add[{index}] must be an object", decoded)
        item_missing = item_keys - item.keys()
        item_extra = item.keys() - item_keys
        if item_missing:
            raise PlannerReplyError(
                f"plan_frontier add[{index}] missing key {sorted(item_missing)[0]!r}",
                raw=decoded,
            )
        if item_extra:
            raise PlannerReplyError(
                f"plan_frontier add[{index}] has extra key {sorted(item_extra)[0]!r}",
                raw=decoded,
            )
        _shape_error(not isinstance(item["id"], str), f"plan_frontier add[{index}].id must be a string", decoded)
        _shape_error(
            not isinstance(item["capability"], str),
            f"plan_frontier add[{index}].capability must be a string",
            decoded,
        )
        _shape_error(
            not isinstance(item["arguments"], dict),
            f"plan_frontier add[{index}].arguments must be an object",
            decoded,
        )
        depends_on = item["depends_on"]
        _shape_error(
            not isinstance(depends_on, list)
            or any(not isinstance(parent, str) for parent in depends_on),
            f"plan_frontier add[{index}].depends_on must be an array of strings",
            decoded,
        )
        specs.append(
            NodeSpec(
                id=item["id"],
                capability=item["capability"],
                arguments=copy.deepcopy(item["arguments"]),
                depends_on=tuple(depends_on),
            )
        )
    return ParsedReply(
        raw=copy.deepcopy(decoded),
        patch=GraphPatch(
            add=tuple(specs),
            finish=decoded["finish"],
            reason=decoded["reason"],
        ),
    )


def _hard(message: str) -> PatchReview:
    return PatchReview(patch=None, hard_error=message)


def _duplicate_cover(
    spec: NodeSpec,
    nodes: tuple[NodeSnapshot, ...],
    limits: LimitsConfig,
) -> DiscardedAddition | None:
    for existing in nodes:
        if (
            existing.state in _ACTIVE_OR_COMPLETE
            and existing.capability == spec.capability
            and existing.arguments == spec.arguments
        ):
            return DiscardedAddition(
                node_id=spec.id,
                reason=(
                    f"duplicate work is covered by node {existing.id!r} "
                    f"in state {existing.state!r}"
                ),
                covering_node_id=existing.id,
                covering_state=existing.state,
                covering_outcome=_truncated_json(existing.outcome, limits.projection_chars),
            )

    return None


def _same_patch_duplicate_cover(
    spec: NodeSpec, same_patch: list[NodeSpec]
) -> DiscardedAddition | None:
    for earlier in same_patch:
        if (
            earlier.capability == spec.capability
            and earlier.arguments == spec.arguments
        ):
            return DiscardedAddition(
                node_id=spec.id,
                reason=f"duplicates same-patch node {earlier.id!r}",
                covering_node_id=earlier.id,
                covering_state="pending",
                covering_outcome=None,
            )
    return None


def validate_patch(
    patch: GraphPatch,
    *,
    manifest: Manifest,
    graph: LiveGraph,
    store: Store,
    limits: LimitsConfig,
    target_id: str | None,
    reschedule_attempted: bool,
) -> PatchReview:
    """Validate and filter a decoded patch without mutating graph or evidence."""
    nodes = graph.nodes()
    by_id = {node.id: node for node in nodes}
    capabilities = {capability.name: capability for capability in manifest.capabilities}
    proposed_ids = [spec.id for spec in patch.add]
    if any(not node_id for node_id in proposed_ids):
        return _hard("node id must be a non-empty string")
    if len(proposed_ids) != len(set(proposed_ids)):
        return _hard("node ids in a patch must be unique")
    for node_id in proposed_ids:
        if node_id in by_id:
            return _hard(f"node id {node_id!r} already exists")
    if any(node.capability == "answer" for node in nodes) and patch.add:
        return _hard("nothing may be added after an answer node was accepted")
    if any(spec.capability == "answer" for spec in patch.add) and len(patch.add) != 1:
        return _hard("answer must be the only addition in its patch")

    kept: list[NodeSpec] = []
    discarded: list[DiscardedAddition] = []
    duplicate_count = 0
    reschedules_in_patch = 0
    for spec in patch.add:
        capability = capabilities.get(spec.capability)
        if capability is None:
            return _hard(
                f"node {spec.id!r} names capability {spec.capability!r}, "
                "which is not offered in this run's manifest"
            )
        try:
            arguments = validate(capability, spec.arguments)
        except CapabilityArgumentError as error:
            return _hard(f"node {spec.id!r} has invalid arguments: {error}")
        dependencies = () if spec.capability == "answer" else spec.depends_on
        validated = NodeSpec(
            id=spec.id,
            capability=spec.capability,
            arguments=arguments,
            depends_on=dependencies,
        )
        if len(dependencies) != len(set(dependencies)):
            return _hard(f"node {spec.id!r} has duplicate dependencies")
        same_patch = [parent for parent in dependencies if parent in proposed_ids]
        other_missing = [
            parent
            for parent in dependencies
            if parent not in proposed_ids and parent not in by_id
        ]
        if other_missing:
            return _hard(
                f"node {spec.id!r} depends on missing node {other_missing[0]!r}"
            )
        failed = [
            parent
            for parent in dependencies
            if parent in by_id and by_id[parent].state == "failed"
        ]
        if failed:
            return _hard(f"node {spec.id!r} depends on failed node {failed[0]!r}")
        if same_patch:
            discarded.append(
                DiscardedAddition(
                    node_id=spec.id,
                    reason=(
                        f"depends on same-patch node {same_patch[0]!r}; "
                        "propose again next round"
                    ),
                )
            )
            continue

        duplicate = _same_patch_duplicate_cover(validated, kept)
        if duplicate is not None:
            discarded.append(duplicate)
            duplicate_count += 1
            continue

        if spec.capability == "reschedule_work_order":
            reschedules_in_patch += 1
            work_order_id = arguments.get("work_order_id")
            if target_id is None or work_order_id != target_id:
                return _hard(
                    f"node {spec.id!r} may reschedule only target {target_id!r}, "
                    f"not {work_order_id!r}"
                )
            if reschedule_attempted or reschedules_in_patch > 1:
                return _hard("reschedule_work_order may be attempted only once per run")
            model_read_nodes = [
                node
                for node in nodes
                if node.state == "succeeded"
                and node.capability == "WorkOrder.get"
                and node.arguments == {"id": target_id}
            ]
            if not model_read_nodes or not store.got(
                "WorkOrder.get", target_id, model_only=True
            ):
                return _hard(
                    "reschedule_work_order requires a succeeded model WorkOrder.get "
                    "of the target"
                )
        duplicate = _duplicate_cover(validated, nodes, limits)
        if duplicate is not None:
            discarded.append(duplicate)
            duplicate_count += 1
            continue
        if spec.capability == "answer" and graph.has_pending_or_running():
            return _hard("answer may be proposed only when no node is pending or running")
        kept.append(validated)

    if not patch.add and not graph.has_pending_or_running():
        return _hard("an empty patch is invalid when no node is pending or running")
    if (
        patch.add
        and not kept
        and not graph.has_pending_or_running()
        and duplicate_count != len(patch.add)
    ):
        details = "; ".join(
            f"{item.node_id!r}: {item.reason}" for item in discarded
        )
        return PatchReview(
            patch=None,
            discarded=tuple(discarded),
            hard_error=f"all additions were discarded; {details}",
        )
    if len(kept) > limits.max_new_tasks:
        return _hard(
            f"patch keeps {len(kept)} additions, exceeding max_new_tasks={limits.max_new_tasks}"
        )
    if len(graph) + len(kept) > limits.max_nodes:
        return _hard(
            f"patch would create {len(graph) + len(kept)} nodes, exceeding max_nodes={limits.max_nodes}"
        )
    return PatchReview(
        patch=GraphPatch(add=tuple(kept), finish=patch.finish, reason=patch.reason),
        discarded=tuple(discarded),
        duplicates_only=bool(patch.add) and duplicate_count == len(patch.add),
    )


def critic(
    patch: GraphPatch,
    *,
    store: Store,
    target_id: str | None,
    reschedule_requested: bool,
    reschedule_attempted: bool,
) -> CriticResult:
    """Check contradictions and evidence readiness for a patch containing answer."""
    answer_nodes = [spec for spec in patch.add if spec.capability == "answer"]
    if not answer_nodes:
        return CriticResult()
    arguments = answer_nodes[0].arguments
    outcome = arguments["outcome"]
    refusal_reason = arguments["refusal_reason"]
    target_read = target_id is not None and store.got(
        "WorkOrder.get", target_id, model_only=True
    )
    if outcome == "answered" and target_id is None:
        return CriticResult(hard_error="answered requires a supplied target id")
    if outcome == "answered" and not target_read:
        return CriticResult(
            hard_error="answered requires a succeeded model WorkOrder.get of the target"
        )
    if outcome == "answered" and refusal_reason is not None:
        return CriticResult(hard_error="refusal_reason must be null for answered")
    if outcome == "refused" and refusal_reason not in _REFUSAL_REASONS:
        return CriticResult(
            hard_error="refusal_reason must be permitted for refused"
        )
    if outcome == "refused" and refusal_reason == "not_found" and target_read:
        return CriticResult(
            hard_error="refused with not_found contradicts the succeeded model target read"
        )
    if outcome == "refused":
        return CriticResult()
    missing = missing_read_calls(store, target_id)
    if reschedule_requested and not reschedule_attempted:
        missing.append("reschedule_work_order not yet called")
    return CriticResult(missing=tuple(missing))


def _reject(
    *,
    raw: Any,
    kind: RepairKind,
    message: str,
    state: PlannerState,
    discarded: tuple[DiscardedAddition, ...] = (),
) -> PlannerDecision:
    remaining = (
        state.hard_repairs_left if kind == "hard" else state.soft_repairs_left
    )
    if remaining == 0:
        raise PlannerError(message, kind=kind, raw=raw, state=state)
    attempt = RejectedAttempt(copy.deepcopy(raw), kind, message)
    updated = replace(
        state,
        hard_repairs_left=state.hard_repairs_left - (kind == "hard"),
        soft_repairs_left=state.soft_repairs_left - (kind == "soft"),
        rejected_attempts=(*state.rejected_attempts, attempt),
    )
    return PlannerDecision(
        patch=None,
        state=updated,
        discarded=discarded,
        repair_kind=kind,
        message=message,
    )


def consider_reply(
    parsed: ParsedReply,
    *,
    manifest: Manifest,
    graph: LiveGraph,
    store: Store,
    limits: LimitsConfig,
    state: PlannerState,
    target_id: str | None,
    reschedule_requested: bool,
    reschedule_attempted: bool,
) -> PlannerDecision:
    """Apply validation, critic, and repair budgets to one parsed reply."""
    review = validate_patch(
        parsed.patch,
        manifest=manifest,
        graph=graph,
        store=store,
        limits=limits,
        target_id=target_id,
        reschedule_attempted=reschedule_attempted,
    )
    if review.hard_error is not None:
        return _reject(
            raw=parsed.raw,
            kind="hard",
            message=review.hard_error,
            state=state,
            discarded=review.discarded,
        )
    assert review.patch is not None
    if review.duplicates_only:
        covers = "; ".join(item.reason for item in review.discarded)
        return _reject(
            raw=parsed.raw,
            kind="soft",
            message=f"duplicates-only patch; {covers}",
            state=state,
            discarded=review.discarded,
        )
    criticism = critic(
        review.patch,
        store=store,
        target_id=target_id,
        reschedule_requested=reschedule_requested,
        reschedule_attempted=reschedule_attempted,
    )
    if criticism.hard_error is not None:
        return _reject(
            raw=parsed.raw,
            kind="hard",
            message=criticism.hard_error,
            state=state,
            discarded=review.discarded,
        )
    if criticism.missing and state.soft_repairs_left > 0:
        return _reject(
            raw=parsed.raw,
            kind="soft",
            message=f"not ready; missing={list(criticism.missing)!r}",
            state=state,
            discarded=review.discarded,
        )
    return PlannerDecision(
        patch=review.patch,
        state=state,
        discarded=review.discarded,
        missing=criticism.missing,
    )


def plan_frontier(
    client: Any,
    *,
    request: str,
    target_id: str | None,
    today: date,
    manifest: Manifest,
    graph: LiveGraph,
    store: Store,
    limits: LimitsConfig,
    state: PlannerState,
    reschedule_requested: bool,
    reschedule_attempted: bool,
    repair_messages: tuple[str, ...] = (),
) -> PlannerDecision:
    """Call the metered planner once and return an accepted patch or repair."""
    messages = build_messages(
        request=request,
        target_id=target_id,
        today=today,
        manifest=manifest,
        graph=graph,
        store=store,
        limits=limits,
        state=state,
        reschedule_requested=reschedule_requested,
        reschedule_attempted=reschedule_attempted,
        repair_messages=repair_messages,
    )
    response = client.chat(
        messages,
        tools=[PLAN_FRONTIER],
        tool_choice=PLAN_FRONTIER_CHOICE,
    )
    try:
        parsed = parse_reply(response)
    except PlannerReplyError as error:
        return _reject(
            raw=error.raw,
            kind="hard",
            message=str(error),
            state=state,
        )
    return consider_reply(
        parsed,
        manifest=manifest,
        graph=graph,
        store=store,
        limits=limits,
        state=state,
        target_id=target_id,
        reschedule_requested=reschedule_requested,
        reschedule_attempted=reschedule_attempted,
    )


__all__ = [
    "PLAN_FRONTIER",
    "PLAN_FRONTIER_CHOICE",
    "CriticResult",
    "DiscardedAddition",
    "ParsedReply",
    "PatchReview",
    "PlannerDecision",
    "PlannerError",
    "PlannerReplyError",
    "PlannerState",
    "RejectedAttempt",
    "build_messages",
    "consider_reply",
    "critic",
    "missing_read_calls",
    "parse_reply",
    "plan_frontier",
    "validate_patch",
]
