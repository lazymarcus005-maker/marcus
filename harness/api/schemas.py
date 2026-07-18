import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from harness.db.enums import (
    ApprovalStatus,
    Channel,
    LlmProvider,
    McpHealthStatus,
    RiskTier,
    RunStatus,
    ScheduledJobStatus,
    SkillStatus,
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


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    role: str
    content: str
    created_at: datetime


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
    messages: list[MessageResponse] = Field(default_factory=list)
    steps: list[StepResponse]
    tool_executions: list[ToolExecutionResponse]


class RunFeedbackRequest(BaseModel):
    thumbs_up: bool
    comment: str | None = None


class SkillUsageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    revision_id: uuid.UUID
    run_id: uuid.UUID
    success: bool | None
    latency_ms: int | None
    token_usage: dict[str, Any]
    feedback: dict[str, Any] | None
    created_at: datetime


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


class ApiKeyCreateRequest(BaseModel):
    name: str
    user_id: uuid.UUID


class ApiKeyCreateResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    name: str
    prefix: str
    key: str
    enabled: bool
    created_at: datetime


class ApiKeyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    name: str
    prefix: str
    enabled: bool
    last_used_at: datetime | None
    created_at: datetime


class ScheduledJobCreateRequest(BaseModel):
    name: str
    cron_expression: str
    goal: str
    channel: Channel = Channel.schedule
    channel_metadata: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ScheduledJobUpdateRequest(BaseModel):
    cron_expression: str | None = None
    goal: str | None = None
    channel: Channel | None = None
    channel_metadata: dict[str, Any] | None = None
    enabled: bool | None = None


class ScheduledJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    cron_expression: str
    goal: str
    channel: Channel
    channel_metadata: dict[str, Any]
    enabled: bool
    status: ScheduledJobStatus
    last_run_at: datetime | None
    next_run_at: datetime | None
    last_run_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class SkillCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=10_000)
    owner_user_id: uuid.UUID | None = None


class SkillUpdateRequest(BaseModel):
    description: str | None = Field(default=None, max_length=10_000)
    owner_user_id: uuid.UUID | None = None


class SkillResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    description: str
    status: SkillStatus
    active_revision_id: uuid.UUID | None
    owner_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class SkillRevisionCreateRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=100_000)
    change_reason: str = Field(min_length=1, max_length=10_000)
    manifest: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    required_tools: list[str] = Field(default_factory=list, max_length=100)
    created_from_run_id: uuid.UUID | None = None


class SkillRevisionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    skill_id: uuid.UUID
    version: int
    status: SkillStatus
    instruction: str
    manifest: dict[str, Any]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    required_tools: list[str]
    change_reason: str
    created_from_run_id: uuid.UUID | None
    created_at: datetime


class SkillRevisionUsageStatsResponse(BaseModel):
    revision_id: uuid.UUID
    total_runs: int
    successes: int
    success_rate: float | None
    avg_latency_ms: float | None
    avg_tokens: float | None


class LlmSettingsUpdateRequest(BaseModel):
    provider: LlmProvider
    model: str
    api_key: str | None = None
    base_url: str | None = None


class LlmSettingsResponse(BaseModel):
    provider: LlmProvider
    base_url: str
    model: str
    has_api_key: bool
    updated_at: datetime


class LlmModelsRequest(BaseModel):
    provider: LlmProvider
    api_key: str | None = None
    base_url: str | None = None


class LlmModelsResponse(BaseModel):
    models: list[str]


class SlackSettingsUpdateRequest(BaseModel):
    bot_token: str | None = None
    signing_secret: str | None = None


class SlackSettingsResponse(BaseModel):
    has_bot_token: bool
    has_signing_secret: bool
    webhook_url: str
    updated_at: datetime
