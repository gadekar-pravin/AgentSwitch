"""Offline tests for the live capability registry and argument gate."""

from __future__ import annotations

import copy
import json
from datetime import date
from typing import Any

import pytest

from agentswitch.agent import build_tool_menu, run_agent
from agentswitch.capabilities import (
    Capability,
    CapabilityArgumentError,
    build_manifest,
    validate,
)


def _object_schema(
    properties: dict[str, Any], *, required: list[str] | None = None
) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "required": [] if required is None else required,
        "additionalProperties": False,
    }


def _tool(
    name: str,
    properties: dict[str, Any],
    *,
    description: str = "",
    required: list[str] | None = None,
    annotations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "annotations": (
            {"readOnlyHint": True, "destructiveHint": False}
            if annotations is None
            else annotations
        ),
        "inputSchema": _object_schema(properties, required=required),
    }


def _catalogue() -> list[Any]:
    material = _tool(
        "MaterialRequest.list",
        {
            "work_order_id": {"type": "string", "minLength": 1},
            "required_by_date": {
                "type": "string",
                "format": "date",
                "default": "2026-01-01",
            },
        },
        description=" Material requests. ",
    )
    work_orders = _tool(
        "WorkOrder.list",
        {
            "bom_id": {"type": "string", "minLength": 1},
            "status": {"type": "string", "default": "draft"},
            "planned_start_date": {"type": "string", "format": "date"},
        },
        description="Work orders.",
    )
    quality = _tool(
        "QualityInspection.list",
        {
            "reference_type": {
                "type": "string",
                "enum": ["WorkOrder", "StockEntry"],
            },
            "reference_id": {"type": "string", "minLength": 1},
        },
        description="Quality inspections.",
    )
    finite = _tool(
        "endpoint.manufacturing.finite_schedule",
        {
            "horizon_days": {
                "type": "integer",
                "description": "Scheduling horizon.",
                "minimum": 1,
                "maximum": 90,
            }
        },
        description="Finite schedule.",
    )
    missing_annotations = _tool("BOM.get", {"id": {"type": "string"}})
    missing_annotations.pop("annotations")
    return [
        _tool(
            "WorkOrder.get",
            {"id": {"type": "string", "minLength": 1}},
            description=" Read one work order. ",
            required=["id"],
        ),
        material,
        work_orders,
        quality,
        finite,
        _tool(
            "SalesOrder.get",
            {"id": {"type": "string"}},
            annotations={"readOnlyHint": False, "destructiveHint": False},
        ),
        missing_annotations,
        _tool("Employee.get", {"id": {"type": "string"}}),
        None,
        _tool(
            "Item.get",
            {"id": {"type": "string", "pattern": "^[A-Z]+$"}},
            required=["id"],
        ),
    ]


class _CatalogueTools:
    def __init__(self, catalogue: list[Any]) -> None:
        self.catalogue = catalogue
        self.list_calls = 0

    def list_tools(self) -> list[Any]:
        self.list_calls += 1
        return self.catalogue


