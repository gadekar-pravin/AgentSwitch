"""Live capability registry and transport-independent argument validation."""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from typing import Any

from .mcp_client import ProtocolError

ALLOWED_MCP_TOOLS = (
    "WorkOrder.get",
    "WorkOrder.list",
    "MaterialRequest.list",
    "SubcontractOrder.list",
    "JobCard.list",
    "DowntimeEntry.list",
    "QualityInspection.list",
    "EngineeringChangeOrder.list",
    "BOM.get",
    "BOM.list",
    "Workstation.list",
    "SalesOrder.get",
    "Item.get",
    "endpoint.manufacturing.finite_schedule",
)
LIST_TOOL_FILTERS = {
    "WorkOrder.list": ("bom_id", "status", "item_id", "sales_order_id"),
    "MaterialRequest.list": ("work_order_id",),
    "SubcontractOrder.list": ("work_order_id",),
    "JobCard.list": ("work_order_id",),
    "DowntimeEntry.list": ("work_order_id", "job_card_id"),
    "QualityInspection.list": ("reference_type", "reference_id"),
    "EngineeringChangeOrder.list": (),
    "BOM.list": (),
    "Workstation.list": (),
}

_REFUSAL_REASONS = {"not_found", "outside_seat", "unsupported", "source_unavailable"}
_ANNOTATION_KEYWORDS = {"description", "$schema", "title", "format", "examples"}
_SCALAR_TYPES = {"string", "integer", "number", "boolean"}
_ROOT_COMPOSITION_KEYWORDS = {"anyOf", "oneOf", "allOf", "not", "if"}
_LOG = logging.getLogger(__name__)


class CapabilityArgumentError(ValueError):
    """Arguments did not satisfy a capability's exposed schema."""


@dataclass(frozen=True)
class Capability:
    """One model-visible operation derived from the live catalogue or local code."""

    name: str
    native_name: str
    families: frozenset[str]
    description: str
    schema: dict[str, Any]
    mcp_tool: str | None


@dataclass(frozen=True)
class Manifest:
    """The ordered capabilities available for one agent run."""

    capabilities: tuple[Capability, ...]
    catalogue_names: list[str]
    dropped: list[dict[str, str]]

    def by_native_name(self) -> dict[str, Capability]:
        """Return capabilities keyed by their model-visible function names."""
        return {capability.native_name: capability for capability in self.capabilities}


def _native_name(name: str) -> str:
    if "__" in name:
        raise ValueError(f"MCP tool name {name!r} cannot be mapped reversibly")
    mapped = name.replace(".", "__")
    if len(mapped) > 64:
        raise ValueError(f"MCP tool name {name!r} exceeds the native-function name limit")
    return mapped


def _strip_defaults(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_defaults(item)
            for key, item in value.items()
            if key != "default"
        }
    if isinstance(value, list):
        return [_strip_defaults(item) for item in value]
    return value


class _UnsupportedSchema(ValueError):
    pass


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _value_matches_type(value: Any, declared_type: str) -> bool:
    if declared_type == "string":
        return isinstance(value, str)
    if declared_type == "integer":
        return _is_integer(value)
    if declared_type == "number":
        return _is_number(value)
    if declared_type == "boolean":
        return isinstance(value, bool)
    return False


def _validate_annotations(schema: dict[str, Any], *, location: str) -> None:
    if "description" in schema and not isinstance(schema["description"], str):
        raise _UnsupportedSchema(f"{location} has a malformed description")
    if "$schema" in schema and not isinstance(schema["$schema"], str):
        raise _UnsupportedSchema(f"{location} has a malformed $schema")
    if "title" in schema and not isinstance(schema["title"], str):
        raise _UnsupportedSchema(f"{location} has a malformed title")
    if "format" in schema and not isinstance(schema["format"], str):
        raise _UnsupportedSchema(f"{location} has a malformed format")
    if "examples" in schema and not isinstance(schema["examples"], list):
        raise _UnsupportedSchema(f"{location} has malformed examples")


