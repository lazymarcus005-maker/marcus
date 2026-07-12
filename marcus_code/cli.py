import argparse
import asyncio
import contextlib
import inspect
import subprocess
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
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
from marcus_code.modes import AgentMode
from marcus_code.prompt import build_system_prompt
from marcus_code.skills import build_skill_catalog
from marcus_code.tools import build_marcus_tools
from marcus_code.ui import TerminalUI


def _version_string() -> str:
    try:
        package_version = version("marcus")
    except PackageNotFoundError:
        package_version = "unknown"
    return f"Marcus Code {package_version}"


def _new_session_name() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def _git_summary(root: Path) -> str | None:
    try:
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not branch and not status:
        return None
    state = "clean" if not status else f"{len(status)} changed path(s)"
    return f"branch={branch or '<detached/unknown>'}; worktree={state}"


async def _amain(prompt: str | None = None, mode: AgentMode | None = None) -> None:
    root = Path.cwd()
    ui = TerminalUI()
    settings = resolve_settings()
    if mode is None:
        try:
            mode = AgentMode(settings.cli_default_mode)
        except ValueError:
            mode = AgentMode.agent

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
    if (
        mode is AgentMode.yolo
        and hasattr(ui, "confirm_yolo_mode")
        and not ui.confirm_yolo_mode()
    ):
        mode = AgentMode.agent
    ui.print_banner(root, model=settings.llm_model, session_name=session_name, mode=mode.value)

    llm = LLMGateway(settings=settings)
    tools = build_marcus_tools(root, settings)
    project_instructions = load_project_instructions(root)
    loop = MarcusLoop(
        llm,
        tools,
        ui,
        model=settings.llm_model,
        system_prompt=build_system_prompt(
            root,
            project_instructions=project_instructions,
            git_summary=_git_summary(root),
            skill_catalog=build_skill_catalog(root),
            mode=mode,
        ),
        max_steps=settings.cli_max_steps,
        max_history_messages=settings.cli_max_history_messages,
        max_total_tokens=settings.cli_max_total_tokens,
        history_summary_enabled=settings.cli_history_summary_enabled,
        mode=mode,
        context_window_tokens=settings.cli_context_window_tokens,
        compact_threshold_percent=settings.cli_compact_threshold_percent,
        compact_target_percent=settings.cli_compact_target_percent,
        llm_recovery_timeout_seconds=settings.cli_llm_recovery_timeout_seconds,
    )
    if hasattr(ui, "bind_status"):
        ui.bind_status(lambda: loop.status(str(root)))
    ctx = CommandContext(ui=ui, loop=loop, settings=settings)

    try:
        if prompt is not None:
            try:
                await loop.run_turn(prompt)
            finally:
                if hasattr(tools, "process_manager"):
                    await tools.process_manager.aclose()
            return
        while True:
            user_input = ui.prompt_user()
            if inspect.isawaitable(user_input):
                user_input = await user_input
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
                if hasattr(ui, "register_interrupt") and ui.register_interrupt():
                    break
                if not hasattr(ui, "register_interrupt"):
                    ui.print_interrupted()
            finally:
                if hasattr(tools, "process_manager"):
                    await tools.process_manager.aclose()
    finally:
        if hasattr(tools, "aclose"):
            await tools.aclose()
        await ctx.loop.llm.aclose()
        if hasattr(ui, "aclose"):
            await ui.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="marcus")
    parser.add_argument("-v", "--version", action="version", version=_version_string())
    parser.add_argument(
        "--mode", choices=[mode.value for mode in AgentMode]
    )
    parser.add_argument("-p", "--prompt", help="run one prompt non-interactively")
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_amain(args.prompt, AgentMode(args.mode) if args.mode else None))


if __name__ == "__main__":
    main()
