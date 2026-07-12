import pytest

from harness.runtime.command_policy import inspect_shell_command
from harness.runtime.tools import ToolRuntimeError


def test_classifies_and_hashes_build_command():
    metadata = inspect_shell_command("dotnet build MyApp.csproj")

    assert metadata.executable == "dotnet"
    assert metadata.category == "build"
    assert len(metadata.command_hash) == 64


@pytest.mark.parametrize(
    "command",
    ["printenv", "Get-ChildItem Env:", "cat .env", "Get-Content credentials.json"],
)
def test_blocks_commands_that_dump_secrets(command):
    with pytest.raises(ToolRuntimeError, match="may expose"):
        inspect_shell_command(command)
