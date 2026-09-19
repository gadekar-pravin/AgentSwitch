"""Regression tests for harness scan classification and drift scoring."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from agentswitch.harness.tasks import PAGE_LIMIT, _paged_list
from agentswitch.harness.verifiers import (
    FreshReader,
    _list_scan_state,
    _scan_sequence_state,
    downstream_complete,
    expected_causes_present,
)

TARGET_ID = "WO-target"
CONSUMER_ID = "WO-consumer"
ITEM_ID = "ITEM-target"
CONSUMER_BOM_ID = "BOM-consumer"
OTHER_CONSUMER_BOM_ID = "BOM-consumer-other"


class ScriptedTools:
    def __init__(self, outcomes: list[dict[str, Any]]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> SimpleNamespace:
        self.calls.append((name, dict(arguments)))
        return SimpleNamespace(structured=self.outcomes.pop(0))


class StubFresh:
    def __init__(
        self,
        *,
        records: dict[tuple[str, Any], tuple[str, Any]] | None = None,
        lists: dict[tuple[str, tuple[tuple[str, Any], ...]], tuple[str, Any]] | None = None,
    ) -> None:
        self.records = records or {}
        self.lists = lists or {}

    def get(self, entity: str, identifier: Any) -> tuple[str, Any]:
        return self.records.get((entity, identifier), ("error", "unexpected fresh get"))

    def list(self, entity: str, filters: dict[str, Any]) -> tuple[str, Any]:
        key = (entity, tuple(sorted(filters.items())))
        return self.lists.get(key, ("ok", []))


def _subject_list_call(
    entity: str = "WorkOrder",
    *,
    rows: Any = None,
    total: Any = None,
    offset: Any = 0,
    limit: Any = PAGE_LIMIT,
    filters: dict[str, Any] | None = None,
    outcome: str = "ok",
) -> dict[str, Any]:
    if rows is None:
        rows = []
    if total is None:
        total = len(rows) if isinstance(rows, list) else 0
    arguments = dict(filters or {})
    arguments.update({"limit": limit, "offset": offset})
    call = {
        "phase": "subject",
        "tool": f"{entity}.list",
        "arguments": arguments,
        "outcome": outcome,
    }
    if outcome == "ok":
        call["structuredContent"] = {"data": rows, "total": total}
    return call


def _complete_scan(entity: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [_subject_list_call(entity, filters=filters)]


def _unavailable_scan(entity: str) -> list[dict[str, Any]]:
    return [_subject_list_call(entity, outcome="TransportError")]


def _source_anomaly_scan(entity: str) -> list[dict[str, Any]]:
    return [_subject_list_call(entity, rows="invalid", total=1)]


def _stopped_scan(entity: str) -> list[dict[str, Any]]:
    return [_subject_list_call(entity, rows=[{"id": "first"}], total=2)]


def _target_observation() -> dict[tuple[str, Any], list[dict[str, Any]]]:
    return {
        ("WorkOrder", TARGET_ID): [
            {"id": TARGET_ID, "item_id": ITEM_ID, "status": "in_progress"}
        ]
    }


def _matching_bom() -> dict[str, Any]:
    return {"id": CONSUMER_BOM_ID, "materials": [{"item_id": ITEM_ID}]}


def _consumer(status: str = "draft") -> dict[str, Any]:
    return {"id": CONSUMER_ID, "bom_id": CONSUMER_BOM_ID, "status": status}


def _downstream_fresh(*, include_consumer: bool = True) -> StubFresh:
    work_orders = [_consumer()] if include_consumer else []
    return StubFresh(
        lists={
            ("BOM", ()): ("ok", [_matching_bom()]),
            ("WorkOrder", (("bom_id", CONSUMER_BOM_ID),)): ("ok", work_orders),
        }
    )


def _downstream_fresh_with_two_consumer_boms() -> StubFresh:
    matching_boms = [
        _matching_bom(),
        {"id": OTHER_CONSUMER_BOM_ID, "materials": [{"item_id": ITEM_ID}]},
    ]
    return StubFresh(
        lists={
            ("BOM", ()): ("ok", matching_boms),
            ("WorkOrder", (("bom_id", CONSUMER_BOM_ID),)): ("ok", [_consumer()]),
            ("WorkOrder", (("bom_id", OTHER_CONSUMER_BOM_ID),)): (
                "ok",
                [
                    {
                        "id": CONSUMER_ID,
                        "bom_id": OTHER_CONSUMER_BOM_ID,
                        "status": "draft",
                    }
                ],
            ),
        }
    )


def _downstream_result(
    observations: dict[tuple[str, Any], list[dict[str, Any]]],
    subject_calls: list[dict[str, Any]],
    *,
    fresh: StubFresh | None = None,
) -> dict[str, Any]:
    claims = {"downstream": {"potential_consumers": []}}
    return downstream_complete(
        claims,
        TARGET_ID,
        observations,
        subject_calls,
        fresh or _downstream_fresh(),
    )


def _consumer_finding(result: dict[str, Any]) -> dict[str, Any]:
    return next(
        finding
        for finding in result["evidence"]["findings"]
        if finding.get("id") == CONSUMER_ID
    )


def _assert_consumer_result(result: dict[str, Any], verdict: str, branch: str) -> None:
    assert result["verdict"] == verdict
    finding = _consumer_finding(result)
    assert finding["verdict"] == verdict
    assert finding["branch"] == branch


def test_scan_sequence_complete() -> None:
    """Spec: AI (Codex) A valid sequence reaching its total is complete."""
    sequence = [
        _subject_list_call(rows=[{"id": "one"}], total=2),
        _subject_list_call(rows=[{"id": "two"}], total=2, offset=1),
    ]

    assert _scan_sequence_state(sequence) == "complete"


@pytest.mark.parametrize(
    "call",
    [
        _subject_list_call(offset=1),
        _subject_list_call(limit=0),
        {
            "phase": "subject",
            "tool": "WorkOrder.list",
            "arguments": {},
            "outcome": "ok",
            "structuredContent": {"data": [], "total": 0},
        },
        _subject_list_call(rows=[{"id": "one"}], total=2),
    ],
    ids=("bad-offset", "bad-limit", "missing-paging-arguments", "stopped-early"),
)
def test_scan_sequence_subject_paging_faults_are_incomplete(call: dict[str, Any]) -> None:
    """Spec: AI (Codex) Bad paging arguments and early stops are subject incompleteness."""
    assert _scan_sequence_state([call]) == "incomplete"


@pytest.mark.parametrize(
    "sequence",
    [
        [_subject_list_call(rows="invalid", total=0)],
        [_subject_list_call(rows=[], total=True)],
        [_subject_list_call(rows=[{}], total=1)],
        [_subject_list_call(rows=[{"id": "same"}, {"id": "same"}], total=2)],
        [_subject_list_call(rows=[], total=1)],
        [
            _subject_list_call(rows=[{"id": "one"}], total=2),
            _subject_list_call(rows=[{"id": "two"}], total=3, offset=1),
        ],
    ],
    ids=(
        "invalid-rows",
        "invalid-total",
        "missing-id",
        "duplicate-id",
        "empty-before-total",
        "total-changed",
    ),
)
def test_scan_sequence_source_anomaly_causes(sequence: list[dict[str, Any]]) -> None:
    """Spec: AI (Codex) Source-broken envelopes, ids, pages, and totals are anomalies."""
    assert _scan_sequence_state(sequence) == "source_anomaly"


def test_scan_sequence_failed_call_can_be_retried_at_same_offset() -> None:
    """Spec: AI (Codex) A failed page followed by a successful same-offset retry can complete."""
    sequence = [
        _subject_list_call(rows=[{"id": "one"}], total=2),
        _subject_list_call(offset=1, outcome="TransportError"),
        _subject_list_call(rows=[{"id": "two"}], total=2, offset=1),
    ]

    assert _scan_sequence_state(sequence) == "complete"


def test_scan_sequence_last_failed_call_is_unavailable() -> None:
    """Spec: AI (Codex) A sequence whose final needed page failed is unavailable."""
    sequence = [
        _subject_list_call(rows=[{"id": "one"}], total=2),
        _subject_list_call(offset=1, outcome="TransportError"),
    ]

    assert _scan_sequence_state(sequence) == "unavailable"


def test_list_scan_state_is_not_attempted_without_covering_calls() -> None:
    """Spec: AI (Codex) A scan with no subject call covering the filters was not attempted."""
    calls = [_subject_list_call(filters={"status": "draft"})]

    assert _list_scan_state(calls, "WorkOrder", [{"bom_id": CONSUMER_BOM_ID}]) == "not_attempted"


def test_list_scan_state_complete_sequence_wins_over_later_incomplete_sequence() -> None:
    """Spec: AI (Codex) Any complete attempt wins over a later incomplete attempt."""
    calls = [
        _subject_list_call(),
        _subject_list_call(rows=[{"id": "one"}], total=2),
    ]

    assert _list_scan_state(calls, "WorkOrder", [{}]) == "complete"


def test_list_scan_state_uses_latest_sequence_when_none_complete() -> None:
    """Spec: AI (Codex) Without a complete attempt, the latest sequence controls state."""
    calls = [
        _subject_list_call(outcome="TransportError"),
        _subject_list_call(rows=[{"id": "one"}], total=2),
    ]

    assert _list_scan_state(calls, "WorkOrder", [{}]) == "incomplete"


def test_fresh_reader_rejects_total_change_between_pages() -> None:
    """Spec: AI (Codex) FreshReader marks a changing list total incomplete."""
    tools = ScriptedTools(
        [
            {"data": [{"id": "one"}], "total": 2},
            {"data": [{"id": "two"}], "total": 3},
        ]
    )

    state, detail = FreshReader(tools).list("WorkOrder", {})

    assert state == "incomplete"
    assert detail == "WorkOrder.list total changed between pages"
    assert [arguments["offset"] for _, arguments in tools.calls] == [0, 1]


def test_selection_paged_list_rejects_total_change_between_pages() -> None:
    """Spec: AI (Codex) Selection paging reports a changing total as incomplete."""
    tools = ScriptedTools(
        [
            {"data": [{"id": "one"}], "total": 2},
            {"data": [{"id": "two"}], "total": 3},
        ]
    )

    records, page = _paged_list(tools, "WorkOrder.list", {})

    assert records == [{"id": "one"}]
    assert page == {"complete": False, "reason": "total_changed", "totals": [2, 3]}


def test_observed_non_consumer_that_is_fresh_consumer_is_drift() -> None:
    """Spec: AI (Codex) A newly consuming observed work order is inconclusive drift."""
    observations = _target_observation()
    observations[("WorkOrder", CONSUMER_ID)] = [_consumer("completed")]

    result = _downstream_result(observations, [])

    _assert_consumer_result(result, "inconclusive", "observed_not_consumer_fresh_consumer")


def test_observed_non_consumer_that_is_not_fresh_consumer_passes() -> None:
    """Spec: AI (Codex) A work order that was and remains a non-consumer passes."""
    observations = _target_observation()
    observations[("WorkOrder", CONSUMER_ID)] = [_consumer("completed")]
    observations[("BOM", CONSUMER_BOM_ID)] = [_matching_bom()]

    result = _downstream_result(observations, [], fresh=_downstream_fresh(include_consumer=False))

    _assert_consumer_result(result, "pass", "observed_not_consumer")


@pytest.mark.parametrize("filters", [{}, {"bom_id": CONSUMER_BOM_ID}], ids=("unfiltered", "bom-filter"))
def test_fresh_only_consumer_after_complete_work_order_scan_is_drift(
    filters: dict[str, Any],
) -> None:
    """Spec: AI (Codex) Either complete covering WorkOrder scan excuses a fresh-only consumer."""
    result = _downstream_result(_target_observation(), _complete_scan("WorkOrder", filters))

    _assert_consumer_result(result, "inconclusive", "appeared_after_complete_scan")


def test_fresh_only_consumer_with_previously_nonmatching_bom_is_drift() -> None:
    """Spec: AI (Codex) A BOM lacking the item in every observed version excuses omission."""
    observations = _target_observation()
    observations[("BOM", CONSUMER_BOM_ID)] = [
        {"id": CONSUMER_BOM_ID, "materials": [{"item_id": "other-one"}]},
        {"id": CONSUMER_BOM_ID, "materials": [{"item_id": "other-two"}]},
    ]

    result = _downstream_result(observations, [])

    _assert_consumer_result(result, "inconclusive", "bom_matched_after_subject_observation")


def test_fresh_only_consumer_with_finally_matching_bom_fails() -> None:
    """Spec: AI (Codex) Final matching BOM knowledge does not excuse an omitted consumer."""
    observations = _target_observation()
    observations[("BOM", CONSUMER_BOM_ID)] = [
        {"id": CONSUMER_BOM_ID, "materials": [{"item_id": "other"}]},
        _matching_bom(),
    ]

    result = _downstream_result(observations, [])

    _assert_consumer_result(result, "fail", "unobserved_without_complete_scan")


def test_fresh_only_consumer_with_matching_then_nonmatching_bom_fails() -> None:
    """Spec: AI (Codex) The order-independent rule rejects any previously matching BOM."""
    observations = _target_observation()
    observations[("BOM", CONSUMER_BOM_ID)] = [
        _matching_bom(),
        {"id": CONSUMER_BOM_ID, "materials": [{"item_id": "other"}]},
    ]

    result = _downstream_result(observations, [])

    _assert_consumer_result(result, "fail", "unobserved_without_complete_scan")


def test_fresh_only_consumer_with_bom_absent_from_complete_bom_scan_is_drift() -> None:
    """Spec: AI (Codex) A BOM absent from a complete subject BOM scan excuses omission."""
    result = _downstream_result(_target_observation(), _complete_scan("BOM"))

    _assert_consumer_result(result, "inconclusive", "bom_matched_after_subject_observation")


@pytest.mark.parametrize(
    "other_bom_versions",
    [
        [
            {
                "id": OTHER_CONSUMER_BOM_ID,
                "materials": [{"item_id": "other"}],
            }
        ],
        [],
    ],
    ids=("other-bom-nonmatching", "other-bom-unobserved"),
)
def test_fresh_only_consumer_with_any_observed_matching_bom_fails(
    other_bom_versions: list[dict[str, Any]],
) -> None:
    """Spec: AI (Codex) Any matching associated BOM defeats the drift exemption."""
    observations = _target_observation()
    observations[("BOM", CONSUMER_BOM_ID)] = [_matching_bom()]
    if other_bom_versions:
        observations[("BOM", OTHER_CONSUMER_BOM_ID)] = other_bom_versions

    result = _downstream_result(
        observations,
        _complete_scan("BOM") if not other_bom_versions else [],
        fresh=_downstream_fresh_with_two_consumer_boms(),
    )

    _assert_consumer_result(result, "fail", "unobserved_without_complete_scan")


def test_fresh_only_consumer_with_all_observed_boms_nonmatching_is_drift() -> None:
    """Spec: AI (Codex) All associated BOMs nonmatching still excuse the omission."""
    observations = _target_observation()
    observations[("BOM", CONSUMER_BOM_ID)] = [
        {"id": CONSUMER_BOM_ID, "materials": [{"item_id": "other"}]}
    ]
    observations[("BOM", OTHER_CONSUMER_BOM_ID)] = [
        {"id": OTHER_CONSUMER_BOM_ID, "materials": [{"item_id": "other"}]}
    ]

    result = _downstream_result(
        observations,
        [],
        fresh=_downstream_fresh_with_two_consumer_boms(),
    )

    _assert_consumer_result(result, "inconclusive", "bom_matched_after_subject_observation")


@pytest.mark.parametrize(
    ("subject_calls", "branch"),
    [
        (_unavailable_scan("BOM"), "subject_read_unavailable"),
        (_source_anomaly_scan("BOM"), "subject_read_source_anomaly"),
    ],
    ids=("unavailable", "source-anomaly"),
)
def test_fresh_only_consumer_with_uncertain_subject_scan_is_inconclusive(
    subject_calls: list[dict[str, Any]], branch: str
) -> None:
    """Spec: AI (Codex) Unavailable and source-anomalous scans excuse a fresh-only consumer."""
    result = _downstream_result(_target_observation(), subject_calls)

    _assert_consumer_result(result, "inconclusive", branch)


@pytest.mark.parametrize(
    "subject_calls",
    [[], _stopped_scan("BOM")],
    ids=("not-attempted", "stopped-early"),
)
def test_fresh_only_consumer_without_excusing_scan_fails(
    subject_calls: list[dict[str, Any]],
) -> None:
    """Spec: AI (Codex) No scan or a subject-stopped scan cannot excuse an omitted consumer."""
    observations = _target_observation()
    observations[("BOM", CONSUMER_BOM_ID)] = [_matching_bom()]

    result = _downstream_result(observations, subject_calls)

    _assert_consumer_result(result, "fail", "unobserved_without_complete_scan")


@pytest.mark.parametrize(
    ("subject_calls", "branch"),
    [
        (_unavailable_scan("BOM"), "subject_read_unavailable"),
        (_source_anomaly_scan("BOM"), "subject_read_source_anomaly"),
        (_complete_scan("BOM"), "bom_matched_after_subject_observation"),
    ],
    ids=("unavailable", "source-anomaly", "complete"),
)
def test_observed_consumer_with_unobserved_bom_and_excusing_scan_is_inconclusive(
    subject_calls: list[dict[str, Any]], branch: str
) -> None:
    """Spec: AI (Codex) Uncertain or complete BOM scans excuse an observed consumer's omission."""
    observations = _target_observation()
    observations[("WorkOrder", CONSUMER_ID)] = [_consumer()]

    result = _downstream_result(observations, subject_calls)

    _assert_consumer_result(result, "inconclusive", branch)


