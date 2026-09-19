"""Threaded graph executor and graph-agent entry point."""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from . import planner, reads
from .answer import Store, build_raw, coverage
from .capabilities import Capability, Manifest, build_manifest
from .config import Config
from .graph import Journal, LiveGraph, NodeSnapshot


class GraphAgentError(Exception):
    """A failed graph run carrying its complete auditable partial state."""

    def __init__(self, error: Exception, agent: dict[str, Any]) -> None:
        self.original_type = type(error).__name__
        self.original_error = error
        self.agent = agent
        super().__init__(f"{self.original_type}: {error}")


@dataclass(frozen=True)
class _WorkerResult:
    succeeded: bool
    outcome: dict[str, Any] | None = None
    failure_reason: str | None = None
    error_detail: Any | None = None
    raw: dict[str, Any] | None = None
    reschedule: dict[str, Any] | None = None


class _ObservedClient:
    """Expose the metered client API while retaining the latest response."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.model = getattr(client, "model", None)
        self.response: dict[str, Any] | None = None

    def reset(self) -> None:
        self.response = None

    def chat(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        response = self._client.chat(*args, **kwargs)
        self.response = copy.deepcopy(response)
        return response

    def ledger(self) -> dict[str, Any]:
        return self._client.ledger()


def _usage_summary(calls: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, int | float] = {}
    for call in calls:
        usage = call.get("usage")
        if not isinstance(usage, dict):
            continue
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value
    return {"calls": copy.deepcopy(calls), "totals": totals}


def _manifest_record(manifest: Manifest | None) -> dict[str, Any]:
    return {
        "offered": (
            []
            if manifest is None
            else [capability.name for capability in manifest.capabilities]
        ),
        "dropped": [] if manifest is None else copy.deepcopy(manifest.dropped),
    }


def _patch_record(
    journal: Journal, state: planner.PlannerState
) -> dict[str, list[dict[str, Any]]]:
    accepted = [
        {
            "round": event["round"],
            **copy.deepcopy(event["data"]),
        }
        for event in journal.events
        if event["type"] == "graph_patched"
        and event["data"].get("kind") == "patch"
    ]
    rejected = [asdict(attempt) for attempt in state.rejected_attempts]
    return {"accepted": accepted, "rejected": rejected}


def _meter_references(entries: list[dict[str, Any]]) -> list[dict[str, int]]:
    references = []
    for entry in entries:
        round_number = entry.get("round")
        attempt = entry.get("attempt")
        if isinstance(round_number, int) and isinstance(attempt, int):
            references.append({"round": round_number, "attempt": attempt})
    return references


def _discard_feedback(item: planner.DiscardedAddition) -> str:
    message = f"discarded addition {item.node_id!r}: {item.reason}"
    if item.covering_node_id is not None:
        message += (
            f"; covering node {item.covering_node_id!r}"
            f" is in state {item.covering_state!r}"
        )
        if item.covering_outcome is not None:
            message += f" with outcome projection {item.covering_outcome}"
    return message


def eligible_ready_node(graph: LiveGraph, manifest: Manifest) -> NodeSnapshot | None:
    """Return the next ready node allowed by terminal and exclusive gates."""
    capabilities = {
        capability.name: capability for capability in manifest.capabilities
    }
    all_nodes = graph.nodes()
    running = tuple(node for node in all_nodes if node.state == "running")
    if any(
        "exclusive" in capabilities[node.capability].families for node in running
    ):
        return None
    for node in graph.ready_nodes():
        capability = capabilities[node.capability]
        if "exclusive" in capability.families and running:
            continue
        if "terminal" in capability.families and any(
            other.id != node.id
            and other.state in {"pending", "running"}
            for other in all_nodes
        ):
            continue
        return node
    return None


def run_graph_agent(
    tools: Any,
    llm: Any,
    *,
    request: str,
    target_id: str | None,
    today: date,
    own_user_id: str | None,
    reschedule_requested: bool,
    config: Config,
    authority: dict[str, Any],
) -> dict[str, Any]:
    """Run the bounded frontier planner and execute its validated live graph."""
    if authority.get("write"):
        raise NotImplementedError("write authority arrives in phase 6")

    journal = Journal()
    graph = LiveGraph(journal)
    store = Store()
    state = planner.PlannerState.from_limits(config.limits)
    observed_llm = _ObservedClient(llm)
    manifest: Manifest | None = None
    pool: ThreadPoolExecutor | None = None
    usage_calls: list[dict[str, Any]] = []
    repairs: list[str] = []
    repair_messages: tuple[str, ...] = ()
    discarded_since_patch: list[dict[str, Any]] = []
    discarded_count = 0
    rejection_cursor = 0
    ledger_cursor = 0
    rounds = 0
    valid_replies = 0
    reschedule_attempted = False
    reschedule_invoked = False
    reschedule_result: dict[str, Any] | None = None
    final_outcome: str | None = None
    final_refusal_reason: str | None = None
    final_prose: str | None = None
    final_raw: dict[str, Any] | None = None
    missing: tuple[str, ...] = ()
    last_model = getattr(llm, "model", None)

    def planner_record() -> dict[str, int]:
        return {
            "rounds": rounds,
            "valid_replies": valid_replies,
            "hard_repairs_used": config.limits.hard_repairs
            - state.hard_repairs_left,
            "soft_repairs_used": config.limits.soft_repairs
            - state.soft_repairs_left,
            "discarded_additions": discarded_count,
        }

    def result_record() -> dict[str, Any]:
        result: dict[str, Any] = {
            "outcome": final_outcome,
            "refusal_reason": final_refusal_reason,
            "prose": final_prose,
            "reschedule": copy.deepcopy(reschedule_result),
            "model": last_model,
            "usage": _usage_summary(usage_calls),
            "turns": rounds,
            "repairs": list(repairs),
            "coverage": coverage(
                store, target_id, rescheduled=reschedule_invoked
            ),
            "manifest": _manifest_record(manifest),
            "journal": list(journal.events),
            "graph": graph.export(),
            "patches": _patch_record(journal, state),
            "authority": copy.deepcopy(authority),
            "missing": list(missing),
            "planner": planner_record(),
        }
        if final_outcome == "answered" and final_raw is not None:
            result["raw"] = copy.deepcopy(final_raw)
        return result

    def capability_for(node: NodeSnapshot) -> Capability:
        assert manifest is not None
        for capability in manifest.capabilities:
            if capability.name == node.capability:
                return capability
        raise RuntimeError(f"node {node.id!r} has no offered capability")

    def worker(node: NodeSnapshot, capability: Capability) -> _WorkerResult:
        if capability.mcp_tool is not None:
            try:
                outcome = reads.call_read(
                    tools,
                    store,
                    capability.mcp_tool,
                    node.arguments,
                    page_size=config.limits.page_size,
                    character_limit=config.limits.projection_chars,
                )
            except Exception as error:
                return _WorkerResult(
                    False,
                    failure_reason="error",
                    error_detail=reads.error_result(error),
                )
            if "list" in capability.families and outcome.get("complete") is False:
                return _WorkerResult(
                    False,
                    failure_reason="incomplete_scan",
                    error_detail=copy.deepcopy(outcome),
                )
            return _WorkerResult(True, outcome=outcome)

        if node.capability == "reschedule_work_order":
            journal.append(
                "action_started",
                round=rounds,
                node=node.id,
                data={"action": "reschedule_work_order", "target_id": target_id},
            )
            try:
                assert target_id is not None
                full_result = reads.guarded_reschedule(
                    tools,
                    store,
                    target_id,
                    today=today,
                    own_user_id=own_user_id,
                    allow_write=False,
                )
            except Exception as error:
                detail = reads.error_result(error)
                journal.append(
                    "action_finished",
                    round=rounds,
                    node=node.id,
                    data={"action": "reschedule_work_order", "result": detail},
                )
                return _WorkerResult(
                    False, failure_reason="error", error_detail=detail
                )
            outcome = {
                key: copy.deepcopy(full_result.get(key))
                for key in ("action", "reason", "proposed", "applied", "notes")
            }
            journal.append(
                "action_finished",
                round=rounds,
                node=node.id,
                data={"action": "reschedule_work_order", "result": outcome},
            )
            return _WorkerResult(
                True, outcome=outcome, reschedule=full_result
            )

        if node.capability == "answer":
            outcome = node.arguments["outcome"]
            refusal_reason = node.arguments["refusal_reason"]
            prose = node.arguments["prose"]
            raw = None
            if outcome == "answered":
                if target_id is None:
                    raise RuntimeError("answered terminal node has no target id")
                raw = build_raw(store, target_id, today=today)
            return _WorkerResult(
                True,
                outcome={
                    "outcome": outcome,
                    "refusal_reason": refusal_reason,
                    "summary": prose[:500],
                },
                raw=raw,
            )

        raise RuntimeError(f"node {node.id!r} has no worker")

    def latest_target_read() -> NodeSnapshot | None:
        candidates = [
            node
            for node in graph.nodes()
            if node.state == "succeeded"
            and node.capability == "WorkOrder.get"
            and node.arguments == {"id": target_id}
        ]
        if not candidates or target_id is None or not store.got(
            "WorkOrder.get", target_id, model_only=True
        ):
            return None
        return max(candidates, key=lambda node: (node.frontier, node.id))

    def execute_one() -> bool:
        nonlocal final_outcome, final_prose, final_raw, final_refusal_reason
        nonlocal reschedule_attempted, reschedule_invoked, reschedule_result
        assert manifest is not None
        node = eligible_ready_node(graph, manifest)
        if node is None:
            if graph.has_pending_or_running():
                raise RuntimeError("pending graph nodes cannot make progress")
            return False
        capability = capability_for(node)
        if node.capability == "reschedule_work_order":
            target_read = latest_target_read()
            if target_read is None:
                graph.start(node.id, round=rounds)
                graph.fail(
                    node.id,
                    "target_read_required",
                    {"target_id": target_id},
                    round=rounds,
                )
                return False
            if target_read.id not in graph.ancestors(node.id):
                graph.add_dependency(target_read.id, node.id, round=rounds)
            reschedule_attempted = True
            reschedule_invoked = True

        graph.start(node.id, round=rounds)
        assert pool is not None
        completed = pool.submit(worker, node, capability).result()
        if completed.succeeded:
            assert completed.outcome is not None
            graph.succeed(node.id, completed.outcome, round=rounds)
            if completed.reschedule is not None:
                reschedule_result = copy.deepcopy(completed.reschedule)
            if node.capability == "answer":
                final_outcome = node.arguments["outcome"]
                final_refusal_reason = node.arguments["refusal_reason"]
                final_prose = node.arguments["prose"]
                final_raw = copy.deepcopy(completed.raw)
                return True
        else:
            assert completed.failure_reason is not None
            graph.fail(
                node.id,
                completed.failure_reason,
                completed.error_detail,
                round=rounds,
            )
        return False

    try:
        manifest = build_manifest(tools.list_tools())
        journal.append(
            "run_started",
            data={
                "target_id": target_id,
                "today": today.isoformat(),
                "authority": copy.deepcopy(authority),
                "limits": asdict(config.limits),
            },
        )
        pool = ThreadPoolExecutor(max_workers=config.limits.max_workers)

        while final_outcome is None:
            rounds += 1
            observed_llm.reset()
            decision: planner.PlannerDecision
            try:
                decision = planner.plan_frontier(
                    observed_llm,
                    request=request,
                    target_id=target_id,
                    today=today,
                    manifest=manifest,
                    graph=graph,
                    store=store,
                    limits=config.limits,
                    state=state,
                    reschedule_requested=reschedule_requested,
                    reschedule_attempted=reschedule_attempted,
                    repair_messages=repair_messages,
                )
            finally:
                response = observed_llm.response
                usage_calls.append(
                    {
                        "round": rounds,
                        "usage": (
                            copy.deepcopy(response.get("usage", {}))
                            if isinstance(response, dict)
                            else {}
                        ),
                        "provider": (
                            response.get("provider")
                            if isinstance(response, dict)
                            else None
                        ),
                        "model": (
                            response.get("model")
                            if isinstance(response, dict)
                            else getattr(llm, "model", None)
                        ),
                        "finish_reason": (
                            response.get("finish_reason")
                            if isinstance(response, dict)
                            else None
                        ),
                    }
                )
                if isinstance(response, dict):
                    last_model = response.get("model") or last_model
                    try:
                        planner.parse_reply(response)
                    except planner.PlannerReplyError:
                        pass
                    else:
                        valid_replies += 1

            state = decision.state
            current_discarded = [asdict(item) for item in decision.discarded]
            discarded_since_patch.extend(current_discarded)
            discarded_count += len(current_discarded)
            if not decision.accepted:
                assert decision.message is not None
                repairs.append(decision.message)
                repair_messages = (decision.message,)
                continue

            assert decision.patch is not None
            missing = decision.missing
            ledger_entries = llm.ledger().get("entries", [])
            if not isinstance(ledger_entries, list):
                ledger_entries = []
            rejected = [
                asdict(item)
                for item in state.rejected_attempts[rejection_cursor:]
            ]
            rejected_hard = sum(item["kind"] == "hard" for item in rejected)
            rejected_soft = sum(item["kind"] == "soft" for item in rejected)
            frontier = graph.apply_patch(
                decision.patch,
                round=rounds,
                journal_data={
                    "discarded": copy.deepcopy(discarded_since_patch),
                    "rejected": rejected,
                    "repairs": {
                        "hard_used": rejected_hard,
                        "soft_used": rejected_soft,
                        "hard_remaining": state.hard_repairs_left,
                        "soft_remaining": state.soft_repairs_left,
                    },
                    "meter": _meter_references(ledger_entries[ledger_cursor:]),
                },
            )
            rejection_cursor = len(state.rejected_attempts)
            ledger_cursor = len(ledger_entries)
            discarded_since_patch.clear()
            repair_messages = tuple(
                _discard_feedback(item) for item in decision.discarded
            )

            if config.limits.replan == "node":
                execute_one()
            else:
                while not graph.frontier_settled(frontier):
                    if execute_one():
                        break

        journal.append(
            "run_finished",
            round=rounds,
            data={"outcome": final_outcome},
        )
        return result_record()
    except Exception as error:
        if isinstance(error, planner.PlannerError):
            state = error.state
            repairs.append(str(error))
        journal.append(
            "run_failed",
            round=rounds or None,
            data={"error_type": type(error).__name__, "message": str(error)},
        )
        raise GraphAgentError(error, result_record()) from error
    finally:
        if pool is not None:
            pool.shutdown(wait=True)


__all__ = ["GraphAgentError", "run_graph_agent"]
