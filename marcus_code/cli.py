import argparse
import asyncio
import contextlib
import inspect
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import httpx

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
from marcus_code.ollama_usage import is_ollama_cloud, load_cached_ollama_email
from marcus_code.prompt import build_system_prompt
from marcus_code.skills import build_skill_catalog
from marcus_code.tools import build_marcus_tools
from marcus_code.ui import TerminalUI


def _current_version() -> str:
    try:
        return version("marcus")
    except PackageNotFoundError:
        return "unknown"


def _version_string() -> str:
    return f"Marcus Code {_current_version()}"


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


def _detect_install_method() -> str:
    """Best-effort detection of how the current marcus executable was installed.

    Returns one of: 'uv_tool', 'pipx', 'pip', 'local_dev', or 'unknown'.
    """
    executable = Path(sys.executable).resolve()

    # uv tool environments live under a path like .../uv/tools/marcus/...
    # and use uv's tool-specific Python wrapper.
    uv_tool_dir = os.environ.get("UV_TOOL_DIR", "")
    if uv_tool_dir and str(executable).startswith(Path(uv_tool_dir).as_posix()):
        return "uv_tool"
    if "uv/tools/" in str(executable).replace("\\", "/"):
        return "uv_tool"

    # pipx environments are virtual environments named after the package.
    pipx_home = os.environ.get("PIPX_HOME", "")
    pipx_local_venvs = Path.home() / ".local" / "pipx" / "venvs"
    if pipx_home and str(executable).startswith(Path(pipx_home).as_posix()):
        return "pipx"
    if str(executable).startswith(str(pipx_local_venvs.resolve())):
        return "pipx"

    # A plain site-packages installation (pip install --user, system pip, etc.).
    if "site-packages" in str(executable):
        return "pip"

    # Running from the repo source tree with `uv run marcus`.
    marker = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if marker.is_file():
        return "local_dev"

    return "unknown"


def _update_command(install_method: str, target_version: str | None = None) -> list[str]:
    """Return the command to run to update marcus for the given install method.

    If ``target_version`` is given, pin the install to that release.
    """
    specifier = f"marcus=={target_version}" if target_version else "marcus"
    if install_method == "uv_tool":
        return ["uv", "tool", "install", "--force", specifier]
    if install_method == "pipx":
        if target_version:
            return ["pipx", "upgrade", "--include-injected", specifier]
        return ["pipx", "upgrade", "marcus"]
    if install_method == "pip":
        return [sys.executable, "-m", "pip", "install", "--upgrade", specifier]
    return []


def _version_cache_file() -> Path:
    cache_dir = Path.home() / ".marcus"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "version-check.json"


def _read_cached_latest_version() -> tuple[str | None, bool]:
    """Return (cached_version, cache_is_fresh).

    The cache is considered fresh for 24 hours.
    """
    cache_file = _version_cache_file()
    if not cache_file.is_file():
        return None, False
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        cached = data.get("version")
        timestamp = data.get("checked_at", 0)
        if not isinstance(cached, str) or not isinstance(timestamp, (int, float)):
            return None, False
        age_seconds = (datetime.now() - datetime.fromtimestamp(timestamp)).total_seconds()
        return cached, age_seconds < 24 * 3600
    except (OSError, ValueError, TypeError):
        return None, False


def _write_cached_latest_version(version: str) -> None:
    cache_file = _version_cache_file()
    payload = {"version": version, "checked_at": datetime.now().timestamp()}
    with contextlib.suppress(OSError):
        cache_file.write_text(json.dumps(payload), encoding="utf-8")


def _latest_release_info(timeout: float = 5.0) -> dict | None:
    """Fetch the latest GitHub release payload.

    Returns the parsed JSON, or None if the request fails.
    """
    try:
        response = httpx.get(
            "https://api.github.com/repos/lazymarcus005-maker/marcus/releases/latest",
            timeout=timeout,
            headers={"Accept": "application/vnd.github+json"},
        )
        response.raise_for_status()
        return response.json()
    except Exception:  # noqa: BLE001 - network/API failures are non-fatal
        return None


