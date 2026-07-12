import orjson
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from harness.db.session import get_session
from harness.slack.service import (
    SlackTenantResolutionError,
    handle_slack_event_callback,
    verify_slack_signature_and_identify_tenant,
)

router = APIRouter(prefix="/v1/slack", tags=["slack"])


@router.post("/events")
async def slack_events(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    body = await request.body()
    settings = request.app.state.settings
    valid, tenant = await verify_slack_signature_and_identify_tenant(
        session,
        settings=settings,
        timestamp=request.headers.get("X-Slack-Request-Timestamp"),
        signature=request.headers.get("X-Slack-Signature"),
        body=body,
    )
    if not valid:
        raise HTTPException(status_code=401, detail="invalid Slack signature")

    payload = orjson.loads(body)
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}

    if payload.get("type") != "event_callback":
        return {"ok": True, "ignored": payload.get("type", "unknown")}

    try:
        return await handle_slack_event_callback(
            session, payload=payload, settings=settings, tenant=tenant
        )
    except SlackTenantResolutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
