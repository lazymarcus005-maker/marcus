import enum


class RunStatus(enum.StrEnum):
    pending = "pending"
    running = "running"
    waiting_user_input = "waiting_user_input"
    waiting_approval = "waiting_approval"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    timed_out = "timed_out"


TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.completed, RunStatus.failed, RunStatus.cancelled, RunStatus.timed_out}
)
WAITING_RUN_STATUSES = frozenset({RunStatus.waiting_user_input, RunStatus.waiting_approval})


class StepType(enum.StrEnum):
    llm_call = "llm_call"
    tool_result = "tool_result"
    summary = "summary"


class MessageRole(enum.StrEnum):
    user = "user"
    assistant = "assistant"
    system = "system"
    tool = "tool"


class ToolExecutionStatus(enum.StrEnum):
    started = "started"
    succeeded = "succeeded"
    failed = "failed"
    unknown = "unknown"


class RiskTier(enum.StrEnum):
    read_only = "read_only"
    low_risk_write = "low_risk_write"
    sensitive_write = "sensitive_write"
    destructive = "destructive"


class UserRole(enum.StrEnum):
    admin = "admin"
    member = "member"


class Channel(enum.StrEnum):
    api = "api"
    web = "web"
    slack = "slack"
    schedule = "schedule"


class McpHealthStatus(enum.StrEnum):
    unknown = "unknown"
    healthy = "healthy"
    unhealthy = "unhealthy"


class ApprovalStatus(enum.StrEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"


class SkillStatus(enum.StrEnum):
    draft = "draft"
    approved = "approved"
    published = "published"
    deprecated = "deprecated"


class ScheduledJobStatus(enum.StrEnum):
    idle = "idle"
    firing = "firing"
    disabled = "disabled"


class LlmProvider(enum.StrEnum):
    ollama_cloud = "ollama_cloud"
    openai = "openai"
    openrouter = "openrouter"


LLM_PROVIDER_DEFAULT_BASE_URLS = {
    LlmProvider.ollama_cloud: "https://ollama.com/v1",
    LlmProvider.openai: "https://api.openai.com/v1",
    LlmProvider.openrouter: "https://openrouter.ai/api/v1",
}