def _latest_release_version(timeout: float = 5.0) -> str | None:
    """Fetch the latest release tag name from GitHub.

    Uses a 24-hour on-disk cache to avoid hitting the API every startup.
    Returns the version without the leading 'v', or None if the check fails.
    """
    cached, fresh = _read_cached_latest_version()
    if fresh:
        return cached

    info = _latest_release_info(timeout=timeout)
    tag = info.get("tag_name", "") if info else ""
    latest = tag.lstrip("v") if tag else None
    if latest:
        _write_cached_latest_version(latest)
    return latest or cached


def _version_is_newer(latest: str, current: str) -> bool:
    """Compare two PEP 440-ish version strings.

    Uses a simple numeric tuple comparison. Pre-release suffixes (a/b/rc/dev)
    are treated as older than the final release with the same base.
    """
    import re

    def _parse(value: str) -> tuple[list[int], int, str]:
        # Split pre-release suffix so that 1.0.0 is newer than 1.0.0a1.
        match = re.match(r"^(\d+(?:\.\d+)*)(.*)$", value.strip())
        if not match:
            return ([0], 1, "")
        base = [int(part) for part in match.group(1).split(".")]
        suffix = match.group(2).lower()
        if not suffix:
            pre = 5  # final release
        elif suffix.startswith("rc"):
            pre = 4
        elif suffix.startswith("b"):
            pre = 3
        elif suffix.startswith("a"):
            pre = 2
        elif suffix.startswith("dev"):
            pre = 1
        else:
            pre = 0
        return (base, pre, suffix)

    latest_base, latest_pre, latest_suffix = _parse(latest)
    current_base, current_pre, current_suffix = _parse(current)

    # Pad numeric parts to the same length so a shorter base doesn't win.
    length = max(len(latest_base), len(current_base))
    latest_base.extend([0] * (length - len(latest_base)))
    current_base.extend([0] * (length - len(current_base)))

    if latest_base != current_base:
        return latest_base > current_base
    if latest_pre != current_pre:
        return latest_pre > current_pre
    # Same pre-release class (e.g. both alpha); compare suffix text for a/b/rc number.
    return latest_suffix > current_suffix


def _print_release_notes(version: str, info: dict | None) -> None:
    """Print a compact release notes excerpt if available."""
    if not info:
        return
    body = info.get("body") or ""
    if not body or "..." in body or "Full Changelog" in body:
        return
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        return
    print("Release notes:")
    for line in lines[:8]:
        print(f"  - {line}")
    if len(lines) > 8:
        print(f"  ... and {len(lines) - 8} more line(s).")


def _notify_if_update_available() -> None:
    """Print a small notification when a newer release exists.

    Silently ignores network failures so startup is never blocked.
    """
    current = _current_version()
    if current in {"unknown", "0.0.0", ""}:
        return
    latest = _latest_release_version()
    if latest and _version_is_newer(latest, current):
        print(
            f"[update available] Marcus {latest} is out (you have {current}). "
            f"Run 'marcus --update' to upgrade."
        )


