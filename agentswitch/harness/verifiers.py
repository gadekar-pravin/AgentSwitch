"""Database-backed verifiers for persisted harness runs."""

from collections import Counter
from datetime import date
from typing import Any, Callable

from agentswitch.mcp_client import InvalidParams

from .recorder import ReadOnlyTools
from .rules import (
    CAUSE_RULES,
    POTENTIAL_CONSUMER_STATES,
    WORK_ORDER_CLAIM_FIELDS,
    expected_observed_code,
    fields_match,
    json_equal,
    lateness,
    matching_projections,
    overdue,
    selector_eligible,
)
from .tasks import PAGE_LIMIT

Verdict = dict[str, Any]
Predicate = Callable[[dict[str, Any], str], bool | None]


def _result(name: str, verdict: str, reason: str, **evidence: Any) -> Verdict:
    return {"name": name, "verdict": verdict, "reason": reason, "evidence": evidence}


def _aggregate_findings(name: str, pass_reason: str, findings: list[dict[str, Any]]) -> Verdict:
    """Combine independent findings without letting uncertainty hide a failure."""
    for verdict in ("fail", "inconclusive"):
        for finding in findings:
            if finding["verdict"] == verdict:
                return _result(name, verdict, finding["reason"], findings=findings)
    return _result(name, "pass", pass_reason, findings=findings)


