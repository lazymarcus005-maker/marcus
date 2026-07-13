import copy
import re
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator


@dataclass(frozen=True)
class ToolArgumentError(ValueError):
    code: str
    message: str
    path: str = ""

    def __str__(self) -> str:
        location = f" at {self.path}" if self.path else ""
        return f"{self.code}{location}: {self.message}"


def normalize_and_validate_arguments(schema: dict[str, Any], arguments: Any) -> dict[str, Any]:
    """Apply conservative scalar coercions, then validate against the tool schema."""
    if not isinstance(arguments, dict):
        raise ToolArgumentError("INVALID_ARGUMENT", "tool arguments must be an object")
    normalized = copy.deepcopy(arguments)
    _coerce_object(schema, normalized)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(normalized), key=lambda e: list(e.path)
    )
    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.absolute_path)
        raise ToolArgumentError("INVALID_ARGUMENT", error.message, path)
    return normalized


def _coerce_object(schema: dict[str, Any], value: dict[str, Any]) -> None:
    properties = schema.get("properties") or {}
    for name, child_schema in properties.items():
        if name not in value or not isinstance(child_schema, dict):
            continue
        value[name] = _coerce_value(child_schema, value[name])


def _coerce_value(schema: dict[str, Any], value: Any) -> Any:
    expected = schema.get("type")
    if expected == "object" and isinstance(value, dict):
        _coerce_object(schema, value)
        return value
    if expected == "array" and isinstance(value, list):
        item_schema = schema.get("items") or {}
        return [_coerce_value(item_schema, item) for item in value]
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if expected == "integer" and re.fullmatch(r"[-+]?\d+", stripped):
        return int(stripped)
    if expected == "number" and re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", stripped):
        return float(stripped)
    if expected == "boolean" and stripped.lower() in {"true", "false"}:
        return stripped.lower() == "true"
    return value
