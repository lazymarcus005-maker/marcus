import uuid

import pytest

from harness.db.enums import MessageRole, StepType
from harness.db.models import Tenant
from harness.llm.types import LLMMessage
from harness.runtime.compaction import KEEP_RECENT_STEPS, estimate_tokens, maybe_compact
from harness.runtime.context import build_llm_messages, load_steps
from harness.runtime.repository import RunRepository
from tests.fakes import ScriptedLLMGateway, text_response


async def _make_run(session, *, token_budget=1000):
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    session.add(tenant)
    await session.flush()
    repo = RunRepository(session)
    run = await repo.create_run(
        tenant_id=tenant.id, goal="long investigation", token_budget=token_budget
    )
    await repo.add_message(run.id, MessageRole.user, "long investigation")
    await session.commit()
    return run


async def _add_llm_call_step(repo, session, run_id, step_no, content: str):
    await repo.add_step(
        run_id,
        step_no,
        StepType.llm_call,
        {"content": content, "tool_calls": [], "finish_reason": "stop", "model": "test-model"},
        token_usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    )
    await session.commit()


def test_estimate_tokens_is_roughly_chars_over_four():
    messages = [LLMMessage(role="user", content="a" * 400)]
    assert estimate_tokens(messages) == 100


@pytest.mark.asyncio
async def test_maybe_compact_does_nothing_below_step_threshold(db_session):
    run = await _make_run(db_session)
    repo = RunRepository(db_session)
    for i in range(KEEP_RECENT_STEPS):
        await _add_llm_call_step(repo, db_session, run.id, i, "short")

    llm = ScriptedLLMGateway([])  # must not be called
    await maybe_compact(db_session, llm, run)

    steps = await load_steps(db_session, run.id)
    assert all(s.type == StepType.llm_call for s in steps)


@pytest.mark.asyncio
async def test_maybe_compact_does_nothing_below_token_threshold(db_session):
    # Many steps, but each is tiny — well under 70% of a large token budget.
    run = await _make_run(db_session, token_budget=1_000_000)
    repo = RunRepository(db_session)
    for i in range(KEEP_RECENT_STEPS + 5):
        await _add_llm_call_step(repo, db_session, run.id, i, "short")

    llm = ScriptedLLMGateway([])  # must not be called
    await maybe_compact(db_session, llm, run)

    steps = await load_steps(db_session, run.id)
    assert all(s.type == StepType.llm_call for s in steps)


@pytest.mark.asyncio
async def test_maybe_compact_summarizes_older_steps_and_keeps_recent_verbatim(db_session):
    # Small token budget + long step content forces compaction quickly.
    run = await _make_run(db_session, token_budget=200)
    repo = RunRepository(db_session)
    total_steps = KEEP_RECENT_STEPS + 5
    for i in range(total_steps):
        await _add_llm_call_step(repo, db_session, run.id, i, "x" * 200)

    llm = ScriptedLLMGateway([text_response("condensed summary of early steps")])
    await maybe_compact(db_session, llm, run)

    steps = await load_steps(db_session, run.id)
    summary_steps = [s for s in steps if s.type == StepType.summary]
    assert len(summary_steps) == 1
    assert summary_steps[0].payload["summary"] == "condensed summary of early steps"
    covers_up_to = summary_steps[0].payload["covers_up_to_step_no"]
    assert covers_up_to == total_steps - 1 - KEEP_RECENT_STEPS

    # build_llm_messages must fold in the summary and skip the covered raw steps.
    run = await repo.get(run.id)
    messages = await build_llm_messages(db_session, run)
    summary_messages = [m for m in messages if "condensed summary" in (m.content or "")]
    assert len(summary_messages) == 1

    # Steps at or below covers_up_to must not appear verbatim anymore.
    remaining_llm_steps = [
        s for s in steps if s.type == StepType.llm_call and s.step_no > covers_up_to
    ]
    assert len(remaining_llm_steps) == KEEP_RECENT_STEPS


@pytest.mark.asyncio
async def test_maybe_compact_is_idempotent_once_everything_is_covered(db_session):
    run = await _make_run(db_session, token_budget=200)
    repo = RunRepository(db_session)
    total_steps = KEEP_RECENT_STEPS + 5
    for i in range(total_steps):
        await _add_llm_call_step(repo, db_session, run.id, i, "x" * 200)

    llm = ScriptedLLMGateway([text_response("summary one")])
    await maybe_compact(db_session, llm, run)

    run = await repo.get(run.id)
    llm2 = ScriptedLLMGateway([])  # must not be called again — nothing new to summarize
    await maybe_compact(db_session, llm2, run)

    steps = await load_steps(db_session, run.id)
    assert len([s for s in steps if s.type == StepType.summary]) == 1
