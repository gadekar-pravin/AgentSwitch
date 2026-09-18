"""Read-only investigation of a late work order.

``investigate`` returns a JSON-serialisable mapping with ``found``,
``work_order_id``, ``today``, ``started_at``, ``finished_at``, ``work_order``,
``lateness``, ``causes``, ``downstream``, ``unknowns``, ``blocked_sources``,
``evidence_log``, and ``calls``.  ``downstream`` contains ``sales_order`` and
``potential_consumers``.
Cause candidates are evidence-led leads, not proven causes.  A false ``found``
value means the target was not obtained; callers must inspect ``calls`` and
``blocked_sources`` before concluding that the record does not exist.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from .mcp_client import InvalidParams, McpClient, PermissionDenied, ProtocolError, ToolNotFound

_PAGE_LIMIT = 1000

_WORK_ORDER_STATES = {
    "draft",
    "not_started",
    "in_progress",
    "completed",
    "stopped",
    "cancelled",
}
_OPEN_WORK_ORDER_STATES = {"not_started", "in_progress", "stopped"}
_POTENTIAL_CONSUMER_STATES = {"draft", "not_started", "in_progress", "stopped"}
_MATERIAL_REQUEST_STATES = {
    "draft",
    "submitted",
    "partially_ordered",
    "ordered",
    "received",
    "cancelled",
}
_SUBCONTRACT_ORDER_STATES = {
    "draft",
    "submitted",
    "materials_sent",
    "in_progress",
    "received",
    "quality_check",
    "completed",
    "cancelled",
}
_JOB_CARD_STATES = {"open", "in_progress", "completed", "cancelled"}
_QUALITY_INSPECTION_STATES = {"draft", "in_progress", "completed"}
_QUALITY_RESULTS = {"accepted", "rejected", "conditional", None}
_ECO_STATES = {"draft", "submitted", "under_review", "approved", "implemented", "rejected"}
_ECO_ACTIONS = {"continue_old", "switch_to_new", "scrap_and_restart"}
_WORKSTATION_STATES = {"active", "under_maintenance", "decommissioned"}
_SALES_ORDER_STATES = {"draft", "confirmed", "partially_delivered", "delivered", "cancelled"}

_WORK_ORDER_SNAPSHOT_FIELDS = (
    "id",
    "number",
    "status",
    "docstatus",
    "item_id",
    "_item_id_display",
    "bom_id",
    "sales_order_id",
    "qty",
    "produced_qty",
    "planned_start_date",
    "planned_end_date",
    "actual_start_date",
    "actual_end_date",
    "priority",
    "approval_status",
    "quality_inspection_required",
    "updated_at",
)


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) <= 10:
        return None
    rendered = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _is_reversed_interval(start: Any, end: Any) -> bool | None:
    if start is None or end is None:
        return None
    start_is_date = isinstance(start, str) and len(start) == 10
    end_is_date = isinstance(end, str) and len(end) == 10
    if start_is_date or end_is_date:
        start_date = _parse_date(start)
        end_date = _parse_date(end)
        if start_date is None or end_date is None:
            return None
        return end_date < start_date
    start_datetime = _parse_datetime(start)
    end_datetime = _parse_datetime(end)
    if start_datetime is None or end_datetime is None:
        return None
    return end_datetime < start_datetime


def _record_label(entity: str, record: dict[str, Any]) -> str:
    identifier = record.get("id")
    return f"{entity} {identifier!r}" if identifier is not None else entity


def _add_unknown(unknowns: list[str], message: str) -> None:
    if message not in unknowns:
        unknowns.append(message)


def _check_state(
    entity: str,
    record: dict[str, Any],
    known_states: set[Any],
    unknowns: list[str],
) -> bool:
    state = record.get("status")
    if state in known_states:
        return True
    _add_unknown(unknowns, f"{_record_label(entity, record)} has unknown status {state!r}.")
    return False


def _evidence_ref(entity: str, record: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    identifier = record.get("id")
    if identifier is None and entity == "FiniteScheduleOrder":
        identifier = record.get("work_order_id")
    return {
        "entity": entity,
        "id": identifier,
        "number": record.get("number"),
        "fields": {field: record.get(field) for field in fields},
    }


def _cause(
    code: str,
    basis: str,
    summary: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    return {"code": code, "basis": basis, "summary": summary, "evidence": evidence}


def _dated_evidence_ref(
    entity: str,
    record: dict[str, Any],
    fields: tuple[str, ...],
    *,
    overdue: bool,
) -> dict[str, Any]:
    reference = _evidence_ref(entity, record, fields)
    reference["fields"]["overdue"] = overdue
    return reference


def analyze_lateness(work_order: dict[str, Any], *, today: date) -> dict[str, Any]:
    """Apply the status-first lateness rule to an already-fetched work order."""
    status = work_order.get("status")
    planned_end = _parse_date(work_order.get("planned_end_date"))
    planned_start = _parse_date(work_order.get("planned_start_date"))
    draft_past_end = status == "draft" and planned_end is not None and planned_end < today
    past_start_not_started = (
        status in {"draft", "not_started"} and planned_start is not None and planned_start < today
    )

    is_late: bool | None
    days_late: int | None = None
    if status in {"draft", "completed", "cancelled"}:
        is_late = False
    elif status in _OPEN_WORK_ORDER_STATES:
        if planned_end is None:
            is_late = None
        else:
            is_late = planned_end < today
            if is_late:
                days_late = (today - planned_end).days
    else:
        is_late = None

    return {
        "is_late": is_late,
        "days_late": days_late,
        "draft_past_planned_end": draft_past_end,
        "past_planned_start_not_started": past_start_not_started,
        "rule": (
            "Status is evaluated first: draft, completed, and cancelled are not late; "
            "not_started, in_progress, and stopped are late only when planned_end_date is before today."
        ),
        "today": today.isoformat(),
    }


def analyze_causes(
    work_order: dict[str, Any],
    *,
    material_requests: list[dict[str, Any]],
    subcontract_orders: list[dict[str, Any]],
    job_cards: list[dict[str, Any]],
    downtime_entries: list[dict[str, Any]],
    quality_inspections: list[dict[str, Any]],
    engineering_change_orders: list[dict[str, Any]],
    bom: dict[str, Any] | None,
    workstations: list[dict[str, Any]],
    schedule_order: dict[str, Any] | None,
    today: date,
) -> dict[str, list[Any]]:
    """Derive candidate causes and unknowns from already-fetched records."""
    causes: list[dict[str, Any]] = []
    unknowns: list[str] = []
    target_id = work_order.get("id")
    _add_unknown(
        unknowns,
        "Unlinked downtime on shared workstations is not attributed to this work order.",
    )

    if _check_state("WorkOrder", work_order, _WORK_ORDER_STATES, unknowns):
        work_order_status = work_order.get("status")
        if work_order_status in _OPEN_WORK_ORDER_STATES and _parse_date(
            work_order.get("planned_end_date")
        ) is None:
            _add_unknown(
                unknowns,
                "The open work order has a missing or unparseable planned_end_date.",
            )
        if work_order_status == "stopped":
            causes.append(
                _cause(
                    "work_order_stopped",
                    "observed",
                    "The work order is recorded as stopped.",
                    [_evidence_ref("WorkOrder", work_order, ("status",))],
                )
            )

    for request in material_requests:
        if not _check_state("MaterialRequest", request, _MATERIAL_REQUEST_STATES, unknowns):
            continue
        status = request.get("status")
        required_by = _parse_date(request.get("required_by_date"))
        overdue = required_by is not None and required_by < today
        if status in {"submitted", "partially_ordered", "ordered"}:
            causes.append(
                _cause(
                    "material_request_open",
                    "observed",
                    "A linked material request remains unresolved.",
                    [
                        _dated_evidence_ref(
                            "MaterialRequest",
                            request,
                            ("status", "required_by_date", "items"),
                            overdue=overdue,
                        )
                    ],
                )
            )
        elif status == "draft":
            causes.append(
                _cause(
                    "material_request_draft",
                    "possible",
                    "A linked material request is still a draft.",
                    [_evidence_ref("MaterialRequest", request, ("status", "required_by_date", "items"))],
                )
            )

    for order in subcontract_orders:
        if not _check_state("SubcontractOrder", order, _SUBCONTRACT_ORDER_STATES, unknowns):
            continue
        status = order.get("status")
        expected = _parse_date(order.get("expected_delivery_date"))
        overdue = expected is not None and expected < today
        fields = ("status", "expected_delivery_date", "actual_delivery_date")
        if status in {"submitted", "materials_sent", "in_progress"}:
            causes.append(
                _cause(
                    "subcontract_awaiting_receipt",
                    "observed",
                    "A linked subcontract order has not reached receipt.",
                    [_dated_evidence_ref("SubcontractOrder", order, fields, overdue=overdue)],
                )
            )
        elif status in {"received", "quality_check"}:
            causes.append(
                _cause(
                    "subcontract_not_completed",
                    "observed",
                    "A linked subcontract order is received or in quality check but not completed.",
                    [_evidence_ref("SubcontractOrder", order, fields)],
                )
            )
        elif status == "draft":
            causes.append(
                _cause(
                    "subcontract_draft",
                    "possible",
                    "A linked subcontract order is still a draft.",
                    [_evidence_ref("SubcontractOrder", order, fields)],
                )
            )

    open_job_cards: list[dict[str, Any]] = []
    for card in job_cards:
        if not _check_state("JobCard", card, _JOB_CARD_STATES, unknowns):
            continue
        if card.get("status") not in {"open", "in_progress"}:
            continue
        open_job_cards.append(card)
        planned_end = _parse_date(card.get("planned_end"))
        overdue = planned_end is not None and planned_end < today
        fields = (
            "status",
            "planned_end",
            "for_qty",
            "completed_qty",
            "workstation_id",
        )
        causes.append(
            _cause(
                "job_card_overdue" if overdue else "job_card_open",
                "observed" if overdue else "context",
                "An open job card is past its planned end."
                if overdue
                else "A linked job card remains open but is not known to be overdue.",
                [_evidence_ref("JobCard", card, fields)],
            )
        )

    job_card_ids = {card.get("id") for card in job_cards if card.get("id") is not None}
    seen_downtime_ids: set[Any] = set()
    for entry in downtime_entries:
        entry_id = entry.get("id")
        if entry_id in seen_downtime_ids:
            continue
        seen_downtime_ids.add(entry_id)
        linked = entry.get("work_order_id") == target_id or entry.get("job_card_id") in job_card_ids
        if not linked:
            continue
        reversed_interval = _is_reversed_interval(entry.get("from_time"), entry.get("to_time"))
        if reversed_interval is None:
            _add_unknown(
                unknowns,
                f"{_record_label('DowntimeEntry', entry)} has a missing or unparseable interval.",
            )
        elif reversed_interval:
            _add_unknown(unknowns, f"{_record_label('DowntimeEntry', entry)} has a reversed interval.")
        causes.append(
            _cause(
                "recorded_downtime",
                "possible",
                "A recorded interruption is linked to the order; contribution to the delay is unverified.",
                [
                    _evidence_ref(
                        "DowntimeEntry",
                        entry,
                        (
                            "work_order_id",
                            "job_card_id",
                            "reason",
                            "downtime_mins",
                            "from_time",
                            "to_time",
                            "workstation_id",
                            "remarks",
                        ),
                    )
                ],
            )
        )

    for change in engineering_change_orders:
        affected = change.get("affected_work_orders")
        if not isinstance(affected, list):
            affected = []
        links = [item for item in affected if isinstance(item, dict) and item.get("work_order_id") == target_id]
        if not links:
            continue
        _add_unknown(unknowns, "Whether an engineering change holds a work order is untested.")
        if not _check_state("EngineeringChangeOrder", change, _ECO_STATES, unknowns):
            continue
        status = change.get("status")
        for link in links:
            action = link.get("action")
            if action not in _ECO_ACTIONS:
                _add_unknown(
                    unknowns,
                    f"{_record_label('EngineeringChangeOrder', change)} has unknown action {action!r}.",
                )
                continue
            if status in {"draft", "rejected"}:
                continue
            evidence_reference = _evidence_ref(
                "EngineeringChangeOrder",
                change,
                ("status", "bom_id", "effectivity_date"),
            )
            evidence_reference["fields"]["work_order_id"] = link.get("work_order_id")
            evidence_reference["fields"]["action"] = action
            evidence = [evidence_reference]
            if status in {"submitted", "under_review", "approved"} and action in {
                "switch_to_new",
                "scrap_and_restart",
            }:
                causes.append(
                    _cause(
                        "eco_possible_hold",
                        "possible",
                        "A pending or approved engineering change calls for disruptive action.",
                        evidence,
                    )
                )
            elif action == "continue_old" or status == "implemented":
                causes.append(
                    _cause(
                        "eco_affects_order",
                        "context",
                        "An engineering change is linked to the work order.",
                        evidence,
                    )
                )

    if quality_inspections:
        _add_unknown(unknowns, "Whether quality inspections gate work-order transitions is untested.")
    for inspection in quality_inspections:
        if not _check_state("QualityInspection", inspection, _QUALITY_INSPECTION_STATES, unknowns):
            continue
        result = inspection.get("overall_result")
        if result not in _QUALITY_RESULTS:
            _add_unknown(
                unknowns,
                f"{_record_label('QualityInspection', inspection)} has unknown result {result!r}.",
            )
            continue
        status = inspection.get("status")
        evidence = [
            _evidence_ref("QualityInspection", inspection, ("status", "overall_result")),
            _evidence_ref("WorkOrder", work_order, ("quality_inspection_required",)),
        ]
        if status == "completed" and result == "rejected":
            causes.append(
                _cause("quality_rejected", "observed", "A linked quality inspection was rejected.", evidence)
            )
        elif status in {"draft", "in_progress"}:
            causes.append(
                _cause(
                    "quality_inspection_pending",
                    "possible",
                    "A linked quality inspection is pending.",
                    evidence,
                )
            )
        elif status == "completed" and result == "conditional":
            causes.append(
                _cause(
                    "quality_conditional",
                    "context",
                    "A linked quality inspection completed with a conditional result.",
                    evidence,
                )
            )
        elif status == "completed" and result is None:
            _add_unknown(
                unknowns,
                f"{_record_label('QualityInspection', inspection)} is completed without an overall result.",
            )

    workstation_ids: list[Any] = []
    for card in open_job_cards:
        workstation_id = card.get("workstation_id")
        if workstation_id is not None and workstation_id not in workstation_ids:
            workstation_ids.append(workstation_id)
    operations = bom.get("operations") if isinstance(bom, dict) else []
    if isinstance(operations, list):
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            workstation_id = operation.get("workstation_id")
            if workstation_id is not None and workstation_id not in workstation_ids:
                workstation_ids.append(workstation_id)

    workstations_by_id = {row.get("id"): row for row in workstations if row.get("id") is not None}
    for workstation_id in workstation_ids:
        workstation = workstations_by_id.get(workstation_id)
        if workstation is None:
            _add_unknown(unknowns, f"Workstation {workstation_id!r} was not found in Workstation.list.")
            continue
        if not _check_state("Workstation", workstation, _WORKSTATION_STATES, unknowns):
            continue
        if workstation.get("status") in {"under_maintenance", "decommissioned"}:
            causes.append(
                _cause(
                    "workstation_unavailable",
                    "possible",
                    "A workstation used by the order is unavailable.",
                    [_evidence_ref("Workstation", workstation, ("status", "is_active"))],
                )
            )

    if schedule_order is not None:
        causes.append(
            _cause(
                "schedule_verdict",
                "context",
                "The finite-schedule endpoint returned a verdict for this work order.",
                [
                    _evidence_ref(
                        "FiniteScheduleOrder",
                        schedule_order,
                        ("verdict", "verdict_code", "days_late", "projected_finish"),
                    )
                ],
            )
        )
        _add_unknown(unknowns, "The finite-schedule projected_finish value is not a forecast.")

    return {"causes": causes, "unknowns": unknowns}


def analyze_downstream(
    work_order: dict[str, Any],
    *,
    sales_order: dict[str, Any] | None,
    boms: list[dict[str, Any]],
    work_orders_by_bom: dict[Any, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Derive confirmed sales context and one-hop potential consumers."""
    unknowns: list[str] = []
    sales_finding: dict[str, Any] | None = None
    if sales_order is not None:
        status = sales_order.get("status")
        if status not in _SALES_ORDER_STATES:
            exposure = None
            _add_unknown(
                unknowns,
                f"{_record_label('SalesOrder', sales_order)} has unknown status {status!r}.",
            )
        elif status in {"confirmed", "partially_delivered"}:
            exposure = True
        elif status in {"delivered", "cancelled"}:
            exposure = False
        else:
            exposure = None
        sales_finding = {
            "id": sales_order.get("id"),
            "number": sales_order.get("number"),
            "status": status,
            "delivery_date": sales_order.get("delivery_date"),
            "expected_shipment_date": sales_order.get("expected_shipment_date"),
            "delivered_status": sales_order.get("delivered_status"),
            "link": "confirmed",
            "open_customer_exposure": exposure,
        }
        _add_unknown(
            unknowns,
            "Delivery risk attributable to this work order is unverified; sales exposure reflects only sales-order status.",
        )

    item_id = work_order.get("item_id")
    matching_bom_ids: list[Any] = []
    if item_id is not None:
        for bom in boms:
            materials = bom.get("materials")
            if not isinstance(materials, list):
                continue
            if any(isinstance(material, dict) and material.get("item_id") == item_id for material in materials):
                bom_id = bom.get("id")
                if bom_id is not None and bom_id not in matching_bom_ids:
                    matching_bom_ids.append(bom_id)

    target_id = work_order.get("id")
    target_end = _parse_date(work_order.get("planned_end_date"))
    consumers: list[dict[str, Any]] = []
    seen_ids: set[Any] = set()
    for bom_id in matching_bom_ids:
        for candidate in work_orders_by_bom.get(bom_id, []):
            candidate_id = candidate.get("id")
            if candidate_id == target_id or candidate_id in seen_ids:
                continue
            status = candidate.get("status")
            if status not in _WORK_ORDER_STATES:
                _add_unknown(
                    unknowns,
                    f"{_record_label('WorkOrder', candidate)} has unknown status {status!r}.",
                )
                continue
            if status not in _POTENTIAL_CONSUMER_STATES:
                continue
            seen_ids.add(candidate_id)
            candidate_start = _parse_date(candidate.get("planned_start_date"))
            starts_before = (
                candidate_start < target_end
                if candidate_start is not None and target_end is not None
                else None
            )
            consumers.append(
                {
                    "id": candidate_id,
                    "number": candidate.get("number"),
                    "status": status,
                    "planned_start_date": candidate.get("planned_start_date"),
                    "sales_order_id": candidate.get("sales_order_id"),
                    "bom_id": candidate.get("bom_id"),
                    "starts_before_target_ends": starts_before,
                    "link": "potential",
                }
            )

    _add_unknown(unknowns, "Stock or another work order may supply the item used by potential consumers.")
    return {
        "downstream": {"sales_order": sales_finding, "potential_consumers": consumers},
        "unknowns": unknowns,
    }


