import uuid

import pytest
import sqlalchemy as sa

from harness.db.enums import MessageRole, RunStatus, StepType
from harness.db.models import AgentStep, Tenant
from harness.runtime.engine import RunEngine
from harness.runtime.repository import RunRepository
from harness.runtime.tools import Tool
from tests.fakes import ScriptedLLMGateway, text_response, tool_call_response


async def _make_run(session, *, goal="investigate the outage"):
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    session.add(tenant)
    await session.flush()
    repo = RunRepository(session)
    run = await repo.create_run(tenant_id=tenant.id, goal=goal)
    await repo.add_message(run.id, MessageRole.user, goal)
    await session.commit()
    return run


async def _search_handler(arguments: dict) -> dict:
    return {"hits": [f"log line matching {arguments.get('query')}"]}


def _search_tool() -> Tool:
    return Tool(
        name="search_logs",
        description="Search logs for a query string.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        handler=_search_handler,
    )


def _failing_tool() -> Tool:
    async def handler(arguments: dict) -> dict:
        raise RuntimeError("backend unavailable")

    return Tool(
        name="failing_tool",
        description="Always fails.",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )


@pytest.mark.asyncio
async def test_engine_runs_tool_then_finishes(db_session):
    run = await _make_run(db_session)

    llm = ScriptedLLMGateway(
        [
            tool_call_response("search_logs", {"query": "500 error"}),
            tool_call_response("finish", {"result": "found root cause", "summary": "done"}),
        ]
    )
    engine = RunEngine(db_session, llm, tools=[_search_tool()])

    final = await engine.run_until_blocked(run.id)

    assert final.status == RunStatus.completed
    assert final.final_result["result"] == "found root cause"
    assert final.final_result["summary"] == "done"
    assert final.final_result["status"] == "ok"
    assert final.final_result["evidence_id"]
    assert final.current_step == 2

    steps = (
        (
            await db_session.execute(
                sa.select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.step_no)
            )
        )
        .scalars()
        .all()
    )
    assert [s.step_no for s in steps] == [0, 1]
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_engine_persists_provider_reasoning_fields_across_tool_turns(db_session):
    run = await _make_run(db_session)
    first = tool_call_response("search_logs", {"query": "500 error"})
    first.provider_fields = {
        "reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque"}]
    }
    llm = ScriptedLLMGateway(
        [first, tool_call_response("finish", {"result": "done", "summary": "done"})]
    )
    engine = RunEngine(db_session, llm, tools=[_search_tool()])

    await engine.run_until_blocked(run.id)

    second_call_messages = llm.calls[1]["messages"]
    assistant = next(message for message in second_call_messages if message.role == "assistant")
    assert assistant.provider_fields == first.provider_fields


@pytest.mark.asyncio
async def test_engine_plain_text_response_waits_for_user(db_session):
    run = await _make_run(db_session)
    llm = ScriptedLLMGateway([text_response("I need more context to proceed.")])
    engine = RunEngine(db_session, llm)

    final = await engine.run_until_blocked(run.id)

    assert final.status == RunStatus.waiting_user_input
    reloaded = await RunRepository(db_session).get_with_history(final.id)
    assert any(m.content == "I need more context to proceed." for m in reloaded.messages)


@pytest.mark.asyncio
async def test_engine_ask_user_tool_waits_for_user(db_session):
    run = await _make_run(db_session)
    llm = ScriptedLLMGateway([tool_call_response("ask_user", {"question": "which environment?"})])
    engine = RunEngine(db_session, llm)

    final = await engine.run_until_blocked(run.id)

    assert final.status == RunStatus.waiting_user_input
    reloaded = await RunRepository(db_session).get_with_history(final.id)
    assert any(m.content == "which environment?" for m in reloaded.messages)


@pytest.mark.asyncio
async def test_engine_resumes_after_user_reply(db_session):
    run = await _make_run(db_session)
    llm = ScriptedLLMGateway([tool_call_response("ask_user", {"question": "which environment?"})])
    engine = RunEngine(db_session, llm)
    run = await engine.run_until_blocked(run.id)
    assert run.status == RunStatus.waiting_user_input

    repo = RunRepository(db_session)
    await repo.add_message(run.id, MessageRole.user, "production")
    run = await repo.checkpoint(run, status=RunStatus.running)
    await db_session.commit()

    llm2 = ScriptedLLMGateway([tool_call_response("finish", {"result": "checked production"})])
    engine2 = RunEngine(db_session, llm2)
    final = await engine2.run_until_blocked(run.id)

    assert final.status == RunStatus.completed
    # The resumed LLM call must have seen the user's reply in context.
    sent_messages = llm2.calls[0]["messages"]
    assert any(m.content == "production" for m in sent_messages)


