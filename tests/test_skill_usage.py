import uuid

import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient

from harness.api.app import create_app
from harness.db.enums import MessageRole, RunStatus
from harness.db.models import SkillUsage, Tenant
from harness.db.session import get_session
from harness.runtime.engine import RunEngine
from harness.runtime.repository import RunRepository
from harness.skills.registry import SkillRegistry
from harness.skills.usage import record_skill_usage_for_run
from tests.fakes import ScriptedLLMGateway, tool_call_response


@pytest.fixture
def app(db_sessionmaker):
    application = create_app()

    async def override_get_session():
        async with db_sessionmaker() as session:
            yield session

    application.dependency_overrides[get_session] = override_get_session
    return application


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _make_tenant(session):
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    session.add(tenant)
    await session.flush()
    return tenant


async def _make_run(session, tenant, *, goal="triage the incident"):
    repo = RunRepository(session)
    run = await repo.create_run(tenant_id=tenant.id, goal=goal)
    await repo.add_message(run.id, MessageRole.user, goal)
    await session.commit()
    return run


async def _publish_skill(session, tenant):
    registry = SkillRegistry(session)
    skill = await registry.create_skill(
        tenant_id=tenant.id,
        name=f"incident_triage_{uuid.uuid4()}",
        description="Incident triage flow.",
    )
    revision = await registry.create_revision(
        tenant_id=tenant.id,
        skill_id=skill.id,
        instruction="Follow the incident triage checklist.",
        change_reason="initial",
    )
    assert revision is not None
    await registry.approve_revision(tenant.id, skill.id, revision.id)
    await registry.publish_revision(tenant.id, skill.id, revision.id)
    await session.commit()
    return skill, revision


@pytest.mark.asyncio
async def test_completed_skill_run_writes_usage_record(db_session):
    tenant = await _make_tenant(db_session)
    run = await _make_run(db_session, tenant)
    skill, revision = await _publish_skill(db_session, tenant)

    llm = ScriptedLLMGateway(
        [
            tool_call_response("use_skill", {"name": skill.name}),
            tool_call_response("finish", {"result": "incident handled"}),
        ]
    )
    engine = RunEngine(db_session, llm)

    final = await engine.run_until_blocked(run.id)

    usage = (
        await db_session.execute(sa.select(SkillUsage).where(SkillUsage.run_id == run.id))
    ).scalar_one()
    assert final.status == RunStatus.completed
    assert usage.revision_id == revision.id
    assert usage.success is True
    assert usage.latency_ms is not None
    assert usage.token_usage == {"tokens_used": final.tokens_used, "tool_calls_used": 2}


@pytest.mark.asyncio
async def test_run_feedback_endpoint_updates_skill_usage(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        tenant = await _make_tenant(session)
        run = await _make_run(session, tenant)
        _skill, revision = await _publish_skill(session, tenant)
        repo = RunRepository(session)
        run = await repo.checkpoint(run, status=RunStatus.running)
        run = await repo.checkpoint(
            run,
            status=RunStatus.completed,
            active_skill_revision_id=revision.id,
            tokens_used=42,
            tool_calls_used=1,
        )
        await record_skill_usage_for_run(session, run)
        await session.commit()
        tenant_id = tenant.id
        run_id = run.id

    resp = await client.post(
        f"/v1/runs/{run_id}/feedback",
        json={"thumbs_up": False, "comment": "missed the root cause"},
        headers={"X-Tenant-Id": str(tenant_id)},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == str(run_id)
    assert body["revision_id"] == str(revision.id)
    assert body["feedback"] == {"thumbs_up": False, "comment": "missed the root cause"}


@pytest.mark.asyncio
async def test_revision_usage_stats_endpoint_reports_success_rate(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        tenant = await _make_tenant(session)
        skill, revision = await _publish_skill(session, tenant)
        repo = RunRepository(session)
        for status, tokens in [(RunStatus.completed, 100), (RunStatus.failed, 50)]:
            run = await repo.create_run(tenant_id=tenant.id, goal=f"{status} run")
            run = await repo.checkpoint(run, status=RunStatus.running)
            run = await repo.checkpoint(
                run,
                status=status,
                active_skill_revision_id=revision.id,
                tokens_used=tokens,
                tool_calls_used=1,
            )
            await record_skill_usage_for_run(session, run)
        await session.commit()
        tenant_id = tenant.id
        skill_id = skill.id
        revision_id = revision.id

    resp = await client.get(
        f"/v1/skills/{skill_id}/revisions/{revision_id}/usage-stats",
        headers={"X-Tenant-Id": str(tenant_id)},
    )

    assert resp.status_code == 200
    assert resp.json()["total_runs"] == 2
    assert resp.json()["successes"] == 1
    assert resp.json()["success_rate"] == 0.5
    assert resp.json()["avg_tokens"] == 75.0