@pytest.mark.parametrize(
    ("bom_versions", "subject_calls"),
    [
        ([_matching_bom()], []),
        ([], []),
        ([], _stopped_scan("BOM")),
    ],
    ids=("matching-bom-observed", "not-attempted", "stopped-early"),
)
def test_observed_consumer_without_excusing_bom_scan_fails(
    bom_versions: list[dict[str, Any]], subject_calls: list[dict[str, Any]]
) -> None:
    """Spec: AI (Codex) Matching BOM evidence or absent/incomplete scans preserve failure."""
    observations = _target_observation()
    observations[("WorkOrder", CONSUMER_ID)] = [_consumer()]
    if bom_versions:
        observations[("BOM", CONSUMER_BOM_ID)] = bom_versions

    result = _downstream_result(observations, subject_calls)

    _assert_consumer_result(result, "fail", "observed_consumer_omitted")


@pytest.mark.parametrize(
    "bom_versions",
    [
        [
            {"id": CONSUMER_BOM_ID, "materials": [{"item_id": "other"}]},
            _matching_bom(),
        ],
        [
            _matching_bom(),
            {"id": CONSUMER_BOM_ID, "materials": [{"item_id": "other"}]},
        ],
    ],
    ids=("nonmatching-then-matching", "matching-then-nonmatching"),
)
def test_observed_consumer_with_any_matching_bom_observation_fails(
    bom_versions: list[dict[str, Any]],
) -> None:
    """Spec: AI (Codex) Any observed BOM match makes an omitted open consumer fail."""
    observations = _target_observation()
    observations[("WorkOrder", CONSUMER_ID)] = [_consumer()]
    observations[("BOM", CONSUMER_BOM_ID)] = bom_versions

    result = _downstream_result(observations, [])

    _assert_consumer_result(result, "fail", "observed_consumer_omitted")


