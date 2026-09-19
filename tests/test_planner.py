"""Offline tests for frontier planning, validation, repairs, and criticism."""

from __future__ import annotations

import copy
import json
from datetime import date
from typing import Any

import pytest

from agentswitch.answer import Store
from agentswitch.capabilities import Capability, Manifest
from agentswitch.config import (
    BudgetConfig,
    Config,
    LimitsConfig,
    ModelConfig,
    PricingConfig,
    PricingRate,
)
from agentswitch.economics import MeteredClient
from agentswitch.graph import GraphPatch, LiveGraph, NodeSpec
from agentswitch.offline import offline_llm_client, scripted_tool_response
from agentswitch.planner import (
    PLAN_FRONTIER,
    PLAN_FRONTIER_CHOICE,
    PlannerError,
    PlannerState,
    build_messages,
    consider_reply,
    parse_reply,
    plan_frontier,
)

TODAY = date(2026, 9, 19)
TARGET = "WO-1"


def _schema(
    properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _capability(
    name: str,
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
    families: frozenset[str] = frozenset({"read"}),
) -> Capability:
    return Capability(
        name=name,
        native_name=name.replace(".", "__"),
        families=families,
        description=f"Use {name}.",
        schema=_schema(properties, required),
        mcp_tool=name if "read" in families else None,
    )


def _manifest() -> Manifest:
    capabilities = (
        _capability("WorkOrder.get", {"id": {"type": "string"}}, required=["id"]),
        _capability(
            "WorkOrder.list",
            {"bom_id": {"type": "string"}},
            families=frozenset({"read", "list"}),
        ),
        _capability(
            "MaterialRequest.list",
            {"work_order_id": {"type": "string"}},
            families=frozenset({"read", "list"}),
        ),
        _capability(
            "reschedule_work_order",
            {"work_order_id": {"type": "string"}},
            required=["work_order_id"],
            families=frozenset({"side_effect", "exclusive"}),
        ),
        Capability(
            name="answer",
            native_name="finish",
            families=frozenset({"terminal"}),
            description="Finish.",
            schema=_schema(
                {
                    "outcome": {
                        "type": "string",
                        "enum": ["answered", "refused"],
                    },
                    "refusal_reason": {
                        "anyOf": [
                            {
                                "type": "string",
                                "enum": [
                                    "not_found",
                                    "outside_seat",
                                    "source_unavailable",
                                    "unsupported",
                                ],
                            },
                            {"type": "null"},
                        ]
                    },
                    "prose": {"type": "string"},
                },
                ["outcome", "refusal_reason", "prose"],
            ),
            mcp_tool=None,
        ),
    )
    return Manifest(capabilities=capabilities, catalogue_names=[], dropped=[])


def _limits(**changes: Any) -> LimitsConfig:
    values = {
        "max_workers": 1,
        "replan": "frontier",
        "max_new_tasks": 8,
        "max_nodes": 16,
        "hard_repairs": 2,
        "soft_repairs": 1,
        "page_size": 100,
        "projection_chars": 200,
        "projection_total_chars": 1000,
    }
    values.update(changes)
    return LimitsConfig(**values)


def _config(limits: LimitsConfig | None = None) -> Config:
    selected_limits = limits or _limits()
    return Config(
        path="offline",
        models=ModelConfig(
            agent="offline/model",
            reasoning_effort="medium",
            seed=17,
            max_tokens=100,
            timeout_seconds=1,
        ),
        pricing=PricingConfig(
            default=PricingRate(0, 0),
            models={"offline/model": PricingRate(0, 0)},
        ),
        budgets=BudgetConfig(
            run_usd=1,
            max_attempts_per_round=1,
            max_attempts_per_run=20,
            admission_safety_factor=1,
        ),
        limits=selected_limits,
        values={},
        overrides=(),
        sha256="offline",
    )


def _patch(
    additions: list[dict[str, Any]], *, finish: bool = False, reason: str = "test"
) -> dict[str, Any]:
    return {"add": additions, "finish": finish, "reason": reason}


def _addition(
    node_id: str,
    capability: str,
    arguments: dict[str, Any],
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "capability": capability,
        "arguments": arguments,
        "depends_on": depends_on or [],
    }


def _answer(
    *, outcome: str = "answered", refusal_reason: str | None = None
) -> dict[str, Any]:
    return _addition(
        "answer",
        "answer",
        {"outcome": outcome, "refusal_reason": refusal_reason, "prose": "Done."},
    )


def _response(arguments: dict[str, Any]) -> dict[str, Any]:
    return scripted_tool_response("plan_frontier", arguments)


def _client(response: dict[str, Any], limits: LimitsConfig | None = None):
    config = _config(limits)
    raw, transport = offline_llm_client(config, [(200, response)])
    return MeteredClient(raw, config, sleep=lambda _: None), transport


def _planner_call(
    response: dict[str, Any],
    *,
    graph: LiveGraph | None = None,
    store: Store | None = None,
    state: PlannerState | None = None,
    limits: LimitsConfig | None = None,
    reschedule_requested: bool = False,
    reschedule_attempted: bool = False,
):
    selected_limits = limits or _limits()
    client, transport = _client(response, selected_limits)
    decision = plan_frontier(
        client,
        request="Investigate this work order.",
        target_id=TARGET,
        today=TODAY,
        manifest=_manifest(),
        graph=graph or LiveGraph(),
        store=store or Store(),
        limits=selected_limits,
        state=state or PlannerState.from_limits(selected_limits),
        reschedule_requested=reschedule_requested,
        reschedule_attempted=reschedule_attempted,
    )
    return decision, transport


def _succeeded_target(*, model_read: bool = True) -> tuple[LiveGraph, Store]:
    graph = LiveGraph()
    graph.apply_patch(
        GraphPatch(
            add=(NodeSpec("target", "WorkOrder.get", {"id": TARGET}),),
            finish=False,
            reason="target",
        )
    )
    graph.start("target")
    graph.succeed("target", {"ok": True, "record": {"id": TARGET}})
    store = Store()
    store.add_get(
        "WorkOrder.get", {"id": TARGET}, {"id": TARGET}, model_read=model_read
    )
    return graph, store


def _failed_target(detail: dict[str, Any]) -> LiveGraph:
    graph = LiveGraph()
    graph.apply_patch(
        GraphPatch(
            add=(NodeSpec("target", "WorkOrder.get", {"id": TARGET}),),
            finish=False,
            reason="target",
        )
    )
    graph.start("target")
    graph.fail("target", "error", detail)
    return graph


@pytest.mark.parametrize(
    ("addition", "expected"),
    [
        (_addition("bad", "Unknown.get", {}), "not offered"),
        (
            _addition("bad", "WorkOrder.get", {"id": TARGET, "extra": True}),
            "Unknown argument",
        ),
        (_addition("bad", "WorkOrder.get", {}), "Missing required"),
        (
            _addition(
                "bad", "MaterialRequest.list", {"work_order_id": ":placeholder"}
            ),
            "placeholder",
        ),
    ],
)
def test_invalid_capability_arguments_are_hard_repairs(addition, expected):
    """Spec: AI (Codex) Unknown capabilities and invalid argument forms name the hard error."""
    decision, transport = _planner_call(_response(_patch([addition])))

    assert decision.repair_kind == "hard"
    assert expected in decision.message
    assert decision.state.hard_repairs_left == 1
    assert decision.state.rejected_attempts[-1].raw["add"][0]["id"] == "bad"
    body = transport.calls[0]["body"]
    assert body["tool_choice"] == PLAN_FRONTIER_CHOICE
    assert body["tools"] == [PLAN_FRONTIER]


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
                "model": "offline/model",
            },
            "no tool call",
        ),
        (
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "plan_frontier",
                                        "arguments": json.dumps(_patch([])),
                                    }
                                },
                                {
                                    "function": {
                                        "name": "plan_frontier",
                                        "arguments": json.dumps(_patch([])),
                                    }
                                },
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "model": "offline/model",
            },
            "2 tool calls",
        ),
        (scripted_tool_response("other", _patch([])), "expected 'plan_frontier'"),
        (
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "plan_frontier",
                                        "arguments": "{broken",
                                    }
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "model": "offline/model",
            },
            "not valid JSON",
        ),
    ],
)
def test_malformed_model_replies_are_hard_repairs(response, expected):
    """Spec: AI (Codex) Missing, multiple, misnamed, and invalid-JSON tool calls are hard repairs."""
    decision, _transport = _planner_call(response)

    assert decision.repair_kind == "hard"
    assert expected in decision.message