def _confirm_update(
    current: str, target: str, method: str, info: dict | None, *, assume_yes: bool = False
) -> bool:
    """Ask the user to confirm an update."""
    if assume_yes:
        return True
    print(f"Will update Marcus from {current} to {target} using {method}.")
    _print_release_notes(target, info)
    try:
        answer = input("Proceed? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("Update cancelled.")
        return False
    if answer in {"", "y", "yes"}:
        return True
    print("Update cancelled.")
    return False


def _run_update(target_version: str | None = None, *, assume_yes: bool = False) -> int:
    """Run the appropriate update command and report the result."""
    current = _current_version()
    method = _detect_install_method()
    if method == "local_dev":
        print(
            "Marcus is running from the local source tree. To update, run:\n"
            "  git pull && uv sync --all-extras --dev"
        )
        return 0

    info: dict | None = None
    if target_version is not None:
        # A full check would paginate releases; for CLI use we accept the latest
        # release or any version string the user explicitly requests. Installing
        # a non-existent version will simply fail at the package-manager step.
        latest = target_version
    elif current not in {"unknown", "0.0.0", ""}:
        info = _latest_release_info()
        latest_value = info.get("tag_name", "").lstrip("v") if info else None
        if latest_value is None:
            print("Could not reach GitHub to check the latest release. Update aborted.")
            return 1
        if not _version_is_newer(latest_value, current):
            print(f"Marcus is already up to date ({current}). No update needed.")
            return 0
        latest = latest_value
    else:
        latest = None

    if latest is None:
        print("No target version available for update.")
        return 1

    if not _confirm_update(current, latest, method, info, assume_yes=assume_yes):
        return 0

    print(f"Updating Marcus from {current} to {latest} ({method}) ...")

    command = _update_command(method, target_version=latest)
    if not command:
        print(f"Unable to detect how Marcus was installed ({method}).")
        print("Try one of:\n  uv tool install --force marcus\n  pipx upgrade marcus")
        return 1

    binary = command[0]
    if shutil.which(binary) is None:
        print(f"Updater not found on PATH: {binary}")
        print(f"Please run manually: {' '.join(command)}")
        return 1

    print(f"  {' '.join(command)}")
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Update failed: {exc}")
        return 1

    if result.returncode != 0:
        print("Update command failed:")
        if result.stderr:
            print(result.stderr)
        return result.returncode

    print("Marcus updated successfully. Restart the CLI to use the new version.")
    return 0


async def _amain(
    prompt: str | None = None, mode: AgentMode | None = None, *, no_color: bool = False
) -> None:
    root = Path.cwd()
    ui = TerminalUI(no_color=no_color)
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
    _notify_if_update_available()
    if mode is AgentMode.yolo and hasattr(ui, "confirm_yolo_mode") and not ui.confirm_yolo_mode():
        mode = AgentMode.agent
    profile_email = (
        load_cached_ollama_email() if is_ollama_cloud(settings.llm_base_url) else "(not available)"
    )
    ui.print_banner(
        root,
        model=settings.llm_model,
        session_name=session_name,
        mode=mode.value,
        provider_url=settings.llm_base_url,
        profile_email=profile_email,
        marcus_version=_current_version(),
    )

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
        max_tool_calls_per_step=settings.cli_max_tool_calls_per_step,
        max_argument_repairs=settings.cli_max_argument_repairs,
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
            raw_input = ui.prompt_user()
            if inspect.isawaitable(raw_input):
                user_input = await raw_input
            else:
                user_input = raw_input
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marcus")
    parser.add_argument("-v", "--version", action="version", version=_version_string())
    parser.add_argument(
        "--update",
        action="store_true",
        help="update Marcus to the latest release (or the version given as an argument)",
    )
    parser.add_argument("-y", "--yes", action="store_true", help="skip confirmation prompts")
    parser.add_argument("--mode", choices=[mode.value for mode in AgentMode])
    parser.add_argument("-p", "--prompt", help="run one prompt non-interactively")
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable colored output and use ASCII characters for progress bars",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args, remaining = parser.parse_known_args()

    # Support both `marcus --update` and the subcommand style `marcus update`.
    if args.update or (remaining and remaining[0] == "update"):
        target_version = None
        if remaining and remaining[0] == "update" and len(remaining) > 1:
            target_version = remaining[1].lstrip("v")
        raise SystemExit(_run_update(target_version, assume_yes=args.yes))
    if remaining:
        parser.error(f"unrecognized arguments: {' '.join(remaining)}")

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(
            _amain(
                args.prompt,
                AgentMode(args.mode) if args.mode else None,
                no_color=args.no_color,
            )
        )


if __name__ == "__main__":
    main()
