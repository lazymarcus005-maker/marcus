from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from harness.api.deps import AuthPrincipal, require_admin
from harness.api.schemas import SlackSettingsResponse, SlackSettingsUpdateRequest
from harness.config import Settings
from harness.db.models import TenantSlackSetting
from harness.db.session import get_session
from harness.mcp.crypto import encrypt

router = APIRouter(prefix="/v1/slack-settings", tags=["slack-settings"])


def _webhook_url(settings: Settings) -> str:
    return f"{settings.web_base_url}/v1/slack/events"


def _to_response(setting: TenantSlackSetting, settings: Settings) -> SlackSettingsResponse:
    return SlackSettingsResponse(
        has_bot_token=True,
        has_signing_secret=True,
        webhook_url=_webhook_url(settings),
        updated_at=setting.updated_at,
    )


@router.get("", response_model=SlackSettingsResponse | None)
async def get_slack_settings(
    request: Request,
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> SlackSettingsResponse | None:
    setting = await session.get(TenantSlackSetting, principal.tenant.id)
    if setting is None:
        return None
    return _to_response(setting, request.app.state.settings)


@router.put("", response_model=SlackSettingsResponse)
async def upsert_slack_settings(
    body: SlackSettingsUpdateRequest,
    request: Request,
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> SlackSettingsResponse:
    setting = await session.get(TenantSlackSetting, principal.tenant.id)

    if setting is None:
        if not body.bot_token or not body.signing_secret:
            raise HTTPException(status_code=400, detail="bot_token and signing_secret are required")
        setting = TenantSlackSetting(
            tenant_id=principal.tenant.id,
            bot_token_encrypted=encrypt(body.bot_token),
            signing_secret_encrypted=encrypt(body.signing_secret),
        )
        session.add(setting)
    else:
        if body.bot_token:
            setting.bot_token_encrypted = encrypt(body.bot_token)
        if body.signing_secret:
            setting.signing_secret_encrypted = encrypt(body.signing_secret)

    await session.commit()
    await session.refresh(setting)
    return _to_response(setting, request.app.state.settings)


@router.delete("", status_code=204)
async def delete_slack_settings(
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    setting = await session.get(TenantSlackSetting, principal.tenant.id)
    if setting is not None:
        await session.delete(setting)
        await session.commit()
