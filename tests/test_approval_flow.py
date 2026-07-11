import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from harness.db.enums import (
    ApprovalStatus,
    MessageRole,
    RiskTier,
    RunStatus,
    ToolExecutionStatus,
)
from harness.db.models import ApprovalRequest, Tenant, ToolExecution
from harness.runtime.engine import RunEngine
from harness.runtime.repository import RunRepository
from harness.runtime.tools import Tool
from tests.fakes import ScriptedLLMGateway, tool_call_response


async def _make_run(session, *, goal="delete the stale resource"):
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    session.add(tenant)
    await session.flush()
    repo = RunRepository(session)
    run = await repo.create_run(tenant_id=tenant.id, goal=goal)
    await repo.add_message(run.id, MessageRole.user, goal)
    await session.commit()
    return run


async def _delete_handler(arguments: dict) -> dict:
    return {"deleted": arguments.get("id")}


def _delete_tool() -> Tool:
    return Tool(
        name="delete_resource",
        description="Delete a resource by id.",
        parameters={
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        handler=_delete_handler,
        risk_tier=RiskTier.destructive,
    )


async def _get_approval(session, run_id) -> ApprovalRequest:
    result = await session.execute(
        sa.select(ApprovalRequest).where(ApprovalRequest.run_id == run_id)
    )
    return result.scalar_one()


@pytest.mark.asyncio
async def test_destructive_tool_call_parks_run_waiting_approval(db_session):
    run = await _make_run(db_session)
    llm = ScriptedLLMGateway([tool_call_response("delete_resource", {"id": "abc"})])
    engine = RunEngine(db_session, llm, tools=[_delete_tool()])

    final = await engine.run_until_blocked(run.id)

    assert final.status == RunStatus.waiting_approval
    assert final.current_step == 0  # must not advance — replays this step on resume

    approval = await _get_approval(db_session, run.id)
    assert approval.status == ApprovalStatus.pending
    assert approval.tool_name == "delete_resource"
    assert approval.risk_tier == RiskTier.destructive
    assert approval.step_no == 0
    assert approval.call_index == 0

    # The approval gate runs before write-ahead — no ToolExecution row yet.
    exec_result = await db_session.execute(
        sa.select(ToolExecution).where(ToolExecution.run_id == run.id)
    )
    assert exec_result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_approved_call_executes_and_run_completes(db_session):
    run = await _make_run(db_session)
    tool = _delete_tool()
    llm = ScriptedLLMGateway([tool_call_response("delete_resource", {"id": "abc"})])
    engine = RunEngine(db_session, llm, tools=[tool])
    run = await engine.run_until_blocked(run.id)
    assert run.status == RunStatus.waiting_approval

    approval = await _get_approval(db_session, run.id)
    approval.status = ApprovalStatus.approved
    approval.decided_at = datetime.now(UTC)
    await db_session.commit()

    repo = RunRepository(db_session)
    run = await repo.checkpoint(run, status=RunStatus.running)
    await db_session.commit()

    llm2 = ScriptedLLMGateway([tool_call_response("finish", {"result": "deleted abc"})])
    engine2 = RunEngine(db_session, llm2, tools=[tool])
    final = await engine2.run_until_blocked(run.id)

    assert final.status == RunStatus.completed
    exec_result = await db_session.execute(
        sa.select(ToolExecution).where(
            ToolExecution.run_id == run.id, ToolExecution.tool_name == "delete_resource"
        )
    )
    execution = exec_result.scalar_one()
    assert execution.status == ToolExecutionStatus.succeeded
    assert execution.result == {"deleted": "abc"}


@pytest.mark.asyncio
async def test_rejected_call_records_failure_and_run_continues(db_session):
    run = await _make_run(db_session)
    tool = _delete_tool()
    llm = ScriptedLLMGateway([tool_call_response("delete_resource", {"id": "abc"})])
    engine = RunEngine(db_session, llm, tools=[tool])
    run = await engine.run_until_blocked(run.id)
    assert run.status == RunStatus.waiting_approval

    approval = await _get_approval(db_session, run.id)
    approval.status = ApprovalStatus.rejected
    approval.reason = "too risky right now"
    approval.decided_at = datetime.now(UTC)
    await db_session.commit()

    repo = RunRepository(db_session)
    run = await repo.checkpoint(run, status=RunStatus.running)
    await db_session.commit()

    llm2 = ScriptedLLMGateway([tool_call_response("finish", {"result": "acknowledged"})])
    engine2 = RunEngine(db_session, llm2, tools=[tool])
    final = await engine2.run_until_blocked(run.id)

    assert final.status == RunStatus.completed
    sent_messages = llm2.calls[0]["messages"]
    tool_messages = [m for m in sent_messages if m.role == "tool"]
    assert any("rejected" in (m.content or "") for m in tool_messages)

    exec_result = await db_session.execute(
        sa.select(ToolExecution).where(
            ToolExecution.run_id == run.id, ToolExecution.tool_name == "delete_resource"
        )
    )
    execution = exec_result.scalar_one()
    assert execution.status == ToolExecutionStatus.failed
    assert "rejected" in execution.error


@pytest.mark.asyncio
async def test_replaying_a_pending_call_does_not_create_duplicate_approvals(db_session):
    """Simulates the worker re-delivering the same message before a decision

    is made (e.g. a stale-run reap that never should have fired, or a
    duplicate MQ delivery) — the natural (run_id, step_no, call_index) key
    must make this a no-op, not a second approval request.
    """
    run = await _make_run(db_session)
    tool = _delete_tool()
    llm = ScriptedLLMGateway([tool_call_response("delete_resource", {"id": "abc"})])
    engine = RunEngine(db_session, llm, tools=[tool])
    run = await engine.run_until_blocked(run.id)
    assert run.status == RunStatus.waiting_approval

    # Re-run the same run_id while still waiting_approval is a no-op today
    # (run_until_blocked only loops while status == running), but exercise
    # the executor's approval lookup directly to confirm idempotency.
    from harness.llm.types import ToolCall
    from harness.runtime.tool_executor import ToolExecutor

    executor = ToolExecutor(db_session)
    outcome = await executor.execute(
        run, 0, 0, ToolCall(id="call_1", name="delete_resource", arguments={"id": "abc"}), tool=tool
    )
    assert outcome.needs_approval is True

    result = await db_session.execute(
        sa.select(sa.func.count()).select_from(ApprovalRequest).where(ApprovalRequest.run_id == run.id)
    )
    assert result.scalar_one() == 1