@pytest.mark.asyncio
async def test_engine_finish_without_result_is_rejected_then_retried(db_session):
    run = await _make_run(db_session)
    llm = ScriptedLLMGateway(
        [
            tool_call_response("finish", {"summary": "oops no result field"}),
            tool_call_response("finish", {"result": "corrected"}),
        ]
    )
    engine = RunEngine(db_session, llm)

    final = await engine.run_until_blocked(run.id)

    assert final.status == RunStatus.completed
    assert final.final_result["result"] == "corrected"
    assert final.final_result["status"] == "ok"
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_engine_blocks_success_after_unresolved_tool_failure(db_session):
    run = await _make_run(db_session)
    llm = ScriptedLLMGateway(
        [
            tool_call_response("failing_tool", {}),
            tool_call_response("finish", {"result": "done"}),
            tool_call_response("finish", {"result": "backend unavailable", "outcome": "failed"}),
        ]
    )
    engine = RunEngine(db_session, llm, tools=[_failing_tool()])

    final = await engine.run_until_blocked(run.id)

    assert final.status == RunStatus.completed
    assert final.final_result["outcome"] == "failed"
    assert len(llm.calls) == 3


@pytest.mark.asyncio
async def test_engine_stops_at_max_steps(db_session):
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    db_session.add(tenant)
    await db_session.flush()
    repo = RunRepository(db_session)
    run = await repo.create_run(tenant_id=tenant.id, goal="loop forever", max_steps=2)
    await db_session.commit()

    llm = ScriptedLLMGateway(
        [
            tool_call_response("search_logs", {"query": "a"}),
            tool_call_response("search_logs", {"query": "b"}),
            tool_call_response("search_logs", {"query": "c"}),
        ]
    )
    engine = RunEngine(db_session, llm, tools=[_search_tool()])

    final = await engine.run_until_blocked(run.id)

    assert final.status == RunStatus.timed_out
    assert "max_steps" in final.error
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_engine_stops_on_repeated_identical_tool_calls(db_session):
    run = await _make_run(db_session)
    llm = ScriptedLLMGateway(
        [
            tool_call_response("search_logs", {"query": "same"}),
            tool_call_response("search_logs", {"query": "same"}),
            tool_call_response("search_logs", {"query": "same"}),
        ]
    )
    engine = RunEngine(db_session, llm, tools=[_search_tool()])

    final = await engine.run_until_blocked(run.id)

    assert final.status == RunStatus.failed
    assert "identical arguments" in final.error


@pytest.mark.asyncio
async def test_engine_resumes_mid_step_without_recalling_llm(db_session):
    """A crash after the LLM call was persisted but before the tool ran must not

    re-invoke the LLM on resume — it should replay the persisted tool_calls and
    pick up exactly where it left off.
    """
    run = await _make_run(db_session)

    # Simulate a crash right after the LLM call for step 0 was persisted: the
    # AgentStep row exists, but no ToolExecution has been written yet.
    repo = RunRepository(db_session)
    await repo.add_step(
        run.id,
        0,
        StepType.llm_call,
        {
            "content": None,
            "tool_calls": [{"id": "call_1", "name": "search_logs", "arguments": {"query": "x"}}],
            "finish_reason": "tool_calls",
            "model": "test-model",
        },
        token_usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )
    await db_session.commit()

    # No responses scripted: if the engine calls the LLM again, this fails loudly.
    llm = ScriptedLLMGateway([tool_call_response("finish", {"result": "done"})])
    engine = RunEngine(db_session, llm, tools=[_search_tool()])

    final = await engine.run_until_blocked(run.id)

    assert final.status == RunStatus.completed
    # Only one LLM call was made (for step 1's finish), not a re-call of step 0.
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_engine_unknown_tool_reports_error_and_continues(db_session):
    run = await _make_run(db_session)
    llm = ScriptedLLMGateway(
        [
            tool_call_response("does_not_exist", {"x": 1}),
            tool_call_response("finish", {"result": "handled the error"}),
        ]
    )
    engine = RunEngine(db_session, llm)

    final = await engine.run_until_blocked(run.id)

    assert final.status == RunStatus.completed
    second_call_messages = llm.calls[1]["messages"]
    tool_messages = [m for m in second_call_messages if m.role == "tool"]
    assert any("unknown tool" in (m.content or "") for m in tool_messages)