def test_hard_repair_exhaustion_raises_planner_error():
    """Spec: AI (Codex) A hard finding with no hard repairs left fails visibly with its record."""
    state = PlannerState(hard_repairs_left=0, soft_repairs_left=1)

    with pytest.raises(PlannerError, match="not offered") as caught:
        _planner_call(
            _response(_patch([_addition("bad", "Unknown.get", {})])),
            state=state,
        )

    assert caught.value.kind == "hard"
    assert len(caught.value.state.rejected_attempts) == 1


def test_answer_missing_read_soft_repairs_then_is_accepted_with_unknowns():
    """Spec: AI (Codex) Missing exact reads consume soft repair, then become accepted unknowns."""
    graph, store = _succeeded_target()
    parsed = parse_reply(_decoded_response(_patch([_answer()])))
    limits = _limits()
    initial = PlannerState.from_limits(limits)

    repaired = consider_reply(
        parsed,
        manifest=_manifest(),
        graph=graph,
        store=store,
        limits=limits,
        state=initial,
        target_id=TARGET,
        reschedule_requested=False,
        reschedule_attempted=False,
    )
    accepted = consider_reply(
        parsed,
        manifest=_manifest(),
        graph=graph,
        store=store,
        limits=limits,
        state=repaired.state,
        target_id=TARGET,
        reschedule_requested=False,
        reschedule_attempted=False,
    )

    exact = f'MaterialRequest.list {{"work_order_id":"{TARGET}"}}'
    assert repaired.repair_kind == "soft"
    assert "not ready; missing=" in repaired.message
    assert exact in repaired.message
    assert accepted.accepted is True
    assert exact in accepted.missing


