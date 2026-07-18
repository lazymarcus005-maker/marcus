import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from harness.api.app import create_app
from harness.auth import create_api_key
from harness.db.enums import ApprovalStatus, MessageRole, RiskTier, RunStatus, UserRole
from harness.db.models import ApprovalRequest, Tenant, User
from harness.db.session import get_session
from harness.runtime.repository import RunRepository


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


async def _tenant_user_key(session, *, role=UserRole.admin, tenant_name=None):
    tenant = Tenant(name=tenant_name or f"t-{uuid.uuid4()}")
    session.add(tenant)
    await session.flush()
    user = User(tenant_id=tenant.id, display_name=f"{role}-user", role=role)
    session.add(user)
    await session.flush()
    _api_key, raw_key = await create_api_key(
        session, tenant_id=tenant.id, user_id=user.id, name=f"{role}-key"
    )
    await session.commit()
    return tenant, user, raw_key


@pytest.mark.asyncio
async def test_missing_api_key_returns_401_once_keys_exist(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        tenant, _user, _key = await _tenant_user_key(session)

    resp = await client.post(
        "/v1/runs",
        json={"goal": "do something"},
        headers={"X-Tenant-Id": str(tenant.id)},
    )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_legacy_tenant_header_can_be_disabled(client, app, db_sessionmaker):
    app.state.settings.legacy_auth_enabled = False
    async with db_sessionmaker() as session:
        tenant = Tenant(name=f"t-{uuid.uuid4()}")
        session.add(tenant)
        await session.commit()

    resp = await client.post(
        "/v1/runs",
        json={"goal": "do something"},
        headers={"X-Tenant-Id": str(tenant.id)},
    )

    assert resp.status_code == 401
    assert resp.json()["detail"] == "missing API key"


@pytest.mark.asyncio
async def test_api_key_scopes_runs_to_own_tenant(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        tenant_a, _user_a, key_a = await _tenant_user_key(session)
        tenant_b, _user_b, key_b = await _tenant_user_key(session)
        repo = RunRepository(session)
        run_b = await repo.create_run(tenant_id=tenant_b.id, goal="tenant b run")
        await session.commit()

    create_resp = await client.post(
        "/v1/runs",
        json={"goal": "tenant a run"},
        headers={"Authorization": f"Bearer {key_a}"},
    )
    assert create_resp.status_code == 201
    assert create_resp.json()["tenant_id"] == str(tenant_a.id)

    hidden = await client.get(f"/v1/runs/{run_b.id}", headers={"X-API-Key": key_a})
    assert hidden.status_code == 404

    visible = await client.get(f"/v1/runs/{run_b.id}", headers={"X-API-Key": key_b})
    assert visible.status_code == 200


@pytest.mark.asyncio
async def test_member_can_only_list_own_runs(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        tenant = Tenant(name=f"t-{uuid.uuid4()}")
        session.add(tenant)
        await session.flush()
        member = User(tenant_id=tenant.id, display_name="member", role=UserRole.member)
        admin = User(tenant_id=tenant.id, display_name="admin", role=UserRole.admin)
        session.add_all([member, admin])
        await session.flush()
        _member_key, raw_member = await create_api_key(
            session, tenant_id=tenant.id, user_id=member.id, name="member"
        )
        repo = RunRepository(session)
        own = await repo.create_run(
            tenant_id=tenant.id, created_by_user_id=member.id, goal="own run"
        )
        await repo.create_run(tenant_id=tenant.id, created_by_user_id=admin.id, goal="admin run")
        await session.commit()

    resp = await client.get("/v1/runs", headers={"X-API-Key": raw_member})

    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["id"] == str(own.id)


@pytest.mark.asyncio
async def test_member_cannot_decide_approval(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        tenant = Tenant(name=f"t-{uuid.uuid4()}")
        session.add(tenant)
        await session.flush()
        member = User(tenant_id=tenant.id, display_name="member", role=UserRole.member)
        admin = User(tenant_id=tenant.id, display_name="admin", role=UserRole.admin)
        session.add_all([member, admin])
        await session.flush()
        _member_key, raw_member = await create_api_key(
            session, tenant_id=tenant.id, user_id=member.id, name="member"
        )
        _admin_key, raw_admin = await create_api_key(
            session, tenant_id=tenant.id, user_id=admin.id, name="admin"
        )
        repo = RunRepository(session)
        run = await repo.create_run(tenant_id=tenant.id, goal="delete")
        await repo.add_message(run.id, MessageRole.user, "delete")
        run = await repo.checkpoint(run, status=RunStatus.running)
        approval = ApprovalRequest(
            tenant_id=tenant.id,
            run_id=run.id,
            step_no=0,
            call_index=0,
            tool_name="delete_resource",
            risk_tier=RiskTier.destructive,
            args={},
            status=ApprovalStatus.pending,
            expires_at=run.created_at,
        )
        session.add(approval)
        await session.commit()
        approval_id = approval.id

    member_resp = await client.post(
        f"/v1/approvals/{approval_id}/decide",
        json={"decision": "approved"},
        headers={"X-API-Key": raw_member},
    )
    assert member_resp.status_code == 403

    admin_resp = await client.post(
        f"/v1/approvals/{approval_id}/decide",
        json={"decision": "rejected", "reason": "too risky"},
        headers={"X-API-Key": raw_admin},
    )
    assert admin_resp.status_code == 200
    assert admin_resp.json()["decided_by_user_id"] == str(admin.id)


@pytest.mark.asyncio
async def test_api_key_rate_limit(client, app, db_sessionmaker):
    app.state.settings.api_key_rate_limit_per_minute = 1
    async with db_sessionmaker() as session:
        _tenant, _user, raw_key = await _tenant_user_key(session)

    first = await client.get("/v1/runs", headers={"X-API-Key": raw_key})
    second = await client.get("/v1/runs", headers={"X-API-Key": raw_key})

    assert first.status_code == 200
    assert second.status_code == 429