def test_manifest_and_menu_match_the_independent_expected_projection() -> None:
    """Spec: AI (Codex) Synthetic catalogue selection, projection, drops, and immutability."""
    catalogue = _catalogue()
    original = copy.deepcopy(catalogue)
    tools = _CatalogueTools(catalogue)

    actual = build_tool_menu(tools)
    manifest = build_manifest(catalogue)

    expected_functions = [
        {
            "type": "function",
            "function": {
                "name": "WorkOrder__get",
                "description": "Read one work order.",
                "parameters": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {"id": {"type": "string", "minLength": 1}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "MaterialRequest__list",
                "description": "Material requests.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "work_order_id": {"type": "string", "minLength": 1}
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "WorkOrder__list",
                "description": "Work orders.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "bom_id": {"type": "string", "minLength": 1},
                        "status": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "QualityInspection__list",
                "description": "Quality inspections.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reference_type": {
                            "type": "string",
                            "enum": ["WorkOrder", "StockEntry"],
                        },
                        "reference_id": {"type": "string", "minLength": 1},
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "endpoint__manufacturing__finite_schedule",
                "description": "Finite schedule.",
                "parameters": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {
                        "horizon_days": {
                            "type": "integer",
                            "description": "Scheduling horizon.",
                            "minimum": 1,
                            "maximum": 90,
                        }
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "reschedule_work_order",
                "description": (
                    "Guardedly propose or apply a reschedule for the supplied target only."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"work_order_id": {"type": "string"}},
                    "required": ["work_order_id"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finish",
                "description": "Finish with an answer or a principled refusal.",
                "parameters": {
                    "type": "object",
                    "properties": {
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
                    "required": ["outcome", "refusal_reason", "prose"],
                    "additionalProperties": False,
                },
            },
        },
    ]
    expected_native_to_mcp = {
        "WorkOrder__get": "WorkOrder.get",
        "MaterialRequest__list": "MaterialRequest.list",
        "WorkOrder__list": "WorkOrder.list",
        "QualityInspection__list": "QualityInspection.list",
        "endpoint__manufacturing__finite_schedule": (
            "endpoint.manufacturing.finite_schedule"
        ),
    }
    expected_catalogue_names = [
        "WorkOrder.get",
        "MaterialRequest.list",
        "WorkOrder.list",
        "QualityInspection.list",
        "endpoint.manufacturing.finite_schedule",
        "SalesOrder.get",
        "BOM.get",
        "Employee.get",
        "Item.get",
    ]

    assert actual == (
        expected_functions,
        expected_native_to_mcp,
        expected_catalogue_names,
    )
    assert manifest.dropped == [
        {
            "name": "Item.get",
            "reason": "property 'id' uses unsupported keyword 'pattern'",
        }
    ]
    assert [capability.name for capability in manifest.capabilities] == [
        "WorkOrder.get",
        "MaterialRequest.list",
        "WorkOrder.list",
        "QualityInspection.list",
        "endpoint.manufacturing.finite_schedule",
        "reschedule_work_order",
        "answer",
    ]
    assert catalogue == original
    assert tools.list_calls == 1


def _capability(manifest, name: str) -> Capability:
    return next(item for item in manifest.capabilities if item.name == name)


def test_manifest_drops_non_string_scalar_type_without_losing_other_tools() -> None:
    """Spec: AI (Codex) A malformed scalar type drops only its tool."""
    catalogue = [
        _tool(
            "Item.get",
            {"id": {"type": ["string", "null"]}},
            required=["id"],
        ),
        _tool(
            "WorkOrder.get",
            {"id": {"type": "string"}},
            required=["id"],
        ),
    ]

    manifest = build_manifest(catalogue)

    assert [item["name"] for item in manifest.dropped] == ["Item.get"]
    assert manifest.dropped[0]["reason"]
    assert _capability(manifest, "WorkOrder.get").mcp_tool == "WorkOrder.get"


def test_manifest_drops_list_requiring_a_filter_absent_from_properties() -> None:
    """Spec: AI (Codex) An unexposed required list filter drops only its tool."""
    catalogue = [
        _tool("WorkOrder.list", {}, required=["status"]),
        _tool(
            "WorkOrder.get",
            {"id": {"type": "string"}},
            required=["id"],
        ),
    ]

    manifest = build_manifest(catalogue)

    assert [item["name"] for item in manifest.dropped] == ["WorkOrder.list"]
    assert manifest.dropped[0]["reason"]
    assert _capability(manifest, "WorkOrder.get").mcp_tool == "WorkOrder.get"


def test_validate_rejects_invalid_values_and_preserves_valid_arguments() -> None:
    """Spec: AI (Codex) The gate enforces shape, scalar, range, list, enum, and null rules."""
    manifest = build_manifest(_catalogue())
    get = _capability(manifest, "WorkOrder.get")
    work_orders = _capability(manifest, "WorkOrder.list")
    material = _capability(manifest, "MaterialRequest.list")
    quality = _capability(manifest, "QualityInspection.list")
    finite = _capability(manifest, "endpoint.manufacturing.finite_schedule")
    answer = _capability(manifest, "answer")

    with pytest.raises(CapabilityArgumentError, match="unsupported key"):
        validate(work_orders, {"made_up": "x"})
    with pytest.raises(CapabilityArgumentError, match="Missing required"):
        validate(get, {})
    with pytest.raises(CapabilityArgumentError, match="length must be at least 1"):
        validate(get, {"id": ""})
    with pytest.raises(CapabilityArgumentError, match="expected string"):
        validate(get, {"id": 3})
    for value in (True, 1.5, 0, 91):
        with pytest.raises(CapabilityArgumentError):
            validate(finite, {"horizon_days": value})
    assert validate(finite, {"horizon_days": 30}) == {"horizon_days": 30}

    with pytest.raises(
        CapabilityArgumentError,
        match="starts with ':' and is a placeholder",
    ):
        validate(material, {"work_order_id": "  :placeholder"})
    for value in ("", "   "):
        with pytest.raises(CapabilityArgumentError, match="whitespace-only"):
            validate(material, {"work_order_id": value})
    with pytest.raises(CapabilityArgumentError, match="require a string value"):
        validate(material, {"work_order_id": 12})
    with pytest.raises(CapabilityArgumentError, match="catalogue schema allows only"):
        validate(quality, {"reference_type": "PurchaseOrder"})

    strict_enum = Capability(
        name="strict_enum",
        native_name="strict_enum",
        families=frozenset({"read"}),
        description="",
        schema={
            "type": "object",
            "properties": {"value": {"type": "number", "enum": [1]}},
            "required": ["value"],
            "additionalProperties": False,
        },
        mcp_tool=None,
    )
    with pytest.raises(CapabilityArgumentError, match="allowed values"):
        validate(strict_enum, {"value": 1.0})

    nullable = validate(
        answer,
        {"outcome": "answered", "refusal_reason": None, "prose": "done"},
    )
    assert nullable == {
        "outcome": "answered",
        "refusal_reason": None,
        "prose": "done",
    }
    arguments = {"id": "  WO-1  "}
    validated = validate(get, arguments)
    assert validated == arguments
    assert validated is not arguments


class _NoCallTools(_CatalogueTools):
    def __init__(self, catalogue: list[Any]) -> None:
        super().__init__(catalogue)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        allow_write: bool = False,
    ) -> Any:
        self.calls.append((name, arguments))
        raise AssertionError("invalid arguments must not reach MCP")


class _ScriptedLlm:
    model = "fake/model"

    def __init__(self) -> None:
        self.turn = 0

    def chat(self, messages, **kwargs):
        self.turn += 1
        if self.turn == 1:
            function = {"name": "WorkOrder__get", "arguments": '{"id":""}'}
        else:
            function = {
                "name": "finish",
                "arguments": json.dumps(
                    {
                        "outcome": "refused",
                        "refusal_reason": "source_unavailable",
                        "prose": "The source could not be read.",
                    }
                ),
            }
        return {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": f"call-{self.turn}", "type": "function", "function": function}
                ],
            },
            "model": self.model,
            "usage": {},
            "finish_reason": "tool_calls",
        }


def test_run_agent_rejects_invalid_read_before_mcp_and_records_manifest() -> None:
    """Spec: AI (Codex) Invalid get arguments stay local and run state records offered and dropped."""
    catalogue = [
        _tool(
            "WorkOrder.get",
            {"id": {"type": "string", "minLength": 1}},
            required=["id"],
        ),
        _tool(
            "Item.get",
            {"id": {"type": "string", "pattern": "^[A-Z]+$"}},
            required=["id"],
        ),
    ]
    tools = _NoCallTools(catalogue)

    result = run_agent(
        tools,
        _ScriptedLlm(),
        request="investigate",
        target_id="WO-1",
        today=date(2026, 9, 19),
        own_user_id=None,
        allow_write=False,
        max_turns=2,
    )

    invalid_reply = next(
        json.loads(message["content"])
        for message in result["transcript"]
        if message.get("role") == "tool" and message.get("name") == "WorkOrder__get"
    )
    assert invalid_reply == {
        "ok": False,
        "error": {
            "type": "invalid_params",
            "message": "Invalid argument 'id': length must be at least 1.",
        },
    }
    assert tools.calls == []
    assert tools.list_calls == 1
    assert result["manifest"] == {
        "offered": ["WorkOrder.get", "reschedule_work_order", "answer"],
        "dropped": [
            {
                "name": "Item.get",
                "reason": "property 'id' uses unsupported keyword 'pattern'",
            }
        ],
    }
