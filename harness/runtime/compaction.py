from sqlalchemy.ext.asyncio import AsyncSession

from harness.db.enums import StepType
from harness.db.models import AgentRun
from harness.llm.gateway import LLMGateway
from harness.llm.types import LLMMessage
from harness.runtime.context import (
    latest_covered_step_no,
    llm_call_step_to_messages,
    load_steps,
    load_tool_executions_by_step,
)
from harness.runtime.repository import RunRepository

COMPACTION_THRESHOLD_FRACTION = 0.7
KEEP_RECENT_STEPS = 10
CHARS_PER_TOKEN_ESTIMATE = 4

SUMMARIZE_INSTRUCTION = (
    "Summarize the tool calls, results, and reasoning above concisely, in a few "
    "sentences. Preserve concrete facts, findings, and conclusions that would be "
    "needed to continue the task. Do not include pleasantries."
)


def estimate_tokens(messages: list[LLMMessage]) -> int:
    """Rough chars-per-token heuristic — no real tokenizer is available for an

    arbitrary OpenAI-compatible model, so this is intentionally approximate.
    """
    total_chars = 0
    for message in messages:
        total_chars += len(message.content or "")
        for call in message.tool_calls:
            total_chars += len(call.name) + len(str(call.arguments))
    return total_chars // CHARS_PER_TOKEN_ESTIMATE


async def maybe_compact(session: AsyncSession, llm: LLMGateway, run: AgentRun) -> None:
    """Fold older steps into a summary step once context grows past ~70% of budget.

    Keeps the most recent KEEP_RECENT_STEPS llm_call steps verbatim; everything
    older than that (and not already covered by a prior summary) is condensed
    into one summary step via an extra LLM call. Original steps are never
    deleted — build_llm_messages just skips ones a summary already covers —
    so the audit trail in agent_steps stays intact.
    """
    steps = await load_steps(session, run.id)
    covered_up_to = latest_covered_step_no(steps)
    uncovered = [s for s in steps if s.type == StepType.llm_call and s.step_no > covered_up_to]

    if len(uncovered) <= KEEP_RECENT_STEPS:
        return

    executions_by_step = await load_tool_executions_by_step(session, run.id)
    uncovered_messages = [
        msg
        for step in uncovered
        for msg in llm_call_step_to_messages(step, executions_by_step.get(step.step_no, []))
    ]
    if estimate_tokens(uncovered_messages) < run.token_budget * COMPACTION_THRESHOLD_FRACTION:
        return

    to_summarize = uncovered[:-KEEP_RECENT_STEPS]
    covers_up_to_step_no = to_summarize[-1].step_no

    messages_to_summarize = [
        msg
        for step in to_summarize
        for msg in llm_call_step_to_messages(step, executions_by_step.get(step.step_no, []))
    ]
    messages_to_summarize.append(LLMMessage(role="user", content=SUMMARIZE_INSTRUCTION))

    response = await llm.complete(messages_to_summarize, tools=None)

    existing_summary_count = sum(1 for s in steps if s.type == StepType.summary)
    summary_step_no = -(existing_summary_count + 1)  # negative range never collides with step_no

    repo = RunRepository(session)
    await repo.add_step(
        run.id,
        summary_step_no,
        StepType.summary,
        {"summary": response.content or "", "covers_up_to_step_no": covers_up_to_step_no},
    )
    await session.commit()
