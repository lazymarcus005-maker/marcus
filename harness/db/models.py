import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from harness.db.base import Base
from harness.db.enums import (
    ApprovalStatus,
    Channel,
    McpHealthStatus,
    MessageRole,
    RiskTier,
    RunStatus,
    StepType,
    ToolExecutionStatus,
    UserRole,
)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    created_at: Mapped[datetime] = _created_at()


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_tenant_id", "tenant_id"),
        UniqueConstraint("tenant_id", "slack_user_id", name="uq_users_tenant_slack_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(String(20), nullable=False, default=UserRole.member)
    slack_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = _created_at()


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    status: Mapped[RunStatus] = mapped_column(String(30), nullable=False, default=RunStatus.pending)
    # Optimistic-locking fencing token. Every checkpoint write must match the
    # version it read; a mismatch means another worker already advanced the run.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    goal: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[Channel] = mapped_column(String(20), nullable=False, default=Channel.api)
    channel_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    current_step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False)
    max_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)

    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_calls_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    active_skill_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # Names of MCP tools this run has unlocked via the load_tool meta-tool
    # (progressive disclosure — see decisions.md / issue #15). Only tools
    # named here get their full schema sent to the LLM.
    active_tool_names: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    # Crash-recovery lease (see decisions.md D5 / issue #10).
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    final_result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(nullable=False, default=False)

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    messages: Mapped[list["AgentMessage"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="AgentMessage.created_at"
    )
    steps: Mapped[list["AgentStep"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="AgentStep.step_no"
    )

    __table_args__ = (
        Index("ix_agent_runs_tenant_id", "tenant_id"),
        Index("ix_agent_runs_status", "status"),
        Index("ix_agent_runs_lease_expires_at", "lease_expires_at"),
    )


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[MessageRole] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()

    run: Mapped[AgentRun] = relationship(back_populates="messages")

    __table_args__ = (Index("ix_agent_messages_run_id", "run_id"),)


class AgentStep(Base):
    __tablename__ = "agent_steps"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[StepType] = mapped_column(String(20), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    token_usage: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()

    run: Mapped[AgentRun] = relationship(back_populates="steps")

    __table_args__ = (
        Index("ix_agent_steps_run_id", "run_id"),
        UniqueConstraint("run_id", "step_no", name="uq_agent_steps_run_step_no"),
    )


class ToolExecution(Base):
    __tablename__ = "tool_executions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    call_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # Written before the tool call executes so a crash mid-call is detectable on resume.
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mcp_server_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    risk_tier: Mapped[RiskTier] = mapped_column(String(20), nullable=False)
    idempotent: Mapped[bool] = mapped_column(nullable=False, default=False)

    args: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[ToolExecutionStatus] = mapped_column(
        String(20), nullable=False, default=ToolExecutionStatus.started
    )
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_tool_executions_run_id", "run_id"),
        Index("ix_tool_executions_status", "status"),
    )


class UsageRecord(Base):
    __tablename__ = "usage_records"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        Index("ix_usage_records_tenant_id", "tenant_id"),
        Index("ix_usage_records_tenant_created_at", "tenant_id", "created_at"),
    )


class McpServer(Base):
    """A registered MCP-over-HTTP server. Also the Level-1 "domain" for
    progressive tool disclosure (issue #15) — one server, one domain.
    """

    __tablename__ = "mcp_servers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    auth_header_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Fernet-encrypted (harness.mcp.crypto), never stored or returned in plaintext.
    auth_header_value_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    default_risk_tier: Mapped[RiskTier] = mapped_column(
        String(20), nullable=False, default=RiskTier.read_only
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    health_status: Mapped[McpHealthStatus] = mapped_column(
        String(20), nullable=False, default=McpHealthStatus.unknown
    )
    last_health_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    tools: Mapped[list["McpTool"]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_mcp_servers_tenant_id", "tenant_id"),
        UniqueConstraint("tenant_id", "name", name="uq_mcp_servers_tenant_name"),
    )


class McpTool(Base):
    """A tool discovered from an MCP server's tools/list, cached so the engine

    doesn't have to hit the server on every LLM call (see harness/mcp/registry.py).
    """

    __tablename__ = "mcp_tools"

    id: Mapped[uuid.UUID] = _uuid_pk()
    mcp_server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Seeded from the server's default_risk_tier at discovery time, then
    # independently overridable per tool (decisions.md — Phase 2 clarification).
    risk_tier: Mapped[RiskTier] = mapped_column(
        String(20), nullable=False, default=RiskTier.read_only
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    discovered_at: Mapped[datetime] = _created_at()

    server: Mapped[McpServer] = relationship(back_populates="tools")

    __table_args__ = (
        Index("ix_mcp_tools_mcp_server_id", "mcp_server_id"),
        UniqueConstraint("mcp_server_id", "name", name="uq_mcp_tools_server_name"),
    )


class ApprovalRequest(Base):
    """Human approval gate for sensitive_write/destructive tool calls (issue #17,

    decisions.md D6/Q15). Keyed on the same (run_id, step_no, call_index)
    natural key as tool_executions.idempotency_key so the engine can look up
    a call's approval state deterministically on replay.
    """

    __tablename__ = "approval_requests"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    call_index: Mapped[int] = mapped_column(Integer, nullable=False)

    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    risk_tier: Mapped[RiskTier] = mapped_column(String(20), nullable=False)
    args: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    status: Mapped[ApprovalStatus] = mapped_column(
        String(20), nullable=False, default=ApprovalStatus.pending
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    requested_at: Mapped[datetime] = _created_at()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_approval_requests_tenant_status", "tenant_id", "status"),
        Index("ix_approval_requests_expires_at", "expires_at"),
        UniqueConstraint(
            "run_id", "step_no", "call_index", name="uq_approval_requests_run_step_call"
        ),
    )