@pytest.mark.parametrize("reverse_versions", [False, True], ids=("matching-first", "matching-last"))
def test_open_observed_consumer_with_bom_link_drift_fails(reverse_versions: bool) -> None:
    """Spec: AI (Codex) BOM-link drift cannot excuse an observed open consumer."""
    observations = _target_observation()
    versions = [
        _consumer(),
        {
            "id": CONSUMER_ID,
            "bom_id": OTHER_CONSUMER_BOM_ID,
            "status": "draft",
        },
    ]
    observations[("WorkOrder", CONSUMER_ID)] = list(reversed(versions)) if reverse_versions else versions
    observations[("BOM", CONSUMER_BOM_ID)] = [_matching_bom()]
    observations[("BOM", OTHER_CONSUMER_BOM_ID)] = [
        {"id": OTHER_CONSUMER_BOM_ID, "materials": [{"item_id": "other"}]}
    ]

    result = _downstream_result(observations, [])

    _assert_consumer_result(result, "fail", "observed_consumer_omitted")


def test_observed_consumer_status_drift_remains_inconclusive() -> None:
    """Spec: AI (Codex) A later closed status still excuses an observed consumer."""
    observations = _target_observation()
    observations[("WorkOrder", CONSUMER_ID)] = [
        _consumer(),
        _consumer("completed"),
    ]
    observations[("BOM", CONSUMER_BOM_ID)] = [_matching_bom()]

    result = _downstream_result(observations, [])

    _assert_consumer_result(result, "inconclusive", "observed_consumer_drift")


