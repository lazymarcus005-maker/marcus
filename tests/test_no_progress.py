import uuid

import pytest

from harness.db.enums import RiskTier, ToolExecutionStatus
from harness.db.models import Tenant, ToolExecution
from harness.runtime.guardrails import GuardrailViolation, check_no_progress
from harness.runtime.repository import RunRepository


@pytest.mark.asyncio
async def test_cosmetic_argument_changes_with_same_error_are_no_progress(db_session):
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    db_session.add(tenant)
    await db_session.flush()
    run = await RunRepository(db_session).create_run(tenant_id=tenant.id, goal="repair")
    for index in range(3):
        db_session.add(
            ToolExecution(
                run_id=run.id,
                step_no=index,
                call_index=0,
                idempotency_key=f"{run.id}:{index}:0",
                tool_name="build",
                risk_tier=RiskTier.read_only,
                args={"attempt": index},
                status=ToolExecutionStatus.failed,
                error="INVALID_ARGUMENT: bad value",
            )
        )
    await db_session.commit()

    with pytest.raises(GuardrailViolation, match="no progress"):
        await check_no_progress(db_session, run.id)
