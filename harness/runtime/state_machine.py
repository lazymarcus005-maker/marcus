from harness.db.enums import RunStatus


class InvalidTransitionError(Exception):
    """Raised when a status transition is not allowed by the run state machine."""


ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.pending: frozenset({RunStatus.running, RunStatus.cancelled}),
    RunStatus.running: frozenset(
        {
            RunStatus.running,
            RunStatus.waiting_user_input,
            RunStatus.waiting_approval,
            RunStatus.completed,
            RunStatus.failed,
            RunStatus.cancelled,
            RunStatus.timed_out,
        }
    ),
    RunStatus.waiting_user_input: frozenset(
        {RunStatus.running, RunStatus.cancelled, RunStatus.timed_out}
    ),
    RunStatus.waiting_approval: frozenset(
        {RunStatus.running, RunStatus.cancelled, RunStatus.timed_out, RunStatus.failed}
    ),
    RunStatus.completed: frozenset(),
    RunStatus.failed: frozenset(),
    RunStatus.cancelled: frozenset(),
    RunStatus.timed_out: frozenset(),
}


def assert_valid_transition(current: RunStatus, target: RunStatus) -> None:
    if target == current:
        return
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransitionError(f"cannot transition run from {current} to {target}")
