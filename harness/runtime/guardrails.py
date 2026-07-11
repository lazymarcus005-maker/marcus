import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from harness.db.enums import RunStatus
from harness.db.models import AgentRun, ToolExecution

REPEATED_CALL_WINDOW = 3


class GuardrailViolation(Exception):
    """Raised when a run exceeds a configured limit; carries the terminal status to apply."""

    def __init__(self, reason: str, status: RunStatus = RunStatus.failed) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def check_before_step(run: AgentRun) -> None:
    if run.cancel_requested:
        raise GuardrailViolation("run was cancelled", status=RunStatus.cancelled)
    if run.current_step >= run.max_steps:
        raise GuardrailViolation(
            f"exceeded max_steps ({run.max_steps})", status=RunStatus.timed_out
        )
    elapsed = (datetime.now(UTC) - run.created_at).total_seconds()
    if elapsed > run.timeout_seconds:
        raise GuardrailViolation(
            f"exceeded timeout_seconds ({run.timeout_seconds})", status=RunStatus.timed_out
        )


def token_budget_exceeded(run: AgentRun, tokens_used: int) -> bool:
    return tokens_used > run.token_budget


def check_tool_call_budget(run: AgentRun, incoming_call_count: int) -> None:
    if run.tool_calls_used + incoming_call_count > run.max_tool_calls:
        raise GuardrailViolation(
            f"exceeded max_tool_calls ({run.max_tool_calls})", status=RunStatus.timed_out
        )


async def check_repeated_calls(
    session: AsyncSession,
    run_id: uuid.UUID,
    tool_name: str,
    arguments: dict,
    *,
    window: int = REPEATED_CALL_WINDOW,
) -> None:
    """Abort the run if this call would repeat the same tool+args ``window`` times running."""
    result = await session.execute(
        sa.select(ToolExecution.tool_name, ToolExecution.args)
        .where(ToolExecution.run_id == run_id)
        .order_by(ToolExecution.started_at.desc())
        .limit(window - 1)
    )
    rows = result.all()
    if len(rows) < window - 1:
        return
    if all(name == tool_name and args == arguments for name, args in rows):
        raise GuardrailViolation(
            f"tool {tool_name!r} called with identical arguments {window} times in a row",
            status=RunStatus.failed,
        )