def _decoded_response(arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "message": {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "plan_frontier",
                        "arguments": copy.deepcopy(arguments),
                    }
                }
            ],
        }
    }


def test_duplicate_read_is_discarded_and_covering_node_is_reported():
    """Spec: AI (Codex) A duplicates-only read patch names its covering node and costs soft repair."""
    graph, store = _succeeded_target()
    reply = _response(
        _patch([_addition("again", "WorkOrder.get", {"id": TARGET})])
    )

    decision, _transport = _planner_call(reply, graph=graph, store=store)

    assert decision.repair_kind == "soft"
    assert "duplicates-only" in decision.message
    assert "target" in decision.message
    assert decision.discarded[0].covering_node_id == "target"
    assert decision.discarded[0].covering_state == "succeeded"
    assert "record" in decision.discarded[0].covering_outcome


def test_later_same_patch_duplicate_is_discarded_with_pending_cover():
    """Spec: AI (Codex). The first identical read in one patch covers later copies."""
    reply = _response(
        _patch(
            [
                _addition("first", "WorkOrder.get", {"id": TARGET}),
                _addition("later", "WorkOrder.get", {"id": TARGET}),
            ]
        )
    )

    decision, _transport = _planner_call(reply)

    assert decision.accepted is True
    assert [node.id for node in decision.patch.add] == ["first"]
    assert len(decision.discarded) == 1
    discarded = decision.discarded[0]
    assert discarded.node_id == "later"
    assert discarded.covering_node_id == "first"
    assert discarded.covering_state == "pending"
    assert discarded.covering_outcome is None
    assert "duplicates same-patch node 'first'" in discarded.reason
    assert decision.state == PlannerState.from_limits(_limits())


