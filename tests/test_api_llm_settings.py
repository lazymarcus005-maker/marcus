import uuid
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from harness.api.app import create_app
from harness.auth import create_api_key
from harness.db.enums import UserRole
from harness.db.models import Tenant, User
from harness.db.session import get_session


class _FakeModelsResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeAsyncClient:
    def __init__(self, response=None, *, error=None):
        self._response = response
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, path):
        if self._error is not None:
            raise self._error
        return self._response


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


async def _admin_key(session, *, role=UserRole.admin):
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    session.add(tenant)
    await session.flush()
    user = User(tenant_id=tenant.id, display_name="admin", role=role)
    session.add(user)
    await session.flush()
    _key, raw = await create_api_key(session, tenant_id=tenant.id, user_id=user.id, name="admin")
    await session.commit()
    return tenant, raw


@pytest.mark.asyncio
async def test_get_llm_settings_returns_null_when_unconfigured(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        _tenant, raw = await _admin_key(session)

    resp = await client.get("/v1/llm-settings", headers={"X-API-Key": raw})

    assert resp.status_code == 200
    assert resp.json() is None


@pytest.mark.asyncio
async def test_put_llm_settings_creates_and_hides_api_key(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        _tenant, raw = await _admin_key(session)

    resp = await client.put(
        "/v1/llm-settings",
        json={"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-secret-value"},
        headers={"X-API-Key": raw},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "openai"
    assert body["base_url"] == "https://api.openai.com/v1"
    assert body["model"] == "gpt-4o-mini"
    assert body["has_api_key"] is True
    assert "api_key" not in body
    assert "sk-secret-value" not in resp.text


@pytest.mark.asyncio
async def test_put_llm_settings_without_api_key_requires_existing_key(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        _tenant, raw = await _admin_key(session)

    resp = await client.put(
        "/v1/llm-settings",
        json={"provider": "openrouter", "model": "some-model"},
        headers={"X-API-Key": raw},
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_put_llm_settings_update_keeps_existing_key_when_omitted(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        _tenant, raw = await _admin_key(session)

    await client.put(
        "/v1/llm-settings",
        json={"provider": "ollama_cloud", "model": "gpt-oss:120b", "api_key": "first-key"},
        headers={"X-API-Key": raw},
    )

    update_resp = await client.put(
        "/v1/llm-settings",
        json={"provider": "ollama_cloud", "model": "gpt-oss:20b"},
        headers={"X-API-Key": raw},
    )

    assert update_resp.status_code == 200
    assert update_resp.json()["model"] == "gpt-oss:20b"
    assert update_resp.json()["has_api_key"] is True

    get_resp = await client.get("/v1/llm-settings", headers={"X-API-Key": raw})
    assert get_resp.json()["model"] == "gpt-oss:20b"


@pytest.mark.asyncio
async def test_delete_llm_settings_reverts_to_unconfigured(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        _tenant, raw = await _admin_key(session)

    await client.put(
        "/v1/llm-settings",
        json={"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-secret"},
        headers={"X-API-Key": raw},
    )

    delete_resp = await client.delete("/v1/llm-settings", headers={"X-API-Key": raw})
    assert delete_resp.status_code == 204

    get_resp = await client.get("/v1/llm-settings", headers={"X-API-Key": raw})
    assert get_resp.json() is None


@pytest.mark.asyncio
async def test_llm_settings_require_admin(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        _tenant, raw = await _admin_key(session, role=UserRole.member)

    resp = await client.get("/v1/llm-settings", headers={"X-API-Key": raw})

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_llm_models_uses_provided_api_key(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        _tenant, raw = await _admin_key(session)

    fake_response = _FakeModelsResponse({"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4o"}]})
    with patch(
        "harness.api.routes.llm_settings.httpx.AsyncClient",
        return_value=_FakeAsyncClient(fake_response),
    ):
        resp = await client.post(
            "/v1/llm-settings/models",
            json={"provider": "openai", "api_key": "sk-fresh-key"},
            headers={"X-API-Key": raw},
        )

    assert resp.status_code == 200
    assert resp.json()["models"] == ["gpt-4o", "gpt-4o-mini"]


@pytest.mark.asyncio
async def test_list_llm_models_falls_back_to_saved_key(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        _tenant, raw = await _admin_key(session)

    await client.put(
        "/v1/llm-settings",
        json={"provider": "ollama_cloud", "model": "gpt-oss:120b", "api_key": "sk-saved"},
        headers={"X-API-Key": raw},
    )

    fake_response = _FakeModelsResponse({"data": [{"id": "gpt-oss:120b"}]})
    with patch(
        "harness.api.routes.llm_settings.httpx.AsyncClient",
        return_value=_FakeAsyncClient(fake_response),
    ):
        resp = await client.post(
            "/v1/llm-settings/models",
            json={"provider": "ollama_cloud"},
            headers={"X-API-Key": raw},
        )

    assert resp.status_code == 200
    assert resp.json()["models"] == ["gpt-oss:120b"]


@pytest.mark.asyncio
async def test_list_llm_models_without_key_or_saved_settings_returns_400(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        _tenant, raw = await _admin_key(session)

    resp = await client.post(
        "/v1/llm-settings/models",
        json={"provider": "openai"},
        headers={"X-API-Key": raw},
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_llm_models_surfaces_provider_error_as_502(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        _tenant, raw = await _admin_key(session)

    with patch(
        "harness.api.routes.llm_settings.httpx.AsyncClient",
        return_value=_FakeAsyncClient(error=httpx.ConnectError("boom")),
    ):
        resp = await client.post(
            "/v1/llm-settings/models",
            json={"provider": "openai", "api_key": "sk-bad"},
            headers={"X-API-Key": raw},
        )

    assert resp.status_code == 502
