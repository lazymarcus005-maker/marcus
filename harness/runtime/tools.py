import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from harness.db.enums import RiskTier
from harness.llm.types import ToolSpec

ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    risk_tier: RiskTier = RiskTier.read_only
    idempotent: bool = False
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