def test_duplicates_only_with_no_soft_repairs_fails_visibly():
    """Spec: AI (Codex) A duplicates-only patch cannot spin after soft repairs are exhausted."""
    graph, store = _succeeded_target()
    state = PlannerState(hard_repairs_left=2, soft_repairs_left=0)

    with pytest.raises(PlannerError, match="duplicates-only") as caught:
        _planner_call(
            _response(
                _patch([_addition("again", "WorkOrder.get", {"id": TARGET})])
            ),
            graph=graph,
            store=store,
            state=state,
        )

    assert caught.value.kind == "soft"


def test_answer_mixed_with_read_is_hard_repair():
    """Spec: AI (Codex) Answer cannot share its patch with a read."""
    graph, store = _succeeded_target()
    reply = _response(
        _patch(
            [
                _addition("read", "WorkOrder.get", {"id": "WO-2"}),
                _answer(),
            ],
            finish=True,
        )
    )

    decision, _transport = _planner_call(reply, graph=graph, store=store)

    assert decision.repair_kind == "hard"
    assert "answer must be the only addition" in decision.message


def test_answer_while_node_is_pending_is_hard_repair():
    """Spec: AI (Codex) Answer is rejected while any graph node remains pending."""
    graph = LiveGraph()
    graph.apply_patch(
        GraphPatch(
            add=(NodeSpec("pending", "WorkOrder.get", {"id": TARGET}),),
            finish=False,
            reason="pending",
        )
    )

    decision, _transport = _planner_call(_response(_patch([_answer()])), graph=graph)

    assert decision.repair_kind == "hard"
    assert "no node is pending or running" in decision.message


def test_answer_dependency_on_failed_node_is_dropped_without_repair():
    """Spec: AI (Codex) A failed dependency is removed from an accepted answer node."""
    graph = _failed_target({"error": {"type": "not_found"}})
    answer = _answer(outcome="refused", refusal_reason="not_found")
    answer["depends_on"] = ["target"]

    decision, _transport = _planner_call(_response(_patch([answer])), graph=graph)

    assert decision.accepted is True
    assert decision.patch.add[0].depends_on == ()
    assert decision.state == PlannerState.from_limits(_limits())


def test_answer_dependency_on_missing_node_is_dropped_without_repair():
    """Spec: AI (Codex) A missing dependency is removed from an accepted answer node."""
    answer = _answer(outcome="refused", refusal_reason="not_found")
    answer["depends_on"] = ["missing"]

    decision, _transport = _planner_call(_response(_patch([answer])))

    assert decision.accepted is True
    assert decision.patch.add[0].depends_on == ()
    assert decision.state == PlannerState.from_limits(_limits())


def test_same_patch_dependency_is_discarded_for_next_round():
    """Spec: AI (Codex) A same-patch child is discarded without spending a repair."""
    reply = _response(
        _patch(
            [
                _addition("parent", "WorkOrder.get", {"id": TARGET}),
                _addition(
                    "child",
                    "WorkOrder.list",
                    {"bom_id": "BOM-1"},
                    depends_on=["parent"],
                ),
            ]
        )
    )

    decision, _transport = _planner_call(reply)

    assert decision.accepted is True
    assert [node.id for node in decision.patch.add] == ["parent"]
    assert decision.state == PlannerState.from_limits(_limits())
    assert decision.discarded[0].node_id == "child"
    assert "propose again next round" in decision.discarded[0].reason


def test_duplicate_parent_and_same_patch_child_is_hard_repair():
    """Spec: AI (Codex) A duplicate parent cannot leave its same-patch child as an accepted empty patch."""
    graph, store = _succeeded_target()
    reply = _response(
        _patch(
            [
                _addition("again", "WorkOrder.get", {"id": TARGET}),
                _addition(
                    "child",
                    "WorkOrder.list",
                    {"bom_id": "BOM-1"},
                    depends_on=["again"],
                ),
            ]
        )
    )

    decision, _transport = _planner_call(reply, graph=graph, store=store)

    assert decision.repair_kind == "hard"
    assert decision.state.hard_repairs_left == 1
    assert "all additions were discarded" in decision.message
    assert "duplicate work is covered" in decision.message
    assert "depends on same-patch node" in decision.message


