import uuid

import pytest
import sqlalchemy as sa

from harness.db.models import Tenant, UsageRecord
from harness.llm.types import Usage
from harness.llm.usage import record_usage
from harness.runtime.repository import RunRepository


@pytest.mark.asyncio
async def test_record_usage_persists_to_usage_records(db_session):
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    db_session.add(tenant)
    await db_session.flush()

    repo = RunRepository(db_session)
    run = await repo.create_run(tenant_id=tenant.id, goal="goal")

    await record_usage(
        db_session,
        tenant_id=tenant.id,
        run_id=run.id,
        model="gpt-oss:120b",
        usage=Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
    )

    result = await db_session.execute(sa.select(UsageRecord).where(UsageRecord.run_id == run.id))
    record = result.scalar_one()
    assert record.total_tokens == 120
    assert record.model == "gpt-oss:120b"
