import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from harness.api.app import create_app
from harness.auth import create_api_key
from harness.config import get_settings
from harness.db.enums import Channel, ScheduledJobStatus, UserRole
from harness.db.models import ScheduledJob, Tenant, TenantQuota, User
from harness.db.session import get_session
from harness.runtime.repository import RunRepository
from harness.scheduler import fire_scheduled_job, next_run_time


@pytest.fixture
def app(db_sessionmaker):
    application = create_app()
    application.state.settings.api_key_rate_limit_per_minute = 0

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


async def _admin_key(session):
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    session.add(tenant)
    await session.flush()
    user = User(tenant_id=tenant.id, display_name="admin", role=UserRole.admin)
    session.add(user)
    await session.flush()
    _key, raw = await create_api_key(session, tenant_id=tenant.id, user_id=user.id, name="admin")
    await session.commit()
    return tenant, raw


@pytest.mark.asyncio
async def test_metrics_endpoint(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "harness_http_requests_total" in resp.text


@pytest.mark.asyncio
async def test_scheduled_job_api_validates_cron_and_sets_next_run(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        _tenant, raw = await _admin_key(session)

    bad = await client.post(
        "/v1/scheduled-jobs",
        json={"name": "bad", "cron_expression": "not cron", "goal": "run"},
        headers={"X-API-Key": raw},
    )
    assert bad.status_code == 400

    good = await client.post(
        "/v1/scheduled-jobs",
        json={"name": "daily", "cron_expression": "0 9 * * *", "goal": "daily report"},
        headers={"X-API-Key": raw},
    )
    assert good.status_code == 201
    assert good.json()["next_run_at"] is not None
    assert good.json()["status"] == "idle"


@pytest.mark.asyncio
async def test_fire_scheduled_job_creates_run_and_updates_job(db_session):
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    db_session.add(tenant)
    await db_session.flush()
    job = ScheduledJob(
        tenant_id=tenant.id,
        name="heartbeat",
        cron_expression="* * * * *",
        goal="check health",
        channel=Channel.schedule,
        channel_metadata={},
        enabled=True,
        status=ScheduledJobStatus.idle,
        next_run_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db_session.add(job)
    await db_session.commit()
    published: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def publisher(run_id, tenant_id):
        published.append((run_id, tenant_id))

    run = await fire_scheduled_job(db_session, job, settings=get_settings(), publisher=publisher)

    assert run.goal == "check health"
    assert run.channel == Channel.schedule
    assert job.last_run_id == run.id
    assert job.status == ScheduledJobStatus.idle
    assert published == [(run.id, tenant.id)]


@pytest.mark.asyncio
async def test_active_run_quota_blocks_create_run(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        tenant, raw = await _admin_key(session)
        session.add(TenantQuota(tenant_id=tenant.id, daily_token_quota=1_000_000, max_active_runs=1))
        repo = RunRepository(session)
        await repo.create_run(tenant_id=tenant.id, goal="already active")
        await session.commit()

    resp = await client.post(
        "/v1/runs",
        json={"goal": "new run"},
        headers={"X-API-Key": raw},
    )
    assert resp.status_code == 429
    assert "active run quota" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_fire_scheduled_job_respects_active_run_quota(db_session):
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(TenantQuota(tenant_id=tenant.id, daily_token_quota=1_000_000, max_active_runs=1))
    repo = RunRepository(db_session)
    await repo.create_run(tenant_id=tenant.id, goal="already active")
    job = ScheduledJob(
        tenant_id=tenant.id,
        name="heartbeat",
        cron_expression="* * * * *",
        goal="check health",
        channel=Channel.schedule,
        channel_metadata={},
        enabled=True,
        status=ScheduledJobStatus.idle,
        next_run_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db_session.add(job)
    await db_session.commit()
    published: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def publisher(run_id, tenant_id):
        published.append((run_id, tenant_id))

    with pytest.raises(HTTPException) as exc_info:
        await fire_scheduled_job(db_session, job, settings=get_settings(), publisher=publisher)

    assert exc_info.value.status_code == 429
    assert published == []
    assert job.status == ScheduledJobStatus.idle
    assert job.last_run_id is None


def test_next_run_time_parses_cron():
    assert next_run_time("*/5 * * * *", datetime(2026, 1, 1, tzinfo=UTC)) is not None
