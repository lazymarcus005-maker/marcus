from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from harness.config import Settings
from harness.llm.gateway import LLMGateway
from marcus_code.config import has_llm_credentials, resolve_settings, save_user_config
from marcus_code.loop import MarcusLoop
from marcus_code.ui import TerminalUI

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
        self.settings = new_settings
        await old_llm.aclose()


CommandHandler = Callable[[CommandContext, str], Awaitable[None]]


async def _cmd_help(ctx: CommandContext, args: str) -> None:
    ctx.ui.print_help()


async def _cmd_model(ctx: CommandContext, args: str) -> None:
    name = args.strip()
    if not name:
        ctx.ui.print_info(f"Current model: {ctx.loop.model or ctx.settings.llm_model}")
        return
    ctx.loop.model = name
    ctx.ui.print_info(f"Model switched to {name!r} for this session.")


async def _cmd_usage(ctx: CommandContext, args: str) -> None:
    ctx.ui.print_usage(ctx.loop.usage, session_started_at=ctx.loop.started_at)


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
        )
        if result is None:
            return
        new_key, base_url, model = result
        if new_key is None:
            if not has_llm_credentials(ctx.settings):
                ctx.ui.print_error("no API key configured yet — you must enter one")
                return
            new_key = ctx.settings.llm_api_key
        save_user_config(api_key=new_key, base_url=base_url, model=model)
        await ctx.replace_llm(resolve_settings())
        ctx.ui.print_info("Config updated.")
        return
    ctx.ui.print_error(f"unknown /config action: {action!r} (use /config, /config edit)")


COMMANDS: dict[str, CommandHandler] = {
    "/help": _cmd_help,
    "/?": _cmd_help,
    "/model": _cmd_model,
    "/usage": _cmd_usage,
    "/config": _cmd_config,
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
