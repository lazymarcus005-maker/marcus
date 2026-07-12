import argparse
import asyncio
import contextlib
import subprocess
from datetime import datetime
from pathlib import Path

from harness.config import Settings
from harness.llm.gateway import LLMGateway
from marcus_code.commands import CommandContext, dispatch
from marcus_code.config import (
    has_llm_credentials,
    load_project_instructions,
    resolve_settings,
    save_user_config,
)
from marcus_code.loop import MarcusLoop
from marcus_code.prompt import build_system_prompt
from marcus_code.skills import build_skill_catalog
from marcus_code.tools import build_marcus_tools
from marcus_code.ui import TerminalUI


def _new_session_name() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def _git_summary(root: Path) -> str | None:
    try:
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=root, capture_output=True, text=True, timeout=2, check=False).stdout.strip()
        status = subprocess.run(["git", "status", "--short"], cwd=root, capture_output=True, text=True, timeout=2, check=False).stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not branch and not status:
        return None
    state = "clean" if not status else f"{len(status)} changed path(s)"
    return f"branch={branch or '<detached/unknown>'}; worktree={state}"


async def _amain(prompt: str | None = None) -> None:
    root = Path.cwd()
    ui = TerminalUI()
    settings = resolve_settings()

    if not has_llm_credentials(settings):
        setup = ui.run_first_time_setup(
            default_base_url=Settings.model_fields["llm_base_url"].default,
            default_model=Settings.model_fields["llm_model"].default,
        )
        if setup is not None:
            api_key, base_url, model = setup
            save_user_config(api_key=api_key, base_url=base_url, model=model)
            settings = resolve_settings()

    session_name = _new_session_name()
    ui.print_banner(root, model=settings.llm_model, session_name=session_name)

    llm = LLMGateway(settings=settings)
    tools = build_marcus_tools(root, settings)
    project_instructions = load_project_instructions(root)
    loop = MarcusLoop(
        llm,
        tools,
        ui,
        model=settings.llm_model,
        system_prompt=build_system_prompt(root, project_instructions=project_instructions, git_summary=_git_summary(root), skill_catalog=build_skill_catalog(root)),
        max_history_messages=settings.cli_max_history_messages,
        max_total_tokens=settings.cli_max_total_tokens,
        history_summary_enabled=settings.cli_history_summary_enabled,
    )
    ctx = CommandContext(ui=ui, loop=loop, settings=settings)

    try:
        if prompt is not None:
            await loop.run_turn(prompt)
            return
        while True:
            user_input = ui.prompt_user()
            if user_input is None:
                break
            stripped = user_input.strip()
            if not stripped:
                continue
            if stripped.startswith("/"):
                should_continue = await dispatch(ctx, stripped)
                if not should_continue:
                    break
                continue
            try:
                await loop.run_turn(user_input)
            except KeyboardInterrupt:
                ui.print_interrupted()
    finally:
        await ctx.loop.llm.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="marcus")
    parser.add_argument("-p", "--prompt", help="run one prompt non-interactively")
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_amain(args.prompt))


if __name__ == "__main__":
    main()
