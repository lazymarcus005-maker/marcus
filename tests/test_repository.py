import uuid

import pytest

from harness.db.enums import MessageRole, RunStatus, StepType
from harness.db.models import Tenant
from harness.runtime.repository import RunRepository, StaleRunError
from harness.runtime.state_machine import InvalidTransitionError, assert_valid_transition


async def _make_tenant(session) -> uuid.UUID:
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    session.add(tenant)
    await session.flush()
    return tenant.id


@pytest.mark.asyncio
async def test_create_run_applies_defaults(db_session):
    tenant_id = await _make_tenant(db_session)
    repo = RunRepository(db_session)

    run = await repo.create_run(tenant_id=tenant_id, goal="investigate the outage")

    assert run.status == RunStatus.pending
    assert run.version == 0
    assert run.max_steps > 0
    assert run.token_budget > 0


@pytest.mark.asyncio
async def test_checkpoint_advances_version_and_status(db_session):
    tenant_id = await _make_tenant(db_session)
    repo = RunRepository(db_session)
    run = await repo.create_run(tenant_id=tenant_id, goal="goal")

    updated = await repo.checkpoint(run, status=RunStatus.running, current_step=1)

    assert updated.version == 1
    assert updated.status == RunStatus.running
    assert updated.current_step == 1


@pytest.mark.asyncio
async def test_checkpoint_rejects_invalid_transition(db_session):
    tenant_id = await _make_tenant(db_session)
    repo = RunRepository(db_session)
    run = await repo.create_run(tenant_id=tenant_id, goal="goal")
    await repo.checkpoint(run, status=RunStatus.running)
    await repo.checkpoint(run, status=RunStatus.completed, final_result={"ok": True})

    with pytest.raises(InvalidTransitionError):
        await repo.checkpoint(run, status=RunStatus.running)


@pytest.mark.asyncio
async def test_checkpoint_raises_stale_run_error_on_concurrent_write(db_sessionmaker):
    async with db_sessionmaker() as setup_session:
        tenant_id = await _make_tenant(setup_session)
        repo = RunRepository(setup_session)
        run = await repo.create_run(tenant_id=tenant_id, goal="goal")
        run_id = run.id
        await setup_session.commit()

    # Two independent "workers" each load their own copy of the run at version 0.
    async with db_sessionmaker() as session_a, db_sessionmaker() as session_b:
        repo_a = RunRepository(session_a)
        repo_b = RunRepository(session_b)

        run_a = await repo_a.get(run_id)
        run_b = await repo_b.get(run_id)

        await repo_a.checkpoint(run_a, status=RunStatus.running, current_step=1)
        await session_a.commit()

        with pytest.raises(StaleRunError):
            await repo_b.checkpoint(run_b, status=RunStatus.running, current_step=1)


@pytest.mark.asyncio
async def test_lease_acquire_renew_release_roundtrip(db_session):
    tenant_id = await _make_tenant(db_session)
    repo = RunRepository(db_session)
    run = await repo.create_run(tenant_id=tenant_id, goal="goal")

    acquired = await repo.try_acquire_lease(run, "worker-a", ttl_seconds=30)
    assert acquired
    assert run.lease_owner == "worker-a"

    renewed = await repo.renew_lease(run, "worker-a", ttl_seconds=30)
    assert renewed

    released = await repo.release_lease(run)
    assert released
    assert run.lease_owner is None


@pytest.mark.asyncio
async def test_lease_cannot_be_stolen_while_active(db_sessionmaker):
    async with db_sessionmaker() as setup_session:
        tenant_id = await _make_tenant(setup_session)
        repo = RunRepository(setup_session)
        run = await repo.create_run(tenant_id=tenant_id, goal="goal")
        run_id = run.id
        await setup_session.commit()

    async with db_sessionmaker() as session_a, db_sessionmaker() as session_b:
        repo_a = RunRepository(session_a)
        repo_b = RunRepository(session_b)

        run_a = await repo_a.get(run_id)
        assert await repo_a.try_acquire_lease(run_a, "worker-a", ttl_seconds=30)
        await session_a.commit()

        run_b = await repo_b.get(run_id)
        stolen = await repo_b.try_acquire_lease(run_b, "worker-b", ttl_seconds=30)
        assert not stolen


@pytest.mark.asyncio
async def test_resume_after_crash_reads_persisted_checkpoint(db_sessionmaker):
    async with db_sessionmaker() as session_a:
        tenant_id = await _make_tenant(session_a)
        repo_a = RunRepository(session_a)
        run = await repo_a.create_run(tenant_id=tenant_id, goal="long task")
        await repo_a.checkpoint(run, status=RunStatus.running, current_step=1)
        await repo_a.add_message(run.id, MessageRole.user, "do the thing")
        await repo_a.add_step(run.id, 0, StepType.llm_call, {"note": "first step"})
        await session_a.commit()
        run_id = run.id
    # session_a "crashes" here (goes out of scope without further writes)

    async with db_sessionmaker() as session_b:
        repo_b = RunRepository(session_b)
        resumed = await repo_b.get_with_history(run_id)

        assert resumed is not None
        assert resumed.status == RunStatus.running
        assert resumed.current_step == 1
        assert len(resumed.messages) == 1
        assert len(resumed.steps) == 1


def test_state_machine_rejects_leaving_terminal_states():
    with pytest.raises(InvalidTransitionError):
        assert_valid_transition(RunStatus.completed, RunStatus.running)
    with pytest.raises(InvalidTransitionError):
        assert_valid_transition(RunStatus.cancelled, RunStatus.pending)


def test_state_machine_allows_expected_paths():
    assert_valid_transition(RunStatus.pending, RunStatus.running)
    assert_valid_transition(RunStatus.running, RunStatus.waiting_approval)
    assert_valid_transition(RunStatus.waiting_approval, RunStatus.running)
    assert_valid_transition(RunStatus.running, RunStatus.completed)
