from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from harness.config import Settings
from harness.llm.gateway import LLMGateway
from harness.llm.types import LLMMessage
from marcus_code.runtime.agent import MarcusLoop
from marcus_code.runtime.modes import AgentMode, mode_help, mode_hint, mode_instructions
from marcus_code.runtime.ollama_usage import (
    OllamaCloudUsageClient,
    OllamaUsageError,
    is_ollama_cloud,
)
from marcus_code.state.config import (
    has_llm_credentials,
    resolve_settings,
    save_user_config,
    validate_reasoning_effort,
)
from marcus_code.ui.console import TerminalUI

EXIT_COMMANDS = {"/exit", "/quit"}


@dataclass
class CommandContext:
    """Shared mutable state slash-command handlers can read and act on."""

    ui: TerminalUI
    loop: MarcusLoop
    settings: Settings

    async def replace_llm(self, new_settings: Settings) -> None:
        """Swap the loop's LLMGateway for one built from new_settings — used
        when /config edit changes the base URL or API key mid-session (the
        client bakes those in at construction, unlike the model, which
        MarcusLoop.complete() already overrides per-call)."""
        old_llm = self.loop.llm
        self.loop.llm = LLMGateway(settings=new_settings)
        self.loop.model = new_settings.llm_model
        self.loop.reasoning_effort = new_settings.llm_reasoning_effort
        self.loop.max_completion_tokens = new_settings.llm_max_completion_tokens
        self.settings = new_settings
        await old_llm.aclose()


CommandHandler = Callable[[CommandContext, str], Awaitable[None]]


async def _cmd_help(ctx: CommandContext, args: str) -> None:
    ctx.ui.print_help()


async def _cmd_model(ctx: CommandContext, args: str) -> None:
    from marcus_code.state.config import has_llm_credentials, save_user_config

    name = args.strip()
    if not name:
        ctx.ui.print_info(f"Current model: {ctx.loop.model or ctx.settings.llm_model}")
        return
    ctx.loop.model = name
    # Persist the override to ~/.marcus/config.toml so it becomes the default
    # for future sessions. Preserve the existing base URL and API key.
    api_key = ctx.settings.llm_api_key if has_llm_credentials(ctx.settings) else ""
    try:
        save_user_config(
            api_key=api_key,
            base_url=ctx.settings.llm_base_url,
            model=name,
            reasoning_effort=ctx.settings.llm_reasoning_effort,
            max_completion_tokens=ctx.settings.llm_max_completion_tokens,
        )
    except ValueError as exc:
        ctx.ui.print_error(f"Model switched for this session, but could not save default: {exc}")
        return
    ctx.ui.print_info(f"Model switched to {name!r} and saved as default.")


async def _cmd_effort(ctx: CommandContext, args: str) -> None:
    value = args.strip().lower()
    if not value:
        max_tokens = ctx.loop.max_completion_tokens
        suffix = (
            f"; max completion tokens: {max_tokens:,}"
            if max_tokens is not None
            else "; max completion tokens: provider default"
        )
        ctx.ui.print_info(f"Current reasoning effort: {ctx.loop.reasoning_effort}{suffix}")
        return
    try:
        effort = validate_reasoning_effort(value)
    except ValueError as exc:
        ctx.ui.print_error(str(exc))
        return
    ctx.loop.reasoning_effort = effort
    ctx.settings = ctx.settings.model_copy(update={"llm_reasoning_effort": effort})
    api_key = ctx.settings.llm_api_key if has_llm_credentials(ctx.settings) else ""
    try:
        save_user_config(
            api_key=api_key,
            base_url=ctx.settings.llm_base_url,
            model=ctx.settings.llm_model,
            reasoning_effort=effort,
            max_completion_tokens=ctx.settings.llm_max_completion_tokens,
        )
    except ValueError as exc:
        ctx.ui.print_error(f"Reasoning effort switched for this session, but could not save default: {exc}")
        return
    if hasattr(ctx.ui, "refresh_status"):
        ctx.ui.refresh_status()
    ctx.ui.print_info(f"Reasoning effort switched to {effort!r} and saved as default.")