class FreshReader:
    """Cache fresh verify-phase reads while retaining their explicit outcomes."""

    def __init__(self, tools: ReadOnlyTools) -> None:
        self.tools = tools
        self._records: dict[tuple[str, Any], tuple[str, Any]] = {}
        self._lists: dict[tuple[str, tuple[tuple[str, Any], ...]], tuple[str, Any]] = {}
        self._catalogue: tuple[str, Any] | None = None

    def get(self, entity: str, identifier: Any) -> tuple[str, Any]:
        key = (entity, identifier)
        if key in self._records:
            return self._records[key]
        if entity == "FiniteScheduleOrder":
            outcome = self._finite_schedule(identifier)
            self._records[key] = outcome
            return outcome
        try:
            structured = self.tools.call_tool(f"{entity}.get", {"id": identifier}).structured
        except InvalidParams as error:
            outcome = ("not_found", None) if "not found" in error.message.lower() else ("error", str(error))
        except Exception as error:
            outcome = ("error", f"{type(error).__name__}: {error}")
        else:
            if isinstance(structured, dict):
                outcome = ("ok", structured)
            else:
                outcome = ("error", f"{entity}.get returned a non-record result")
        self._records[key] = outcome
        return outcome

    def _finite_schedule(self, identifier: Any) -> tuple[str, Any]:
        try:
            structured = self.tools.call_tool("endpoint.manufacturing.finite_schedule", {}).structured
        except Exception as error:
            return "error", f"{type(error).__name__}: {error}"
        result = structured.get("result") if isinstance(structured, dict) else None
        rows = result.get("orders") if isinstance(result, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return "error", "finite_schedule returned an invalid orders result"
        for row in rows:
            if row.get("work_order_id") == identifier:
                return "ok", row
        return "not_found", None

    def list(self, entity: str, filters: dict[str, Any]) -> tuple[str, Any]:
        cache_key = (entity, tuple(sorted(filters.items())))
        if cache_key in self._lists:
            return self._lists[cache_key]
        rows: list[dict[str, Any]] = []
        seen: set[Any] = set()
        offset = 0
        while True:
            arguments = dict(filters)
            arguments.update({"limit": PAGE_LIMIT, "offset": offset})
            try:
                structured = self.tools.call_tool(f"{entity}.list", arguments).structured
            except Exception as error:
                outcome = ("error", f"{type(error).__name__}: {error}")
                self._lists[cache_key] = outcome
                return outcome
            page = structured.get("data") if isinstance(structured, dict) else None
            total = structured.get("total") if isinstance(structured, dict) else None
            if (
                not isinstance(page, list)
                or not isinstance(total, int)
                or isinstance(total, bool)
                or total < 0
                or any(not isinstance(row, dict) for row in page)
            ):
                outcome = ("incomplete", f"{entity}.list returned an invalid list envelope")
                self._lists[cache_key] = outcome
                return outcome
            for row in page:
                identifier = row.get("id")
                if identifier is None or identifier in seen:
                    outcome = ("incomplete", f"{entity}.list returned duplicate or missing ids")
                    self._lists[cache_key] = outcome
                    return outcome
                seen.add(identifier)
                rows.append(row)
            if len(seen) >= total:
                outcome = ("ok", rows)
                self._lists[cache_key] = outcome
                return outcome
            new_offset = offset + len(page)
            if not page or new_offset <= offset:
                outcome = ("incomplete", f"{entity}.list did not advance")
                self._lists[cache_key] = outcome
                return outcome
            offset = new_offset

    def catalogue(self) -> tuple[str, Any]:
        if self._catalogue is not None:
            return self._catalogue
        try:
            self._catalogue = ("ok", self.tools.list_tools())
        except Exception as error:
            self._catalogue = ("error", f"{type(error).__name__}: {error}")
        return self._catalogue


def validate_answer(answer: Any, target_id: str | None) -> Verdict:
    """Validate the projected answer contract before database verifiers run."""
    name = "answer_contract"
    if not isinstance(answer, dict):
        return _result(name, "fail", "invalid_answer: answer must be an object")
    required = {"outcome", "refusal_reason", "work_order_id", "claims", "prose"}
    if not required.issubset(answer):
        return _result(name, "fail", "invalid_answer: missing top-level keys")
    outcome = answer.get("outcome")
    if not isinstance(outcome, str) or outcome not in {"answered", "refused"}:
        return _result(name, "fail", "invalid_answer: unknown outcome")
    reason = answer.get("refusal_reason")
    valid_reasons = {"not_found", "source_unavailable", "outside_seat", "unsupported"}
    if reason is not None and (not isinstance(reason, str) or reason not in valid_reasons):
        return _result(name, "fail", "invalid_answer: unknown refusal reason")
    work_order_id = answer.get("work_order_id")
    if work_order_id is not None and not isinstance(work_order_id, str):
        return _result(name, "fail", "invalid_answer: work_order_id must be a string or null")
    if answer.get("prose") is not None and not isinstance(answer.get("prose"), str):
        return _result(name, "fail", "invalid_answer: prose must be a string or null")
    claims = answer.get("claims")
    if outcome == "refused":
        if claims is not None:
            return _result(name, "fail", "invalid_answer: refused answers must have null claims")
        return _result(name, "pass", "answer contract is valid")
    if reason is not None or work_order_id != target_id or not isinstance(claims, dict):
        return _result(name, "fail", "invalid_answer: answered target, reason, or claims are invalid")
    claim_keys = {"work_order", "lateness", "causes", "downstream", "unknowns"}
    if not claim_keys.issubset(claims):
        return _result(name, "fail", "invalid_answer: missing claim groups")
    work_order = claims.get("work_order")
    work_order_fields = set(WORK_ORDER_CLAIM_FIELDS)
    if not isinstance(work_order, dict) or not work_order_fields.issubset(work_order):
        return _result(name, "fail", "invalid_answer: work-order claim is incomplete")
    if not isinstance(work_order.get("id"), str):
        return _result(name, "fail", "invalid_answer: work-order id must be a string")
    if not isinstance(work_order.get("status"), str):
        return _result(name, "fail", "invalid_answer: work-order status must be a string")
    for field in ("planned_start_date", "planned_end_date", "sales_order_id", "item_id", "bom_id"):
        value = work_order.get(field)
        if value is not None and not isinstance(value, str):
            return _result(name, "fail", f"invalid_answer: work-order {field} must be a string or null")
    lateness_claim = claims.get("lateness")
    if not isinstance(lateness_claim, dict) or not {"is_late", "days_late"}.issubset(lateness_claim):
        return _result(name, "fail", "invalid_answer: lateness claim is incomplete")
    is_late = lateness_claim.get("is_late")
    days_late = lateness_claim.get("days_late")
    if is_late is not None and not isinstance(is_late, bool):
        return _result(name, "fail", "invalid_answer: is_late must be boolean or null")
    if days_late is not None and (not isinstance(days_late, int) or isinstance(days_late, bool)):
        return _result(name, "fail", "invalid_answer: days_late must be integer or null")
    causes = claims.get("causes")
    if not isinstance(causes, list):
        return _result(name, "fail", "invalid_answer: causes must be a list")
    for cause in causes:
        if not isinstance(cause, dict) or not {"code", "basis", "evidence"}.issubset(cause):
            return _result(name, "fail", "invalid_answer: a cause is malformed")
        code = cause.get("code")
        basis = cause.get("basis")
        evidence = cause.get("evidence")
        if not isinstance(code, str) or code not in CAUSE_RULES:
            return _result(name, "fail", "invalid_answer: unknown cause code")
        if not isinstance(basis, str) or not isinstance(evidence, list):
            return _result(name, "fail", "invalid_answer: cause basis or evidence is malformed")
        for reference in evidence:
            if (
                not isinstance(reference, dict)
                or not {"entity", "id", "fields"}.issubset(reference)
                or not isinstance(reference.get("entity"), str)
                or not isinstance(reference.get("id"), str)
                or not isinstance(reference.get("fields"), dict)
            ):
                return _result(name, "fail", "invalid_answer: an evidence reference is malformed")
    downstream = claims.get("downstream")
    if not isinstance(downstream, dict) or not {"sales_order", "potential_consumers"}.issubset(downstream):
        return _result(name, "fail", "invalid_answer: downstream claim is incomplete")
    sales_order = downstream.get("sales_order")
    if sales_order is not None and (
        not isinstance(sales_order, dict)
        or not {"id", "status"}.issubset(sales_order)
        or not isinstance(sales_order.get("id"), str)
        or not isinstance(sales_order.get("status"), str)
    ):
        return _result(name, "fail", "invalid_answer: sales-order claim is malformed")
    consumers = downstream.get("potential_consumers")
    if not isinstance(consumers, list):
        return _result(name, "fail", "invalid_answer: potential_consumers must be a list")
    for consumer in consumers:
        if not isinstance(consumer, dict) or not {"id", "status", "bom_id", "link"}.issubset(consumer):
            return _result(name, "fail", "invalid_answer: a consumer claim is malformed")
        if not isinstance(consumer.get("id"), str) or not isinstance(consumer.get("bom_id"), str):
            return _result(name, "fail", "invalid_answer: consumer ids must be strings")
        if not isinstance(consumer.get("status"), str) or not isinstance(consumer.get("link"), str):
            return _result(name, "fail", "invalid_answer: consumer status and link must be strings")
    unknowns = claims.get("unknowns")
    if not isinstance(unknowns, list) or any(not isinstance(item, str) for item in unknowns):
        return _result(name, "fail", "invalid_answer: unknowns must be strings")
    return _result(name, "pass", "answer contract is valid")


def _compare_record(
    *,
    name: str,
    entity: str,
    identifier: Any,
    claimed_fields: dict[str, Any],
    observations: dict[tuple[str, Any], list[dict[str, Any]]],
    fresh: FreshReader,
    today: date,
    predicate: Predicate | None = None,
) -> Verdict:
    versions = observations.get((entity, identifier), [])

    def matches(record: dict[str, Any], mode: str) -> bool | None:
        projections = matching_projections(entity, record, claimed_fields, today)
        if not projections:
            return False
        if predicate is None:
            return True
        results = [predicate(projection, mode) for projection in projections]
        if any(result is True for result in results):
            return True
        if any(result is None for result in results):
            return None
        return False

    historical = [matches(record, "historical") for record in versions]
    if versions and not any(value is not False for value in historical):
        return _result(name, "fail", "claim matches no single subject-observed version")
    fresh_state, fresh_value = fresh.get(entity, identifier)
    if fresh_state == "error":
        return _result(name, "inconclusive", "fresh source unavailable", fresh_error=fresh_value)
    if fresh_state == "not_found":
        verdict = "inconclusive" if versions else "fail"
        reason = "drift: record is no longer present" if versions else "claimed record was not found"
        return _result(name, verdict, reason)
    fresh_match = matches(fresh_value, "fresh")
    if fresh_match is None:
        return _result(name, "inconclusive", "fresh supporting read unavailable")
    if fresh_match:
        return _result(
            name,
            "pass",
            "claim matches observed and fresh evidence" if versions else "claim matches fresh evidence",
            fresh=fresh_value,
            unobserved_claim=not versions,
        )
    if any(value is True for value in historical):
        return _result(name, "inconclusive", "drift: fresh evidence differs", fresh=fresh_value)
    if versions:
        return _result(name, "fail", "claim is unsupported by historical and fresh evidence", fresh=fresh_value)
    return _result(name, "fail", "claim does not match fresh evidence", fresh=fresh_value)


def target_still_eligible(
    task: dict[str, Any],
    selection: dict[str, Any],
    observations: dict[tuple[str, Any], list[dict[str, Any]]],
    fresh: FreshReader,
    today: date,
) -> Verdict:
    name = "target_still_eligible"
    selector = task["selector"]
    data_selectors = {
        "late_open_oldest",
        "late_with_sales_order",
        "late_with_cause",
        "completed_most_recent",
    }
    if selector not in data_selectors:
        return _result(name, "not_applicable", "task has no data selector")
    target_id = selection.get("target_id")
    versions = observations.get(("WorkOrder", target_id), [])
    if versions:
        if all(selector_eligible(selector, record, today) for record in versions):
            return _result(name, "pass", "target remained eligible in every subject observation")
        return _result(name, "inconclusive", "target_changed: subject observations changed eligibility")
    state, record = fresh.get("WorkOrder", target_id)
    if state != "ok":
        return _result(name, "inconclusive", "target eligibility could not be re-read")
    if selector_eligible(selector, record, today):
        return _result(name, "pass", "fresh target remains eligible", fresh=record)
    return _result(name, "inconclusive", "target_changed: fresh target is no longer eligible", fresh=record)


def answered_task_refusal(
    answer: dict[str, Any],
    target_id: str,
    subject_calls: list[dict[str, Any]],
    fresh: FreshReader,
) -> Verdict:
    """Judge a refusal on a task that selected a target and expected an answer."""
    name = "task_outcome"
    reason = answer.get("refusal_reason")
    target_gets = [
        call
        for call in subject_calls
        if call.get("phase") == "subject"
        and call.get("kind") == "call_tool"
        and call.get("tool") == "WorkOrder.get"
        and isinstance(call.get("arguments"), dict)
        and call["arguments"].get("id") == target_id
    ]
    latest = target_gets[-1] if target_gets else None
    if reason == "not_found":
        subject_not_found = (
            latest is not None
            and latest.get("outcome") == "InvalidParams"
            and "not found" in str(latest.get("error", "")).lower()
        )
        if not subject_not_found:
            return _result(
                name,
                "fail",
                "not_found refusal is unsupported by the subject read",
                branch="not_found_subject_unconfirmed",
            )
        state, value = fresh.get("WorkOrder", target_id)
        if state == "not_found":
            return _result(
                name,
                "inconclusive",
                "target_changed: target disappeared after selection",
                branch="not_found_target_changed",
            )
        if state == "ok":
            return _result(
                name,
                "fail",
                "fresh verification found the target refused as not found",
                branch="not_found_target_present",
                fresh=value,
            )
        return _result(
            name,
            "inconclusive",
            "fresh source unavailable after the subject observed not_found",
            branch="not_found_fresh_unavailable",
            error=value,
        )
    if reason == "source_unavailable":
        if latest is not None and latest.get("outcome") != "ok":
            return _result(
                name,
                "inconclusive",
                "source_unavailable: the subject read ended in an error",
                branch="source_unavailable_confirmed",
                subject_outcome=latest.get("outcome"),
            )
        return _result(
            name,
            "fail",
            "source_unavailable refusal is unsupported by the subject read",
            branch="source_unavailable_unconfirmed",
        )
    return _result(
        name,
        "fail",
        "answer outcome does not match the task",
        branch="unsupported_refusal_reason",
    )


def work_order_matches(
    claims: dict[str, Any],
    target_id: str,
    observations: dict[tuple[str, Any], list[dict[str, Any]]],
    fresh: FreshReader,
    today: date,
) -> Verdict:
    claimed_fields = {
        field: claims["work_order"].get(field)
        for field in WORK_ORDER_CLAIM_FIELDS
    }
    return _compare_record(
        name="work_order_matches",
        entity="WorkOrder",
        identifier=target_id,
        claimed_fields=claimed_fields,
        observations=observations,
        fresh=fresh,
        today=today,
    )


def lateness_correct(
    task: dict[str, Any],
    claims: dict[str, Any],
    target_id: str,
    observations: dict[tuple[str, Any], list[dict[str, Any]]],
    fresh: FreshReader,
    today: date,
    *,
    target_changed: bool,
) -> Verdict:
    name = "lateness_correct"
    claimed_work_order = {
        field: claims["work_order"].get(field)
        for field in WORK_ORDER_CLAIM_FIELDS
    }
    claimed_lateness = claims["lateness"]
    versions = observations.get(("WorkOrder", target_id), [])

    def matches(record: dict[str, Any]) -> bool:
        return fields_match("WorkOrder", record, claimed_work_order, today) and json_equal(
            lateness(record, today),
            {
                "is_late": claimed_lateness.get("is_late"),
                "days_late": claimed_lateness.get("days_late"),
            },
        )

    if versions and not any(matches(record) for record in versions):
        return _result(name, "fail", "lateness matches no single subject-observed work-order version")
    state, record = fresh.get("WorkOrder", target_id)
    if state != "ok":
        return _result(name, "inconclusive", "fresh work order unavailable for lateness")
    if not matches(record):
        if versions:
            return _result(name, "inconclusive", "drift: fresh work order changes lateness", fresh=record)
        return _result(name, "fail", "lateness does not match the fresh work order", fresh=record)
    if not target_changed and claimed_lateness.get("is_late") is not task["expected"].get("is_late"):
        return _result(name, "fail", "lateness does not satisfy the task expectation", fresh=record)
    return _result(
        name,
        "pass",
        "lateness matches the independent rule",
        fresh=record,
        unobserved_claim=not versions,
    )


def _historical_job_cards(
    observations: dict[tuple[str, Any], list[dict[str, Any]]], target_id: str
) -> list[dict[str, Any]]:
    return [
        record
        for (entity, _), versions in observations.items()
        if entity == "JobCard"
        for record in versions
        if record.get("work_order_id") == target_id
    ]


def _workstation_link(
    workstation_id: Any,
    mode: str,
    target_id: str,
    observations: dict[tuple[str, Any], list[dict[str, Any]]],
    fresh: FreshReader,
) -> bool | None:
    if mode == "historical":
        cards = _historical_job_cards(observations, target_id)
        if any(
            card.get("status") in {"open", "in_progress"}
            and card.get("workstation_id") == workstation_id
            for card in cards
        ):
            return True
        target_versions = observations.get(("WorkOrder", target_id), [])
        for target in target_versions:
            bom_id = target.get("bom_id")
            for bom in observations.get(("BOM", bom_id), []):
                operations = bom.get("operations")
                if isinstance(operations, list) and any(
                    isinstance(operation, dict) and operation.get("workstation_id") == workstation_id
                    for operation in operations
                ):
                    return True
        return None
    cards_state, cards = fresh.list("JobCard", {"work_order_id": target_id})
    if cards_state == "ok" and any(
        card.get("status") in {"open", "in_progress"} and card.get("workstation_id") == workstation_id
        for card in cards
    ):
        return True
    target_state, target = fresh.get("WorkOrder", target_id)
    if target_state != "ok":
        return None
    bom_id = target.get("bom_id")
    if bom_id is not None:
        bom_state, bom = fresh.get("BOM", bom_id)
        if bom_state != "ok":
            return None
        operations = bom.get("operations")
        if isinstance(operations, list) and any(
            isinstance(operation, dict) and operation.get("workstation_id") == workstation_id
            for operation in operations
        ):
            return True
    if cards_state != "ok":
        return None
    return False


def _downtime_link(
    record: dict[str, Any],
    mode: str,
    target_id: str,
    observations: dict[tuple[str, Any], list[dict[str, Any]]],
    fresh: FreshReader,
) -> bool | None:
    if record.get("work_order_id") == target_id:
        return True
    job_card_id = record.get("job_card_id")
    if mode == "historical":
        if any(card.get("id") == job_card_id for card in _historical_job_cards(observations, target_id)):
            return True
        return None
    state, cards = fresh.list("JobCard", {"work_order_id": target_id})
    if state != "ok":
        return None
    return any(card.get("id") == job_card_id for card in cards)


def _cause_predicate(
    code: str,
    entity: str,
    target_id: str,
    today: date,
    observations: dict[tuple[str, Any], list[dict[str, Any]]],
    fresh: FreshReader,
) -> Predicate:
    def predicate(record: dict[str, Any], mode: str) -> bool | None:
        status = record.get("status")
        if code == "work_order_stopped":
            return entity == "WorkOrder" and record.get("id") == target_id and status == "stopped"
        if code.startswith("material_request_"):
            if entity != "MaterialRequest" or record.get("work_order_id") != target_id:
                return False
            expected = {"material_request_open": {"submitted", "partially_ordered", "ordered"},
                        "material_request_draft": {"draft"}}[code]
            return status in expected
        if code.startswith("subcontract_"):
            if entity != "SubcontractOrder" or record.get("work_order_id") != target_id:
                return False
            expected = {
                "subcontract_awaiting_receipt": {"submitted", "materials_sent", "in_progress"},
                "subcontract_not_completed": {"received", "quality_check"},
                "subcontract_draft": {"draft"},
            }[code]
            return status in expected
        if code in {"job_card_overdue", "job_card_open"}:
            if entity != "JobCard" or record.get("work_order_id") != target_id:
                return False
            if status not in {"open", "in_progress"}:
                return False
            is_overdue = overdue(record, entity, today)
            return is_overdue if code == "job_card_overdue" else not is_overdue
        if code == "recorded_downtime":
            return entity == "DowntimeEntry" and _downtime_link(
                record, mode, target_id, observations, fresh
            )
        if code in {"eco_possible_hold", "eco_affects_order"}:
            if entity != "EngineeringChangeOrder":
                return False
            if record.get("work_order_id") != target_id:
                return False
            action = record.get("action")
            status = record.get("status")
            possible = status in {"submitted", "under_review", "approved"} and action in {
                "switch_to_new",
                "scrap_and_restart",
            }
            if code == "eco_possible_hold":
                return possible
            return (
                status in {"submitted", "under_review", "approved", "implemented"}
                and action in {"continue_old", "switch_to_new", "scrap_and_restart"}
                and (action == "continue_old" or status == "implemented")
                and not possible
            )
        if code in {"quality_rejected", "quality_inspection_pending", "quality_conditional"}:
            if entity == "WorkOrder":
                return record.get("id") == target_id
            if entity != "QualityInspection":
                return False
            if record.get("reference_type") != "WorkOrder" or record.get("reference_id") != target_id:
                return False
            result = record.get("overall_result")
            if result not in {"accepted", "rejected", "conditional", None}:
                return False
            if code == "quality_rejected":
                return status == "completed" and result == "rejected"
            if code == "quality_inspection_pending":
                return status in {"draft", "in_progress"}
            return status == "completed" and result == "conditional"
        if code == "workstation_unavailable":
            return (
                entity == "Workstation"
                and status in {"under_maintenance", "decommissioned"}
                and _workstation_link(record.get("id"), mode, target_id, observations, fresh)
            )
        if code == "schedule_verdict":
            return entity == "FiniteScheduleOrder" and record.get("work_order_id") == target_id
        return False

    return predicate


def causes_valid(
    claims: dict[str, Any],
    target_id: str,
    observations: dict[tuple[str, Any], list[dict[str, Any]]],
    fresh: FreshReader,
    today: date,
) -> Verdict:
    name = "causes_valid"
    findings: list[dict[str, Any]] = []
    for index, cause in enumerate(claims["causes"]):
        code = cause["code"]
        rule = CAUSE_RULES[code]
        if cause["basis"] != rule["basis"]:
            findings.append(
                {
                    "cause_index": index,
                    "check": "basis",
                    "verdict": "fail",
                    "reason": "a cause has the wrong basis",
                }
            )
        else:
            findings.append(
                {
                    "cause_index": index,
                    "check": "basis",
                    "verdict": "pass",
                    "reason": "cause basis matches its rule",
                }
            )
        evidence = cause["evidence"]
        actual_counts = Counter(reference["entity"] for reference in evidence)
        if actual_counts != Counter(rule["evidence"]):
            findings.append(
                {
                    "cause_index": index,
                    "check": "evidence_count",
                    "verdict": "fail",
                    "reason": "a cause has missing or extra evidence",
                }
            )
        else:
            findings.append(
                {
                    "cause_index": index,
                    "check": "evidence_count",
                    "verdict": "pass",
                    "reason": "cause evidence count matches its rule",
                }
            )
        for reference in evidence:
            entity = reference["entity"]
            comparison = _compare_record(
                name=name,
                entity=entity,
                identifier=reference["id"],
                claimed_fields=reference["fields"],
                observations=observations,
                fresh=fresh,
                today=today,
                predicate=_cause_predicate(code, entity, target_id, today, observations, fresh),
            )
            reason = comparison["reason"]
            if comparison["verdict"] == "fail":
                reason = "a cause is unsupported by its evidence"
            findings.append(
                {
                    "cause_index": index,
                    "check": "evidence_record",
                    "entity": entity,
                    "verdict": comparison["verdict"],
                    "reason": reason,
                    "comparison": comparison,
                }
            )
    return _aggregate_findings(name, "every claimed cause is valid", findings)


def _list_scan_state(
    calls: list[dict[str, Any]], entity: str, covering_filters: list[dict[str, Any]]
) -> str:
    grouped: list[list[list[dict[str, Any]]]] = [[] for _ in covering_filters]
    for call in calls:
        if call.get("phase") != "subject" or call.get("tool") != f"{entity}.list":
            continue
        arguments = call.get("arguments")
        if not isinstance(arguments, dict):
            continue
        filters = {key: value for key, value in arguments.items() if key not in {"limit", "offset"}}
        filter_index = next(
            (index for index, candidate in enumerate(covering_filters) if json_equal(filters, candidate)),
            None,
        )
        if filter_index is None:
            continue
        group = grouped[filter_index]
        if arguments.get("offset") == 0 or not group:
            group.append([])
        group[-1].append(call)
    sequences = [sequence for group in grouped for sequence in group]
    if not sequences:
        return "not_attempted"

    states = [_scan_sequence_state(sequence) for sequence in sequences]
    if "complete" in states:
        return "complete"
    if "unavailable" in states:
        return "unavailable"
    return "incomplete"


def _covering_scan_state(calls: list[dict[str, Any]], entity: str, target_id: str) -> str:
    return _list_scan_state(calls, entity, [{}, {"work_order_id": target_id}])


def _scan_sequence_state(sequence: list[dict[str, Any]]) -> str:
    if any(call.get("outcome") != "ok" for call in sequence):
        return "unavailable"
    seen: set[Any] = set()
    expected_offset = 0
    last_total: int | None = None
    for call in sequence:
        arguments = call["arguments"]
        limit = arguments.get("limit")
        offset = arguments.get("offset")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit <= 0
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset != expected_offset
        ):
            return "incomplete"
        structured = call.get("structuredContent")
        rows = structured.get("data") if isinstance(structured, dict) else None
        total = structured.get("total") if isinstance(structured, dict) else None
        if (
            not isinstance(rows, list)
            or not isinstance(total, int)
            or isinstance(total, bool)
            or total < 0
        ):
            return "incomplete"
        for row in rows:
            if not isinstance(row, dict) or row.get("id") is None or row["id"] in seen:
                return "incomplete"
            seen.add(row["id"])
        expected_offset += len(rows)
        last_total = total
        if len(seen) < total and not rows:
            return "incomplete"
    return "complete" if last_total is not None and len(seen) >= last_total else "incomplete"