class _Collector:
    def __init__(self, client: McpClient) -> None:
        self.client = client
        self.calls: list[dict[str, Any]] = []
        self.blocked_sources: list[dict[str, str]] = []
        self.evidence_log: list[dict[str, Any]] = []
        self._evidence_keys: set[tuple[str, Any]] = set()

    def _log_call(self, tool: str, arguments: dict[str, Any], outcome: str) -> None:
        self.calls.append({"tool": tool, "arguments": dict(arguments), "outcome": outcome})

    def _block(self, tool: str, error: PermissionDenied | ToolNotFound) -> None:
        self.blocked_sources.append(
            {"tool": tool, "error_type": type(error).__name__, "message": str(error)}
        )

    def _evidence(self, entity: str, record: dict[str, Any], tool: str) -> None:
        identifier = record.get("id")
        if identifier is None and entity == "FiniteScheduleOrder":
            identifier = record.get("work_order_id")
        key = (entity, identifier)
        if key in self._evidence_keys:
            return
        self._evidence_keys.add(key)
        self.evidence_log.append(
            {
                "entity": entity,
                "id": identifier,
                "number": record.get("number"),
                "updated_at": record.get("updated_at"),
                "tool": tool,
            }
        )

    def get(
        self,
        tool: str,
        identifier: Any,
        *,
        entity: str,
    ) -> tuple[dict[str, Any] | None, str]:
        arguments = {"id": identifier}
        try:
            result = self.client.call_tool(tool, arguments)
        except InvalidParams as error:
            if "not found" not in error.message.lower():
                raise
            self._log_call(tool, arguments, "not_found")
            return None, "not_found"
        except PermissionDenied as error:
            self._log_call(tool, arguments, "denied")
            self._block(tool, error)
            return None, "denied"
        except ToolNotFound as error:
            self._log_call(tool, arguments, "unavailable")
            self._block(tool, error)
            return None, "unavailable"
        record = result.structured
        if not isinstance(record, dict):
            raise ProtocolError(f"{tool} structured result must be a record object")
        self._log_call(tool, arguments, "ok")
        self._evidence(entity, record, tool)
        return record, "ok"

    def list(
        self,
        tool: str,
        filters: dict[str, Any],
        *,
        entity: str,
    ) -> tuple[list[dict[str, Any]], str]:
        records: list[dict[str, Any]] = []
        seen_ids: set[Any] = set()
        offset = 0
        while True:
            arguments = dict(filters)
            arguments.update({"limit": _PAGE_LIMIT, "offset": offset})
            try:
                result = self.client.call_tool(tool, arguments)
            except PermissionDenied as error:
                self._log_call(tool, arguments, "denied")
                self._block(tool, error)
                return records, "denied"
            except ToolNotFound as error:
                self._log_call(tool, arguments, "unavailable")
                self._block(tool, error)
                return records, "unavailable"

            envelope = result.structured
            if not isinstance(envelope, dict):
                raise ProtocolError(f"{tool} structured result must be a list envelope object")
            page = envelope.get("data")
            total = envelope.get("total")
            if (
                not isinstance(page, list)
                or not isinstance(total, int)
                or isinstance(total, bool)
                or total < 0
            ):
                raise ProtocolError(f"{tool} list envelope must contain a data list and non-negative total")
            if any(not isinstance(record, dict) for record in page):
                raise ProtocolError(f"{tool} list data must contain record objects")
            if not page and len(records) < total:
                self._log_call(tool, arguments, "incomplete")
                return records, "incomplete"

            self._log_call(tool, arguments, "ok")
            added_records = 0
            for record in page:
                identifier = record.get("id")
                if identifier is not None:
                    if identifier in seen_ids:
                        continue
                    seen_ids.add(identifier)
                records.append(record)
                added_records += 1
                self._evidence(entity, record, tool)
            if len(records) >= total:
                return records, "ok"
            if page and added_records == 0:
                self.calls[-1]["outcome"] = "incomplete"
                return records, "incomplete"
            new_offset = offset + len(page)
            if new_offset <= offset:
                self.calls[-1]["outcome"] = "incomplete"
                return records, "incomplete"
            offset = new_offset

    def endpoint(self, tool: str) -> tuple[dict[str, Any] | None, str]:
        arguments: dict[str, Any] = {}
        try:
            result = self.client.call_tool(tool, arguments)
        except PermissionDenied as error:
            self._log_call(tool, arguments, "denied")
            self._block(tool, error)
            return None, "denied"
        except ToolNotFound as error:
            self._log_call(tool, arguments, "unavailable")
            self._block(tool, error)
            return None, "unavailable"
        envelope = result.structured
        if (
            not isinstance(envelope, dict)
            or envelope.get("status") != "ok"
            or not isinstance(envelope.get("result"), dict)
        ):
            raise ProtocolError(f"{tool} structured result must contain status 'ok' and a result object")
        self._log_call(tool, arguments, "ok")
        return envelope["result"], "ok"


