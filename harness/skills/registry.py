import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from harness.db.enums import SkillStatus
from harness.db.models import Skill, SkillRevision


class SkillRegistry:
    """DB-backed skill lifecycle service for manual Phase 3 operations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_skill(
        self,
        *,
        tenant_id: uuid.UUID,
        name: str,
        description: str = "",
        owner_user_id: uuid.UUID | None = None,
    ) -> Skill:
        skill = Skill(
            tenant_id=tenant_id,
            name=name,
            description=description,
            owner_user_id=owner_user_id,
        )
        self.session.add(skill)
        await self.session.flush()
        return skill

    async def list_skills(self, tenant_id: uuid.UUID) -> list[Skill]:
        result = await self.session.execute(
            sa.select(Skill).where(Skill.tenant_id == tenant_id).order_by(Skill.name)
        )
        return list(result.scalars().all())

    async def list_published_skills(self, tenant_id: uuid.UUID) -> list[Skill]:
        result = await self.session.execute(
            sa.select(Skill)
            .where(
                Skill.tenant_id == tenant_id,
                Skill.status == SkillStatus.published,
                Skill.active_revision_id.is_not(None),
            )
            .order_by(Skill.name)
        )
        return list(result.scalars().all())

    async def get_skill(self, tenant_id: uuid.UUID, skill_id: uuid.UUID) -> Skill | None:
        result = await self.session.execute(
            sa.select(Skill).where(Skill.id == skill_id, Skill.tenant_id == tenant_id)
        )
        return result.scalar_one_or_none()

    async def get_revision(
        self, tenant_id: uuid.UUID, skill_id: uuid.UUID, revision_id: uuid.UUID
    ) -> SkillRevision | None:
        result = await self.session.execute(
            sa.select(SkillRevision)
            .join(Skill, SkillRevision.skill_id == Skill.id)
            .where(
                Skill.tenant_id == tenant_id,
                Skill.id == skill_id,
                SkillRevision.id == revision_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_active_revision_by_skill_name(
        self, tenant_id: uuid.UUID, name: str
    ) -> tuple[Skill, SkillRevision] | None:
        result = await self.session.execute(
            sa.select(Skill, SkillRevision)
            .join(SkillRevision, Skill.active_revision_id == SkillRevision.id)
            .where(
                Skill.tenant_id == tenant_id,
                Skill.name == name,
                Skill.status == SkillStatus.published,
            )
        )
        row = result.one_or_none()
        if row is None:
            return None
        return row.Skill, row.SkillRevision

    async def get_revision_by_id(
        self, tenant_id: uuid.UUID, revision_id: uuid.UUID
    ) -> tuple[Skill, SkillRevision] | None:
        result = await self.session.execute(
            sa.select(Skill, SkillRevision)
            .join(SkillRevision, SkillRevision.skill_id == Skill.id)
            .where(Skill.tenant_id == tenant_id, SkillRevision.id == revision_id)
        )
        row = result.one_or_none()
        if row is None:
            return None
        return row.Skill, row.SkillRevision

    async def list_revisions(self, tenant_id: uuid.UUID, skill_id: uuid.UUID) -> list[SkillRevision]:
        result = await self.session.execute(
            sa.select(SkillRevision)
            .join(Skill, SkillRevision.skill_id == Skill.id)
            .where(Skill.tenant_id == tenant_id, Skill.id == skill_id)
            .order_by(SkillRevision.version)
        )
        return list(result.scalars().all())

    async def create_revision(
        self,
        *,
        tenant_id: uuid.UUID,
        skill_id: uuid.UUID,
        instruction: str,
        change_reason: str,
        manifest: dict | None = None,
        input_schema: dict | None = None,
        output_schema: dict | None = None,
        required_tools: list[str] | None = None,
        created_from_run_id: uuid.UUID | None = None,
    ) -> SkillRevision | None:
        skill = await self.get_skill(tenant_id, skill_id)
        if skill is None:
            return None

        if not instruction.strip():
            raise ValueError("skill instruction must not be empty")
        if len(instruction) > 100_000:
            raise ValueError("skill instruction exceeds the 100000 character limit")
        if len(change_reason.strip()) > 10_000:
            raise ValueError("skill change_reason exceeds the 10000 character limit")
        tools = required_tools or []
        if len(set(tools)) != len(tools):
            raise ValueError("skill required_tools must not contain duplicates")

        # Serialize version allocation per skill; the unique constraint remains
        # the final backstop for concurrent writers.
        locked = await self.session.execute(
            sa.select(Skill)
            .where(Skill.id == skill_id, Skill.tenant_id == tenant_id)
            .with_for_update()
        )
        skill = locked.scalar_one_or_none()
        if skill is None:
            return None

        next_version_result = await self.session.execute(
            sa.select(sa.func.coalesce(sa.func.max(SkillRevision.version), 0) + 1).where(
                SkillRevision.skill_id == skill_id
            )
        )
        revision = SkillRevision(
            skill_id=skill_id,
            version=next_version_result.scalar_one(),
            instruction=instruction,
            manifest=manifest or {},
            input_schema=input_schema or {},
            output_schema=output_schema or {},
            required_tools=required_tools or [],
            change_reason=change_reason,
            created_from_run_id=created_from_run_id,
        )
        self.session.add(revision)
        await self.session.flush()
        return revision

    async def approve_revision(
        self, tenant_id: uuid.UUID, skill_id: uuid.UUID, revision_id: uuid.UUID
    ) -> SkillRevision | None:
        revision = await self.get_revision(tenant_id, skill_id, revision_id)
        if revision is None:
            return None
        if revision.status != SkillStatus.draft:
            return revision
        revision.status = SkillStatus.approved
        await self.session.flush()
        return revision

    async def publish_revision(
        self, tenant_id: uuid.UUID, skill_id: uuid.UUID, revision_id: uuid.UUID
    ) -> SkillRevision | None:
        skill = await self.get_skill(tenant_id, skill_id)
        if skill is None:
            return None
        revision = await self.get_revision(tenant_id, skill_id, revision_id)
        if revision is None:
            return None
        if revision.status not in (SkillStatus.approved, SkillStatus.published):
            raise ValueError("skill revision must be approved before publishing")
        if revision.status == SkillStatus.approved:
            revision.status = SkillStatus.published
        skill.active_revision_id = revision.id
        skill.status = SkillStatus.published
        await self.session.flush()
        return revision

    async def rollback(
        self, tenant_id: uuid.UUID, skill_id: uuid.UUID, revision_id: uuid.UUID
    ) -> SkillRevision | None:
        return await self.publish_revision(tenant_id, skill_id, revision_id)

    async def deprecate_skill(self, tenant_id: uuid.UUID, skill_id: uuid.UUID) -> Skill | None:
        skill = await self.get_skill(tenant_id, skill_id)
        if skill is None:
            return None
        skill.status = SkillStatus.deprecated
        skill.active_revision_id = None
        await self.session.flush()
        return skill
