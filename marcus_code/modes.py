import enum
import re

from harness.db.enums import RiskTier


class AgentMode(enum.StrEnum):
    ask = "ask"
    agent = "agent"
    auto = "auto"
    yolo = "yolo"


MODE_HINTS = {
    AgentMode.ask: "Read and analyze only; file changes and commands are blocked.",
    AgentMode.agent: "Normal workflow; Marcus asks before sensitive or destructive tools.",
    AgentMode.auto: "Build, test, and edit automatically; asks only for high-risk commands.",
    AgentMode.yolo: "Runs tools without approval; hard safety guardrails remain active.",
}


def mode_hint(mode: AgentMode) -> str:
    return MODE_HINTS[mode]


def mode_help() -> str:
    lines = ["Available modes:"]
    lines.extend(f"  {mode.value:<5} - {mode_hint(mode)}" for mode in AgentMode)
    return "\n".join(lines)


_DANGEROUS_COMMAND_PATTERNS = (
    r"git\s+push\b",
    r"git\s+reset\b.*--hard",
    r"git\s+clean\b.*-[^\s]*[fd]",
    r"\b(rm|rmdir|remove-item)\b.*(-r|-recurse|/s|--force)",
    r"\b(drop|truncate)\s+(table|database)\b",
    r"\bdelete\s+from\b",
    r"\b(shutdown|reboot|restart-computer|stop-computer)\b",
)


def command_requires_approval(command: str) -> bool:
    lowered = command.lower()
    return any(re.search(pattern, lowered) for pattern in _DANGEROUS_COMMAND_PATTERNS)


def tool_is_allowed(mode: AgentMode, risk_tier: RiskTier) -> bool:
    return mode is not AgentMode.ask or risk_tier is RiskTier.read_only


def tool_requires_approval(
    mode: AgentMode,
    *,
    tool_name: str,
    risk_tier: RiskTier,
    arguments: dict,
) -> bool:
    if mode in {AgentMode.ask, AgentMode.yolo}:
        return False
    if mode is AgentMode.agent:
        return risk_tier in {RiskTier.sensitive_write, RiskTier.destructive}
    if tool_name in {"run_cli", "start_process"}:
        return command_requires_approval(str(arguments.get("command", "")))
    return False


def mode_instructions(mode: AgentMode) -> str:
    return {
        AgentMode.ask: (
            "ASK mode is active. Only inspect and explain; do not attempt file changes, "
            "commands, or process operations."
        ),
        AgentMode.agent: "AGENT mode is active. Use the normal risk-based approval flow.",
        AgentMode.auto: (
            "AUTO mode is active. Work autonomously; only high-risk commands require approval."
        ),
        AgentMode.yolo: (
            "YOLO mode is active. Tool approvals are bypassed, but all hard safety guardrails "
            "remain mandatory."
        ),
    }[mode]