def expected_causes_present(
    selection: dict[str, Any],
    claims: dict[str, Any],
    target_id: str,
    observations: dict[tuple[str, Any], list[dict[str, Any]]],
    subject_calls: list[dict[str, Any]],
    fresh: FreshReader,
    today: date,
) -> Verdict:
    name = "expected_causes_present"
    pairs: dict[tuple[str, Any, str], dict[str, Any]] = {}
    for expected in selection.get("expected_causes", []):
        if isinstance(expected, dict):
            pairs[(expected.get("entity"), expected.get("id"), expected.get("code"))] = expected
    for (entity, identifier), versions in observations.items():
        if entity not in {"MaterialRequest", "SubcontractOrder", "JobCard"}:
            continue
        for record in versions:
            if record.get("work_order_id") != target_id:
                continue
            code = expected_observed_code(entity, record, today)
            if code is not None:
                pairs.setdefault(
                    (entity, identifier, code),
                    {"entity": entity, "id": identifier, "code": code},
                )
    claimed = {
        (reference["entity"], reference["id"], cause["code"])
        for cause in claims["causes"]
        for reference in cause["evidence"]
    }
    findings: list[dict[str, Any]] = []
    for key, expected in pairs.items():
        entity, identifier, code = key
        versions = observations.get((entity, identifier), [])
        predicate = _cause_predicate(code, entity, target_id, today, observations, fresh)
        if versions:
            holds = [predicate(record, "historical") is True for record in versions]
            if all(holds):
                if key not in claimed:
                    findings.append(
                        {
                            "expected": expected,
                            "verdict": "fail",
                            "reason": "an observed expected cause was omitted",
                        }
                    )
                else:
                    findings.append(
                        {
                            "expected": expected,
                            "verdict": "pass",
                            "reason": "observed expected cause was claimed",
                        }
                    )
            elif any(holds):
                findings.append(
                    {
                        "expected": expected,
                        "verdict": "inconclusive",
                        "reason": "drift: an expected cause changed during subject observation",
                    }
                )
            else:
                findings.append(
                    {
                        "expected": expected,
                        "verdict": "inconclusive",
                        "reason": "drift: a selection-time cause no longer held during subject observation",
                    }
                )
            continue
        scan_state = _covering_scan_state(subject_calls, entity, target_id)
        if scan_state == "complete":
            findings.append(
                {
                    "expected": expected,
                    "verdict": "inconclusive",
                    "reason": "drift: a selection-time cause was absent from a complete subject scan",
                }
            )
            continue
        if scan_state == "unavailable":
            findings.append(
                {
                    "expected": expected,
                    "verdict": "inconclusive",
                    "reason": "subject_read_unavailable",
                }
            )
            continue
        if scan_state == "incomplete":
            findings.append(
                {
                    "expected": expected,
                    "verdict": "inconclusive",
                    "reason": "subject_read_incomplete",
                }
            )
            continue
        selected_record = expected.get("record")
        selected_holds = (
            isinstance(selected_record, dict)
            and predicate(selected_record, "historical") is not False
        )
        state, record = fresh.get(entity, identifier)
        if state != "ok":
            findings.append(
                {
                    "expected": expected,
                    "verdict": "inconclusive",
                    "reason": "drift: an unobserved expected cause could not be re-read",
                }
            )
            continue
        fresh_holds = predicate(record, "fresh") is True
        if selected_holds and fresh_holds and key not in claimed:
            findings.append(
                {
                    "expected": expected,
                    "verdict": "fail",
                    "reason": "an expected cause was omitted without a covering scan",
                }
            )
        elif selected_holds != fresh_holds:
            findings.append(
                {
                    "expected": expected,
                    "verdict": "inconclusive",
                    "reason": "drift: an unobserved expected cause changed",
                }
            )
        else:
            findings.append(
                {
                    "expected": expected,
                    "verdict": "pass",
                    "reason": "unobserved expected cause is accounted for",
                }
            )
    return _aggregate_findings(name, "all required observed causes are present", findings)