def _validate_scalar_schema(schema: Any, *, location: str) -> None:
    if not isinstance(schema, dict):
        raise _UnsupportedSchema(f"{location} must be an object")
    allowed = {
        "type",
        "enum",
        "minLength",
        "minimum",
        "maximum",
        *_ANNOTATION_KEYWORDS,
    }
    unsupported = [key for key in schema if key not in allowed]
    if unsupported:
        raise _UnsupportedSchema(
            f"{location} uses unsupported keyword {unsupported[0]!r}"
        )
    _validate_annotations(schema, location=location)
    declared_type = schema.get("type")
    if not isinstance(declared_type, str) or declared_type not in _SCALAR_TYPES:
        raise _UnsupportedSchema(
            f"{location} type must be one of {sorted(_SCALAR_TYPES)!r}"
        )
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not enum:
            raise _UnsupportedSchema(f"{location} has a malformed enum")
        if any(not _value_matches_type(value, declared_type) for value in enum):
            raise _UnsupportedSchema(
                f"{location} has an enum value inconsistent with its type"
            )
    if "minLength" in schema:
        minimum_length = schema["minLength"]
        if (
            declared_type != "string"
            or not _is_integer(minimum_length)
            or minimum_length < 0
        ):
            raise _UnsupportedSchema(f"{location} has a malformed minLength")
    for keyword in ("minimum", "maximum"):
        if keyword not in schema:
            continue
        bound = schema[keyword]
        if declared_type not in {"integer", "number"} or not _is_number(bound):
            raise _UnsupportedSchema(f"{location} has a malformed {keyword}")
    if (
        "minimum" in schema
        and "maximum" in schema
        and schema["minimum"] > schema["maximum"]
    ):
        raise _UnsupportedSchema(f"{location} has minimum greater than maximum")


def _validate_null_schema(schema: Any, *, location: str) -> bool:
    if not isinstance(schema, dict) or schema.get("type") != "null":
        return False
    unsupported = [
        key for key in schema if key != "type" and key not in _ANNOTATION_KEYWORDS
    ]
    if unsupported:
        raise _UnsupportedSchema(
            f"{location} uses unsupported keyword {unsupported[0]!r}"
        )
    _validate_annotations(schema, location=location)
    return True


def _validate_property_schema(schema: Any, *, location: str) -> None:
    if not isinstance(schema, dict):
        raise _UnsupportedSchema(f"{location} must be an object")
    if "anyOf" not in schema:
        _validate_scalar_schema(schema, location=location)
        return
    unsupported = [
        key for key in schema if key != "anyOf" and key not in _ANNOTATION_KEYWORDS
    ]
    if unsupported:
        raise _UnsupportedSchema(
            f"{location} uses unsupported keyword {unsupported[0]!r}"
        )
    _validate_annotations(schema, location=location)
    branches = schema["anyOf"]
    if not isinstance(branches, list) or len(branches) != 2:
        raise _UnsupportedSchema(
            f"{location} anyOf must contain one scalar and one null branch"
        )
    null_indexes = [
        index
        for index, branch in enumerate(branches)
        if _validate_null_schema(branch, location=f"{location}.anyOf[{index}]")
    ]
    if len(null_indexes) != 1:
        raise _UnsupportedSchema(
            f"{location} anyOf must contain one scalar and one null branch"
        )
    scalar_index = 1 - null_indexes[0]
    _validate_scalar_schema(
        branches[scalar_index], location=f"{location}.anyOf[{scalar_index}]"
    )


def _check_supported_schema(schema: dict[str, Any]) -> None:
    allowed = {
        "type",
        "properties",
        "required",
        "additionalProperties",
        *_ANNOTATION_KEYWORDS,
    }
    unsupported = [key for key in schema if key not in allowed]
    if unsupported:
        raise _UnsupportedSchema(
            f"root schema uses unsupported keyword {unsupported[0]!r}"
        )
    _validate_annotations(schema, location="root schema")
    if schema.get("type") != "object":
        raise _UnsupportedSchema("root schema type must be 'object'")
    properties = schema.get("properties")
    if not isinstance(properties, dict) or any(
        not isinstance(name, str) for name in properties
    ):
        raise _UnsupportedSchema("root schema properties must be an object")
    additional = schema.get("additionalProperties", False)
    if additional is not False:
        raise _UnsupportedSchema("root schema additionalProperties must be false")
    required = schema.get("required", [])
    if (
        not isinstance(required, list)
        or any(not isinstance(name, str) for name in required)
        or len(set(required)) != len(required)
        or any(name not in properties for name in required)
    ):
        raise _UnsupportedSchema("root schema required must name exposed properties")
    for name, property_schema in properties.items():
        _validate_property_schema(property_schema, location=f"property {name!r}")


def _check_list_envelope(original: str, schema: dict[str, Any]) -> None:
    composition = [key for key in _ROOT_COMPOSITION_KEYWORDS if key in schema]
    if composition:
        raise _UnsupportedSchema(
            f"list root schema uses composition keyword {sorted(composition)[0]!r}"
        )
    required = schema.get("required", [])
    if not isinstance(required, list) or any(
        not isinstance(name, str) for name in required
    ):
        raise _UnsupportedSchema("list root schema has malformed required")
    properties = schema.get("properties")
    exposed_filters = {
        name
        for name in LIST_TOOL_FILTERS[original]
        if isinstance(properties, dict) and name in properties
    }
    allowed_required = {*exposed_filters, "limit", "offset"}
    unsupported = [name for name in required if name not in allowed_required]
    if unsupported:
        raise _UnsupportedSchema(
            f"list root schema requires unexposed property {unsupported[0]!r}"
        )


