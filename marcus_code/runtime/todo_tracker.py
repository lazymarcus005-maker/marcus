"""Simple workflow tracker for the Marcus agent lifecycle.

The tracker exposes the standard user-facing pipeline:

    Receive → Analyze → Plan → Implement → Validate → Deliver

UI implementations render the tracker to show the user where the agent is
in the workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Phase(Enum):
    receive = "receive"
    analyze = "analyze"
    plan = "plan"
    implement = "implement"
    validate = "validate"
    deliver = "deliver"


_PHASE_LABELS: dict[Phase, str] = {
    Phase.receive: "Receive",
    Phase.analyze: "Analyze requirements",
    Phase.plan: "Plan",
    Phase.implement: "Implement",
    Phase.validate: "Validate",
    Phase.deliver: "Deliver / summarize",
}


@dataclass
class TodoTracker:
    """Tracks progress through the agent workflow."""

    current: Phase = Phase.receive
    steps: list[tuple[Phase, str]] = field(default_factory=list)
    finished: bool = False

    def advance(self, phase: Phase, note: str = "") -> None:
        """Move to the next phase, recording an optional note."""
        if phase == self.current and not note:
            return
        self.current = phase
        self.steps.append((phase, note))

    def finish(self, note: str = "") -> None:
        """Mark the workflow as complete."""
        self.advance(Phase.deliver, note or "Deliver / summarize")
        self.finished = True

    def label(self, phase: Phase | None = None) -> str:
        return _PHASE_LABELS.get(phase or self.current, "")

    def is_active(self, phase: Phase) -> bool:
        """Return True when the workflow is still running in the given phase."""
        return not self.finished and self.current == phase

    def is_done(self, phase: Phase) -> bool:
        """Return True when the workflow has reached or passed the given phase."""
        if self.finished:
            return True
        order = list(Phase)
        return order.index(self.current) >= order.index(phase)

    def reset(self) -> None:
        """Start a fresh workflow for a new user turn."""
        self.current = Phase.receive
        self.steps = []
        self.finished = False
