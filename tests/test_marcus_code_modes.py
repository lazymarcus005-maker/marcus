import pytest

from harness.db.enums import RiskTier
from marcus_code.modes import AgentMode, command_requires_approval, tool_requires_approval


@pytest.mark.parametrize(
    "command",
    ["git push origin main", "git reset --hard HEAD", "Remove-Item x -Recurse", "DROP TABLE x"],
)
def test_dangerous_commands_require_approval(command):
    assert command_requires_approval(command) is True


@pytest.mark.parametrize("command", ["dotnet build", "pytest -q", "curl localhost:5000"])
def test_normal_commands_do_not_require_approval(command):
    assert command_requires_approval(command) is False


def test_auto_prompts_only_for_dangerous_commands():
    assert (
        tool_requires_approval(
            AgentMode.auto,
            tool_name="run_cli",
            risk_tier=RiskTier.destructive,
            arguments={"command": "dotnet build"},
        )
        is False
    )
    assert (
        tool_requires_approval(
            AgentMode.auto,
            tool_name="run_cli",
            risk_tier=RiskTier.destructive,
            arguments={"command": "git push origin main"},
        )
        is True
    )


def test_yolo_never_requests_human_approval():
    assert (
        tool_requires_approval(
            AgentMode.yolo,
            tool_name="run_cli",
            risk_tier=RiskTier.destructive,
            arguments={"command": "git reset --hard HEAD"},
        )
        is False
    )
