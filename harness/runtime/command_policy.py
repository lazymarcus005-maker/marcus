import hashlib
import re
from dataclasses import dataclass

from harness.runtime.tools import ToolErrorCode, ToolRuntimeError

_SECRET_DUMP = re.compile(
    r"(?i)(?:^|[;&|]\s*)(?:env|printenv|set)(?:\s|$)"
    r"|(?:get-childitem|gci|dir)\s+env:"
    r"|(?:cat|type|get-content|gc)\s+[^;&|]*(?:\.env(?:\.|\s|$)|credentials\.json|\.pem|\.key)"
)


@dataclass(frozen=True)
class CommandMetadata:
    executable: str
    category: str
    command_hash: str

    def as_dict(self) -> dict[str, str]:
        return {
            "executable": self.executable,
            "category": self.category,
            "command_hash": self.command_hash,
        }


def inspect_shell_command(command: str) -> CommandMetadata:
    """Apply cheap deterministic policy before invoking a shell."""
    normalized = " ".join(command.strip().split())
    if not normalized:
        raise ToolRuntimeError("command is empty", code=ToolErrorCode.invalid_argument)
    if _SECRET_DUMP.search(normalized):
        raise ToolRuntimeError(
            "command blocked because it may expose environment variables or secret files",
            code=ToolErrorCode.policy_denied,
        )

    executable = normalized.split(maxsplit=1)[0].strip("'\"").lower()
    category = _classify(normalized.lower(), executable)
    return CommandMetadata(
        executable=executable,
        category=category,
        command_hash=hashlib.sha256(normalized.encode()).hexdigest(),
    )


def _classify(command: str, executable: str) -> str:
    if any(marker in command for marker in (" test", "pytest", "npm test", "dotnet test")):
        return "test"
    if any(marker in command for marker in (" build", "dotnet build", "npm run build")):
        return "build"
    if executable in {"curl", "wget", "irm", "invoke-webrequest"}:
        return "network"
    if executable in {"git"}:
        return "git"
    if executable in {"npm", "pnpm", "yarn", "pip", "uv", "dotnet"}:
        return "package"
    return "shell"
