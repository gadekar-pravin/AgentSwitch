"""Construct classified answers from agent-read manufacturing records."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .investigate import analyze_causes, analyze_downstream, analyze_lateness, matching_bom_ids


def is_hashable(value: Any) -> bool:
    try:
        hash(value)
    except TypeError:
        return False
    return True


@dataclass
class Store:
    records: dict[str, dict[Any, list[dict[str, Any]]]] = field(default_factory=dict)
    list_calls: list[dict[str, Any]] = field(default_factory=list)
    endpoints: dict[str, list[Any]] = field(default_factory=dict)
    endpoint_calls: list[dict[str, Any]] = field(default_factory=list)
    successful_gets: list[dict[str, Any]] = field(default_factory=list)
    target_before_reschedule: dict[str, Any] | None = None
    target_snapshot_pinned: bool = False

    def merge(self, other: Store) -> None:
        """Append a private read fragment without changing target snapshot state."""
        if other.target_snapshot_pinned:
            raise ValueError("cannot merge a store with a pinned target snapshot")
        for entity, records in other.records.items():
            destination = self.records.setdefault(entity, {})
            for identifier, versions in records.items():
                destination.setdefault(identifier, []).extend(copy.deepcopy(versions))
        self.list_calls.extend(copy.deepcopy(other.list_calls))
        for tool, values in other.endpoints.items():
            self.endpoints.setdefault(tool, []).extend(copy.deepcopy(values))
        self.endpoint_calls.extend(copy.deepcopy(other.endpoint_calls))
        self.successful_gets.extend(copy.deepcopy(other.successful_gets))

    def add_record(self, entity: str, record: dict[str, Any]) -> None:
        identifier = record.get("id")
        if identifier is None or not is_hashable(identifier):
            return
        self.records.setdefault(entity, {}).setdefault(identifier, []).append(copy.deepcopy(record))

    def add_get(
        self,
        tool: str,
        arguments: dict[str, Any],
        record: dict[str, Any],
        *,
        model_read: bool,
    ) -> None:
        self.add_record(tool.rsplit(".", 1)[0], record)
        self.successful_gets.append(
            {
                "tool": tool,
                "arguments": dict(arguments),
                "record": copy.deepcopy(record),
                "model_read": model_read,
            }
        )

    def add_list(
        self,
        tool: str,
        filters: dict[str, Any],
        rows: list[dict[str, Any]],
        *,
        complete: bool,
    ) -> None:
        entity = tool.rsplit(".", 1)[0]
        for row in rows:
            self.add_record(entity, row)
        self.list_calls.append(
            {"tool": tool, "filters": copy.deepcopy(filters), "complete": complete}
        )

    def latest(self, entity: str, identifier: Any) -> dict[str, Any] | None:
        if not is_hashable(identifier):
            return None
        versions = self.records.get(entity, {}).get(identifier, [])
        return versions[-1] if versions else None

    def pin_target_before_reschedule(self, target_id: str) -> None:
        target = self.latest_get("WorkOrder.get", target_id, model_only=True)
        if target is None:
            raise RuntimeError("Cannot pin a target that the model has not read successfully")
        self.target_before_reschedule = copy.deepcopy(target)
        self.target_snapshot_pinned = True

    def target_for_claims(self, target_id: str) -> dict[str, Any] | None:
        if self.target_snapshot_pinned:
            return self.target_before_reschedule
        return self.latest("WorkOrder", target_id)

    def rows(self, entity: str) -> list[dict[str, Any]]:
        return [versions[-1] for versions in self.records.get(entity, {}).values() if versions]

    def latest_get(
        self, tool: str, identifier: Any, *, model_only: bool = False
    ) -> dict[str, Any] | None:
        for call in reversed(self.successful_gets):
            if model_only and call["model_read"] is not True:
                continue
            if call["tool"] == tool and call["arguments"].get("id") == identifier:
                return call["record"]
        return None

    def got(self, tool: str, identifier: Any, *, model_only: bool = False) -> bool:
        return self.latest_get(tool, identifier, model_only=model_only) is not None

    def scanned(self, tool: str, filters: dict[str, Any]) -> bool:
        return any(
            call["tool"] == tool and call["filters"] == filters and call["complete"] is True
            for call in self.list_calls
        )

    def scanned_either(self, tool: str, filters: dict[str, Any]) -> bool:
        return self.scanned(tool, {}) or self.scanned(tool, filters)

    def latest_endpoint_arguments(self, tool: str) -> dict[str, Any] | None:
        for call in reversed(self.endpoint_calls):
            if call["tool"] == tool:
                return copy.deepcopy(call["arguments"])
        return None


def read_requirements(
    store: Store, target_id: str | None
) -> list[tuple[str, str, dict[str, Any], bool]]:
    requirements: list[tuple[str, str, dict[str, Any], bool]] = []

    def require(name: str, tool: str, arguments: dict[str, Any], read: bool) -> None:
        requirements.append((name, tool, arguments, read))

    target = store.target_for_claims(target_id) if target_id is not None else None
    require(
        "WorkOrder.get target",
        "WorkOrder.get",
        {"id": target_id},
        target_id is not None and store.got("WorkOrder.get", target_id),
    )
    linked_filters = {"work_order_id": target_id}
    for tool in ("MaterialRequest.list", "SubcontractOrder.list", "JobCard.list"):
        require(
            tool,
            tool,
            linked_filters,
            target_id is not None and store.scanned(tool, linked_filters),
        )
    require(
        "DowntimeEntry.list",
        "DowntimeEntry.list",
        linked_filters,
        target_id is not None and store.scanned("DowntimeEntry.list", linked_filters),
    )
    job_cards = [
        row for row in store.rows("JobCard") if row.get("work_order_id") == target_id
    ]
    for job_card in job_cards:
        job_card_id = job_card.get("id")
        if job_card_id is not None:
            arguments = {"job_card_id": job_card_id}
            require(
                f"DowntimeEntry.list job card {job_card_id}",
                "DowntimeEntry.list",
                arguments,
                store.scanned("DowntimeEntry.list", arguments),
            )
    inspection_filters = {"reference_type": "WorkOrder", "reference_id": target_id}
    require(
        "QualityInspection.list",
        "QualityInspection.list",
        inspection_filters,
        target_id is not None
        and store.scanned("QualityInspection.list", inspection_filters),
    )
    for tool in (
        "EngineeringChangeOrder.list",
        "BOM.list",
        "Workstation.list",
    ):
        require(tool, tool, {}, store.scanned(tool, {}))
    if isinstance(target, dict) and target.get("bom_id") is not None:
        bom_id = target["bom_id"]
        require(
            "BOM.get target BOM",
            "BOM.get",
            {"id": bom_id},
            store.got("BOM.get", bom_id),
        )
    if isinstance(target, dict) and target.get("sales_order_id") is not None:
        sales_order_id = target["sales_order_id"]
        require(
            "SalesOrder.get linked order",
            "SalesOrder.get",
            {"id": sales_order_id},
            store.got("SalesOrder.get", sales_order_id),
        )

    item_id = target.get("item_id") if isinstance(target, dict) else None
    for bom_id in matching_bom_ids(store.rows("BOM"), item_id):
        arguments = {"bom_id": bom_id}
        require(
            f"WorkOrder.list consumer BOM {bom_id}",
            "WorkOrder.list",
            arguments,
            store.scanned("WorkOrder.list", arguments),
        )
    endpoint_tool = "endpoint.manufacturing.finite_schedule"
    require(
        "endpoint.manufacturing.finite_schedule",
        endpoint_tool,
        store.latest_endpoint_arguments(endpoint_tool) or {},
        bool(store.endpoints.get(endpoint_tool)),
    )
    return requirements


def coverage(store: Store, target_id: str | None, *, rescheduled: bool) -> dict[str, str]:
    coverage = {
        name: "read" if read else "missing"
        for name, _tool, _arguments, read in read_requirements(store, target_id)
    }
    coverage["reschedule_work_order"] = "invoked" if rescheduled else "not_invoked"
    return coverage


def build_raw(store: Store, target_id: str, *, today: date) -> dict[str, Any]:
    work_order = store.target_for_claims(target_id)
    if work_order is None:
        raise RuntimeError("No target work-order record is available")

    material_requests = [
        row for row in store.rows("MaterialRequest") if row.get("work_order_id") == target_id
    ]
    subcontract_orders = [
        row for row in store.rows("SubcontractOrder") if row.get("work_order_id") == target_id
    ]
    job_cards = [row for row in store.rows("JobCard") if row.get("work_order_id") == target_id]
    job_card_ids = [row.get("id") for row in job_cards if row.get("id") is not None]
    downtime_entries = [
        row
        for row in store.rows("DowntimeEntry")
        if row.get("work_order_id") == target_id or row.get("job_card_id") in job_card_ids
    ]
    quality_inspections = [
        row
        for row in store.rows("QualityInspection")
        if row.get("reference_type") == "WorkOrder" and row.get("reference_id") == target_id
    ]
    engineering_changes = store.rows("EngineeringChangeOrder")
    bom_id = work_order.get("bom_id")
    bom = store.latest("BOM", bom_id) if bom_id is not None else None
    boms = store.rows("BOM")
    sales_order_id = work_order.get("sales_order_id")
    sales_order = store.latest("SalesOrder", sales_order_id) if sales_order_id is not None else None

    item_id = work_order.get("item_id")
    work_orders_by_bom = {
        candidate_id: [
            row for row in store.rows("WorkOrder") if row.get("bom_id") == candidate_id
        ]
        for candidate_id in matching_bom_ids(boms, item_id)
    }

    schedule_order = None
    endpoint_values = store.endpoints.get("endpoint.manufacturing.finite_schedule", [])
    if endpoint_values:
        envelope = endpoint_values[-1]
        result = envelope.get("result") if isinstance(envelope, dict) else None
        orders = result.get("orders") if isinstance(result, dict) else None
        if isinstance(orders, list):
            schedule_order = next(
                (
                    row
                    for row in orders
                    if isinstance(row, dict) and row.get("work_order_id") == target_id
                ),
                None,
            )

    cause_findings = analyze_causes(
        work_order,
        material_requests=material_requests,
        subcontract_orders=subcontract_orders,
        job_cards=job_cards,
        downtime_entries=downtime_entries,
        quality_inspections=quality_inspections,
        engineering_change_orders=engineering_changes,
        bom=bom,
        workstations=store.rows("Workstation"),
        schedule_order=schedule_order,
        today=today,
    )
    downstream_findings = analyze_downstream(
        work_order,
        sales_order=sales_order,
        boms=boms,
        work_orders_by_bom=work_orders_by_bom,
    )
    coverage_result = coverage(store, target_id, rescheduled=False)
    unknowns: list[str] = []
    for key, status in coverage_result.items():
        if status == "missing" and key != "reschedule_work_order":
            message = f"{key} was not read completely by the agent"
            if message not in unknowns:
                unknowns.append(message)
    for message in cause_findings["unknowns"] + downstream_findings["unknowns"]:
        if message not in unknowns:
            unknowns.append(message)
    stock_unknown = (
        "Material availability was not checked: its tool is not read-only and the seat cannot "
        "read the stock ledger."
    )
    if stock_unknown not in unknowns:
        unknowns.append(stock_unknown)
    return {
        "found": True,
        "work_order_id": target_id,
        "today": today.isoformat(),
        "work_order": copy.deepcopy(work_order),
        "lateness": analyze_lateness(work_order, today=today),
        "causes": cause_findings["causes"],
        "downstream": downstream_findings["downstream"],
        "unknowns": unknowns,
    }


def refusal(reason: str, target_id: str | None) -> dict[str, Any]:
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


def project_answer(
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


__all__ = [
    "Store",
    "build_raw",
    "coverage",
    "is_hashable",
    "project_answer",
    "read_requirements",
    "refusal",
]