def _snapshot(work_order: dict[str, Any]) -> dict[str, Any]:
    snapshot = {
        field: work_order.get(field)
        for field in _WORK_ORDER_SNAPSHOT_FIELDS
        if field != "_item_id_display"
    }
    if "_item_id_display" in work_order:
        snapshot["_item_id_display"] = work_order["_item_id_display"]
    return snapshot


def _empty_result(
    work_order_id: Any,
    today: date,
    started_at: str,
    collector: _Collector,
    unknowns: list[str],
) -> dict[str, Any]:
    return {
        "found": False,
        "work_order_id": work_order_id,
        "today": today.isoformat(),
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "work_order": None,
        "lateness": None,
        "causes": [],
        "downstream": {"sales_order": None, "potential_consumers": []},
        "unknowns": unknowns,
        "blocked_sources": collector.blocked_sources,
        "evidence_log": collector.evidence_log,
        "calls": collector.calls,
    }


def investigate(client: McpClient, work_order_id: Any, *, today: date) -> dict[str, Any]:
    """Collect read-only evidence about lateness and downstream relationships."""
    started_at = datetime.now(timezone.utc).isoformat()
    collector = _Collector(client)
    work_order, target_outcome = collector.get("WorkOrder.get", work_order_id, entity="WorkOrder")
    if work_order is None:
        if target_outcome == "not_found":
            unknowns = ["The target work order was not found."]
        else:
            unknowns = ["The target work order could not be obtained from the available sources."]
        return _empty_result(work_order_id, today, started_at, collector, unknowns)

    source_unknowns: list[str] = []

    def collect_list(
        tool: str,
        filters: dict[str, Any],
        *,
        entity: str,
    ) -> list[dict[str, Any]]:
        rows, outcome = collector.list(tool, filters, entity=entity)
        if outcome == "incomplete":
            _add_unknown(source_unknowns, f"{tool} returned incomplete paginated results.")
        elif outcome in {"denied", "unavailable"}:
            _add_unknown(source_unknowns, f"{tool} could not be read ({outcome}).")
        return rows

    material_requests = collect_list(
        "MaterialRequest.list",
        {"work_order_id": work_order_id},
        entity="MaterialRequest",
    )
    subcontract_orders = collect_list(
        "SubcontractOrder.list",
        {"work_order_id": work_order_id},
        entity="SubcontractOrder",
    )
    job_cards = collect_list("JobCard.list", {"work_order_id": work_order_id}, entity="JobCard")

    downtime_entries = collect_list(
        "DowntimeEntry.list",
        {"work_order_id": work_order_id},
        entity="DowntimeEntry",
    )
    for job_card in job_cards:
        job_card_id = job_card.get("id")
        if job_card_id is not None:
            downtime_entries.extend(
                collect_list(
                    "DowntimeEntry.list",
                    {"job_card_id": job_card_id},
                    entity="DowntimeEntry",
                )
            )

    quality_inspections = collect_list(
        "QualityInspection.list",
        {"reference_type": "WorkOrder", "reference_id": work_order_id},
        entity="QualityInspection",
    )
    engineering_changes = collect_list(
        "EngineeringChangeOrder.list",
        {},
        entity="EngineeringChangeOrder",
    )

    bom: dict[str, Any] | None = None
    bom_id = work_order.get("bom_id")
    if bom_id is not None:
        bom, bom_outcome = collector.get("BOM.get", bom_id, entity="BOM")
        if bom is None:
            _add_unknown(source_unknowns, f"BOM.get could not obtain the target BOM ({bom_outcome}).")

    boms = collect_list("BOM.list", {}, entity="BOM")
    workstations = collect_list("Workstation.list", {}, entity="Workstation")

    sales_order: dict[str, Any] | None = None
    sales_order_id = work_order.get("sales_order_id")
    if sales_order_id is not None:
        sales_order, sales_outcome = collector.get("SalesOrder.get", sales_order_id, entity="SalesOrder")
        if sales_order is None:
            _add_unknown(
                source_unknowns,
                f"SalesOrder.get could not obtain the linked sales order ({sales_outcome}).",
            )

    matching_bom_ids: list[Any] = []
    item_id = work_order.get("item_id")
    if item_id is not None:
        for candidate_bom in boms:
            materials = candidate_bom.get("materials")
            if not isinstance(materials, list):
                continue
            if any(
                isinstance(material, dict) and material.get("item_id") == item_id
                for material in materials
            ):
                candidate_bom_id = candidate_bom.get("id")
                if candidate_bom_id is not None and candidate_bom_id not in matching_bom_ids:
                    matching_bom_ids.append(candidate_bom_id)
    work_orders_by_bom: dict[Any, list[dict[str, Any]]] = {}
    for consumer_bom_id in matching_bom_ids:
        work_orders_by_bom[consumer_bom_id] = collect_list(
            "WorkOrder.list",
            {"bom_id": consumer_bom_id},
            entity="WorkOrder",
        )

    schedule_result, schedule_outcome = collector.endpoint("endpoint.manufacturing.finite_schedule")
    if schedule_result is None:
        _add_unknown(
            source_unknowns,
            f"endpoint.manufacturing.finite_schedule could not be read ({schedule_outcome}).",
        )
    schedule_order: dict[str, Any] | None = None
    if schedule_result is not None:
        orders = schedule_result.get("orders")
        if not isinstance(orders, list) or any(not isinstance(row, dict) for row in orders):
            raise ProtocolError("finite_schedule result must contain an orders list of objects")
        for row in orders:
            collector._evidence("FiniteScheduleOrder", row, "endpoint.manufacturing.finite_schedule")
            if schedule_order is None and row.get("work_order_id") == work_order_id:
                schedule_order = row

    lateness = analyze_lateness(work_order, today=today)
    cause_findings = analyze_causes(
        work_order,
        material_requests=material_requests,
        subcontract_orders=subcontract_orders,
        job_cards=job_cards,
        downtime_entries=downtime_entries,
        quality_inspections=quality_inspections,
        engineering_change_orders=engineering_changes,
        bom=bom,
        workstations=workstations,
        schedule_order=schedule_order,
        today=today,
    )
    downstream_findings = analyze_downstream(
        work_order,
        sales_order=sales_order,
        boms=boms,
        work_orders_by_bom=work_orders_by_bom,
    )
    unknowns = source_unknowns
    for message in cause_findings["unknowns"] + downstream_findings["unknowns"]:
        _add_unknown(unknowns, message)
    _add_unknown(
        unknowns,
        "Material availability was not checked: its tool is not read-only and the seat cannot read the stock ledger.",
    )

    return {
        "found": True,
        "work_order_id": work_order_id,
        "today": today.isoformat(),
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "work_order": _snapshot(work_order),
        "lateness": lateness,
        "causes": cause_findings["causes"],
        "downstream": downstream_findings["downstream"],
        "unknowns": unknowns,
        "blocked_sources": collector.blocked_sources,
        "evidence_log": collector.evidence_log,
        "calls": collector.calls,
    }


__all__ = ["analyze_causes", "analyze_downstream", "analyze_lateness", "investigate"]
