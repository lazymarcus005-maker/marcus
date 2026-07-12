import pytest

from harness.runtime.tool_validation import ToolArgumentError, normalize_and_validate_arguments

SCHEMA = {
    "type": "object",
    "properties": {
        "timeout": {"type": "number", "minimum": 0.1},
        "count": {"type": "integer"},
        "enabled": {"type": "boolean"},
    },
    "required": ["timeout"],
    "additionalProperties": False,
}


def test_safe_scalar_coercion_then_validation():
    result = normalize_and_validate_arguments(
        SCHEMA, {"timeout": "30", "count": "2", "enabled": "true"}
    )

    assert result == {"timeout": 30.0, "count": 2, "enabled": True}


def test_invalid_arguments_return_typed_error():
    with pytest.raises(ToolArgumentError) as exc_info:
        normalize_and_validate_arguments(SCHEMA, {"timeout": "not-a-number"})

    assert exc_info.value.code == "INVALID_ARGUMENT"
    assert exc_info.value.path == "timeout"


def test_unknown_fields_are_rejected_when_schema_forbids_them():
    with pytest.raises(ToolArgumentError, match="Additional properties"):
        normalize_and_validate_arguments(SCHEMA, {"timeout": 1, "surprise": True})