def _drop(
    dropped: list[dict[str, str]], name: str, error: _UnsupportedSchema
) -> None:
    reason = str(error)
    dropped.append({"name": name, "reason": reason})
    _LOG.warning("Dropping capability %s: %s", name, reason)


def _local_capabilities() -> tuple[Capability, Capability]:
    reschedule = Capability(
        name="reschedule_work_order",
        native_name="reschedule_work_order",
        families=frozenset({"side_effect", "exclusive"}),
        description="Guardedly propose or apply a reschedule for the supplied target only.",
        schema={
            "type": "object",
            "properties": {"work_order_id": {"type": "string"}},
            "required": ["work_order_id"],
            "additionalProperties": False,
        },
        mcp_tool=None,
    )
    answer = Capability(
        name="answer",
        native_name="finish",
        families=frozenset({"terminal"}),
        description="Finish with an answer or a principled refusal.",
        schema={
            "type": "object",
            "properties": {
                "outcome": {"type": "string", "enum": ["answered", "refused"]},
                "refusal_reason": {
                    "anyOf": [
                        {
                            "type": "string",
                            "enum": sorted(_REFUSAL_REASONS),
                        },
                        {"type": "null"},
                    ]
                },
                "prose": {"type": "string"},
            },
            "required": ["outcome", "refusal_reason", "prose"],
            "additionalProperties": False,
        },
        mcp_tool=None,
    )
    return reschedule, answer


def build_manifest(catalogue: list[Any]) -> Manifest:
    """Build an ordered capability manifest from a live tools/list catalogue."""
    catalogue_names = [
        item.get("name")
        for item in catalogue
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]
    allowed = set(ALLOWED_MCP_TOOLS)
    capabilities: list[Capability] = []
    dropped: list[dict[str, str]] = []
    native_names: set[str] = set()
    for tool in catalogue:
        if not isinstance(tool, dict):
            continue
        original = tool.get("name")
        if not isinstance(original, str) or original not in allowed:
            continue
        annotations = tool.get("annotations")
        if (
            not isinstance(annotations, dict)
            or annotations.get("readOnlyHint") is not True
            or annotations.get("destructiveHint") is True
        ):
            continue
        native = _native_name(original)
        if native in native_names:
            raise ValueError(f"Native tool-name collision for {original!r}")
        native_names.add(native)
        raw_schema = tool.get("inputSchema", {})
        if not isinstance(raw_schema, dict):
            raise ProtocolError(f"Tool {original!r} has no valid inputSchema")
        schema = _strip_defaults(copy.deepcopy(raw_schema))
        try:
            if original.endswith(".list"):
                _check_list_envelope(original, schema)
                catalogue_properties = schema.get("properties")
                if not isinstance(catalogue_properties, dict):
                    catalogue_properties = {}
                schema = {
                    "type": "object",
                    "properties": {
                        name: catalogue_properties[name]
                        for name in LIST_TOOL_FILTERS[original]
                        if name in catalogue_properties
                    },
                    "additionalProperties": False,
                }
            _check_supported_schema(schema)
        except _UnsupportedSchema as error:
            _drop(dropped, original, error)
            continue
        description = tool.get("description")
        if original.endswith(".list"):
            families = frozenset({"read", "list"})
        else:
            families = frozenset({"read"})
        capabilities.append(
            Capability(
                name=original,
                native_name=native,
                families=families,
                description=description.strip() if isinstance(description, str) else "",
                schema=schema,
                mcp_tool=original,
            )
        )
    capabilities.extend(_local_capabilities())
    return Manifest(
        capabilities=tuple(capabilities),
        catalogue_names=catalogue_names,
        dropped=dropped,
    )


def _received(value: Any) -> str:
    if value is None:
        return "null value null"
    if isinstance(value, bool):
        kind = "boolean"
    elif isinstance(value, str):
        kind = "string"
    elif isinstance(value, int):
        kind = "integer"
    elif isinstance(value, float):
        kind = "number"
    elif isinstance(value, dict):
        kind = "object"
    elif isinstance(value, list):
        kind = "array"
    else:
        kind = type(value).__name__
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        rendered = repr(value)
    return f"{kind} value {rendered}"


def _enum_contains(enum: list[Any], value: Any) -> bool:
    return any(type(candidate) is type(value) and candidate == value for candidate in enum)


