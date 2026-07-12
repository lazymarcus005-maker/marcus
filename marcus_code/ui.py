import difflib
import math
import re
import sys
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

import orjson
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import NestedCompleter
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.styles import Style
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.measure import Measurement
from rich.padding import Padding
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from harness.config import Settings
from harness.runtime.tools import Tool
from marcus_code.banner import render_banner
from marcus_code.tools import EDIT_FILE_TOOL_NAME, RUN_CLI_TOOL_NAME, START_PROCESS_TOOL_NAME

if TYPE_CHECKING:
    from marcus_code.loop import UsageStats

ApprovalDecision = Literal["yes", "no", "always"]

_APPROVAL_PROMPT = (
    "[#F2B880]Allow this tool call? (y)es / (n)o /[/#F2B880] "
    "[#F28B82](a)lways for this session[/#F28B82]: "
)


class TerminalUI:
    """Presentation layer only — no approval state, no session state.

    The loop owns the "always allow this tool" set; this class just renders
    and collects one decision per call.
    """

    def __init__(self) -> None:
        if sys.platform == "win32":
            # Rich's legacy Windows console path writes through a fixed
            # codepage (cp1252 here) and mangles/crashes on anything outside
            # it — including quote characters models commonly generate.
            # Windows 10 1511+ and Windows Terminal both support ANSI, so
            # force the modern path and UTF-8 output instead of relying on
            # Rich's terminal auto-detection (which is unreliable when
            # stdout isn't a real console, e.g. piped/redirected).
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
        self.console = Console(legacy_windows=False)
        self.mode = "agent"
        self._step_lines: list[str] = []
        self._last_step_lines: list[str] = []
        self._tool_count = 0
        self._live: Live | None = None
        self._status_provider: Callable[[], dict[str, Any]] | None = None
        self._interrupt_count = 0
        self._input_history = InMemoryHistory()
        self._prompt_session: PromptSession[str] | None = None
        self._prompt_style = Style.from_dict(
            {
                "prompt": "bold #5fd7ff",
                # prompt_toolkit's default toolbar style is `reverse`, which
                # turns a dark-terminal toolbar white unless explicitly reset.
                "bottom-toolbar": "noreverse bg:#1f1f1f #c8c8c8",
                "bottom-toolbar.text": "noreverse bg:#1f1f1f #c8c8c8",
            }
        )
        self._completer = NestedCompleter.from_nested_dict(
            {
                "/help": None,
                "/?": None,
                "/model": None,
                "/usage": None,
                "/mode": {"ask": None, "agent": None, "auto": None, "yolo": None},
                "/status": None,
                "/steps": None,
                "/compact": None,
                "/clear": {"--all": None},
                "/config": {"show": None, "edit": None},
                "/exit": None,
                "/quit": None,
            }
        )

    def print_banner(self, root, *, model: str, session_name: str, mode: str = "agent") -> None:
        self.mode = mode
        # Text.from_ansi decodes raw ANSI SGR sequences into Rich's own
        # style representation without touching the "[...]" markup parser —
        # unlike passing the string through console.print()/f-strings, this
        # is safe to combine with markup content in the same panel (see
        # marcus_code/banner.py's module docstring for why that distinction
        # matters here).
        logo = Text.from_ansi(render_banner())
        # ~10px-equivalent margin around the logo art (terminal cells have no
        # true px, so 1 row / 2 columns is the closest analogue); the bottom
        # pad also does the job the old blank Text("") separator did.
        logo_padded = Padding(logo, (1, 2))
        info = Text.from_markup(
            "[dim]AI-powered coding agent   (Harness Recipe via Marcus)[/dim]\n\n"
            f"[bold]Workspace[/bold] : {escape(str(root))}\n"
            f"[bold]Model[/bold]     : {escape(model)}\n"
            f"[bold]Mode[/bold]      : {escape(mode)}\n"
            f"[bold]Session[/bold]   : {escape(session_name)}"
        )
        body = Group(logo_padded, info)

        # Panel.fit sizes exactly to content; measure that natural size and
        # widen the box by 10% on top of it per user request.
        measurement = Measurement.get(self.console, self.console.options, body)
        natural_panel_width = measurement.maximum + 4  # 1-char border + (0,1) padding, each side
        panel_width = min(round(natural_panel_width * 1.1), self.console.width)
        self.console.print(
            Panel(
                body,
                title="Marcus Code",
                title_align="left",
                border_style="cyan",
                width=panel_width,
            )
        )
        self.console.print("Type a task or /help for commands.")

    def print_help(self) -> None:
        self.console.print(
            "Commands:\n"
            "  /help              Show this help\n"
            "  /model [name]      Show or switch the active model for this session\n"
            "  /usage             Show token usage and timing for this session\n"
            "  /steps             Show details from the last completed task\n"
            "  /status            Show session, context, model, and workspace status\n"
            "  /compact           Compact retained conversation context now\n"
            "  /clear [--all]     Clear context; --all also clears approvals\n"
            "  /mode [name]       Show or switch mode (ask, agent, auto, yolo)\n"
            "  /config [edit]     View, or edit, the current LLM config\n"
            "  /exit, /quit       Quit Marcus Code\n"
            "\n"
            "Otherwise, just type what you want done — Marcus will read/search/edit "
            "files in the working directory and ask before anything risky."
        )

    def print_info(self, message: str) -> None:
        self.console.print(f"[cyan]{escape(message)}[/cyan]")

    def print_error(self, message: str) -> None:
        self.console.print(f"[red]{escape(message)}[/red]")

    def print_config(self, settings: Settings) -> None:
        key = settings.llm_api_key
        masked = f"{'*' * 8}{key[-4:]}" if len(key) > 4 else "(not set)"
        self.console.print(
            Panel.fit(
                f"[bold]Base URL[/bold] : {escape(settings.llm_base_url)}\n"
                f"[bold]Model[/bold]    : {escape(settings.llm_model)}\n"
                f"[bold]API key[/bold]  : {escape(masked)}\n\n"
                "[dim]Use /config edit to change these for this session "
                "(saved to ~/.marcus/config.toml).[/dim]",
                title="Current Config",
                border_style="cyan",
            )
        )

    def run_config_edit(
        self, *, current_base_url: str, current_model: str, has_existing_key: bool
    ) -> tuple[str | None, str, str] | None:
        """Prompt to edit LLM config, prefilled with current values. Returns
        (api_key_or_None, base_url, model); api_key is None when the user
        left it blank to keep the existing key. Returns None if cancelled.
        """
        self.console.print(
            Panel.fit(
                "Edit LLM configuration. Press Enter to keep the current value.",
                title="Edit Config",
                border_style="cyan",
            )
        )
        try:
            base_url = (
                self.console.input(f"LLM base URL (current: {escape(current_base_url)}): ").strip()
                or current_base_url
            )
            key_hint = "leave blank to keep current" if has_existing_key else "required"
            api_key = self.console.input(f"LLM API key ({key_hint}): ").strip()
            model = (
                self.console.input(f"LLM model (current: {escape(current_model)}): ").strip()
                or current_model
            )
        except (KeyboardInterrupt, EOFError):
            self.console.print("\n[yellow]edit cancelled[/yellow]")
            return None
        return (api_key or None, base_url, model)

    def print_usage(
        self,
        stats: "UsageStats",
        *,
        session_started_at: datetime,
        max_total_tokens: int | None = None,
    ) -> None:
        elapsed = (datetime.now() - session_started_at).total_seconds()
        budget = "unlimited" if max_total_tokens is None else f"{max_total_tokens:,}"
        remaining = (
            "unlimited"
            if max_total_tokens is None
            else f"{max(0, max_total_tokens - stats.total_tokens):,}"
        )
        self.console.print(
            Panel.fit(
                f"[bold]LLM calls[/bold]         : {stats.llm_calls}\n"
                f"[bold]Prompt tokens[/bold]     : {stats.prompt_tokens:,}\n"
                f"[bold]Completion tokens[/bold] : {stats.completion_tokens:,}\n"
                f"[bold]Total tokens[/bold]      : {stats.total_tokens:,}\n"
                f"[bold]Token budget[/bold]      : {budget}\n"
                f"[bold]Remaining tokens[/bold]  : {remaining}\n"
                f"[bold]LLM time[/bold]          : {stats.elapsed_seconds:.1f}s\n"
                f"[bold]Throughput[/bold]        : {stats.tokens_per_second:.1f} tok/s\n"
                f"[bold]Session time[/bold]      : {elapsed:.0f}s",
                title="Usage",
                border_style="cyan",
            )
        )

    def run_first_time_setup(
        self, *, default_base_url: str, default_model: str
    ) -> tuple[str, str, str] | None:
        """Prompt for LLM credentials on first run. Returns (api_key, base_url,
        model) to be saved to ~/.marcus/config.toml, or None if the user
        cancelled (Ctrl+C/EOF/blank key) — the caller proceeds without saving
        and lets the first LLM call fail with a clear error instead.
        """
        self.console.print(
            Panel.fit(
                "No LLM API key found.\n"
                "Let's set one up — saved to ~/.marcus/config.toml for future runs.\n"
                "Press Ctrl+C to skip and rely on env vars instead.",
                title="First-time setup",
                border_style="cyan",
            )
        )
        try:
            # Note: avoid literal square brackets in Console.input/print text
            # — Rich parses "[...]" as markup, so "[default]" silently
            # vanishes instead of printing (see the (y)/(n)/(a) prompt below).
            base_url = (
                self.console.input(f"LLM base URL (default: {escape(default_base_url)}): ").strip()
                or default_base_url
            )
            api_key = self.console.input("LLM API key: ").strip()
            if not api_key:
                self.console.print("[yellow]no API key entered — skipping setup[/yellow]")
                return None
            model = (
                self.console.input(f"LLM model (default: {escape(default_model)}): ").strip()
                or default_model
            )
        except (KeyboardInterrupt, EOFError):
            self.console.print("\n[yellow]setup cancelled[/yellow]")
            return None
        return api_key, base_url, model

    async def prompt_user(self) -> str | None:
        # Defensive lifecycle boundary: prompt_toolkit and Rich Live must
        # never own the terminal concurrently, even after an empty LLM reply.
        self._stop_live()
        try:
            if self._prompt_session is None:
                if not (sys.stdin.isatty() and sys.stdout.isatty()):
                    result = input(f"{self.mode.upper()} > ")
                    self._interrupt_count = 0
                    return result
                try:
                    self._prompt_session = PromptSession(history=self._input_history)
                except Exception as exc:  # noqa: BLE001 - terminal capability fallback
                    self.print_recovery(
                        f"Advanced prompt unavailable ({type(exc).__name__}); using basic input."
                    )
                    result = input(f"{self.mode.upper()} > ")
                    self._interrupt_count = 0
                    return result
            result = await self._prompt_session.prompt_async(
                [("class:prompt", f"{self.mode.upper()} > ")],
                completer=self._completer,
                complete_while_typing=False,
                bottom_toolbar=self._bottom_toolbar if self._status_provider else None,
                refresh_interval=1,
                style=self._prompt_style,
            )
            self._interrupt_count = 0
            return result
        except EOFError:
            return None
        except KeyboardInterrupt:
            return None if self.register_interrupt() else ""

    def _bottom_toolbar(self) -> list[tuple[str, str]]:
        if self._status_provider is None:
            return []
        status = self._status_provider()
        elapsed = (datetime.now() - status["session_started_at"]).total_seconds()
        hours, remainder = divmod(int(elapsed), 3600)
        minutes, seconds = divmod(remainder, 60)
        used = status["context_tokens"]
        limit = max(1, status["context_limit"])
        ratio = min(1.0, used / limit)
        filled = round(ratio * 20)
        bar = "█" * filled + "░" * (20 - filled)
        line_one = (
            f"Session {hours:02d}:{minutes:02d}:{seconds:02d} │ "
            f"Context [{bar}] ~{_format_tokens(used)}/{_format_tokens(limit)}"
        )
        line_two = (
            f"Model {status['model']} │ Used {_format_tokens(status['total_tokens'])} tok │ "
            f"{status['tokens_per_second']:.1f} tok/s │ Mode {status['mode']}"
        )
        line_three = f"Workspace {status['workspace']}"
        return [
            (
                "class:bottom-toolbar.text",
                f"\n{line_one}\n{line_two}\n{line_three}",
            )
        ]

    def print_recovery(self, message: str) -> None:
        self.console.print(f"[yellow]↻ {escape(message)}[/yellow]")

    async def aclose(self) -> None:
        self._stop_live()
        if self._prompt_session is not None:
            await self._prompt_session.app.cancel_and_wait_for_background_tasks()

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.refresh_status()

    def bind_status(self, provider: Callable[[], dict[str, Any]]) -> None:
        self._status_provider = provider

    def refresh_status(self) -> None:
        if self._live is not None:
            self._live.update(self._working_renderable(), refresh=True)

    def print_status(self) -> None:
        if self._status_provider is not None:
            self.console.print(self._status_renderable())

    def confirm_yolo_mode(self) -> bool:
        self.console.print(
            "[bold red]YOLO mode executes tools without approval.[/bold red] "
            "Hard safety guardrails remain active."
        )
        try:
            answer = self.console.input('Type "yolo" to confirm: ').strip().lower()
        except (KeyboardInterrupt, EOFError):
            return False
        return answer == "yolo"

    def print_assistant(self, text: str) -> None:
        self._pause_live()
        self.console.print(Markdown(text))
        self._resume_live()

    def print_final_answer(self, text: str) -> None:
        had_tool_steps = bool(self._step_lines)
        self.finish_steps(success=True)
        if not had_tool_steps:
            self.console.print(Markdown(text))
            return
        self.console.print()
        self.console.print("[bold cyan]ผลการทำงาน[/bold cyan]")
        self.console.print()
        self.console.print(Markdown(text))

    def print_assistant_delta(self, text: str) -> None:
        self.console.print(text, end="")

    def print_tool_call(self, tool_name: str, arguments: dict) -> None:
        summary = _summarize_arguments(arguments)
        action = _tool_action(tool_name)
        self._tool_count += 1
        self._step_lines.extend([f"→ {action}", f"  {tool_name}({summary})"])
        self._refresh_steps()

    def print_tool_result(self, tool_name: str, result: dict) -> None:
        summary = _tool_result_summary(tool_name, result)
        self._step_lines.append(f"✓ {summary}")
        for label in ("stdout", "stderr"):
            output = result.get(label)
            if not output:
                continue
            text = str(output).strip()
            if not text:
                continue
            self._step_lines.extend([label, text[:2000]])
        self._refresh_steps()

    def print_tool_error(self, tool_name: str, error: str) -> None:
        self._step_lines.append(f"x {tool_name}: {error}")
        self._refresh_steps()

    def print_tool_declined(self, tool_name: str) -> None:
        self._step_lines.append(f"x {tool_name} declined by user")
        self._refresh_steps()

    def print_guardrail_stop(self, reason: str) -> None:
        self.finish_steps(success=False)
        self.console.print(f"[red]stopped: {escape(reason)}[/red]")

    def print_interrupted(self) -> None:
        self.finish_steps(success=False)
        self.console.print("[yellow]interrupted - back to prompt[/yellow]")

    def register_interrupt(self) -> bool:
        """Return True on the third consecutive Ctrl+C, signaling CLI exit."""
        self.finish_steps(success=False)
        self._interrupt_count += 1
        remaining = 3 - self._interrupt_count
        if remaining <= 0:
            self.console.print("[yellow]Ctrl+C pressed 3 times - exiting Marcus.[/yellow]")
            return True
        self.console.print(
            f"[yellow]interrupted - press Ctrl+C {remaining} more "
            f"time{'s' if remaining != 1 else ''} to exit[/yellow]"
        )
        return False

    def begin_turn(self) -> None:
        self._stop_live()
        self._step_lines = []
        self._tool_count = 0

    def finish_steps(self, *, success: bool) -> None:
        if not self._step_lines:
            return
        self._last_step_lines = list(self._step_lines)
        self._stop_live()
        if success:
            self.console.print(
                f"[green]✓ งานสำเร็จ · {self._tool_count} ขั้นตอน[/green] "
                "[dim](ใช้ /steps เพื่อดูรายละเอียด)[/dim]"
            )
        else:
            self.console.print(self._steps_renderable())
        self._step_lines = []
        self._tool_count = 0

    def print_steps(self) -> None:
        lines = self._last_step_lines
        if not lines:
            self.print_info("No completed task steps in this session yet.")
            return
        self.console.print(Panel("\n".join(escape(line) for line in lines), title="Last task steps"))

    def _steps_renderable(self) -> Panel:
        lines: list[str] = []
        max_width = max(20, self.console.size.width - 8)
        for entry in self._step_lines:
            parts = entry.splitlines() or [""]
            lines.extend(part[:max_width] for part in parts)

        # Reserve room for the panel border, prompt, approval text, and final
        # answer. Showing the tail keeps the currently running action visible.
        max_lines = max(4, self.console.size.height - 10)
        hidden = max(0, len(lines) - max_lines)
        if hidden:
            lines = [f"… {hidden} earlier line(s) hidden; use /steps"] + lines[-(max_lines - 1) :]
        return Panel(
            "\n".join(escape(line) for line in lines),
            title=f"Working · {self._tool_count} step(s)",
            border_style="cyan",
        )

    def _status_renderable(self) -> Text:
        if self._status_provider is None:
            return Text("")
        status = self._status_provider()
        elapsed = (datetime.now() - status["session_started_at"]).total_seconds()
        hours, remainder = divmod(int(elapsed), 3600)
        minutes, seconds = divmod(remainder, 60)
        used = status["context_tokens"]
        limit = max(1, status["context_limit"])
        ratio = min(1.0, used / limit)
        filled = round(ratio * 20)
        bar = "█" * filled + "░" * (20 - filled)
        color = "green" if ratio < 0.7 else "yellow" if ratio < 0.85 else "#F2B880" if ratio < 0.95 else "#F28B82"
        text = Text()
        text.append(f"Session {hours:02d}:{minutes:02d}:{seconds:02d} │ Context ")
        text.append(f"[{bar}]", style=color)
        text.append(f" ~{_format_tokens(used)}/{_format_tokens(limit)}\n")
        text.append(
            f"Model {status['model']} │ Used {_format_tokens(status['total_tokens'])} tok │ "
            f"{status['tokens_per_second']:.1f} tok/s │ Mode {status['mode']}\n"
        )
        text.append(f"Workspace {status['workspace']}", style="dim")
        return text

    def _working_renderable(self) -> Group:
        return Group(self._steps_renderable(), self._status_renderable())

    def _refresh_steps(self) -> None:
        renderable = self._working_renderable()
        if self._live is None:
            self._live = Live(renderable, console=self.console, refresh_per_second=8, transient=True)
            self._live.start()
        else:
            self._live.update(renderable, refresh=True)

    def _pause_live(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def _resume_live(self) -> None:
        if self._step_lines:
            self._refresh_steps()

    def _stop_live(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def confirm_tool_call(self, tool: Tool, arguments: dict) -> ApprovalDecision:
        self._pause_live()
        if tool.name == EDIT_FILE_TOOL_NAME:
            self._print_edit_diff(arguments)
        if tool.name in {RUN_CLI_TOOL_NAME, START_PROCESS_TOOL_NAME}:
            warning = _command_warning(str(arguments.get("command", "")))
            if warning:
                self.console.print(f"[bold red]WARNING:[/bold red] {escape(warning)}")

        while True:
            # Note: avoid literal square brackets in text passed through
            # Console.input/print — Rich parses "[...]" as markup tags, so
            # e.g. "[y]es" silently vanishes instead of printing.
            answer = (
                self.console.input(_APPROVAL_PROMPT)
                .strip()
                .lower()
            )
            self._clear_approval_prompt(answer)
            if answer in ("y", "yes"):
                self._resume_live()
                return "yes"
            if answer in ("n", "no"):
                self._resume_live()
                return "no"
            if answer in ("a", "always"):
                self._resume_live()
                return "always"
            self.console.print("[dim]please answer y, n, or a[/dim]")

    def _clear_approval_prompt(self, answer: str) -> None:
        if not self.console.is_terminal:
            return
        plain_prompt = Text.from_markup(_APPROVAL_PROMPT).plain
        width = max(1, self.console.size.width)
        visual_lines = max(1, math.ceil((len(plain_prompt) + len(answer)) / width))
        for _ in range(visual_lines):
            self.console.file.write("\x1b[1A\x1b[2K")
        self.console.file.flush()

    def _print_edit_diff(self, arguments: dict) -> None:
        path = arguments.get("path", "<unknown>")
        old = arguments.get("old_string", "")
        new = arguments.get("new_string", "")
        diff = "\n".join(
            difflib.unified_diff(
                old.splitlines(), new.splitlines(), fromfile=path, tofile=path, lineterm=""
            )
        )
        if diff:
            self.console.print(Syntax(diff, "diff", theme="ansi_dark", background_color="default"))


def _summarize_arguments(arguments: dict, *, max_len: int = 120) -> str:
    parts = []
    for key, value in arguments.items():
        text = str(value)
        if len(text) > max_len:
            text = text[:max_len] + "..."
        parts.append(f"{key}={text!r}")
    return ", ".join(parts)


_TOOL_ACTIONS = {
    "list_files": "Inspect workspace files",
    "read_file": "Read file",
    "grep": "Search source code",
    "write_file": "Create file",
    "edit_file": "Update file",
    "run_cli": "Run command",
    "start_process": "Start background service",
    "wait_for_http": "Wait for HTTP service",
    "read_process_output": "Read service output",
    "stop_process": "Stop background service",
    "fetch_url": "Fetch URL",
    "load_skill": "Load skill instructions",
}


def _tool_action(tool_name: str) -> str:
    return _TOOL_ACTIONS.get(tool_name, f"Use {tool_name}")


def _tool_result_summary(tool_name: str, result: dict) -> str:
    if result.get("_truncated"):
        return f"Result captured (truncated from {result.get('_original_length', '?')} characters)"
    if tool_name == "list_files":
        return f"Found {len(result.get('files', []))} file(s)"
    if tool_name == "read_file":
        return f"Read {result.get('path', 'file')} ({result.get('lines', '?')} lines)"
    if tool_name == "grep":
        return f"Found {len(result.get('matches', []))} match(es)"
    if tool_name in {"write_file", "edit_file"}:
        return f"Updated {result.get('path', 'file')}"
    if tool_name == "run_cli":
        return f"Command finished with exit code {result.get('exit_code', '?')}"
    if tool_name == "start_process":
        return (
            f"Service {result.get('status', 'started')} "
            f"(process {result.get('process_id', '?')}, PID {result.get('pid', '?')})"
        )
    if tool_name == "wait_for_http":
        return f"HTTP service ready (status {result.get('status', '?')})"
    if tool_name == "read_process_output":
        return f"Service status: {result.get('status', 'unknown')}"
    if tool_name == "stop_process":
        return f"Service stopped (process {result.get('process_id', '?')})"
    if tool_name == "fetch_url":
        return f"Fetched URL (status {result.get('status', '?')})"
    return "Tool completed"


def _is_json(text: str) -> bool:
    try:
        orjson.loads(text)
    except orjson.JSONDecodeError:
        return False
    return True


def _format_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _command_warning(command: str) -> str | None:
    lowered = command.lower()
    if re.search(r"rm\s+(-\w*f|--force).*\s-r|rm\s+-r[f\s]|rmdir\s+/s", lowered):
        return "recursive deletion or forced removal"
    if re.search(r"git\s+push\b.*--force|git\s+reset\b.*--hard", lowered):
        return "irreversible or remote Git history rewrite"
    if re.search(r"\b(drop|truncate)\s+(table|database)\b|delete\s+from\b", lowered):
        return "destructive database operation"
    return None