def _sales_get_failed(subject_calls: list[dict[str, Any]], sales_order_id: str) -> bool:
    errors = {"PermissionDenied", "ToolNotFound", "InvalidParams", "TransportError"}
    return any(
        call.get("phase") == "subject"
        and call.get("tool") == "SalesOrder.get"
        and call.get("arguments") == {"id": sales_order_id}
        and call.get("outcome") in errors
        for call in subject_calls
    )


def downstream_valid(
    claims: dict[str, Any],
    target_id: str,
    observations: dict[tuple[str, Any], list[dict[str, Any]]],
    subject_calls: list[dict[str, Any]],
    fresh: FreshReader,
    today: date,
) -> Verdict:
    name = "downstream_valid"
    work_order_claim = claims["work_order"]
    linked_sales_order_id = work_order_claim.get("sales_order_id")
    findings: list[dict[str, Any]] = []
    link_check = _compare_record(
        name=name,
        entity="WorkOrder",
        identifier=target_id,
        claimed_fields={"sales_order_id": linked_sales_order_id},
        observations=observations,
        fresh=fresh,
        today=today,
    )
    findings.append(
        {
            "check": "sales_order_link",
            "verdict": link_check["verdict"],
            "reason": link_check["reason"],
            "comparison": link_check,
        }
    )
    downstream = claims["downstream"]
    sales_claim = downstream["sales_order"]
    if linked_sales_order_id is None:
        if sales_claim is not None:
            findings.append(
                {
                    "check": "sales_order",
                    "verdict": "fail",
                    "reason": "sales order must be null for an unlinked target",
                }
            )
        else:
            findings.append(
                {
                    "check": "sales_order",
                    "verdict": "pass",
                    "reason": "unlinked target has no sales-order claim",
                }
            )
    elif sales_claim is None:
        if _sales_get_failed(subject_calls, linked_sales_order_id):
            findings.append(
                {
                    "check": "sales_order",
                    "verdict": "inconclusive",
                    "reason": "source_unavailable: linked sales order could not be read",
                }
            )
        else:
            findings.append(
                {
                    "check": "sales_order",
                    "verdict": "fail",
                    "reason": "linked sales order was omitted without a failed read",
                }
            )
    else:
        if sales_claim.get("id") != linked_sales_order_id:
            findings.append(
                {
                    "check": "sales_order_linkage",
                    "verdict": "fail",
                    "reason": "sales-order claim does not use the target link",
                }
            )
        else:
            sales_check = _compare_record(
                name=name,
                entity="SalesOrder",
                identifier=linked_sales_order_id,
                claimed_fields={"id": sales_claim.get("id"), "status": sales_claim.get("status")},
                observations=observations,
                fresh=fresh,
                today=today,
            )
            findings.append(
                {
                    "check": "sales_order",
                    "verdict": sales_check["verdict"],
                    "reason": sales_check["reason"],
                    "comparison": sales_check,
                }
            )
    item_id = work_order_claim.get("item_id")
    for index, consumer in enumerate(downstream["potential_consumers"]):
        if consumer["id"] == target_id:
            findings.append(
                {
                    "check": "consumer_identity",
                    "consumer_index": index,
                    "verdict": "fail",
                    "reason": "a work order cannot be its own downstream consumer",
                }
            )
        else:
            findings.append(
                {
                    "check": "consumer_identity",
                    "consumer_index": index,
                    "verdict": "pass",
                    "reason": "consumer differs from the target",
                }
            )
        if consumer.get("link") != "potential" or consumer.get("status") not in POTENTIAL_CONSUMER_STATES:
            findings.append(
                {
                    "check": "consumer_contract",
                    "consumer_index": index,
                    "verdict": "fail",
                    "reason": "a consumer has an invalid link or status",
                }
            )
        else:
            findings.append(
                {
                    "check": "consumer_contract",
                    "consumer_index": index,
                    "verdict": "pass",
                    "reason": "consumer link and status are valid",
                }
            )
        consumer_check = _compare_record(
            name=name,
            entity="WorkOrder",
            identifier=consumer["id"],
            claimed_fields={
                "id": consumer["id"],
                "status": consumer["status"],
                "bom_id": consumer["bom_id"],
            },
            observations=observations,
            fresh=fresh,
            today=today,
            predicate=lambda record, mode: record.get("status") in POTENTIAL_CONSUMER_STATES,
        )
        findings.append(
            {
                "check": "consumer_record",
                "consumer_index": index,
                "verdict": consumer_check["verdict"],
                "reason": consumer_check["reason"],
                "comparison": consumer_check,
            }
        )

        def bom_contains(record: dict[str, Any], mode: str) -> bool:
            del mode
            materials = record.get("materials")
            return isinstance(materials, list) and any(
                isinstance(material, dict) and json_equal(material.get("item_id"), item_id)
                for material in materials
            )

        bom_check = _compare_record(
            name=name,
            entity="BOM",
            identifier=consumer["bom_id"],
            claimed_fields={"id": consumer["bom_id"]},
            observations=observations,
            fresh=fresh,
            today=today,
            predicate=bom_contains,
        )
        findings.append(
            {
                "check": "consumer_bom",
                "consumer_index": index,
                "verdict": bom_check["verdict"],
                "reason": bom_check["reason"],
                "comparison": bom_check,
            }
        )
    return _aggregate_findings(name, "downstream claims match fresh linked records", findings)


