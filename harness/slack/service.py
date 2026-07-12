import re
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from harness.config import Settings
from harness.db.enums import (
    TERMINAL_RUN_STATUSES,
    WAITING_RUN_STATUSES,
    Channel,
    MessageRole,
    RunStatus,
    UserRole,
)
from harness.db.models import (
    AgentMessage,
    AgentRun,
    ApprovalRequest,
    SlackEvent,
    SlackThreadMapping,
    Tenant,
    TenantSlackSetting,
    User,
)
from harness.mcp.crypto import decrypt
from harness.mq import publish_run_standalone
from harness.runtime.quotas import enforce_tenant_run_quota
from harness.runtime.repository import RunRepository
from harness.slack.client import SlackClient
from harness.slack.signing import is_valid_slack_signature

MENTION_RE = re.compile(r"<@[A-Z0-9]+>\s*")


class SlackTenantResolutionError(Exception):
    pass


def clean_slack_text(text: str) -> str:
    return MENTION_RE.sub("", text).strip()


async def resolve_slack_tenant(session: AsyncSession, settings: Settings) -> Tenant:
    if settings.slack_tenant_id:
        tenant = await session.get(Tenant, uuid.UUID(settings.slack_tenant_id))
        if tenant is None:
            raise SlackTenantResolutionError("configured Slack tenant not found")
        return tenant

    result = await session.execute(sa.select(Tenant).order_by(Tenant.created_at))
    tenants = list(result.scalars().all())
    if len(tenants) != 1:
        raise SlackTenantResolutionError("set HARNESS_SLACK_TENANT_ID when multiple tenants exist")
    return tenants[0]


async def verify_slack_signature_and_identify_tenant(
    session: AsyncSession,
    *,
    settings: Settings,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
) -> tuple[bool, Tenant | None]:
    """Validate the request's HMAC signature and, if possible, identify which
    tenant it belongs to.

    Every tenant's Slack app posts to this same shared endpoint, so each
    DB-configured signing secret (/v1/slack-settings) is tried first — the
    one that validates identifies the tenant. Falls back to the legacy
    single process-wide env-var secret (HARNESS_SLACK_SIGNING_SECRET) when
    no tenant has configured its own, in which case the tenant is resolved
    separately (resolve_slack_tenant) once the payload is known.
    """
    result = await session.execute(sa.select(TenantSlackSetting))
    for setting in result.scalars():
        secret = decrypt(setting.signing_secret_encrypted)
        if is_valid_slack_signature(
            signing_secret=secret, timestamp=timestamp, signature=signature, body=body
        ):
            tenant = await session.get(Tenant, setting.tenant_id)
            return True, tenant

    if is_valid_slack_signature(
        signing_secret=settings.slack_signing_secret,
        timestamp=timestamp,
        signature=signature,
        body=body,
    ):
        return True, None

    return False, None


async def get_or_create_slack_user(
    session: AsyncSession, *, tenant_id: uuid.UUID, slack_user_id: str
) -> User:
    result = await session.execute(
        sa.select(User).where(User.tenant_id == tenant_id, User.slack_user_id == slack_user_id)
    )
    user = result.scalar_one_or_none()
    if user is not None:
        return user
    user = User(
        tenant_id=tenant_id,
        display_name=f"slack:{slack_user_id}",
        role=UserRole.member,
        slack_user_id=slack_user_id,
    )
    session.add(user)
    await session.flush()
    return user


async def record_slack_event(
    session: AsyncSession,
    *,
    event_id: str,
    payload: dict[str, Any],
    tenant_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
) -> bool:
    event = SlackEvent(event_id=event_id, tenant_id=tenant_id, run_id=run_id, payload=payload)
    session.add(event)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return False
    return True


async def attach_run_to_slack_event(
    session: AsyncSession, *, event_id: str, run_id: uuid.UUID
) -> None:
    await session.execute(
        sa.update(SlackEvent).where(SlackEvent.event_id == event_id).values(run_id=run_id)
    )