async def _cmd_usage(ctx: CommandContext, args: str) -> None:
    action = args.strip().lower()
    if action not in {"", "login", "logout"}:
        ctx.ui.print_error("usage: /usage, /usage login, or /usage logout")
        return
    ctx.ui.print_usage(
        ctx.loop.usage,
        session_started_at=ctx.loop.started_at,
        max_total_tokens=ctx.loop.max_total_tokens,
    )
    if not is_ollama_cloud(ctx.settings.llm_base_url):
        return

    client = OllamaCloudUsageClient()
    if action == "logout":
        try:
            removed = client.logout()
        except OllamaUsageError as exc:
            ctx.ui.print_error(f"Ollama Cloud logout failed: {exc}")
            return
        ctx.ui.print_info(f"Ollama Cloud login data cleared ({removed} item(s)).")
        return
    # Only the explicit login command may open a visible browser. Plain
    # /usage must remain non-interactive and use the saved session state.
    interactive = action == "login"
    if interactive:
        ctx.ui.print_info(
            "Opening Ollama settings in a browser. Log in there if requested; "
            "Marcus will continue when usage is available."
        )
    try:
        usage = await client.fetch(interactive=interactive)
    except OllamaUsageError as exc:
        ctx.ui.print_error(f"Ollama Cloud usage unavailable: {exc}")
        return
    if hasattr(ctx.ui, "print_ollama_cloud_usage"):
        ctx.ui.print_ollama_cloud_usage(usage)


async def _cmd_retry(ctx: CommandContext, args: str) -> None:
    previous = ctx.loop.state.last_turn_input
    if not previous:
        ctx.ui.print_error("no previous task to retry")
        return
    ctx.ui.print_info(
        f"Retrying previous task: {previous[:80]}{'...' if len(previous) > 80 else ''}"
    )
    await ctx.loop.run_turn(previous)


async def _cmd_continue(ctx: CommandContext, args: str) -> None:
    guardrail = ctx.loop.state.last_turn_guardrail
    if guardrail:
        ctx.loop.state.history.append(
            LLMMessage(
                role="system",
                content=f"The previous turn was stopped by the runtime: {guardrail}. "
                "Continue from where you left off and make progress toward the original goal.",
            )
        )
    ctx.ui.print_info("Continuing from the previous turn...")
    # Preserve the original capabilities/verification policy. Reclassifying
    # the word "continue" would otherwise turn a stopped change into a
    # read-only explanation request.
    await ctx.loop.run_turn(
        "continue from where you stopped",
        contract=ctx.loop.state.active_contract,
    )


async def _cmd_compact(ctx: CommandContext, args: str) -> None:
    before, after = ctx.loop.compact_history()
    ctx.ui.print_info(f"Context compacted: {before:,} → {after:,} estimated tokens.")


async def _cmd_clear(ctx: CommandContext, args: str) -> None:
    action = args.strip().lower()
    if action not in {"", "--all"}:
        ctx.ui.print_error("usage: /clear or /clear --all")
        return
    ctx.loop.clear_history(clear_all=action == "--all")
    detail = "context and approval preferences" if action == "--all" else "conversation context"
    ctx.ui.print_info(f"Cleared {detail}.")
    ctx.ui.clear_screen()


async def _cmd_last(ctx: CommandContext, args: str) -> None:
    if hasattr(ctx.ui, "print_last_guardrail"):
        ctx.ui.print_last_guardrail()


async def _cmd_save(ctx: CommandContext, args: str) -> None:
    from datetime import datetime
    from pathlib import Path

    path_arg = args.strip()
    default = (
        Path.home() / ".marcus" / "sessions" / f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.md"
    )
    path = Path(path_arg) if path_arg else default
    if hasattr(ctx.ui, "save_turn"):
        ctx.ui.save_turn(
            path,
            user_input=ctx.loop.state.last_turn_input,
            final_answer="",
            usage=ctx.loop.usage,
            guardrail=ctx.loop.state.last_turn_guardrail,
        )
    ctx.ui.print_info(f"Turn saved to {path}")


async def _cmd_status(ctx: CommandContext, args: str) -> None:
    if hasattr(ctx.ui, "print_status"):
        ctx.ui.print_status()


async def _cmd_theme(ctx: CommandContext, args: str) -> None:
    value = args.strip().lower()
    valid = {"dark", "light", "high-contrast", "no-color"}
    if not value:
        current = "no-color" if getattr(ctx.ui, "_no_color", False) else "dark"
        ctx.ui.print_info(
            f"Current theme: {current} (available: dark, light, high-contrast, no-color)"
        )
        return
    if value not in valid:
        ctx.ui.print_error(f"unknown theme: {value!r} (choose: {', '.join(valid)})")
        return
    if not hasattr(ctx.ui, "set_theme"):
        ctx.ui.print_error("theme switching is not supported by this UI")
        return
    from marcus_code.ui.console import ThemeName

    theme_value: ThemeName = value  # type: ignore[assignment]
    ctx.ui.set_theme(theme_value)
    ctx.ui.print_info(f"Theme switched to {theme_value!r}.")


