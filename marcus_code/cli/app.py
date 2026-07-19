import asyncio
import base64
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
import typer

from harness.config import Settings
from harness.llm.gateway import LLMGateway
from marcus_code.cli.commands import CommandContext, dispatch
from marcus_code.runtime.event_bus import EventBus
from marcus_code.state.config import (
    has_llm_credentials,
    load_project_instructions,
    resolve_settings,
    save_user_config,
)
from marcus_code.runtime.agent import MarcusLoop
from marcus_code.runtime.modes import AgentMode
from marcus_code.runtime.ollama_usage import is_ollama_cloud, load_cached_ollama_email
from marcus_code.runtime.prompt import build_system_prompt
from marcus_code.runtime.skills import build_skill_catalog
from marcus_code.tools.base import build_marcus_tools
from marcus_code.ui.console import TerminalUI
from marcus_code.ui.renderer import TerminalRenderer


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
    # cli/app.py → cli/ → marcus_code/ → repo root
    marker = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
    if marker.is_file():
        return "local_dev"

    return "unknown"


def _update_command(install_method: str, target_version: str | None = None) -> list[str]:
    """Return the command to run to update marcus for the given install method.

    If ``target_version`` is given, pin the install to that release.
    Marcus releases are published as GitHub tags, not to PyPI, so non-pip
    methods install directly from the git repository.
    """
    repo_url = "https://github.com/lazymarcus005-maker/marcus.git"
    if install_method == "uv_tool":
        ref = f"@v{target_version}" if target_version else ""
        return ["uv", "tool", "install", "--force", f"git+{repo_url}{ref}"]
    if install_method == "pipx":
        specifier = f"marcus=={target_version}" if target_version else "marcus"
        return ["pipx", "upgrade", "--include-injected", specifier]
    if install_method == "pip":
        specifier = f"marcus=={target_version}" if target_version else "marcus"
        return [sys.executable, "-m", "pip", "install", "--upgrade", specifier]
    return []


def _update_state_dir() -> Path:
    state_dir = Path.home() / ".marcus"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _deferred_update_files() -> tuple[Path, Path]:
    state_dir = _update_state_dir()
    return state_dir / "update-result.json", state_dir / "update.log"


def _should_defer_windows_update(install_method: str) -> bool:
    """Return whether updating would replace the running Windows interpreter."""
    return os.name == "nt" and install_method == "uv_tool"


def _schedule_windows_update(command: list[str], target_version: str) -> Path | None:
    """Run a uv tool update after this process exits and releases Windows locks."""
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if powershell is None:
        return None

    result_file, log_file = _deferred_update_files()
    configuration = {
        "parent_pid": os.getpid(),
        "command": command,
        "target_version": target_version,
        "tool_environment": str(Path(sys.executable).resolve().parent.parent),
        "result_path": str(result_file),
        "log_path": str(log_file),
    }
    encoded_configuration = base64.b64encode(json.dumps(configuration).encode("utf-8")).decode(
        "ascii"
    )
    script = r"""
# Windows PowerShell 5.1 wraps native stderr as non-terminating error records.
# uv writes normal progress to stderr, so rely on $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"
$configurationJson = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String("__CONFIGURATION__")
)
$configuration = $configurationJson | ConvertFrom-Json
$command = @($configuration.command | ForEach-Object { [string]$_ })
$executable = $command[0]
$arguments = @($command | Select-Object -Skip 1)
$outputText = ""
$exitCode = 1
$attempt = 0

function Test-ToolEnvironmentInUse([string]$prefix) {
    foreach ($process in Get-Process -ErrorAction SilentlyContinue) {
        try {
            $processPath = $process.Path
        } catch {
            continue
        }
        if ($processPath -and $processPath.StartsWith(
            $prefix,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            return $true
        }
    }
    return $false
}

try {
    $deadline = (Get-Date).AddMinutes(2)
    while ($null -ne (Get-Process -Id ([int]$configuration.parent_pid) -ErrorAction SilentlyContinue)) {
        if ((Get-Date) -ge $deadline) {
            throw "Timed out waiting for Marcus to exit."
        }
        Start-Sleep -Milliseconds 200
    }
    Start-Sleep -Milliseconds 500

    $environmentDeadline = (Get-Date).AddMinutes(30)
    while (Test-ToolEnvironmentInUse ([string]$configuration.tool_environment)) {
        if ((Get-Date) -ge $environmentDeadline) {
            throw "Timed out waiting for other Marcus sessions to exit."
        }
        Start-Sleep -Milliseconds 500
    }

    for ($attempt = 1; $attempt -le 10; $attempt++) {
        $outputLines = @(& $executable @arguments 2>&1)
        $exitCode = if ($null -eq $LASTEXITCODE) { 1 } else { [int]$LASTEXITCODE }
        $outputText = ($outputLines | Out-String).TrimEnd()
        if ($exitCode -eq 0) {
            break
        }
        if ($outputText -notmatch "(?i)(access is denied|os error 5|being used by another process)") {
            break
        }
        Start-Sleep -Seconds 1
    }
} catch {
    $outputText = ($_ | Out-String).TrimEnd()
    $exitCode = 1
}

$status = if ($exitCode -eq 0) { "success" } else { "failed" }
$finishedAt = [DateTime]::UtcNow.ToString("o")
$logText = @(
    "Marcus update to $($configuration.target_version): $status",
    "Finished: $finishedAt",
    "Attempts: $attempt",
    "Command: $($command -join ' ')",
    "",
    $outputText
) -join [Environment]::NewLine
$result = [ordered]@{
    status = $status
    target_version = [string]$configuration.target_version
    exit_code = $exitCode
    finished_at = $finishedAt
    log_path = [string]$configuration.log_path
}
$utf8NoBom = New-Object System.Text.UTF8Encoding -ArgumentList $false
[IO.File]::WriteAllText([string]$configuration.log_path, $logText, $utf8NoBom)
[IO.File]::WriteAllText(
    [string]$configuration.result_path,
    ($result | ConvertTo-Json -Compress),
    $utf8NoBom
)
""".replace("__CONFIGURATION__", encoded_configuration)
    encoded_script = base64.b64encode(script.encode("utf-16-le")).decode("ascii")

    pending = {
        "status": "pending",
        "target_version": target_version,
        "started_at": datetime.now().timestamp(),
        "log_path": str(log_file),
    }
    try:
        result_file.write_text(json.dumps(pending), encoding="utf-8")
        with contextlib.suppress(OSError):
            log_file.unlink()
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
        subprocess.Popen(  # noqa: S603 - command and target are constructed internally
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded_script,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creation_flags,
        )
    except OSError:
        with contextlib.suppress(OSError):
            result_file.unlink()
        return None
    return log_file


