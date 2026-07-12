import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import DBAPIError

from harness.api.app import create_app
from harness.db.enums import Channel, RunStatus
from harness.db.models import AgentRun, SkillRevision, Tenant
from harness.db.session import get_session


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


async def _create_skill_with_revision(client, headers, instruction="Always be concise."):
    skill_resp = await client.post(
        "/v1/skills",
        json={"name": f"support-{uuid.uuid4()}", "description": "Support playbook"},
        headers=headers,
    )
    assert skill_resp.status_code == 201
    skill_id = skill_resp.json()["id"]

    revision_resp = await client.post(
        f"/v1/skills/{skill_id}/revisions",
        json={
            "instruction": instruction,
            "change_reason": "initial draft",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "required_tools": ["search"],
        },
        headers=headers,
    )
    assert revision_resp.status_code == 201
    return skill_resp.json(), revision_resp.json()


@pytest.mark.asyncio
async def test_skill_publish_and_rollback_change_active_revision(client, tenant_id):
    headers = {"X-Tenant-Id": str(tenant_id)}
    skill, first_revision = await _create_skill_with_revision(client, headers)
    skill_id = skill["id"]

    publish_resp = await client.post(
        f"/v1/skills/{skill_id}/revisions/{first_revision['id']}/publish", headers=headers
    )
    assert publish_resp.status_code == 200
    assert publish_resp.json()["active_revision_id"] == first_revision["id"]
    assert publish_resp.json()["status"] == "published"

    second_revision_resp = await client.post(
        f"/v1/skills/{skill_id}/revisions",
        json={"instruction": "Use a warmer tone.", "change_reason": "tone update"},
        headers=headers,
    )
    assert second_revision_resp.status_code == 201
    second_revision = second_revision_resp.json()

    publish_resp = await client.post(
        f"/v1/skills/{skill_id}/revisions/{second_revision['id']}/publish", headers=headers
    )
    assert publish_resp.status_code == 200
    assert publish_resp.json()["active_revision_id"] == second_revision["id"]

    rollback_resp = await client.post(
        f"/v1/skills/{skill_id}/revisions/{first_revision['id']}/rollback", headers=headers
    )
    assert rollback_resp.status_code == 200
    assert rollback_resp.json()["active_revision_id"] == first_revision["id"]


@pytest.mark.asyncio
async def test_skill_routes_are_scoped_to_tenant(client, tenant_id):
    headers = {"X-Tenant-Id": str(tenant_id)}
    skill, _revision = await _create_skill_with_revision(client, headers)

    other_headers = {"X-Tenant-Id": str(uuid.uuid4())}
    resp = await client.get(f"/v1/skills/{skill['id']}", headers=other_headers)
    assert resp.status_code == 404

    resp = await client.get("/v1/skills", headers=headers)
    assert resp.status_code == 200
    assert [item["id"] for item in resp.json()] == [skill["id"]]


@pytest.mark.asyncio
async def test_published_revision_cannot_be_updated_at_db_layer(client, tenant_id, db_sessionmaker):
    headers = {"X-Tenant-Id": str(tenant_id)}
    skill, revision = await _create_skill_with_revision(client, headers)
    publish_resp = await client.post(
        f"/v1/skills/{skill['id']}/revisions/{revision['id']}/publish", headers=headers
    )
    assert publish_resp.status_code == 200

    async with db_sessionmaker() as session:
        published = await session.get(SkillRevision, uuid.UUID(revision["id"]))
        assert published is not None
        published.instruction = "mutated in place"
        with pytest.raises(DBAPIError):
            await session.flush()


@pytest.mark.asyncio
async def test_running_run_keeps_skill_revision_snapshot(client, tenant_id, db_sessionmaker):
    headers = {"X-Tenant-Id": str(tenant_id)}
    skill, first_revision = await _create_skill_with_revision(client, headers)
    skill_id = skill["id"]
    await client.post(
        f"/v1/skills/{skill_id}/revisions/{first_revision['id']}/publish", headers=headers
    )

    async with db_sessionmaker() as session:
        run = AgentRun(
            tenant_id=tenant_id,
            active_skill_revision_id=uuid.UUID(first_revision["id"]),
            status=RunStatus.running,
            goal="handle a ticket",
            channel=Channel.api,
            max_steps=10,
            max_tool_calls=10,
            token_budget=1000,
            timeout_seconds=60,
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    second_revision_resp = await client.post(
        f"/v1/skills/{skill_id}/revisions",
        json={"instruction": "New behavior.", "change_reason": "update"},
        headers=headers,
    )
    second_revision = second_revision_resp.json()
    await client.post(
        f"/v1/skills/{skill_id}/revisions/{second_revision['id']}/publish", headers=headers
    )

    async with db_sessionmaker() as session:
        run = await session.get(AgentRun, run_id)
        assert str(run.active_skill_revision_id) == first_revision["id"]