async def _cmd_edit(ctx: CommandContext, args: str) -> None:
    if not hasattr(ctx.ui, "prompt_multiline"):
        ctx.ui.print_error("multi-line input is not supported by this UI")
        return
    text = ctx.ui.prompt_multiline()
    if text is None or text.strip() == "":
        ctx.ui.print_info("Multi-line input cancelled or empty.")
        return
    ctx.ui.print_info(f"Multi-line input submitted ({len(text)} characters).")
    await ctx.loop.run_turn(text)


async def _cmd_mode(ctx: CommandContext, args: str) -> None:
    value = args.strip().lower()
    if not value:
        ctx.ui.print_info(f"Current mode: {ctx.loop.mode.value}\n{mode_help()}")
        return
    try:
        mode = AgentMode(value)
    except ValueError:
        choices = ", ".join(item.value for item in AgentMode)
        ctx.ui.print_error(f"unknown mode: {value!r} (choose: {choices})")
        return
    if (
        mode is AgentMode.yolo
        and hasattr(ctx.ui, "confirm_yolo_mode")
        and not ctx.ui.confirm_yolo_mode()
    ):
        ctx.ui.print_info("Mode unchanged.")
        return
    ctx.loop.mode = mode
    ctx.loop.state.always_allowed.clear()
    ctx.loop.state.history.append(
        LLMMessage(role="system", content=f"Mode changed. {mode_instructions(mode)}")
    )
    if hasattr(ctx.ui, "set_mode"):
        ctx.ui.set_mode(mode.value)
    ctx.ui.print_info(f"Mode switched to {mode.value!r} for this session.\nHint: {mode_hint(mode)}")


async def _cmd_exit(ctx: CommandContext, args: str) -> None:
    """Placeholder so /exit and /quit appear in help."""


async def _cmd_config(ctx: CommandContext, args: str) -> None:
    action = args.strip().lower()
    if action in ("", "show", "view"):
        ctx.ui.print_config(ctx.settings)
        return
    if action == "edit":
        result = ctx.ui.run_config_edit(
            current_base_url=ctx.settings.llm_base_url,
            current_model=ctx.settings.llm_model,
            has_existing_key=has_llm_credentials(ctx.settings),
            current_api_key=ctx.settings.llm_api_key if has_llm_credentials(ctx.settings) else "",
        )
        if result is None:
            return
        new_key, base_url, model = result
        if new_key is None:
            if not has_llm_credentials(ctx.settings):
                ctx.ui.print_error("no API key configured yet — you must enter one")
                return
            new_key = ctx.settings.llm_api_key
        try:
            save_user_config(
                api_key=new_key,
                base_url=base_url,
                model=model,
                reasoning_effort=ctx.settings.llm_reasoning_effort,
                max_completion_tokens=ctx.settings.llm_max_completion_tokens,
            )
        except ValueError as exc:
            ctx.ui.print_error(str(exc))
            return
        await ctx.replace_llm(resolve_settings())
        ctx.ui.print_info("Config updated.")
        return
    ctx.ui.print_error(f"unknown /config action: {action!r} (use /config, /config edit)")


COMMANDS: dict[str, CommandHandler] = {
    "/help": _cmd_help,
    "/?": _cmd_help,
    "/model": _cmd_model,
    "/effort": _cmd_effort,
    "/usage": _cmd_usage,
    "/retry": _cmd_retry,
    "/continue": _cmd_continue,
    "/compact": _cmd_compact,
    "/clear": _cmd_clear,
    "/last": _cmd_last,
    "/save": _cmd_save,
    "/status": _cmd_status,
    "/mode": _cmd_mode,
    "/config": _cmd_config,
    "/theme": _cmd_theme,
    "/edit": _cmd_edit,
    "/exit": _cmd_exit,
    "/quit": _cmd_exit,
}


async def dispatch(ctx: CommandContext, raw: str) -> bool:
    """Handle a slash command. Returns False if the REPL should exit."""
    parts = raw.split(maxsplit=1)
    name = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if name in EXIT_COMMANDS:
        return False

    handler = COMMANDS.get(name)
    if handler is None:
        ctx.ui.print_error(f"unknown command: {name} (try /help)")
        return True

    await handler(ctx, args)
    return True
