import uuid
from datetime import datetime
from typing import cast

import orjson
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from harness.db.enums import StepType, ToolExecutionStatus
from harness.db.models import AgentMessage, AgentRun, AgentStep, ToolExecution
from harness.llm.types import LLMMessage, Role, ToolCall

SYSTEM_PROMPT_TEMPLATE = (
    "You are an autonomous agent working towards a goal on behalf of a user.\n\n"
    "Goal: {goal}\n\n"
    "Call the `finish` tool once the goal is achieved, passing your result. "
    "Call `ask_user` if you need clarification before continuing."
)


async def build_llm_messages(session: AsyncSession, run: AgentRun) -> list[LLMMessage]:
    # Queried explicitly rather than via run.messages: repository.checkpoint()
    # refreshes `run` after most writes, which expires relationship collections,
    # and lazy-loading them here would require IO the async driver can't do
    # implicitly mid-attribute-access.
    conversation = await load_messages(session, run.id)
    steps = await load_steps(session, run.id)
    executions_by_step = await load_tool_executions_by_step(session, run.id)
    covered_up_to = latest_covered_step_no(steps)

    timeline: list[tuple[datetime, list[LLMMessage]]] = []
    for message in conversation:
        # message.role comes back from the DB as a plain str (the column isn't a
        # native SQL enum), so it's already the right value — just widen the type.
        role = cast(Role, message.role)
        timeline.append((message.created_at, [LLMMessage(role=role, content=message.content)]))
    for step in steps:
        if step.type == StepType.llm_call:
            if step.step_no <= covered_up_to:
                continue  # folded into a summary step below; don't duplicate it
            timeline.append(
                (
                    step.created_at,
                    llm_call_step_to_messages(step, executions_by_step.get(step.step_no, [])),
                )
            )
        elif step.type == StepType.summary:
            summary_text = step.payload.get("summary", "")
            timeline.append(
                (
                    step.created_at,
                    [
                        LLMMessage(
                            role="system", content=f"[Earlier steps summarized]: {summary_text}"
                        )
                    ],
                )
            )

    timeline.sort(key=lambda item: item[0])

    messages = [LLMMessage(role="system", content=SYSTEM_PROMPT_TEMPLATE.format(goal=run.goal))]
    for _, chunk in timeline:
        messages.extend(chunk)
    return messages


def llm_call_step_to_messages(step: AgentStep, executions: list[ToolExecution]) -> list[LLMMessage]:
    tool_calls_raw = step.payload.get("tool_calls", [])
    tool_calls = [
        ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"]) for tc in tool_calls_raw
    ]
    result = [
        LLMMessage(role="assistant", content=step.payload.get("content"), tool_calls=tool_calls)
    ]

    executions_by_index = {e.call_index: e for e in executions}
    for index, call in enumerate(tool_calls):
        execution = executions_by_index.get(index)
        if execution is None:
            content: dict = {"error": "execution record missing"}
        elif execution.status == ToolExecutionStatus.succeeded:
            content = execution.result or {}
        else:
            content = {"error": execution.error or "unknown failure"}
        result.append(
            LLMMessage(role="tool", tool_call_id=call.id, content=orjson.dumps(content).decode())
        )

    return result


def latest_covered_step_no(steps: list[AgentStep]) -> int:
    """Highest llm_call step_no already folded into a summary step, or -1 if none."""
    covered = -1
    for step in steps:
        if step.type == StepType.summary:
            covered = max(covered, step.payload.get("covers_up_to_step_no", -1))
    return covered


async def load_messages(session: AsyncSession, run_id: uuid.UUID) -> list[AgentMessage]:
    result = await session.execute(
        sa.select(AgentMessage)
        .where(AgentMessage.run_id == run_id)
        .order_by(AgentMessage.created_at)
    )
    return list(result.scalars().all())


async def load_steps(session: AsyncSession, run_id: uuid.UUID) -> list[AgentStep]:
    result = await session.execute(
        sa.select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.step_no)
    )
    return list(result.scalars().all())


async def load_tool_executions_by_step(
    session: AsyncSession, run_id: uuid.UUID
) -> dict[int, list[ToolExecution]]:
    result = await session.execute(
        sa.select(ToolExecution)
        .where(ToolExecution.run_id == run_id)
        .order_by(ToolExecution.step_no, ToolExecution.call_index)
    )
    by_step: dict[int, list[ToolExecution]] = {}
    for execution in result.scalars().all():
        by_step.setdefault(execution.step_no, []).append(execution)
    return by_step
