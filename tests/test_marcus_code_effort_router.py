import pytest

from marcus_code.runtime.effort_router import LOW_BUDGET_RATIO, route_reasoning_effort
from marcus_code.runtime.task_contract import (
    Capability,
    ResponseMode,
    TaskContract,
    TaskKind,
    derive_task_contract,
)


def _contract(kind, response_mode, *, capabilities=frozenset()):
    return TaskContract(
        kind=kind,
        response_mode=response_mode,
        capabilities=capabilities,
        requires_plan=response_mode is ResponseMode.agentic,
    )


def test_direct_question_routes_to_low():
    contract = _contract(TaskKind.explain, ResponseMode.direct)
    assert route_reasoning_effort(contract) == "low"


def test_repository_analysis_routes_to_medium():
    contract = _contract(
        TaskKind.explain,
        ResponseMode.agentic,
        capabilities=frozenset({Capability.workspace_read}),
    )
    assert route_reasoning_effort(contract) == "medium"


def test_change_routes_to_high():
    contract = _contract(
        TaskKind.change,
        ResponseMode.agentic,
        capabilities=frozenset({Capability.workspace_write, Capability.command}),
    )
    assert route_reasoning_effort(contract) == "high"


def test_operation_routes_to_high():
    contract = _contract(
        TaskKind.operate,
        ResponseMode.agentic,
        capabilities=frozenset({Capability.command}),
    )
    assert route_reasoning_effort(contract) == "high"


def test_low_budget_steps_effort_down_once():
    change = _contract(
        TaskKind.change,
        ResponseMode.agentic,
        capabilities=frozenset({Capability.workspace_write}),
    )
    explain = _contract(
        TaskKind.explain,
        ResponseMode.agentic,
        capabilities=frozenset({Capability.workspace_read}),
    )
    low = LOW_BUDGET_RATIO - 0.01
    assert route_reasoning_effort(change, budget_remaining_ratio=low) == "medium"
    assert route_reasoning_effort(explain, budget_remaining_ratio=low) == "low"


def test_low_budget_floors_at_low():
    direct = _contract(TaskKind.explain, ResponseMode.direct)
    assert route_reasoning_effort(direct, budget_remaining_ratio=0.0) == "low"


def test_ample_budget_does_not_downgrade():
    change = _contract(
        TaskKind.change,
        ResponseMode.agentic,
        capabilities=frozenset({Capability.workspace_write}),
    )
    assert route_reasoning_effort(change, budget_remaining_ratio=1.0) == "high"
    assert route_reasoning_effort(change, budget_remaining_ratio=LOW_BUDGET_RATIO) == "high"


@pytest.mark.parametrize(
    ("user_input", "expected"),
    [
        ("What is a Python decorator?", "low"),
        ("Explain how the auth module in this repo works", "medium"),
        ("Fix the failing test in agent.py", "high"),
        ("Run the test suite and report the result", "high"),
    ],
)
def test_routes_from_derived_contract(user_input, expected):
    assert route_reasoning_effort(derive_task_contract(user_input)) == expected