def _scalar_branch(schema: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    branches = schema.get("anyOf")
    if not isinstance(branches, list):
        return schema, False
    for branch in branches:
        if isinstance(branch, dict) and branch.get("type") != "null":
            return branch, True
    raise RuntimeError("validated nullable schema has no scalar branch")


def _invalid_list_filter_message(
    capability: Capability, arguments: dict[Any, Any]
) -> str | None:
    schemas = capability.schema["properties"]
    unsupported = [key for key in arguments if key not in schemas]
    if unsupported:
        allowed_display = ", ".join(sorted(schemas)) or "(none)"
        unsupported_display = ", ".join(sorted(repr(key) for key in unsupported))
        return (
            f"Invalid filters for {capability.name}: unsupported key(s) "
            f"{unsupported_display}. Allowed filters: {allowed_display}."
        )
    for key, value in arguments.items():
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return (
                    f"Invalid filter {key!r} for {capability.name}: the value is empty or "
                    "whitespace-only; omit the filter or provide a real value."
                )
            if stripped.startswith(":"):
                return (
                    f"Invalid filter {key!r} for {capability.name}: {value!r} starts with ':' "
                    "and is a placeholder, not a real filter value."
                )
        if isinstance(key, str) and key.endswith("_id") and not isinstance(value, str):
            return (
                f"Invalid filter {key!r} for {capability.name}: received {_received(value)}; "
                "filters ending in '_id' require a string value."
            )
        if key in {"status", "reference_type"}:
            property_schema, _nullable = _scalar_branch(schemas[key])
            declared = property_schema.get("enum")
            if isinstance(declared, list) and not _enum_contains(declared, value):
                rendered = json.dumps(declared, ensure_ascii=False, default=str)
                return (
                    f"Invalid filter {key!r} for {capability.name}: received "
                    f"{_received(value)}; the catalogue schema allows only {rendered}."
                )
    return None


def _validate_value(name: str, schema: dict[str, Any], value: Any) -> None:
    scalar_schema, nullable = _scalar_branch(schema)
    if value is None:
        if nullable:
            return
        raise CapabilityArgumentError(f"Argument {name!r} must not be null")
    declared_type = scalar_schema["type"]
    if not _value_matches_type(value, declared_type):
        raise CapabilityArgumentError(
            f"Invalid argument {name!r}: received {_received(value)}; "
            f"expected {declared_type}."
        )
    enum = scalar_schema.get("enum")
    if isinstance(enum, list) and not _enum_contains(enum, value):
        rendered = json.dumps(enum, ensure_ascii=False, default=str)
        raise CapabilityArgumentError(
            f"Invalid argument {name!r}: received {_received(value)}; allowed values are "
            f"{rendered}."
        )
    minimum_length = scalar_schema.get("minLength")
    if isinstance(minimum_length, int) and len(value) < minimum_length:
        raise CapabilityArgumentError(
            f"Invalid argument {name!r}: length must be at least {minimum_length}."
        )
    if "minimum" in scalar_schema and value < scalar_schema["minimum"]:
        raise CapabilityArgumentError(
            f"Invalid argument {name!r}: value must be at least "
            f"{scalar_schema['minimum']}."
        )
    if "maximum" in scalar_schema and value > scalar_schema["maximum"]:
        raise CapabilityArgumentError(
            f"Invalid argument {name!r}: value must be at most "
            f"{scalar_schema['maximum']}."
        )


def validate(capability: Capability, arguments: Any) -> dict[str, Any]:
    """Return an unchanged argument copy or raise a model-visible validation error."""
    if not isinstance(arguments, dict):
        raise CapabilityArgumentError("Arguments must be an object")
    if "list" in capability.families:
        list_error = _invalid_list_filter_message(capability, arguments)
        if list_error is not None:
            raise CapabilityArgumentError(list_error)
    properties = capability.schema["properties"]
    unknown = [key for key in arguments if key not in properties]
    if unknown:
        rendered = ", ".join(sorted(repr(key) for key in unknown))
        raise CapabilityArgumentError(f"Unknown argument key(s): {rendered}")
    missing = [
        name for name in capability.schema.get("required", []) if name not in arguments
    ]
    if missing:
        rendered = ", ".join(repr(name) for name in missing)
        raise CapabilityArgumentError(f"Missing required argument(s): {rendered}")
    for name, value in arguments.items():
        _validate_value(name, properties[name], value)
    return dict(arguments)


__all__ = [
    "ALLOWED_MCP_TOOLS",
    "LIST_TOOL_FILTERS",
    "Capability",
    "CapabilityArgumentError",
    "Manifest",
    "build_manifest",
    "validate",
]
