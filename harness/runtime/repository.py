import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from harness.config import get_settings
from harness.db.enums import Channel, MessageRole, RunStatus, StepType
from harness.db.models import AgentMessage, AgentRun, AgentStep
from harness.runtime.state_machine import assert_valid_transition


class StaleRunError(Exception):
    """Raised when a checkpoint write's expected version no longer matches the row."""


class RunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_run(
        self,
        *,
        tenant_id: uuid.UUID,
        goal: str,
        channel: Channel = Channel.api,
        channel_metadata: dict | None = None,
        created_by_user_id: uuid.UUID | None = None,
        max_steps: int | None = None,
        max_tool_calls: int | None = None,
        token_budget: int | None = None,
        timeout_seconds: int | None = None,
    ) -> AgentRun:
        settings = get_settings()
        run = AgentRun(
            tenant_id=tenant_id,
            status=RunStatus.pending,
            goal=goal,
            channel=channel,
            channel_metadata=channel_metadata or {},
            created_by_user_id=created_by_user_id,
            max_steps=max_steps or settings.run_default_max_steps,
            max_tool_calls=max_tool_calls or settings.run_default_max_tool_calls,
            token_budget=token_budget or settings.run_default_token_budget,
            timeout_seconds=timeout_seconds or settings.run_default_timeout_seconds,
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def get(self, run_id: uuid.UUID) -> AgentRun | None:
        return await self.session.get(AgentRun, run_id)

    async def get_with_history(self, run_id: uuid.UUID) -> AgentRun | None:
        stmt = (
            sa.select(AgentRun)
            .where(AgentRun.id == run_id)
            .options(selectinload(AgentRun.messages), selectinload(AgentRun.steps))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def _versioned_update(
        self, run: AgentRun, values: dict[str, Any], *, extra_where: Any = None
    ) -> bool:
        """Apply an atomic UPDATE gated on the version this ``run`` was read at.

        Returns True if exactly one row was updated (and refreshes ``run`` in
        place), False if the version had already moved (someone else won).
        """
        expected_version = run.version
        conditions = [AgentRun.id == run.id, AgentRun.version == expected_version]
        if extra_where is not None:
            conditions.append(extra_where)

        stmt = sa.update(AgentRun).where(*conditions).values(**values, version=expected_version + 1)
        result = cast(CursorResult, await self.session.execute(stmt))
        if result.rowcount != 1:
            return False

        await self.session.flush()
        await self.session.refresh(run)
        return True

    async def checkpoint(
        self, run: AgentRun, *, status: RunStatus | None = None, **field_updates: Any
    ) -> AgentRun:
        """Persist a checkpoint for ``run``, gated on the version it was read at.

        Raises StaleRunError if another worker already advanced the run past
        the version this caller last observed.
        """
        if status is not None:
            assert_valid_transition(RunStatus(run.status), status)
            field_updates["status"] = status

        ok = await self._versioned_update(run, field_updates)
        if not ok:
            raise StaleRunError(f"run {run.id} was modified by another worker")
        return run

    async def try_acquire_lease(self, run: AgentRun, owner: str, ttl_seconds: int) -> bool:
        """Atomically claim ``run`` for ``owner`` if unleased or the lease expired."""
        now = datetime.now(UTC)
        ok = await self._versioned_update(
            run,
            {"lease_owner": owner, "lease_expires_at": now + timedelta(seconds=ttl_seconds)},
            extra_where=sa.or_(AgentRun.lease_owner.is_(None), AgentRun.lease_expires_at < now),
        )
        return ok

    async def renew_lease_by_id(self, run_id: uuid.UUID, owner: str, ttl_seconds: int) -> bool:
        """Renew a lease by run_id, independent of the optimistic-locking version.

        Used by the heartbeat task in harness.runtime.lease, which runs
        concurrently with (and on a separate session/connection from) whatever
        is actively checkpointing the run. Renewal must not participate in
        that version fencing — both sides bumping the same version from
        different tasks would make them spuriously invalidate each other even
        though nothing is actually wrong. Lease liveness and state-mutation
        fencing are different concerns; this only ever touches lease_expires_at,
        gated on still owning the lease.
        """
        stmt = (
            sa.update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.lease_owner == owner)
            .values(lease_expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds))
        )
        result = cast(CursorResult, await self.session.execute(stmt))
        await self.session.commit()
        return result.rowcount == 1

    async def release_lease(self, run: AgentRun) -> bool:
        return await self._versioned_update(run, {"lease_owner": None, "lease_expires_at": None})

    async def add_message(self, run_id: uuid.UUID, role: MessageRole, content: str) -> AgentMessage:
        message = AgentMessage(run_id=run_id, role=role, content=content)
        self.session.add(message)
        await self.session.flush()
        return message

    async def add_step(
        self,
        run_id: uuid.UUID,
        step_no: int,
        step_type: StepType,
        payload: dict,
        token_usage: dict | None = None,
    ) -> AgentStep:
        step = AgentStep(
            run_id=run_id,
            step_no=step_no,
            type=step_type,
            payload=payload,
            token_usage=token_usage or {},
        )
        self.session.add(step)
        await self.session.flush()
        return step