def _cause_result(subject_calls: list[dict[str, Any]]) -> dict[str, Any]:
    material_request = {
        "id": "MR-1",
        "work_order_id": TARGET_ID,
        "status": "submitted",
    }
    selection = {
        "expected_causes": [
            {
                "entity": "MaterialRequest",
                "id": "MR-1",
                "code": "material_request_open",
                "record": material_request,
            }
        ]
    }
    claims = {"causes": []}
    fresh = StubFresh(
        records={("MaterialRequest", "MR-1"): ("ok", material_request)},
        lists={
            (
                "MaterialRequest",
                (("work_order_id", TARGET_ID),),
            ): ("ok", [material_request])
        },
    )
    return expected_causes_present(
        selection,
        claims,
        TARGET_ID,
        _target_observation(),
        subject_calls,
        fresh,
        date(2026, 9, 19),
    )


def test_expected_cause_source_anomaly_is_inconclusive() -> None:
    """Spec: AI (Codex) A source-anomalous covering scan makes cause omission inconclusive."""
    calls = _source_anomaly_scan("MaterialRequest")
    calls[0]["arguments"]["work_order_id"] = TARGET_ID

    result = _cause_result(calls)

    assert result["verdict"] == "inconclusive"
    assert result["evidence"]["findings"][-1]["branch"] == "selection_subject_read_source_anomaly"


def test_expected_cause_subject_stopped_scan_fails() -> None:
    """Spec: AI (Codex) A subject-stopped scan does not excuse an omitted expected cause."""
    calls = _stopped_scan("MaterialRequest")
    calls[0]["arguments"]["work_order_id"] = TARGET_ID

    result = _cause_result(calls)

    assert result["verdict"] == "fail"
    assert result["evidence"]["findings"][-1]["branch"] == "selection_unclaimed_without_scan"