def test_all_same_patch_dependents_are_hard_repair():
    """Spec: AI (Codex) A patch containing only same-patch dependents consumes hard repair."""
    reply = _response(
        _patch(
            [
                _addition(
                    "first",
                    "WorkOrder.get",
                    {"id": TARGET},
                    depends_on=["second"],
                ),
                _addition(
                    "second",
                    "WorkOrder.get",
                    {"id": "WO-2"},
                    depends_on=["first"],
                ),
            ]
        )
    )

    decision, _transport = _planner_call(reply)

    assert decision.repair_kind == "hard"
    assert decision.state.hard_repairs_left == 1
    assert "all additions were discarded" in decision.message
    assert "'first': depends on same-patch node 'second'" in decision.message
    assert "'second': depends on same-patch node 'first'" in decision.message


def test_filtered_empty_patch_is_accepted_while_a_node_is_pending():
    """Spec: AI (Codex) Active graph work permits an empty patch after same-patch filtering."""
    graph = LiveGraph()
    graph.apply_patch(
        GraphPatch(
            add=(NodeSpec("pending", "WorkOrder.get", {"id": TARGET}),),
            finish=False,
            reason="pending",
        )
    )
    reply = _response(
        _patch(
            [
                _addition(
                    "first",
                    "WorkOrder.get",
                    {"id": "WO-2"},
                    depends_on=["second"],
                ),
                _addition(
                    "second",
                    "WorkOrder.get",
                    {"id": "WO-3"},
                    depends_on=["first"],
                ),
            ]
        )
    )

    decision, _transport = _planner_call(reply, graph=graph)

    assert decision.accepted is True
    assert decision.patch.add == ()
    assert decision.state == PlannerState.from_limits(_limits())


@pytest.mark.parametrize(
    ("work_order_id", "attempted", "with_read", "expected"),
    [
        ("WO-2", False, True, "only target"),
        (TARGET, True, True, "only once"),
        (TARGET, False, False, "succeeded model WorkOrder.get"),
    ],
)
def test_reschedule_scope_once_and_model_read_rules(
    work_order_id, attempted, with_read, expected
):
    """Spec: AI (Codex) Reschedule is target-only, once-only, and requires a model target read."""
    if with_read:
        graph, store = _succeeded_target()
    else:
        graph, store = LiveGraph(), Store()
    reply = _response(
        _patch(
            [
                _addition(
                    "reschedule",
                    "reschedule_work_order",
                    {"work_order_id": work_order_id},
                )
            ]
        )
    )

    decision, _transport = _planner_call(
        reply,
        graph=graph,
        store=store,
        reschedule_attempted=attempted,
    )

    assert decision.repair_kind == "hard"
    assert expected in decision.message


def test_identical_same_patch_reschedules_keep_first_without_hard_repair():
    """Spec: AI (Codex). Identical reschedules deduplicate before once-only checks."""
    graph, store = _succeeded_target()
    reply = _response(
        _patch(
            [
                _addition(
                    "first",
                    "reschedule_work_order",
                    {"work_order_id": TARGET},
                ),
                _addition(
                    "duplicate",
                    "reschedule_work_order",
                    {"work_order_id": TARGET},
                ),
            ]
        )
    )

    decision, _transport = _planner_call(reply, graph=graph, store=store)

    assert decision.accepted is True
    assert [node.id for node in decision.patch.add] == ["first"]
    assert decision.repair_kind is None
    assert len(decision.discarded) == 1
    discarded = decision.discarded[0]
    assert discarded.node_id == "duplicate"
    assert discarded.covering_node_id == "first"
    assert discarded.covering_state == "pending"
    assert discarded.covering_outcome is None


