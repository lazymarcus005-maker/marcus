"""Slash command metadata for help, auto-suggest, and discoverability."""

from __future__ import annotations

_COMMAND_DESCRIPTIONS: dict[str, str] = {
    "/help": "Show this help",
    "/?": "Show this help",
    "/model": "Show or switch the active model",
    "/effort": "Set reasoning effort, output limit, or thinking budget",
    "/usage": "Show token usage; Ollama Cloud quota when configured",
    "/status": "Show session, context, model, and workspace status",
    "/compact": "Compact retained conversation context now",
    "/retry": "Retry the previous task",
    "/continue": "Continue from the previous turn",
    "/clear": "Clear context; --all also clears approvals",
    "/mode": "Show or switch mode (ask, agent, auto, yolo)",
    "/config": "View or edit the current LLM config",
    "/theme": "Switch theme: dark, light, high-contrast, no-color",
    "/edit": "Enter multi-line input (blank line to finish)",
    "/last": "Show the last guardrail stop reason",
    "/save": "Save last turn summary to a Markdown file",
    "/exit": "Quit Marcus Code",
    "/quit": "Quit Marcus Code",
}

_COMMAND_CATEGORIES: dict[str, tuple[str, ...]] = {
    "Info": ("/help", "/?", "/status", "/usage"),
    "Context": ("/compact", "/clear", "/retry", "/continue"),
    "Config": ("/model", "/effort", "/mode", "/config", "/theme"),
    "Input": ("/edit",),
    "Recovery": ("/last", "/save"),
    "Exit": ("/exit", "/quit"),
}


def command_description(name: str) -> str:
    """Return the one-line description for a slash command."""
    return _COMMAND_DESCRIPTIONS.get(name, "")


def command_categories() -> dict[str, tuple[str, ...]]:
    """Return command names grouped by category."""
    return _COMMAND_CATEGORIES


def all_commands() -> tuple[str, ...]:
    """Return all known slash command names in display order."""
    return tuple(_COMMAND_DESCRIPTIONS.keys())
