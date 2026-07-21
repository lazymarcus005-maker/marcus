"""Resolve an ``auto`` reasoning-effort request into a concrete level.

When the user leaves reasoning effort on ``auto`` the runtime picks a level
per turn from the task contract: direct questions cost little inference, code
reading and explanation get a moderate amount, and changes and operations get
the most. When the remaining session token budget is low the chosen level steps
down once so a long session degrades gracefully instead of stopping abruptly.
"""

from harness.llm.types import ReasoningEffort
from marcus_code.runtime.task_contract import ResponseMode, TaskContract, TaskKind

# Below this fraction of the session token budget the routed effort steps down
# one level to conserve the remaining budget.
LOW_BUDGET_RATIO = 0.2

_STEP_DOWN: dict[ReasoningEffort, ReasoningEffort] = {
    "high": "medium",
    "medium": "low",
    "low": "low",
}


def _base_effort(contract: TaskContract) -> ReasoningEffort:
    if contract.response_mode is ResponseMode.direct:
        return "low"
    if contract.kind in {TaskKind.change, TaskKind.operate}:
        return "high"
    return "medium"


def route_reasoning_effort(
    contract: TaskContract,
    *,
    budget_remaining_ratio: float | None = None,
) -> ReasoningEffort:
    """Return the concrete effort level for an ``auto`` request.

    ``budget_remaining_ratio`` is the fraction of the session token budget still
    available (``None`` when no budget is configured). Below ``LOW_BUDGET_RATIO``
    the routed level steps down once, with ``low`` as the floor.
    """
    effort = _base_effort(contract)
    if budget_remaining_ratio is not None and budget_remaining_ratio < LOW_BUDGET_RATIO:
        return _STEP_DOWN[effort]
    return effort