def test_answered_requires_model_target_read_and_guard_read_does_not_count():
    """Spec: AI (Codex) A guard-only target read cannot support an answered terminal patch."""
    graph, store = _succeeded_target(model_read=False)

    decision, _transport = _planner_call(
        _response(_patch([_answer()])), graph=graph, store=store
    )

    assert decision.repair_kind == "hard"
    assert "succeeded model WorkOrder.get" in decision.message


def test_not_found_refusal_contradicts_succeeded_target_read():
    """Spec: AI (Codex) A successful target read makes a not_found refusal a hard contradiction."""
    graph, store = _succeeded_target()

    decision, _transport = _planner_call(
        _response(_patch([_answer(outcome="refused", refusal_reason="not_found")])),
        graph=graph,
        store=store,
    )

    assert decision.repair_kind == "hard"
    assert "not_found contradicts" in decision.message


def test_probe_patch_read_plus_dependent_answer_and_finish_is_rejected():
    """Spec: AI (Codex) The observed read-plus-dependent-answer probe reply is rejected wholesale."""
    bad_probe = _patch(
        [
            _addition("target", "WorkOrder.get", {"id": TARGET}),
            {
                **_answer(),
                "depends_on": ["target"],
            },
        ],
        finish=True,
        reason="read and finish together",
    )

    decision, _transport = _planner_call(_response(bad_probe))

    assert decision.repair_kind == "hard"
    assert decision.patch is None
    assert "answer must be the only addition" in decision.message


def test_prompt_is_deterministic_bounded_and_uses_canonical_answer_name():
    """Spec: AI (Codex) Prompt ordering is stable, projections are bounded, and manifest says answer."""
    graph = LiveGraph()
    long_a = {"data": [{"id": "A", "text": "x" * 300}], "returned": 1}
    long_b = {"data": [{"id": "B", "text": "y" * 300}], "returned": 1}
    for node_id, outcome in (("a", long_a), ("b", long_b)):
        graph.apply_patch(
            GraphPatch(
                add=(NodeSpec(node_id, "WorkOrder.get", {"id": node_id}),),
                finish=False,
                reason=node_id,
            )
        )
        graph.start(node_id)
        graph.succeed(node_id, outcome)
    limits = _limits(projection_chars=120, projection_total_chars=200)
    state = PlannerState.from_limits(limits)
    kwargs = {
        "request": "Investigate.",
        "target_id": TARGET,
        "today": TODAY,
        "manifest": _manifest(),
        "graph": graph,
        "store": Store(),
        "limits": limits,
        "state": state,
        "reschedule_requested": True,
        "reschedule_attempted": False,
        "repair_messages": ("try exact arguments",),
    }

    first = build_messages(**kwargs)
    second = build_messages(**kwargs)
    payload = json.loads(first[1]["content"])
    projections = [node["outcome_projection"] for node in payload["nodes"]]
    names = [capability["name"] for capability in payload["manifest"]]

    assert first == second
    assert [node["id"] for node in payload["nodes"]] == ["a", "b"]
    assert all(len(projection) <= limits.projection_chars for projection in projections)
    assert sum(len(projection) for projection in projections) <= limits.projection_total_chars
    assert '"truncated":true' in projections[1]
    assert '"state":"succeeded"' in projections[0]
    assert "answer" in names
    assert "finish" not in names
    assert payload["repair_messages"] == ["try exact arguments"]
    assert "reschedule_work_order not yet called" in payload["evidence_checklist"]


def test_prompt_projects_failed_target_not_found_detail():
    """Spec: AI (Codex) A failed target read exposes its not-found type and message."""
    graph = _failed_target(
        {
            "ok": False,
            "error": {
                "type": "not_found",
                "message": "WorkOrder.get id 'WO-1' not found",
            },
        }
    )
    limits = _limits()

    messages = build_messages(
        request="Investigate.",
        target_id=TARGET,
        today=TODAY,
        manifest=_manifest(),
        graph=graph,
        store=Store(),
        limits=limits,
        state=PlannerState.from_limits(limits),
        reschedule_requested=False,
        reschedule_attempted=False,
    )
    node = json.loads(messages[1]["content"])["nodes"][0]

    assert "outcome_projection" not in node
    assert '"type":"not_found"' in node["error_projection"]
    assert "WorkOrder.get id 'WO-1' not found" in node["error_projection"]


