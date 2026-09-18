"""Subject adapters exercised by the harness."""

from datetime import date
from typing import Any

from agentswitch.agent import AgentError, AgentIncomplete, run_agent
from agentswitch.investigate import investigate
from agentswitch.mcp_client import TransportError
from agentswitch.reschedule import reschedule as reschedule_work_order

from .recorder import ReadOnlyTools

SUBJECT_LABEL = "investigate() + scoped reschedule() deterministic adapter; no LLM"


def _llm_label(model: Any) -> str:
    return f"LLM agent ({model}) over MCP; claims classified by code from agent-read records"


def _refusal(reason: str, target_id: str | None) -> dict[str, Any]:
    return {
        "outcome": "refused",
        "refusal_reason": reason,
        "work_order_id": target_id,
        "claims": None,
        "prose": None,
    }


def _project_evidence(reference: Any) -> dict[str, Any]:
    if not isinstance(reference, dict):
        return {"entity": None, "id": None, "fields": {}}
    fields = reference.get("fields")
    return {
        "entity": reference.get("entity"),
        "id": reference.get("id"),
        "fields": dict(fields) if isinstance(fields, dict) else fields,
    }


def _project_answer(
    raw: dict[str, Any],
    target_id: str,
    reschedule_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    work_order = raw.get("work_order")
    lateness = raw.get("lateness")
    downstream = raw.get("downstream")
    work_order = work_order if isinstance(work_order, dict) else {}
    lateness = lateness if isinstance(lateness, dict) else {}
    downstream = downstream if isinstance(downstream, dict) else {}
    causes = raw.get("causes")
    projected_causes = []
    if isinstance(causes, list):
        for cause in causes:
            if not isinstance(cause, dict):
                projected_causes.append(cause)
                continue
            evidence = cause.get("evidence")
            projected_causes.append(
                {
                    "code": cause.get("code"),
                    "basis": cause.get("basis"),
                    "evidence": (
                        [_project_evidence(reference) for reference in evidence]
                        if isinstance(evidence, list)
                        else evidence
                    ),
                }
            )
    sales_order = downstream.get("sales_order")
    projected_sales = None
    if isinstance(sales_order, dict):
        projected_sales = {"id": sales_order.get("id"), "status": sales_order.get("status")}
    consumers = downstream.get("potential_consumers")
    projected_consumers = []
    if isinstance(consumers, list):
        for consumer in consumers:
            if isinstance(consumer, dict):
                projected_consumers.append(
                    {
                        "id": consumer.get("id"),
                        "status": consumer.get("status"),
                        "bom_id": consumer.get("bom_id"),
                        "link": consumer.get("link"),
                    }
                )
            else:
                projected_consumers.append(consumer)
    work_order_fields = (
        "id",
        "status",
        "planned_start_date",
        "planned_end_date",
        "sales_order_id",
        "item_id",
        "bom_id",
    )
    claims = {
        "work_order": {field: work_order.get(field) for field in work_order_fields},
        "lateness": {
            "is_late": lateness.get("is_late"),
            "days_late": lateness.get("days_late"),
        },
        "causes": projected_causes,
        "downstream": {
            "sales_order": projected_sales,
            "potential_consumers": projected_consumers,
        },
        "unknowns": raw.get("unknowns"),
    }
    if reschedule_result is not None:
        claims["reschedule"] = {
            field: reschedule_result.get(field)
            for field in ("action", "reason", "proposed", "applied", "basis", "notes")
        }
    return {
        "outcome": "answered",
        "refusal_reason": None,
        "work_order_id": target_id,
        "claims": claims,
        "prose": None,
    }


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
        return {"answer": _refusal("unsupported", target_id), "label": SUBJECT_LABEL}
    if target_id is None:
        return {"answer": _refusal("unsupported", None), "label": SUBJECT_LABEL}
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
            return {"answer": _refusal("source_unavailable", target_id), "label": SUBJECT_LABEL}
        raise
    calls = raw.get("calls")
    first_outcome = calls[0].get("outcome") if isinstance(calls, list) and calls else None
    if not raw.get("found"):
        reason = "not_found" if first_outcome == "not_found" else "source_unavailable"
        return {"answer": _refusal(reason, target_id), "label": SUBJECT_LABEL}
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
        "answer": _project_answer(raw, target_id, reschedule_result),
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
        answer = _project_answer(result["raw"], target_id, result.get("reschedule"))
        answer["prose"] = result["prose"]
    else:
        answer = _refusal(result["refusal_reason"], target_id)
        answer["prose"] = result["prose"]
    model = result.get("model")
    label = _llm_label(model)
    return {
        "answer": answer,
        "label": label,
        "reschedule": result.get("reschedule"),
        "agent": {
            key: result.get(key)
            for key in ("transcript", "usage", "model", "turns", "repairs", "coverage")
        },
    }


__all__ = ["SUBJECT_LABEL", "investigate_subject", "llm_subject"]
