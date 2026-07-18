import hashlib
import hmac
import time
import uuid
from datetime import UTC, datetime, timedelta

import orjson
import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient

from harness.api.app import create_app
from harness.config import get_settings
from harness.db.enums import Channel, RiskTier, RunStatus
from harness.db.models import (
    AgentRun,
    ApprovalRequest,
    SlackThreadMapping,
    Tenant,
    TenantSlackSetting,
    User,
)
from harness.db.session import get_session
from harness.mcp.crypto import encrypt
from harness.runtime.repository import RunRepository
from harness.slack.service import (
    handle_slack_event_callback,
    post_run_update_to_slack,
    resolve_slack_bot_token,
    verify_slack_signature_and_identify_tenant,
)


def _signed_headers(secret: str, body: bytes) -> dict[str, str]:
    ts = str(int(time.time()))
    base = b"v0:" + ts.encode() + b":" + body
    signature = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": signature}


@pytest.fixture
def app(db_sessionmaker):
    application = create_app()
    application.state.settings.slack_signing_secret = "test-secret"

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
    await session.commit()
    return tenant


def _mention_payload(event_id="Ev1", *, text="<@BOT> investigate outage", thread_ts=None):
    event = {
        "type": "app_mention",
        "channel": "C123",
        "user": "U123",
        "text": text,
        "ts": "1710000000.000100",
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    return {"type": "event_callback", "event_id": event_id, "team_id": "T123", "event": event}


def _thread_message_payload(event_id="Ev2", *, text="production", thread_ts="1710000000.000100"):
    return {
        "type": "event_callback",
        "event_id": event_id,
        "team_id": "T123",
        "event": {
            "type": "message",
            "channel": "C123",
            "user": "U123",
            "text": text,
            "ts": "1710000001.000100",
            "thread_ts": thread_ts,
        },
    }


@pytest.mark.asyncio
async def test_verify_signature_identifies_tenant_from_db_config(db_session):
    tenant_a = await _make_tenant(db_session)
    tenant_b = await _make_tenant(db_session)
    db_session.add(
        TenantSlackSetting(
            tenant_id=tenant_a.id,
            bot_token_encrypted=encrypt("xoxb-a"),
            signing_secret_encrypted=encrypt("secret-a"),
        )
    )
    db_session.add(
        TenantSlackSetting(
            tenant_id=tenant_b.id,
            bot_token_encrypted=encrypt("xoxb-b"),
            signing_secret_encrypted=encrypt("secret-b"),
        )
    )
    await db_session.commit()

    body = orjson.dumps({"type": "event_callback"})
    headers = _signed_headers("secret-b", body)

    valid, tenant = await verify_slack_signature_and_identify_tenant(
        db_session,
        settings=get_settings(),
        timestamp=headers["X-Slack-Request-Timestamp"],
        signature=headers["X-Slack-Signature"],
        body=body,
    )

    assert valid is True
    assert tenant.id == tenant_b.id


@pytest.mark.asyncio
async def test_verify_signature_rejects_unmatched_signature(db_session):
    tenant = await _make_tenant(db_session)
    db_session.add(
        TenantSlackSetting(
            tenant_id=tenant.id,
            bot_token_encrypted=encrypt("xoxb"),
            signing_secret_encrypted=encrypt("real-secret"),
        )
    )
    await db_session.commit()

    body = orjson.dumps({"type": "event_callback"})
    headers = _signed_headers("wrong-secret", body)

    valid, tenant_out = await verify_slack_signature_and_identify_tenant(
        db_session,
        settings=get_settings(),
        timestamp=headers["X-Slack-Request-Timestamp"],
        signature=headers["X-Slack-Signature"],
        body=body,
    )

    assert valid is False
    assert tenant_out is None


@pytest.mark.asyncio
async def test_resolve_slack_bot_token_prefers_tenant_config(db_session):
    tenant = await _make_tenant(db_session)
    db_session.add(
        TenantSlackSetting(
            tenant_id=tenant.id,
            bot_token_encrypted=encrypt("xoxb-tenant-token"),
            signing_secret_encrypted=encrypt("secret"),
        )
    )
    await db_session.commit()

    token = await resolve_slack_bot_token(db_session, tenant_id=tenant.id, settings=get_settings())

    assert token == "xoxb-tenant-token"


@pytest.mark.asyncio
async def test_resolve_slack_bot_token_falls_back_when_unconfigured(db_session):
    tenant = await _make_tenant(db_session)

    token = await resolve_slack_bot_token(db_session, tenant_id=tenant.id, settings=get_settings())

    assert token == get_settings().slack_bot_token


@pytest.mark.asyncio
async def test_slack_url_verification_requires_valid_signature(client):
    body = orjson.dumps({"type": "url_verification", "challenge": "abc123"})
    bad = await client.post("/v1/slack/events", content=body)
    assert bad.status_code == 401

    good = await client.post(
        "/v1/slack/events", content=body, headers=_signed_headers("test-secret", body)
    )
    assert good.status_code == 200
    assert good.json() == {"challenge": "abc123"}


@pytest.mark.asyncio
async def test_app_mention_creates_run_and_dedupes_event(db_session):
    tenant = await _make_tenant(db_session)
    published: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def publisher(run_id, tenant_id):
        published.append((run_id, tenant_id))

    settings = type(
        "Settings",
        (),
        {
            "slack_tenant_id": str(tenant.id),
            "tenant_daily_token_quota_default": 2_000_000,
            "tenant_max_active_runs_default": 25,
        },
    )()
    first = await handle_slack_event_callback(
        db_session, payload=_mention_payload(), settings=settings, publisher=publisher
    )
    second = await handle_slack_event_callback(
        db_session, payload=_mention_payload(), settings=settings, publisher=publisher
    )

    assert first["ok"] is True
    assert second["deduped"] is True
    assert len(published) == 1

    run = await db_session.get(AgentRun, uuid.UUID(first["run_id"]))
    assert run is not None
    assert run.channel == Channel.slack
    assert run.goal == "investigate outage"
    user = (
        await db_session.execute(sa.select(User).where(User.slack_user_id == "U123"))
    ).scalar_one()
    assert run.created_by_user_id == user.id

    mapping = (
        await db_session.execute(
            sa.select(SlackThreadMapping).where(SlackThreadMapping.run_id == run.id)
        )
    ).scalar_one()
    assert mapping.channel_id == "C123"
    assert mapping.thread_ts == "1710000000.000100"


@pytest.mark.asyncio
async def test_thread_reply_resumes_waiting_run(db_session):
    tenant = await _make_tenant(db_session)
    published: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def publisher(run_id, tenant_id):
        published.append((run_id, tenant_id))

    settings = type(
        "Settings",
        (),
        {
            "slack_tenant_id": str(tenant.id),
            "tenant_daily_token_quota_default": 2_000_000,
            "tenant_max_active_runs_default": 25,
        },
    )()
    created = await handle_slack_event_callback(
        db_session, payload=_mention_payload(), settings=settings, publisher=publisher
    )

    repo = RunRepository(db_session)
    run = await repo.get(uuid.UUID(created["run_id"]))
    run = await repo.checkpoint(run, status=RunStatus.running)
    await repo.checkpoint(run, status=RunStatus.waiting_user_input)
    await db_session.commit()

    resumed = await handle_slack_event_callback(
        db_session,
        payload=_thread_message_payload(),
        settings=settings,
        publisher=publisher,
    )

    assert resumed["run_id"] == created["run_id"]
    run = await repo.get(uuid.UUID(created["run_id"]))
    assert run.status == RunStatus.running
    assert len(published) == 2


class FakeSlackClient:
    def __init__(self):
        self.messages = []

    async def post_message(self, *, channel, thread_ts, text):
        self.messages.append({"channel": channel, "thread_ts": thread_ts, "text": text})
        return {"ok": True}


@pytest.mark.asyncio
async def test_post_run_update_to_slack_for_completed_run(db_session):
    tenant = await _make_tenant(db_session)
    repo = RunRepository(db_session)
    run = await repo.create_run(tenant_id=tenant.id, goal="goal", channel=Channel.slack)
    db_session.add(
        SlackThreadMapping(
            tenant_id=tenant.id,
            run_id=run.id,
            channel_id="C123",
            thread_ts="1710000000.000100",
        )
    )
    run = await repo.checkpoint(run, status=RunStatus.running)
    run = await repo.checkpoint(run, status=RunStatus.completed, final_result={"result": "done"})
    await db_session.commit()

    client = FakeSlackClient()
    settings = type("Settings", (), {"slack_bot_token": "xoxb", "web_base_url": "http://web"})()
    posted = await post_run_update_to_slack(db_session, run=run, settings=settings, client=client)

    assert posted is True
    assert client.messages == [
        {"channel": "C123", "thread_ts": "1710000000.000100", "text": "done"}
    ]


@pytest.mark.asyncio
async def test_post_run_update_to_slack_for_approval(db_session):
    tenant = await _make_tenant(db_session)
    repo = RunRepository(db_session)
    run = await repo.create_run(tenant_id=tenant.id, goal="goal", channel=Channel.slack)
    db_session.add(
        SlackThreadMapping(
            tenant_id=tenant.id,
            run_id=run.id,
            channel_id="C123",
            thread_ts="1710000000.000100",
        )
    )
    approval = ApprovalRequest(
        tenant_id=tenant.id,
        run_id=run.id,
        step_no=0,
        call_index=0,
        tool_name="delete_issue",
        risk_tier=RiskTier.destructive,
        args={},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(approval)
    run = await repo.checkpoint(run, status=RunStatus.running)
    run = await repo.checkpoint(run, status=RunStatus.waiting_approval)
    await db_session.commit()

    client = FakeSlackClient()
    settings = type("Settings", (), {"slack_bot_token": "xoxb", "web_base_url": "http://web"})()
    posted = await post_run_update_to_slack(db_session, run=run, settings=settings, client=client)

    assert posted is True
    text = client.messages[0]["text"]
    assert "Approval required" in text
    assert "delete_issue" in text
    assert f"http://web/approvals/{approval.id}" in text
