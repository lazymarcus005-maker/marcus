import uuid

from harness.runtime.tools import (
    ToolErrorCode,
    error_observation,
    success_observation,
)


def test_error_observation_is_typed_and_machine_readable():
    evidence_id = uuid.uuid4()

    result = error_observation(
        "bad input",
        code=ToolErrorCode.invalid_argument,
        retryable=True,
        evidence_id=evidence_id,
    )

    assert result == {
        "status": "error",
        "error": "bad input",
        "code": "INVALID_ARGUMENT",
        "retryable": True,
        "evidence_id": str(evidence_id),
    }


def test_success_metadata_cannot_be_overridden_by_tool_result():
    evidence_id = uuid.uuid4()

    result = success_observation(
        {"value": 42, "status": "fake", "evidence_id": "fake"}, evidence_id=evidence_id
    )

    assert result["value"] == 42
    assert result["status"] == "ok"
    assert result["evidence_id"] == str(evidence_id)
