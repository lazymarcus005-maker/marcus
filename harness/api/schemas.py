import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from harness.db.enums import (
    ApprovalStatus,
    Channel,
    McpHealthStatus,
    RiskTier,
    RunStatus,
    StepType,
)


class RunCreateRequest(BaseModel):
    goal: str
    channel: Channel = Channel.api
    channel_metadata: dict[str, Any] = Field(default_factory=dict)
    max_steps: int | None = None
    max_tool_calls: int | None = None
    token_budget: int | None = None
    timeout_seconds: int | None = None


class RunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    status: RunStatus
    goal: str
    channel: Channel
    current_step: int
    max_steps: int
    max_tool_calls: int
    token_budget: int
    timeout_seconds: int
    tokens_used: int
    tool_calls_used: int
    cancel_requested: bool
    final_result: dict[str, Any] | None
    error: str | None
    created_at: datetime
    updated_at: datetime


class RunListResponse(BaseModel):
    items: list[RunResponse]
    limit: int
    offset: int
    total: int


class MessageCreateRequest(BaseModel):
    content: str


class StepResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    step_no: int
    type: StepType
    payload: dict[str, Any]
    token_usage: dict[str, Any]
    created_at: datetime


class ToolExecutionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    step_no: int
    call_index: int
    tool_name: str
    risk_tier: str
    status: str
    args: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class RunStepsResponse(BaseModel):
    steps: list[StepResponse]
    tool_executions: list[ToolExecutionResponse]


class McpServerCreateRequest(BaseModel):
    name: str
    base_url: str
    auth_header_name: str | None = None
    auth_header_value: str | None = None
    default_risk_tier: RiskTier = RiskTier.read_only


class McpServerUpdateRequest(BaseModel):
    base_url: str | None = None
    auth_header_name: str | None = None
    auth_header_value: str | None = None
    default_risk_tier: RiskTier | None = None
    enabled: bool | None = None


class McpServerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    base_url: str
    auth_header_name: str | None
    default_risk_tier: RiskTier
    enabled: bool
    health_status: McpHealthStatus
    last_health_checked_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class McpServerRefreshResponse(BaseModel):
    tool_count: int


class McpToolResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    mcp_server_id: uuid.UUID
    name: str
    description: str
    parameters: dict[str, Any]
    risk_tier: RiskTier
    enabled: bool
    discovered_at: datetime


class McpToolUpdateRequest(BaseModel):
    risk_tier: RiskTier | None = None
    enabled: bool | None = None


class ApprovalDecisionRequest(BaseModel):
    decision: ApprovalStatus
    reason: str | None = None
    decided_by_user_id: uuid.UUID | None = None


class ApprovalRequestResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    step_no: int
    call_index: int
    tool_name: str
    risk_tier: RiskTier
    args: dict[str, Any]
    status: ApprovalStatus
    reason: str | None
    decided_by_user_id: uuid.UUID | None
    requested_at: datetime
    decided_at: datetime | None
    expires_at: datetime


class ApprovalRequestListResponse(BaseModel):
    items: list[ApprovalRequestResponse]