def _bom_contains_item(record: dict[str, Any], item_id: Any) -> bool:
    materials = record.get("materials")
    return isinstance(materials, list) and any(
        isinstance(material, dict) and json_equal(material.get("item_id"), item_id)
        for material in materials
    )


def _observed_consumer_state(
    record: dict[str, Any],
    target_id: str,
    item_id: Any,
    observations: dict[tuple[str, Any], list[dict[str, Any]]],
    fresh_bom_state: str,
    fresh_matching_bom_ids: set[Any],
) -> bool | None:
    if json_equal(record.get("id"), target_id) or record.get("status") not in POTENTIAL_CONSUMER_STATES:
        return False
    bom_id = record.get("bom_id")
    bom_versions = observations.get(("BOM", bom_id), [])
    if bom_versions:
        matches = [_bom_contains_item(bom, item_id) for bom in bom_versions]
        if all(matches):
            return True
        if any(matches):
            return None
        return False
    if fresh_bom_state != "ok":
        return None
    return bom_id in fresh_matching_bom_ids


def downstream_complete(
    claims: dict[str, Any],
    target_id: str,
    observations: dict[tuple[str, Any], list[dict[str, Any]]],
    subject_calls: list[dict[str, Any]],
    fresh: FreshReader,
) -> Verdict:
    """Check potential-consumer completeness independently of the subject's answer."""
    name = "downstream_complete"
    target_versions = observations.get(("WorkOrder", target_id), [])
    if target_versions:
        item_id = target_versions[0].get("item_id")
        if any(not json_equal(version.get("item_id"), item_id) for version in target_versions[1:]):
            return _result(name, "inconclusive", "drift: target item changed during subject observation")
    else:
        target_state, target = fresh.get("WorkOrder", target_id)
        if target_state != "ok":
            return _result(name, "inconclusive", "target item is unavailable")
        item_id = target.get("item_id")
    if item_id is None:
        return _result(name, "pass", "no item to consume", findings=[])

    observed_matching_bom_ids = {
        identifier
        for (entity, identifier), versions in observations.items()
        if entity == "BOM" and any(_bom_contains_item(version, item_id) for version in versions)
    }
    fresh_bom_state, fresh_boms = fresh.list("BOM", {})
    fresh_matching_bom_ids: set[Any] = set()
    findings: list[dict[str, Any]] = []
    if fresh_bom_state == "ok":
        fresh_matching_bom_ids = {
            bom.get("id")
            for bom in fresh_boms
            if bom.get("id") is not None and _bom_contains_item(bom, item_id)
        }
    else:
        findings.append(
            {
                "check": "fresh_bom_scan",
                "branch": f"fresh_scan_{fresh_bom_state}",
                "verdict": "inconclusive",
                "reason": "fresh BOM scan was unavailable or incomplete",
            }
        )

    matching_bom_ids = observed_matching_bom_ids | fresh_matching_bom_ids
    candidate_ids: set[Any] = set()
    for (entity, identifier), versions in observations.items():
        if entity != "WorkOrder" or identifier == target_id:
            continue
        if any(version.get("bom_id") in matching_bom_ids for version in versions):
            candidate_ids.add(identifier)

    fresh_candidate_boms: dict[Any, set[Any]] = {}
    for bom_id in fresh_matching_bom_ids:
        work_order_state, work_orders = fresh.list("WorkOrder", {"bom_id": bom_id})
        if work_order_state != "ok":
            findings.append(
                {
                    "check": "fresh_work_order_scan",
                    "bom_id": bom_id,
                    "branch": f"fresh_scan_{work_order_state}",
                    "verdict": "inconclusive",
                    "reason": "fresh work-order scan was unavailable or incomplete",
                }
            )
            continue
        for work_order in work_orders:
            identifier = work_order.get("id")
            if (
                identifier is not None
                and not json_equal(identifier, target_id)
                and work_order.get("status") in POTENTIAL_CONSUMER_STATES
                and work_order.get("bom_id") == bom_id
            ):
                candidate_ids.add(identifier)
                fresh_candidate_boms.setdefault(identifier, set()).add(bom_id)

    claimed_ids = {consumer["id"] for consumer in claims["downstream"]["potential_consumers"]}
    for identifier in sorted(candidate_ids, key=str):
        candidate = {"id": identifier}
        if identifier in claimed_ids:
            findings.append(
                {
                    **candidate,
                    "branch": "claimed",
                    "verdict": "pass",
                    "reason": "potential consumer was claimed",
                }
            )
            continue
        versions = observations.get(("WorkOrder", identifier), [])
        if versions:
            states = [
                _observed_consumer_state(
                    version,
                    target_id,
                    item_id,
                    observations,
                    fresh_bom_state,
                    fresh_matching_bom_ids,
                )
                for version in versions
            ]
            if all(state is True for state in states):
                findings.append(
                    {
                        **candidate,
                        "branch": "observed_consumer_omitted",
                        "verdict": "fail",
                        "reason": "a subject-observed potential consumer was omitted",
                    }
                )
            elif any(state is True for state in states) or any(state is None for state in states):
                findings.append(
                    {
                        **candidate,
                        "branch": "observed_consumer_drift",
                        "verdict": "inconclusive",
                        "reason": "drift: consumer status or BOM changed during observation",
                    }
                )
            else:
                findings.append(
                    {
                        **candidate,
                        "branch": "observed_not_consumer",
                        "verdict": "pass",
                        "reason": "subject observations did not show a potential consumer",
                    }
                )
            continue

        bom_ids = fresh_candidate_boms.get(identifier, set())
        bom_scan_state = _list_scan_state(subject_calls, "BOM", [{}])
        work_order_scan_states = [
            _list_scan_state(subject_calls, "WorkOrder", [{}, {"bom_id": bom_id}])
            for bom_id in bom_ids
        ]
        if bom_scan_state == "complete" and "complete" in work_order_scan_states:
            findings.append(
                {
                    **candidate,
                    "branch": "appeared_after_complete_scan",
                    "verdict": "inconclusive",
                    "reason": "drift: consumer appeared after complete subject scans",
                }
            )
        elif bom_scan_state == "unavailable" or "unavailable" in work_order_scan_states:
            findings.append(
                {
                    **candidate,
                    "branch": "subject_read_unavailable",
                    "verdict": "inconclusive",
                    "reason": "subject_read_unavailable",
                }
            )
        else:
            findings.append(
                {
                    **candidate,
                    "branch": "unobserved_without_complete_scan",
                    "verdict": "fail",
                    "reason": "a fresh potential consumer was omitted without complete subject scans",
                }
            )
    return _aggregate_findings(name, "all potential consumers are accounted for", findings)


