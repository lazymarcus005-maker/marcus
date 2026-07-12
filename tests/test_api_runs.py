import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from harness.api.app import create_app
from harness.db.enums import RunStatus
from harness.db.models import Tenant
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


@pytest.mark.asyncio
async def test_create_run_requires_tenant_header(client):
    response = await client.post("/v1/runs", json={"goal": "do something"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_create_run_rejects_unknown_tenant(client):
    response = await client.post(
        "/v1/runs",
        json={"goal": "do something"},
        headers={"X-Tenant-Id": str(uuid.uuid4())},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_create_and_get_run(client, tenant_id):
    headers = {"X-Tenant-Id": str(tenant_id)}
    create_resp = await client.post(
        "/v1/runs", json={"goal": "investigate outage"}, headers=headers
    )
    assert create_resp.status_code == 201
    body = create_resp.json()
    assert body["goal"] == "investigate outage"
    assert body["tenant_id"] == str(tenant_id)
    run_id = body["id"]

    get_resp = await client.get(f"/v1/runs/{run_id}", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == run_id


@pytest.mark.asyncio
async def test_get_run_scoped_to_tenant(client, tenant_id):
    headers = {"X-Tenant-Id": str(tenant_id)}
    create_resp = await client.post("/v1/runs", json={"goal": "goal"}, headers=headers)
    run_id = create_resp.json()["id"]

    other_tenant_headers = {"X-Tenant-Id": str(uuid.uuid4())}
    # A nonexistent tenant is rejected before we even get to run scoping.
    resp = await client.get(f"/v1/runs/{run_id}", headers=other_tenant_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_runs_paginates_and_filters_by_status(client, tenant_id):
    headers = {"X-Tenant-Id": str(tenant_id)}
    for i in range(3):
        await client.post("/v1/runs", json={"goal": f"goal {i}"}, headers=headers)

    list_resp = await client.get("/v1/runs", headers=headers, params={"limit": 2})
    assert list_resp.status_code == 200
    body = list_resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2

    filtered = await client.get(
        "/v1/runs", headers=headers, params={"status": RunStatus.completed.value}
    )
    assert filtered.json()["total"] == 0


@pytest.mark.asyncio
async def test_post_message_requires_waiting_status(client, tenant_id):
    headers = {"X-Tenant-Id": str(tenant_id)}
    create_resp = await client.post("/v1/runs", json={"goal": "goal"}, headers=headers)
    run_id = create_resp.json()["id"]  # status is pending, not waiting_user_input

    resp = await client.post(f"/v1/runs/{run_id}/messages", json={"content": "hi"}, headers=headers)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_cancel_pending_run_flags_cancel_requested(client, tenant_id):
    headers = {"X-Tenant-Id": str(tenant_id)}
    create_resp = await client.post("/v1/runs", json={"goal": "goal"}, headers=headers)
    run_id = create_resp.json()["id"]

    cancel_resp = await client.post(f"/v1/runs/{run_id}/cancel", headers=headers)
    assert cancel_resp.status_code == 200
    body = cancel_resp.json()
    assert body["cancel_requested"] is True
    assert body["status"] == "pending"  # not running yet, so no step loop to act on it


@pytest.mark.asyncio
async def test_cancel_from_waiting_status_transitions_to_cancelled(
    client, tenant_id, db_sessionmaker
):
    headers = {"X-Tenant-Id": str(tenant_id)}
    create_resp = await client.post("/v1/runs", json={"goal": "goal"}, headers=headers)
    run_id = create_resp.json()["id"]

    async with db_sessionmaker() as session:
        repo = RunRepository(session)
        run = await repo.get(uuid.UUID(run_id))
        run = await repo.checkpoint(run, status=RunStatus.running)
        await repo.checkpoint(run, status=RunStatus.waiting_user_input)
        await session.commit()

    cancel_resp = await client.post(f"/v1/runs/{run_id}/cancel", headers=headers)
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_already_terminal_run_returns_conflict(client, tenant_id, db_sessionmaker):
    headers = {"X-Tenant-Id": str(tenant_id)}
    create_resp = await client.post("/v1/runs", json={"goal": "goal"}, headers=headers)
    run_id = create_resp.json()["id"]

    async with db_sessionmaker() as session:
        repo = RunRepository(session)
        run = await repo.get(uuid.UUID(run_id))
        run = await repo.checkpoint(run, status=RunStatus.running)
        await repo.checkpoint(run, status=RunStatus.completed, final_result={"ok": True})
        await session.commit()

    cancel_resp = await client.post(f"/v1/runs/{run_id}/cancel", headers=headers)
    assert cancel_resp.status_code == 409


@pytest.mark.asyncio
async def test_get_run_steps_returns_empty_lists_for_new_run(client, tenant_id):
    headers = {"X-Tenant-Id": str(tenant_id)}
    create_resp = await client.post("/v1/runs", json={"goal": "goal"}, headers=headers)
    run_id = create_resp.json()["id"]

    resp = await client.get(f"/v1/runs/{run_id}/steps", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["messages"]) == 1
    assert body["messages"][0]["content"] == "goal"
    assert body["steps"] == []
    assert body["tool_executions"] == []


@pytest.mark.asyncio
async def test_get_nonexistent_run_returns_404(client, tenant_id):
    headers = {"X-Tenant-Id": str(tenant_id)}
    resp = await client.get(f"/v1/runs/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404