async def handle_slack_event_callback(
    session: AsyncSession,
    *,
    payload: dict[str, Any],
    settings: Settings,
    tenant: Tenant | None = None,
    publisher=publish_run_standalone,
) -> dict[str, Any]:
    event_id = payload.get("event_id")
    event = payload.get("event", {})
    if not event_id:
        return {"ok": False, "ignored": "missing event_id"}

    event_type = event.get("type")
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        created = await record_slack_event(session, event_id=event_id, payload=payload)
        await session.commit()
        return {"ok": True, "ignored": "bot_message", "deduped": not created}

    if event_type not in {"app_mention", "message"}:
        created = await record_slack_event(session, event_id=event_id, payload=payload)
        await session.commit()
        return {"ok": True, "ignored": event_type or "unknown", "deduped": not created}

    # tenant is pre-resolved by the caller when a DB-configured Slack signing
    # secret identified it (verify_slack_signature_and_identify_tenant);
    # otherwise fall back to the legacy single env-var-configured tenant.
    tenant = tenant or await resolve_slack_tenant(session, settings)
    channel_id = event.get("channel")
    slack_user_id = event.get("user")
    event_ts = event.get("ts")
    thread_ts = event.get("thread_ts") or event_ts
    text = clean_slack_text(event.get("text", ""))
    if not channel_id or not thread_ts or not event_ts or not text or not slack_user_id:
        created = await record_slack_event(
            session, event_id=event_id, payload=payload, tenant_id=tenant.id
        )
        await session.commit()
        return {"ok": True, "ignored": "missing channel/thread/text/user", "deduped": not created}

    # Claim the event by inserting it now, before any run-mutating work. The
    # unique constraint on event_id makes this atomic: a concurrent duplicate
    # delivery will either block behind this INSERT and then fail once we
    # commit, or proceed only if we roll back — either way at most one
    # request can create a run for this event_id.
    claimed = await record_slack_event(session, event_id=event_id, payload=payload, tenant_id=tenant.id)
    if not claimed:
        await session.commit()
        return {"ok": True, "deduped": True}

    slack_user = await get_or_create_slack_user(
        session, tenant_id=tenant.id, slack_user_id=slack_user_id
    )
    repo = RunRepository(session)
    mapping = await get_thread_mapping(session, channel_id=channel_id, thread_ts=thread_ts)
    if mapping is None:
        if event_type != "app_mention":
            await session.commit()
            return {"ok": True, "ignored": "unmapped_thread"}

        await enforce_tenant_run_quota(session, tenant_id=tenant.id, settings=settings)
        run = await repo.create_run(
            tenant_id=tenant.id,
            goal=text,
            channel=Channel.slack,
            channel_metadata={"channel_id": channel_id, "thread_ts": thread_ts, "event_ts": event_ts},
            created_by_user_id=slack_user.id,
        )
        await repo.add_message(run.id, MessageRole.user, text)
        session.add(
            SlackThreadMapping(
                tenant_id=tenant.id,
                run_id=run.id,
                channel_id=channel_id,
                thread_ts=thread_ts,
            )
        )
    else:
        loaded_run = await repo.get(mapping.run_id)
        if loaded_run is None:
            await session.commit()
            return {"ok": True, "ignored": "missing_run"}
        run = loaded_run
        if run.status in TERMINAL_RUN_STATUSES:
            await attach_run_to_slack_event(session, event_id=event_id, run_id=run.id)
            await session.commit()
            return {"ok": True, "ignored": f"run_{run.status}"}
        await repo.add_message(run.id, MessageRole.user, text)
        if run.status in WAITING_RUN_STATUSES:
            run = await repo.checkpoint(run, status=RunStatus.running)

    await attach_run_to_slack_event(session, event_id=event_id, run_id=run.id)
    await session.commit()
    await publisher(run.id, tenant.id)
    return {"ok": True, "run_id": str(run.id), "thread_ts": thread_ts}


async def get_thread_mapping(
    session: AsyncSession, *, channel_id: str, thread_ts: str
) -> SlackThreadMapping | None:
    result = await session.execute(
        sa.select(SlackThreadMapping).where(
            SlackThreadMapping.channel_id == channel_id,
            SlackThreadMapping.thread_ts == thread_ts,
        )
    )
    return result.scalar_one_or_none()


async def slack_text_for_run(session: AsyncSession, run: AgentRun) -> str | None:
    if run.status == RunStatus.completed:
        return _format_completed(run.final_result)
    if run.status == RunStatus.waiting_user_input:
        return await _latest_assistant_question(session, run)
    if run.status == RunStatus.waiting_approval:
        return "Approval required before I can continue."
    if run.status == RunStatus.failed:
        return f"Run failed: {run.error or 'unknown error'}"
    if run.status == RunStatus.cancelled:
        return "Run cancelled."
    if run.status == RunStatus.timed_out:
        return f"Run timed out: {run.error or 'time limit reached'}"
    return None


async def resolve_slack_bot_token(
    session: AsyncSession, *, tenant_id: uuid.UUID, settings: Settings
) -> str:
    """Return the tenant's configured bot token, falling back to the
    process-wide env-var default when the tenant hasn't configured its own
    via /v1/slack-settings."""
    tenant_setting = await session.get(TenantSlackSetting, tenant_id)
    if tenant_setting is not None:
        return decrypt(tenant_setting.bot_token_encrypted)
    return settings.slack_bot_token


async def post_run_update_to_slack(
    session: AsyncSession, *, run: AgentRun, settings: Settings, client: SlackClient | None = None
) -> bool:
    mapping_result = await session.execute(
        sa.select(SlackThreadMapping).where(SlackThreadMapping.run_id == run.id)
    )
    mapping = mapping_result.scalar_one_or_none()
    text = await slack_text_for_run(session, run)
    if mapping is None or text is None:
        return False

    if run.status == RunStatus.waiting_approval:
        approval_result = await session.execute(
            sa.select(ApprovalRequest)
            .where(ApprovalRequest.run_id == run.id)
            .order_by(ApprovalRequest.requested_at.desc())
            .limit(1)
        )
        approval = approval_result.scalar_one_or_none()
        if approval is not None:
            text = (
                "Approval required before I can continue.\n"
                f"Tool: `{approval.tool_name}` ({approval.risk_tier})\n"
                f"Review: {settings.web_base_url}/approvals/{approval.id}"
            )

    slack = client
    if slack is None:
        bot_token = await resolve_slack_bot_token(session, tenant_id=run.tenant_id, settings=settings)
        slack = SlackClient(bot_token=bot_token)
    try:
        await slack.post_message(channel=mapping.channel_id, thread_ts=mapping.thread_ts, text=text)
    finally:
        if client is None:
            await slack.aclose()
    return True


def _format_completed(final_result: dict | None) -> str:
    if not final_result:
        return "Done."
    result = final_result.get("result")
    if isinstance(result, str):
        return result
    return str(result if result is not None else final_result)


async def _latest_assistant_question(session: AsyncSession, run: AgentRun) -> str:
    result = await session.execute(
        sa.select(AgentMessage)
        .where(AgentMessage.run_id == run.id, AgentMessage.role == MessageRole.assistant)
        .order_by(AgentMessage.created_at.desc())
        .limit(1)
    )
    message = result.scalar_one_or_none()
    if message is not None:
        return message.content
    return "I need more information before I can continue."
