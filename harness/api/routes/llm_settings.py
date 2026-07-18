import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from harness.api.deps import AuthPrincipal, require_admin
from harness.api.schemas import (
    LlmModelsRequest,
    LlmModelsResponse,
    LlmSettingsResponse,
    LlmSettingsUpdateRequest,
)
from harness.db.enums import LLM_PROVIDER_DEFAULT_BASE_URLS
from harness.db.models import TenantLlmSetting
from harness.db.session import get_session
from harness.mcp.crypto import decrypt, encrypt

router = APIRouter(prefix="/v1/llm-settings", tags=["llm-settings"])


def _to_response(setting: TenantLlmSetting) -> LlmSettingsResponse:
    return LlmSettingsResponse(
        provider=setting.provider,
        base_url=setting.base_url,
        model=setting.model,
        has_api_key=True,
        updated_at=setting.updated_at,
    )


@router.get("", response_model=LlmSettingsResponse | None)
async def get_llm_settings(
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> LlmSettingsResponse | None:
    setting = await session.get(TenantLlmSetting, principal.tenant.id)
    if setting is None:
        return None
    return _to_response(setting)


@router.put("", response_model=LlmSettingsResponse)
async def upsert_llm_settings(
    body: LlmSettingsUpdateRequest,
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> LlmSettingsResponse:
    setting = await session.get(TenantLlmSetting, principal.tenant.id)
    base_url = body.base_url or LLM_PROVIDER_DEFAULT_BASE_URLS[body.provider]

    if setting is None:
        if not body.api_key:
            raise HTTPException(status_code=400, detail="api_key is required")
        setting = TenantLlmSetting(
            tenant_id=principal.tenant.id,
            provider=body.provider,
            base_url=base_url,
            model=body.model,
            api_key_encrypted=encrypt(body.api_key),
        )
        session.add(setting)
    else:
        setting.provider = body.provider
        setting.base_url = base_url
        setting.model = body.model
        if body.api_key:
            setting.api_key_encrypted = encrypt(body.api_key)

    await session.commit()
    await session.refresh(setting)
    return _to_response(setting)


@router.delete("", status_code=204)
async def delete_llm_settings(
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    setting = await session.get(TenantLlmSetting, principal.tenant.id)
    if setting is not None:
        await session.delete(setting)
        await session.commit()


@router.post("/models", response_model=LlmModelsResponse)
async def list_llm_models(
    body: LlmModelsRequest,
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> LlmModelsResponse:
    api_key = body.api_key
    if not api_key:
        setting = await session.get(TenantLlmSetting, principal.tenant.id)
        if setting is None:
            raise HTTPException(status_code=400, detail="api_key is required")
        api_key = decrypt(setting.api_key_encrypted)

    base_url = body.base_url or LLM_PROVIDER_DEFAULT_BASE_URLS[body.provider]
    try:
        async with httpx.AsyncClient(
            base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=15.0
        ) as client:
            response = await client.get("/models")
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"failed to list models: {exc}") from exc

    payload = response.json()
    models = sorted({item["id"] for item in payload.get("data", []) if item.get("id")})
    return LlmModelsResponse(models=models)
