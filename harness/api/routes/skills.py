import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from harness.api.deps import AuthPrincipal, require_admin
from harness.api.schemas import (
    SkillCreateRequest,
    SkillResponse,
    SkillRevisionCreateRequest,
    SkillRevisionResponse,
    SkillRevisionUsageStatsResponse,
    SkillUpdateRequest,
)
from harness.db.models import Skill, SkillRevision, Tenant
from harness.db.session import get_session
from harness.skills.registry import SkillRegistry
from harness.skills.usage import get_revision_usage_stats

router = APIRouter(prefix="/v1/skills", tags=["skills"])


async def _get_owned_skill(session: AsyncSession, tenant: Tenant, skill_id: uuid.UUID) -> Skill:
    skill = await SkillRegistry(session).get_skill(tenant.id, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return skill


async def _get_owned_revision(
    session: AsyncSession, tenant: Tenant, skill_id: uuid.UUID, revision_id: uuid.UUID
) -> SkillRevision:
    revision = await SkillRegistry(session).get_revision(tenant.id, skill_id, revision_id)
    if revision is None:
        raise HTTPException(status_code=404, detail="skill revision not found")
    return revision


@router.post("", response_model=SkillResponse, status_code=201)
async def create_skill(
    body: SkillCreateRequest,
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Skill:
    skill = await SkillRegistry(session).create_skill(
        tenant_id=principal.tenant.id,
        name=body.name,
        description=body.description,
        owner_user_id=body.owner_user_id,
    )
    await session.commit()
    return skill


@router.get("", response_model=list[SkillResponse])
async def list_skills(
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[Skill]:
    return await SkillRegistry(session).list_skills(principal.tenant.id)


@router.get("/{skill_id}", response_model=SkillResponse)
async def get_skill(
    skill_id: uuid.UUID,
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Skill:
    return await _get_owned_skill(session, principal.tenant, skill_id)


@router.patch("/{skill_id}", response_model=SkillResponse)
async def update_skill(
    skill_id: uuid.UUID,
    body: SkillUpdateRequest,
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Skill:
    skill = await _get_owned_skill(session, principal.tenant, skill_id)
    if body.description is not None:
        skill.description = body.description
    if body.owner_user_id is not None:
        skill.owner_user_id = body.owner_user_id
    await session.commit()
    await session.refresh(skill)
    return skill


@router.post("/{skill_id}/revisions", response_model=SkillRevisionResponse, status_code=201)
async def create_revision(
    skill_id: uuid.UUID,
    body: SkillRevisionCreateRequest,
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> SkillRevision:
    revision = await SkillRegistry(session).create_revision(
        tenant_id=principal.tenant.id,
        skill_id=skill_id,
        instruction=body.instruction,
        change_reason=body.change_reason,
        manifest=body.manifest,
        input_schema=body.input_schema,
        output_schema=body.output_schema,
        required_tools=body.required_tools,
        created_from_run_id=body.created_from_run_id,
    )
    if revision is None:
        raise HTTPException(status_code=404, detail="skill not found")
    await session.commit()
    return revision


@router.get("/{skill_id}/revisions", response_model=list[SkillRevisionResponse])
async def list_revisions(
    skill_id: uuid.UUID,
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[SkillRevision]:
    await _get_owned_skill(session, principal.tenant, skill_id)
    return await SkillRegistry(session).list_revisions(principal.tenant.id, skill_id)


@router.get(
    "/{skill_id}/revisions/{revision_id}/usage-stats",
    response_model=SkillRevisionUsageStatsResponse,
)
async def get_revision_usage_stats_route(
    skill_id: uuid.UUID,
    revision_id: uuid.UUID,
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> SkillRevisionUsageStatsResponse:
    await _get_owned_revision(session, principal.tenant, skill_id, revision_id)
    stats = await get_revision_usage_stats(
        session, tenant_id=principal.tenant.id, revision_id=revision_id
    )
    if stats is None:
        raise HTTPException(status_code=404, detail="skill revision not found")
    return SkillRevisionUsageStatsResponse.model_validate(stats)


@router.post("/{skill_id}/revisions/{revision_id}/approve", response_model=SkillRevisionResponse)
async def approve_revision(
    skill_id: uuid.UUID,
    revision_id: uuid.UUID,
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> SkillRevision:
    revision = await _get_owned_revision(session, principal.tenant, skill_id, revision_id)
    approved = await SkillRegistry(session).approve_revision(
        principal.tenant.id, skill_id, revision.id
    )
    if approved is None:
        raise HTTPException(status_code=404, detail="skill revision not found")
    await session.commit()
    return approved


@router.post("/{skill_id}/revisions/{revision_id}/publish", response_model=SkillResponse)
async def publish_revision(
    skill_id: uuid.UUID,
    revision_id: uuid.UUID,
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Skill:
    await _get_owned_revision(session, principal.tenant, skill_id, revision_id)
    revision = await SkillRegistry(session).publish_revision(
        principal.tenant.id, skill_id, revision_id
    )
    if revision is None:
        raise HTTPException(status_code=404, detail="skill revision not found")
    skill = await _get_owned_skill(session, principal.tenant, skill_id)
    await session.commit()
    await session.refresh(skill)
    return skill


@router.post("/{skill_id}/revisions/{revision_id}/rollback", response_model=SkillResponse)
async def rollback_revision(
    skill_id: uuid.UUID,
    revision_id: uuid.UUID,
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Skill:
    await _get_owned_revision(session, principal.tenant, skill_id, revision_id)
    revision = await SkillRegistry(session).rollback(principal.tenant.id, skill_id, revision_id)
    if revision is None:
        raise HTTPException(status_code=404, detail="skill revision not found")
    skill = await _get_owned_skill(session, principal.tenant, skill_id)
    await session.commit()
    await session.refresh(skill)
    return skill


@router.post("/{skill_id}/deprecate", response_model=SkillResponse)
async def deprecate_skill(
    skill_id: uuid.UUID,
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Skill:
    skill = await SkillRegistry(session).deprecate_skill(principal.tenant.id, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    await session.commit()
    await session.refresh(skill)
    return skill