def refusal_valid(
    task: dict[str, Any],
    answer: dict[str, Any],
    target_id: str | None,
    fresh: FreshReader,
) -> Verdict:
    name = "refusal_valid"
    if answer.get("outcome") != "refused" or answer.get("claims") is not None:
        return _result(name, "fail", "task required a refusal")
    expected = task["expected"].get("refusal_reason")
    expected_reasons = set(expected) if isinstance(expected, list) else {expected}
    if answer.get("refusal_reason") not in expected_reasons:
        return _result(name, "fail", "refusal reason does not match the task")
    if task["id"] == "refuse_not_found":
        state, value = fresh.get("WorkOrder", target_id)
        if state == "not_found":
            return _result(name, "pass", "fresh verification confirms the target is not found")
        if state == "ok":
            return _result(name, "fail", "fresh verification found the refused target", fresh=value)
        return _result(name, "inconclusive", "not-found refusal could not be verified", error=value)
    state, catalogue = fresh.catalogue()
    if state != "ok":
        return _result(name, "inconclusive", "fresh tool catalogue unavailable")
    restricted = {"StockLedger", "StockEntry", "Warehouse"}
    present = {
        tool.get("name", "").split(".", 1)[0]
        for tool in catalogue
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    }
    if restricted & present:
        return _result(name, "fail", "outside-seat refusal is contradicted by the fresh catalogue")
    return _result(name, "pass", "fresh catalogue confirms the requested entities are outside the seat")


def no_writes(calls: list[dict[str, Any]], fresh: FreshReader) -> Verdict:
    name = "no_writes"
    if any(call.get("outcome") == "refused_write" for call in calls):
        return _result(name, "fail", "the subject or harness attempted a write tool")
    state, catalogue = fresh.catalogue()
    if state != "ok":
        return _result(name, "inconclusive", "tool catalogue unavailable for read-only audit")
    by_name = {
        tool.get("name"): tool
        for tool in catalogue
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    }
    for call in calls:
        if call.get("kind") != "call_tool" or call.get("outcome") != "ok":
            continue
        tool = by_name.get(call.get("tool"))
        annotations = tool.get("annotations") if isinstance(tool, dict) else None
        if not isinstance(annotations, dict) or annotations.get("readOnlyHint") is not True:
            return _result(name, "fail", "an executed tool was not explicitly read-only")
    return _result(name, "pass", "all executed tools were explicitly read-only")


__all__ = [
    "FreshReader",
    "answered_task_refusal",
    "causes_valid",
    "downstream_complete",
    "downstream_valid",
    "expected_causes_present",
    "lateness_correct",
    "no_writes",
    "refusal_valid",
    "target_still_eligible",
    "validate_answer",
    "work_order_matches",
]
