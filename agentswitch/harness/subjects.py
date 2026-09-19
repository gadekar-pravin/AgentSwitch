"""Subject adapters exercised by the harness."""

from collections.abc import Callable
from datetime import date
from typing import Any

from agentswitch.agent import AgentError, AgentIncomplete, run_agent
from agentswitch.answer import project_answer, refusal
from agentswitch.executor import GraphAgentError, run_graph_agent
from agentswitch.investigate import investigate
from agentswitch.mcp_client import TransportError
from agentswitch.reschedule import reschedule as reschedule_work_order

from .recorder import ReadOnlyTools

SUBJECT_LABEL = "investigate() + scoped reschedule() deterministic adapter; no LLM"


def _llm_label(model: Any) -> str:
    return f"LLM agent ({model}) over MCP; claims classified by code from agent-read records"


def _graph_label(model: Any) -> str:
    return (
        f"graph agent ({model}) over MCP; planner patches validated by code; "
        "claims classified by code from agent-read records"
    )


def investigate_subject(
    tools: ReadOnlyTools,
    *,
    request: str,
    request_kind: str,
    target_id: str | None,
    today: date,
    reschedule: bool = False,
    own_user_id: str | None = None,
) -> dict[str, Any]:
    """Project the deterministic investigation into the scored answer contract."""
    del request
    if request_kind != "work_order_lateness":
        return {"answer": refusal("unsupported", target_id), "label": SUBJECT_LABEL}
    if target_id is None:
        return {"answer": refusal("unsupported", None), "label": SUBJECT_LABEL}
    try:
        raw = investigate(tools, target_id, today=today)
    except TransportError:
        target_gets = [
            call
            for call in tools.calls
            if call.get("phase") == "subject"
            and call.get("tool") == "WorkOrder.get"
            and call.get("arguments") == {"id": target_id}
        ]
        if target_gets and target_gets[-1].get("outcome") != "ok":
            return {"answer": refusal("source_unavailable", target_id), "label": SUBJECT_LABEL}
        raise
    calls = raw.get("calls")
    first_outcome = calls[0].get("outcome") if isinstance(calls, list) and calls else None
    if not raw.get("found"):
        reason = "not_found" if first_outcome == "not_found" else "source_unavailable"
        return {"answer": refusal(reason, target_id), "label": SUBJECT_LABEL}
    reschedule_result = None
    if reschedule:
        reschedule_result = reschedule_work_order(
            tools,
            target_id,
            today=today,
            own_user_id=own_user_id,
            causes=raw.get("causes"),
        )
    return {
        "answer": project_answer(raw, target_id, reschedule_result),
        "label": SUBJECT_LABEL,
        "reschedule": reschedule_result,
    }


def llm_subject(
    tools: ReadOnlyTools,
    *,
    request: str,
    request_kind: str,
    target_id: str | None,
    today: date,
    reschedule: bool,
    own_user_id: str | None,
    llm: Any,
) -> dict[str, Any]:
    """Run the LLM-directed subject and project its code-classified claims."""
    del request_kind, reschedule
    try:
        result = run_agent(
            tools,
            llm,
            request=request,
            target_id=target_id,
            today=today,
            own_user_id=own_user_id,
            allow_write=True,
        )
    except (AgentIncomplete, AgentError) as error:
        agent = error.agent
        error.subject_output = {
            "answer": None,
            "label": _llm_label(agent.get("model")),
            "reschedule": None,
            "agent": agent,
        }
        error.subject_label = error.subject_output["label"]
        raise
    if result["outcome"] == "answered":
        assert target_id is not None
        answer = project_answer(result["raw"], target_id, result.get("reschedule"))
        answer["prose"] = result["prose"]
    else:
        answer = refusal(result["refusal_reason"], target_id)
        answer["prose"] = result["prose"]
    model = result.get("model")
    label = _llm_label(model)
    return {
        "answer": answer,
        "label": label,
        "reschedule": result.get("reschedule"),
        "agent": {
            key: result.get(key)
            for key in (
                "transcript",
                "usage",
                "model",
                "turns",
                "repairs",
                "coverage",
                "manifest",
            )
        },
    }


def graph_subject(
    tools: ReadOnlyTools,
    *,
    request: str,
    request_kind: str,
    target_id: str | None,
    today: date,
    reschedule: bool,
    own_user_id: str | None,
    llm: Any,
    config: Any,
    authority: dict[str, Any],
    receipt: Callable[[dict[str, Any]], str] | None = None,
) -> dict[str, Any]:
    """Run the graph subject and project its code-classified claims."""
    del request_kind
    try:
        result = run_graph_agent(
            tools,
            llm,
            request=request,
            target_id=target_id,
            today=today,
            own_user_id=own_user_id,
            reschedule_requested=reschedule,
            config=config,
            authority=authority,
            receipt=receipt,
        )
    except GraphAgentError as error:
        agent = error.agent
        error.subject_output = {
            "answer": None,
            "label": _graph_label(agent.get("model")),
            "reschedule": None,
            "agent": agent,
        }
        error.subject_label = error.subject_output["label"]
        raise
    if result["outcome"] == "answered":
        assert target_id is not None
        answer = project_answer(result["raw"], target_id, result.get("reschedule"))
        answer["prose"] = result["prose"]
    else:
        answer = refusal(result["refusal_reason"], target_id)
        answer["prose"] = result["prose"]
    label = _graph_label(result.get("model"))
    return {
        "answer": answer,
        "label": label,
        "reschedule": result.get("reschedule"),
        "agent": {
            key: result.get(key)
            for key in (
                "usage",
                "model",
                "turns",
                "repairs",
                "coverage",
                "manifest",
                "journal",
                "graph",
                "patches",
                "authority",
                "action_receipt",
                "missing",
                "planner",
            )
        },
    }


__all__ = ["SUBJECT_LABEL", "graph_subject", "investigate_subject", "llm_subject"]