def test_prompt_projects_transport_outcome_uncertainty():
    """Spec: AI (Codex) A failed transport read exposes its type and outcome uncertainty."""
    graph = _failed_target(
        {
            "ok": False,
            "error": {
                "type": "transport",
                "message": "offline transport fault",
                "outcome_unknown": True,
            },
        }
    )
    limits = _limits()

    messages = build_messages(
        request="Investigate.",
        target_id=TARGET,
        today=TODAY,
        manifest=_manifest(),
        graph=graph,
        store=Store(),
        limits=limits,
        state=PlannerState.from_limits(limits),
        reschedule_requested=False,
        reschedule_attempted=False,
    )
    projection = json.loads(messages[1]["content"])["nodes"][0][
        "error_projection"
    ]

    assert '"type":"transport"' in projection
    assert '"outcome_unknown":true' in projection


@pytest.mark.parametrize("error_type", ["transport", "not_found"])
def test_long_failure_message_preserves_distinguishing_error_type(error_type):
    """Spec: AI (Codex) Long failure messages cannot hide distinguishing metadata."""
    graph = _failed_target(
        {
            "ok": False,
            "error": {
                "message": "x" * 1000,
                "type": error_type,
                "outcome_unknown": error_type == "transport",
            },
        }
    )
    limits = _limits(projection_chars=200, projection_total_chars=200)

    messages = build_messages(
        request="Investigate.",
        target_id=TARGET,
        today=TODAY,
        manifest=_manifest(),
        graph=graph,
        store=Store(),
        limits=limits,
        state=PlannerState.from_limits(limits),
        reschedule_requested=False,
        reschedule_attempted=False,
    )
    projection = json.loads(messages[1]["content"])["nodes"][0][
        "error_projection"
    ]

    assert f'"type":"{error_type}"' in projection
    other_type = "not_found" if error_type == "transport" else "transport"
    assert f'"type":"{other_type}"' not in projection
    if error_type == "transport":
        assert '"outcome_unknown":true' in projection
    assert len(projection) <= limits.projection_chars


def test_large_error_projections_obey_per_node_and_total_caps():
    """Spec: AI (Codex) Large failed-read details share the bounded projection budget."""
    graph = LiveGraph()
    error_types = ("transport", "not_found", "permission_denied")
    for node_id, error_type in zip(("a", "b", "c"), error_types, strict=True):
        graph.apply_patch(
            GraphPatch(
                add=(NodeSpec(node_id, "WorkOrder.get", {"id": node_id}),),
                finish=False,
                reason=node_id,
            )
        )
        graph.start(node_id)
        graph.fail(
            node_id,
            "error",
            {
                "ok": False,
                "error": {"type": error_type, "message": node_id * 300},
            },
        )
    limits = _limits(projection_chars=120, projection_total_chars=200)

    messages = build_messages(
        request="Investigate.",
        target_id=TARGET,
        today=TODAY,
        manifest=_manifest(),
        graph=graph,
        store=Store(),
        limits=limits,
        state=PlannerState.from_limits(limits),
        reschedule_requested=False,
        reschedule_attempted=False,
    )
    projections = [
        node["error_projection"]
        for node in json.loads(messages[1]["content"])["nodes"]
    ]

    assert all(len(projection) <= limits.projection_chars for projection in projections)
    assert sum(len(projection) for projection in projections) <= limits.projection_total_chars
    assert all(
        f'"type":"{error_type}"' in projection
        for projection, error_type in zip(projections, error_types, strict=True)
    )