def _report_deferred_update_result() -> bool:
    """Report a completed background update; return True while one is pending."""
    result_file, default_log_file = _deferred_update_files()
    if not result_file.is_file():
        return False
    try:
        result = json.loads(result_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return False

    status = result.get("status")
    target = result.get("target_version", "the requested version")
    if status == "pending":
        started_at = result.get("started_at", 0)
        if isinstance(started_at, (int, float)) and datetime.now().timestamp() - started_at < 1860:
            print(f"Marcus update to {target} is still running. Try again in a few seconds.")
            return True
        print("A previous Marcus update did not finish. You can run 'marcus --update' again.")
    elif status == "success":
        print(f"Marcus updated successfully to {target}.")
    elif status == "failed":
        log_file = result.get("log_path") or str(default_log_file)
        print(f"Previous Marcus update to {target} failed. See: {log_file}")
    else:
        return False

    with contextlib.suppress(OSError):
        result_file.unlink()
    return False


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
    if _should_defer_windows_update(method):
        log_file = _schedule_windows_update(command, latest)
        if log_file is None:
            print("Could not start the Windows update helper (PowerShell was not available).")
            print(f"Close Marcus, then run manually: {' '.join(command)}")
            return 1
        print("Update scheduled. Marcus will now exit so Windows can replace its files.")
        print("Close any other Marcus sessions; the updater will wait for them safely.")
        print(f"Progress log: {log_file}")
        return 0

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
    # Explicitly wire the event bus at the CLI boundary. The agent emits
    # one-way notifications through the bus; the terminal renderer forwards
    # them to the TerminalUI. This is the seam where a Textual/web renderer
    # would attach in the future.
    events = EventBus()
    events.subscribe(TerminalRenderer(ui).handle)
    loop = MarcusLoop(
        llm,
        tools,
        ui,
        events=events,
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
                    loop.state.active_process_ids.clear()
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
                    loop.state.active_process_ids.clear()
    finally:
        if hasattr(tools, "aclose"):
            await tools.aclose()
        await ctx.loop.llm.aclose()
        if hasattr(ui, "aclose"):
            await ui.aclose()


async def _amain_tui(mode: AgentMode | None) -> None:
    """Interactive TUI variant of ``_amain``.

    The Textual app owns the event loop, so instead of an explicit REPL we
    hand it a submit callback that dispatches slash commands or turns. The
    same ``MarcusLoop`` and ``EventBus`` are used — only the renderer and
    prompter change.
    """
    from marcus_code.ui.tui import MarcusTuiApp, TuiPrompter

    root = Path.cwd()
    settings = resolve_settings()
    if mode is None:
        try:
            mode = AgentMode(settings.cli_default_mode)
        except ValueError:
            mode = AgentMode.agent
    if not has_llm_credentials(settings):
        typer.echo(
            "No LLM credentials configured. Run `marcus` once in your terminal "
            "to complete first-time setup, then relaunch `marcus tui`."
        )
        return
    session_name = _new_session_name()

    llm = LLMGateway(settings=settings)
    tools = build_marcus_tools(root, settings)
    project_instructions = load_project_instructions(root)
    events = EventBus()

    # We need the app instance both for the prompter (approval modal) and
    # the renderer (log writes), so build a stub reference we can fill in
    # once the app has been created below.
    app_holder: dict[str, MarcusTuiApp] = {}

    class _AppProxy:
        """Thin proxy so ``TuiPrompter`` can receive ``self._app`` before
        the real ``MarcusTuiApp`` exists (chicken-and-egg: the loop needs
        the prompter to construct, the app needs the loop to submit to)."""

        def __getattr__(self, name: str):
            return getattr(app_holder["app"], name)

    prompter = TuiPrompter(_AppProxy())  # type: ignore[arg-type]

    loop = MarcusLoop(
        llm,
        tools,
        prompter,
        events=events,
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
    ctx = CommandContext(ui=prompter, loop=loop, settings=settings)

    async def _on_submit(text: str) -> None:
        stripped = text.strip()
        if not stripped:
            return
        try:
            if stripped.startswith("/"):
                await dispatch(ctx, stripped)
                return
            await loop.run_turn(text)
        finally:
            if hasattr(tools, "process_manager"):
                await tools.process_manager.aclose()
                loop.state.active_process_ids.clear()

    tui_app = MarcusTuiApp(
        events,
        on_submit=_on_submit,
        status_provider=lambda: loop.status(str(root)),
        session_name=session_name,
    )
    app_holder["app"] = tui_app
    try:
        await tui_app.run_async()
    finally:
        if hasattr(tools, "aclose"):
            await tools.aclose()
        await loop.llm.aclose()


app = typer.Typer(
    name="marcus",
    help="Marcus Code — an AI coding agent CLI.",
    no_args_is_help=False,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(_version_string())
        raise typer.Exit()


def _run_interactive(
    prompt: str | None, mode: AgentMode | None, *, no_color: bool
) -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_amain(prompt, mode, no_color=no_color))


@app.callback(invoke_without_command=True)
def _default(
    ctx: typer.Context,
    prompt: str | None = typer.Option(
        None, "-p", "--prompt", help="run one prompt non-interactively"
    ),
    mode: AgentMode | None = typer.Option(
        None, "--mode", case_sensitive=False, help="agent mode (ask, agent, auto, yolo)"
    ),
    no_color: bool = typer.Option(
        False, "--no-color", help="disable colored output; use ASCII progress bars"
    ),
    update: bool = typer.Option(
        False, "--update", help="update Marcus to the latest release"
    ),
    yes: bool = typer.Option(
        False, "-y", "--yes", help="skip confirmation prompts"
    ),
    show_version: bool | None = typer.Option(
        None,
        "-v",
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="show program's version and exit",
    ),
) -> None:
    """Default entry — runs the interactive REPL or the requested subcommand."""
    if _report_deferred_update_result():
        raise typer.Exit()
    if update:
        raise typer.Exit(_run_update(None, assume_yes=yes))
    if ctx.invoked_subcommand is not None:
        # A subcommand (e.g. `marcus update`) will handle the run itself;
        # stash shared flags so the subcommand can read them.
        ctx.obj = {"yes": yes}
        return
    _run_interactive(prompt, mode, no_color=no_color)


@app.command("update")
def _cmd_update(
    ctx: typer.Context,
    target: str | None = typer.Argument(
        None, help="specific version to install (defaults to latest release)"
    ),
    yes: bool = typer.Option(False, "-y", "--yes", help="skip confirmation prompts"),
) -> None:
    """Update Marcus to the latest release (or a specific version)."""
    inherited_yes = bool((ctx.obj or {}).get("yes"))
    version_arg = target.lstrip("v") if target else None
    raise typer.Exit(_run_update(version_arg, assume_yes=yes or inherited_yes))


@app.command("version")
def _cmd_version() -> None:
    """Show the installed Marcus version."""
    typer.echo(_version_string())


@app.command("tui")
def _cmd_tui(
    mode: AgentMode | None = typer.Option(
        None, "--mode", case_sensitive=False, help="agent mode (ask, agent, auto, yolo)"
    ),
) -> None:
    """Launch the Textual dashboard (``pip install 'marcus[tui]'``)."""
    try:
        from marcus_code.ui.tui import MarcusTuiApp, TuiPrompter
    except ImportError as exc:
        typer.echo(
            "Textual is not installed. Install the TUI extras with "
            "'uv tool install marcus[tui]' or 'pip install marcus[tui]'."
        )
        typer.echo(f"(missing dependency: {exc.name})")
        raise typer.Exit(1) from exc
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_amain_tui(mode))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
