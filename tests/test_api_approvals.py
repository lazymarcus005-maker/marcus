import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from harness.api.app import create_app
from harness.db.enums import ApprovalStatus, MessageRole, RiskTier, RunStatus
from harness.db.models import ApprovalRequest, Tenant
from harness.db.session import get_session
from harness.runtime.repository import RunRepository


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


@pytest_asyncio.fixture
async def tenant_id(db_sessionmaker):
    async with db_sessionmaker() as session:
        tenant = Tenant(name=f"t-{uuid.uuid4()}")
        session.add(tenant)
        await session.commit()
        return tenant.id


async def _make_pending_approval(db_sessionmaker, tenant_id):
    async with db_sessionmaker() as session:
        repo = RunRepository(session)
        run = await repo.create_run(tenant_id=tenant_id, goal="delete something")
        await repo.add_message(run.id, MessageRole.user, "delete something")
        run = await repo.checkpoint(run, status=RunStatus.running)
        approval = ApprovalRequest(
            tenant_id=tenant_id,
            run_id=run.id,
            step_no=0,
            call_index=0,
            tool_name="delete_resource",
            risk_tier=RiskTier.destructive,
            args={"id": "abc"},
            status=ApprovalStatus.pending,
            expires_at=run.created_at,
        )
        session.add(approval)
        await session.commit()
        return run.id, approval.id


@pytest.mark.asyncio
async def test_decide_unknown_approval_returns_404(client, tenant_id):
    headers = {"X-Tenant-Id": str(tenant_id)}
    resp = await client.post(
        f"/v1/approvals/{uuid.uuid4()}/decide", json={"decision": "approved"}, headers=headers
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_decide_rejects_invalid_decision_value(client, tenant_id, db_sessionmaker):
    _run_id, approval_id = await _make_pending_approval(db_sessionmaker, tenant_id)
    headers = {"X-Tenant-Id": str(tenant_id)}

    resp = await client.post(
        f"/v1/approvals/{approval_id}/decide", json={"decision": "pending"}, headers=headers
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_decide_twice_returns_conflict(client, tenant_id, db_sessionmaker):
    # waiting_approval=False here: the run stays `running` so the endpoint's
    # publish-on-decide path (which needs a live broker) doesn't fire — this
    # test is only exercising the double-decide conflict, not the resume path.
    _run_id, approval_id = await _make_pending_approval(db_sessionmaker, tenant_id)
    headers = {"X-Tenant-Id": str(tenant_id)}

    first = await client.post(
        f"/v1/approvals/{approval_id}/decide",
        json={"decision": "rejected", "reason": "no"},
        headers=headers,
    )
    assert first.status_code == 200
    assert first.json()["status"] == "rejected"

    second = await client.post(
        f"/v1/approvals/{approval_id}/decide", json={"decision": "approved"}, headers=headers
    )
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_list_approvals_filters_by_status(client, tenant_id, db_sessionmaker):
    await _make_pending_approval(db_sessionmaker, tenant_id)

    headers = {"X-Tenant-Id": str(tenant_id)}
    resp = await client.get("/v1/approvals", params={"status": "pending"}, headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = await client.get("/v1/approvals", params={"status": "approved"}, headers=headers)
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_run_approvals(client, tenant_id, db_sessionmaker):
    run_id, _approval_id = await _make_pending_approval(db_sessionmaker, tenant_id)

    headers = {"X-Tenant-Id": str(tenant_id)}
    resp = await client.get(f"/v1/runs/{run_id}/approvals", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["run_id"] == str(run_id)