def test_failed_projection_summaries_keep_block_and_incomplete_scan_metadata():
    """Spec: AI (Codex) Failure summaries retain block and incomplete-scan metadata."""
    graph = LiveGraph()
    graph.apply_patch(
        GraphPatch(
            add=(
                NodeSpec("parent", "WorkOrder.get", {"id": "missing"}),
                NodeSpec("scan", "MaterialRequest.list", {"work_order_id": TARGET}),
            ),
            finish=False,
            reason="failures",
        )
    )
    graph.apply_patch(
        GraphPatch(
            add=(
                NodeSpec(
                    "blocked",
                    "MaterialRequest.list",
                    {"work_order_id": TARGET},
                    depends_on=("parent",),
                ),
            ),
            finish=False,
            reason="dependent",
        )
    )
    graph.start("parent")
    graph.fail(
        "parent",
        "error",
        {"error": {"type": "not_found", "message": "x" * 300}},
    )
    graph.start("scan")
    graph.fail(
        "scan",
        "incomplete_scan",
        {"data": [{"id": "MR-1", "text": "x" * 300}], "total": 4, "complete": False},
    )
    limits = _limits(projection_chars=120, projection_total_chars=240)

    messages = build_messages(
        request="Investigate.",
        target_id=TARGET,
        today=TODAY,
        manifest=_manifest(),
        graph=graph,
        store=Store(),
        limits=limits,
        state=PlannerState.from_limits(limits),
        reschedule_requested=False,
        reschedule_attempted=False,
    )
    nodes = {
        node["id"]: node["error_projection"]
        for node in json.loads(messages[1]["content"])["nodes"]
    }

    assert '"blocked_by":"parent"' in nodes["blocked"]
    assert '"failure_reason":"incomplete_scan"' in nodes["scan"]
    assert '"total":4' in nodes["scan"]
    assert '"rows":1' in nodes["scan"]
    assert '"complete":false' in nodes["scan"]
    assert all(len(projection) <= limits.projection_chars for projection in nodes.values())
    assert sum(len(projection) for projection in nodes.values()) <= limits.projection_total_chars


def test_three_projection_outcomes_fit_the_total_cap():
    """Spec: AI (Codex) Three projections include summaries and truncation within the aggregate cap."""
    graph = LiveGraph()
    for node_id in ("a", "b", "c"):
        graph.apply_patch(
            GraphPatch(
                add=(NodeSpec(node_id, "WorkOrder.get", {"id": node_id}),),
                finish=False,
                reason=node_id,
            )
        )
        graph.start(node_id)
        graph.succeed(node_id, {"data": [{"id": node_id, "text": "x" * 300}]})
    limits = _limits(projection_chars=120, projection_total_chars=200)

    messages = build_messages(
        request="Investigate.",
        target_id=TARGET,
        today=TODAY,
        manifest=_manifest(),
        graph=graph,
        store=Store(),
        limits=limits,
        state=PlannerState.from_limits(limits),
        reschedule_requested=False,
        reschedule_attempted=False,
    )
    projections = [
        node["outcome_projection"]
        for node in json.loads(messages[1]["content"])["nodes"]
    ]

    assert all(len(projection) <= 120 for projection in projections)
    assert sum(len(projection) for projection in projections) <= 200
    assert '"state":"succeeded"' in projections[0]
    assert '"state":"succeeded"' in projections[1]


def test_projection_cap_smaller_than_truncation_marker_is_respected():
    """Spec: AI (Codex) A tiny per-node cap shortens the truncation marker itself."""
    graph = LiveGraph()
    graph.apply_patch(
        GraphPatch(
            add=(NodeSpec("tiny", "WorkOrder.get", {"id": TARGET}),),
            finish=False,
            reason="tiny",
        )
    )
    graph.start("tiny")
    graph.succeed("tiny", {"text": "x" * 300})
    limits = _limits(projection_chars=5, projection_total_chars=5)

    messages = build_messages(
        request="Investigate.",
        target_id=TARGET,
        today=TODAY,
        manifest=_manifest(),
        graph=graph,
        store=Store(),
        limits=limits,
        state=PlannerState.from_limits(limits),
        reschedule_requested=False,
        reschedule_attempted=False,
    )
    projection = json.loads(messages[1]["content"])["nodes"][0][
        "outcome_projection"
    ]

    assert projection == "…"
    assert len(projection) <= limits.projection_chars
