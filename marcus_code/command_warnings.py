"""Dangerous shell command detection for the approval UX."""

from __future__ import annotations

import re

_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"rm\s+(-\w*f|--force).*\s-r|rm\s+-r[f\s]|rmdir\s+/s"), "recursive deletion or forced removal"),
    (re.compile(r"git\s+push\b.*--force|git\s+reset\b.*--hard"), "irreversible or remote Git history rewrite"),
    (re.compile(r"\b(drop|truncate)\s+(table|database)\b|delete\s+from\b"), "destructive database operation"),
    (re.compile(r"\bdd\b.*if=|mkfs\."), "disk or filesystem operation"),
    (re.compile(r"chmod\s+(-R|--recursive).*\s/"), "recursive permission change on root"),
    (re.compile(r"\u003e\s*/dev/[sh]d[a-z]|\u003e\s*/"), "writing directly to a device or root filesystem"),
    (re.compile(r"curl\s+.*\|\s*(sh|bash|zsh|fish)|wget\s+.*\|\s*(sh|bash|zsh|fish)"), "remote code execution via pipe"),
    (re.compile(r"sudo\s+rm|sudo\s+dd|sudo\s+mkfs"), "privileged destructive operation"),
    (re.compile(r"shutdown|reboot|poweroff|halt"), "system shutdown or reboot"),
    (re.compile(r"killall\s+|pkill\s+-9"), "mass process termination"),
    (re.compile(r"\bmv\s+.*\s+/dev/null\b"), "data destruction via /dev/null"),
]


def command_warning(command: str) -> str | None:
    """Return a human-readable warning if the command looks dangerous."""
    lowered = command.lower()
    for pattern, warning in _DANGEROUS_PATTERNS:
        if pattern.search(lowered):
            return warning
    return None


def risk_level(command: str) -> str:
    """Classify a shell command as low, medium, or high risk."""
    if command_warning(command):
        return "high"
    lowered = command.lower()
    medium_patterns = [
        r"\bsudo\b",
        r"\b(git\s+(push|pull|merge|rebase))",
        r"\b(docker\s+(exec|run|rm|stop|kill))",
        r"\b(kubectl\s+(apply|delete|exec))",
        r"\b(apt\s+|yum\s+|pacman\s+|brew\s+install|pip\s+install|uv\s+tool\s+install)",
        r"\bcurl\b|\bwget\b",
    ]
    for pattern in medium_patterns:
        if re.search(pattern, lowered):
            return "medium"
    return "low"
