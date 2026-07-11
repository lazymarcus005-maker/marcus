import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from harness.db.models import UsageRecord
from harness.llm.types import Usage


async def record_usage(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID | None,
    model: str,
    usage: Usage,
) -> UsageRecord:
    record = UsageRecord(
        tenant_id=tenant_id,
        run_id=run_id,
        model=model,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
    )
    session.add(record)
    await session.flush()
    return record
