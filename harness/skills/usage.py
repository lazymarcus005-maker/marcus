import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from harness.db.enums import RunStatus
from harness.db.models import AgentRun, Skill, SkillRevision, SkillUsage

TERMINAL_SUCCESS = {RunStatus.completed}
TERMINAL_FAILURE = {RunStatus.failed, RunStatus.cancelled, RunStatus.timed_out}


def _latency_ms(run: AgentRun) -> int | None:
    if run.created_at is None:
        return None
    end = run.updated_at or datetime.now(UTC)
    return max(0, int((end - run.created_at).total_seconds() * 1000))


def _token_usage(run: AgentRun) -> dict[str, int]:
    return {
        "tokens_used": run.tokens_used,
        "tool_calls_used": run.tool_calls_used,
    }


async def record_skill_usage_for_run(session: AsyncSession, run: AgentRun) -> SkillUsage | None:
    """Create/update the skill_usage row for a terminal run that used a skill."""

    if run.active_skill_revision_id is None:
        return None

    status = RunStatus(run.status)
    if status not in TERMINAL_SUCCESS | TERMINAL_FAILURE:
        return None

    success = status in TERMINAL_SUCCESS
    values = {
        "tenant_id": run.tenant_id,
        "revision_id": run.active_skill_revision_id,
        "run_id": run.id,
        "success": success,
        "latency_ms": _latency_ms(run),
        "token_usage": _token_usage(run),
    }
    stmt = (
        insert(SkillUsage)
        .values(**values)
        .on_conflict_do_update(
            constraint="uq_skill_usage_revision_run",
            set_={
                "success": success,
                "latency_ms": values["latency_ms"],
                "token_usage": values["token_usage"],
            },
        )
        .returning(SkillUsage)
    )
    result = await session.execute(stmt)
    return result.scalar_one()


async def record_feedback_for_run(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    thumbs_up: bool,
    comment: str | None = None,
) -> SkillUsage | None:
    result = await session.execute(
        sa.select(SkillUsage)
        .join(AgentRun, SkillUsage.run_id == AgentRun.id)
        .where(SkillUsage.run_id == run_id, AgentRun.tenant_id == tenant_id)
    )
    usage = result.scalar_one_or_none()
    if usage is None:
        return None

    usage.feedback = {"thumbs_up": thumbs_up, "comment": comment}
    await session.flush()
    return usage


async def get_revision_usage_stats(
    session: AsyncSession, *, tenant_id: uuid.UUID, revision_id: uuid.UUID
) -> dict[str, Any] | None:
    owned = await session.execute(
        sa.select(SkillRevision.id)
        .join(Skill, SkillRevision.skill_id == Skill.id)
        .where(Skill.tenant_id == tenant_id, SkillRevision.id == revision_id)
    )
    if owned.scalar_one_or_none() is None:
        return None

    result = await session.execute(
        sa.select(
            sa.func.count(SkillUsage.id).label("total_runs"),
            sa.func.count(SkillUsage.id).filter(SkillUsage.success.is_(True)).label("successes"),
            sa.func.avg(SkillUsage.latency_ms).label("avg_latency_ms"),
            sa.func.avg(
                sa.cast(SkillUsage.token_usage["tokens_used"].astext, sa.Integer)
            ).label("avg_tokens"),
        ).where(SkillUsage.revision_id == revision_id, SkillUsage.tenant_id == tenant_id)
    )
    row = result.one()
    total_runs = int(row.total_runs or 0)
    successes = int(row.successes or 0)
    return {
        "revision_id": revision_id,
        "total_runs": total_runs,
        "successes": successes,
        "success_rate": (successes / total_runs) if total_runs else None,
        "avg_latency_ms": float(row.avg_latency_ms) if row.avg_latency_ms is not None else None,
        "avg_tokens": float(row.avg_tokens) if row.avg_tokens is not None else None,
    }
