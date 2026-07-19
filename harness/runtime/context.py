import re
import uuid
from datetime import datetime
from typing import cast

import orjson
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from harness.db.enums import StepType, ToolExecutionStatus
from harness.db.models import (
    AgentMessage,
    AgentRun,
    AgentStep,
    Skill,
    SkillRevision,
    SkillUsage,
    ToolExecution,
)
from harness.llm.types import LLMMessage, Role, ToolCall
from harness.skills.registry import SkillRegistry

SYSTEM_PROMPT_TEMPLATE = (
    "You are an autonomous agent working towards a goal on behalf of a user.\n\n"
    "Goal: {goal}\n\n"
    "Call the `finish` tool once the goal is achieved, passing your result. "
    "Call `ask_user` if you need clarification before continuing."
    "{skill_catalog}"
    "{active_skill}"
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

    skill_catalog, active_skill = await build_skill_context(session, run)
    messages = [
        LLMMessage(
            role="system",
            content=SYSTEM_PROMPT_TEMPLATE.format(
                goal=run.goal,
                skill_catalog=skill_catalog,
                active_skill=active_skill,
            ),
        )
    ]
    for _, chunk in timeline:
        messages.extend(chunk)
    return messages


async def build_skill_context(session: AsyncSession, run: AgentRun) -> tuple[str, str]:
    registry = SkillRegistry(session)
    active_skill = ""
    if run.active_skill_revision_id is not None:
        active = await registry.get_revision_by_id(run.tenant_id, run.active_skill_revision_id)
        if active is not None:
            skill, revision = active
            active_skill = (
                f"\n\nActive skill: {skill.name} v{revision.version}\n{revision.instruction}"
            )

    skills = await registry.list_published_skills(run.tenant_id)
    if not skills:
        return "", active_skill

    # Keep the prompt bounded and put likely matches first. The LLM still
    # makes the final selection via use_skill; this is only a cheap retrieval
    # hint based on words in the run goal and skill metadata.
    goal_words = set(re.findall(r"[a-z0-9_:-]{3,}", run.goal.lower()))

    def relevance(skill: Skill) -> tuple[int, str]:
        text = f"{skill.name} {skill.description}".lower()
        matches = len(goal_words & set(re.findall(r"[a-z0-9_:-]{3,}", text)))
        return (-matches, skill.name)

    skills = sorted(skills, key=relevance)[:20]

    catalog_lines = "\n".join(
        f"- {skill.name}: {skill.description or '(no description)'}" for skill in skills
    )
    return (
        "\n\n"
        "Published skill catalog:\n"
        f"{catalog_lines}\n"
        "If one of these skills is relevant, call `use_skill` with its exact name "
        "before proceeding. If several are similarly relevant, do not guess: "
        "ask the user or choose explicitly with `use_skill`.",
        active_skill,
    )


async def select_skill_candidate(
    session: AsyncSession, run: AgentRun
) -> tuple[Skill, SkillRevision] | None:
    """Return a clearly dominant skill, otherwise None for LLM selection.

    Automatic selection is intentionally conservative: a candidate needs at
    least three shared terms and must beat the runner-up by two terms. This
    makes ambiguous requests fall back to the explicit ``use_skill`` flow.
    """
    registry = SkillRegistry(session)
    skills = await registry.list_published_skills(run.tenant_id)
    goal_words = set(re.findall(r"[a-z0-9_:-]{3,}", run.goal.lower()))
    usage_result = await session.execute(
        sa.select(
            SkillUsage.revision_id,
            sa.func.avg(sa.case((SkillUsage.success.is_(True), 1.0), else_=0.0)).label(
                "success_rate"
            ),
        )
        .where(SkillUsage.tenant_id == run.tenant_id)
        .group_by(SkillUsage.revision_id)
    )
    success_rates = {row.revision_id: float(row.success_rate or 0.0) for row in usage_result}
    scored: list[tuple[int, float, Skill]] = []
    for skill in skills:
        metadata = set(re.findall(r"[a-z0-9_:-]{3,}", f"{skill.name} {skill.description}".lower()))
        lexical = len(goal_words & metadata)
        revision = await registry.get_active_revision_by_skill_name(run.tenant_id, skill.name)
        success_rate = success_rates.get(revision[1].id, 0.0) if revision else 0.0
        scored.append((lexical, success_rate, skill))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2].name))
    if not scored or scored[0][0] < 3:
        return None
    if (
        len(scored) > 1
        and scored[0][0] - scored[1][0] < 2
        and (scored[0][0] != scored[1][0] or scored[0][1] - scored[1][1] < 0.2)
    ):
        return None
    active = await registry.get_active_revision_by_skill_name(run.tenant_id, scored[0][2].name)
    return active


def llm_call_step_to_messages(step: AgentStep, executions: list[ToolExecution]) -> list[LLMMessage]:
    tool_calls_raw = step.payload.get("tool_calls", [])
    tool_calls = [
        ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"]) for tc in tool_calls_raw
    ]
    result = [
        LLMMessage(
            role="assistant",
            content=step.payload.get("content"),
            tool_calls=tool_calls,
            provider_fields=step.payload.get("provider_fields") or {},
        )
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
