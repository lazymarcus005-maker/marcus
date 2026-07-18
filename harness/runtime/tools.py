import enum
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from harness.db.enums import RiskTier
from harness.llm.types import ToolSpec

ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]


class ToolErrorCode(enum.StrEnum):
    invalid_argument = "INVALID_ARGUMENT"
    unknown_tool = "UNKNOWN_TOOL"
    approval_denied = "APPROVAL_DENIED"
    execution_failed = "EXECUTION_FAILED"
    outcome_unknown = "OUTCOME_UNKNOWN"
    policy_denied = "POLICY_DENIED"


class ToolRuntimeError(RuntimeError):
    def __init__(self, message: str, *, code: ToolErrorCode, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def error_observation(
    message: str,
    *,
    code: ToolErrorCode | str,
    retryable: bool,
    evidence_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "status": "error",
        "error": message,
        "code": str(code),
        "retryable": retryable,
    }
    if evidence_id is not None:
        observation["evidence_id"] = str(evidence_id)
    return observation


def success_observation(result: dict[str, Any], *, evidence_id: uuid.UUID) -> dict[str, Any]:
    return {**result, "status": "ok", "evidence_id": str(evidence_id)}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    risk_tier: RiskTier = RiskTier.read_only
    idempotent: bool = False
    # Runtime semantics used by the CLI loop. Risk tier answers whether an
    # action needs approval; it does not reliably say whether workspace state
    # changes or whether the result is verification evidence.
    mutates_workspace: bool = False
    evidence_type: str | None = None
    volatile: bool = False
    mcp_server_id: uuid.UUID | None = None

    def to_spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)


@dataclass
class ExecutionOutcome:
    """Result of a single tool call, as fed back to the LLM and the engine."""

    observation: dict[str, Any]
    fatal: bool = False
    fatal_reason: str | None = None
    needs_approval: bool = False
