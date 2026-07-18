import difflib
import math
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import orjson
from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
from prompt_toolkit.completion import NestedCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.console import ColorSystem, Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.measure import Measurement
from rich.padding import Padding
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from harness.config import Settings
from harness.runtime.tools import Tool
from marcus_code.banner import render_banner
from marcus_code.command_info import all_commands, command_categories, command_description
from marcus_code.command_warnings import command_warning, risk_level
from marcus_code.config import USER_CONFIG_DIR
from marcus_code.todo_tracker import Phase, TodoTracker
from marcus_code.tools import EDIT_FILE_TOOL_NAME, RUN_CLI_TOOL_NAME, START_PROCESS_TOOL_NAME

if TYPE_CHECKING:
    from marcus_code.loop import UsageStats
    from marcus_code.ollama_usage import OllamaCloudUsage, UsagePeriod

_SLASH_COMMANDS: tuple[str, ...] = all_commands()


class SlashCommandAutoSuggest(AutoSuggest):
    """Auto-suggest slash commands as the user types.

    Only activates when the buffer starts with ``/``. Returns up to 7 matching
    commands, separated by ``  |  ``, so the user can see options without
    opening the completion menu.
    """

    _MAX_SUGGESTIONS = 7

    async def get_suggestion_async(self, buffer, document: Document) -> Suggestion | None:
        text = document.text
        if not text.startswith("/"):
            return None
        prefix = text.lower()
        matches = [cmd for cmd in _SLASH_COMMANDS if cmd.lower().startswith(prefix)]
        if not matches:
            # Fuzzy fallback for simple typos like /hepl -> /help
            matches = [
                cmd
                for cmd in _SLASH_COMMANDS
                if len(prefix) >= 2
                and sum(1 for ch in prefix if ch in cmd.lower()) >= len(prefix) - 1
            ][: self._MAX_SUGGESTIONS]
        if not matches:
            return None
        display = "  |  ".join(
            f"{cmd} — {command_description(cmd)}" if len(matches) <= 3 else cmd
            for cmd in matches[: self._MAX_SUGGESTIONS]
        )
        return Suggestion(display)

    def get_suggestion(self, buffer, document: Document) -> Suggestion | None:
        # get_suggestion_async is used by PromptSession's buffer; provide a
        # synchronous fallback in case it is ever called directly.
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.get_suggestion_async(buffer, document))
        if loop.is_running():
            return None
        return loop.run_until_complete(self.get_suggestion_async(buffer, document))


ApprovalDecision = Literal["yes", "no", "always"]

ThemeName = Literal["dark", "light", "high-contrast", "no-color"]


@dataclass
class Theme:
    """Named styles used by TerminalUI instead of hard-coded markup colors."""

    info: str = "cyan"
    error: str = "red"
    success: str = "green"
    warning: str = "yellow"
    accent: str = "bold cyan"
    muted: str = "dim"
    prompt: str = "bold #5fd7ff"
    border: str = "cyan"
    toolbar_bg: str = "#1f1f1f"
    toolbar_fg: str = "#c8c8c8"
    approval_prompt: str = "#F2B880"
    approval_always: str = "#F28B82"
    status_good: str = "green"
    status_warn: str = "yellow"
    status_caution: str = "#F2B880"
    status_bad: str = "#F28B82"
    bar_fill: str = "█"
    bar_empty: str = "░"
    success_glyph: str = "✓"
    fail_glyph: str = "x"

    @classmethod
    def dark(cls) -> "Theme":
        return cls()

    @classmethod
    def light(cls) -> "Theme":
        return cls(
            prompt="bold #0066cc",
            border="blue",
            toolbar_bg="#e8e8e8",
            toolbar_fg="#222222",
        )

    @classmethod
    def high_contrast(cls) -> "Theme":
        return cls(
            info="white on black",
            error="black on red",
            success="black on green",
            warning="black on yellow",
            accent="bold white on black",
            muted="dim white",
            prompt="bold white",
            border="white",
            toolbar_bg="#000000",
            toolbar_fg="#ffffff",
            status_good="green",
            status_warn="yellow",
            status_caution="orange3",
            status_bad="red",
        )

    @classmethod
    def no_color(cls) -> "Theme":
        theme = cls(
            info="default",
            error="default",
            success="default",
            warning="default",
            accent="default",
            muted="default",
            prompt="default",
            border="default",
            toolbar_bg="",
            toolbar_fg="",
            approval_prompt="default",
            approval_always="default",
            status_good="default",
            status_warn="default",
            status_caution="default",
            status_bad="default",
            bar_fill="#",
            bar_empty="-",
            success_glyph="*",
            fail_glyph="x",
        )
        return theme


DEFAULT_THEME = Theme.dark()


def _theme_from_env() -> ThemeName | None:
    """Return a theme name requested via NO_COLOR env var."""
    if os.environ.get("NO_COLOR"):
        return "no-color"
    return None


def _detect_no_color() -> bool:
    """True when colors should be disabled (NO_COLOR set)."""
    return bool(os.environ.get("NO_COLOR"))


def _history_path() -> Path:
    path = USER_CONFIG_DIR / "history"
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return path


class TerminalUI:
    """Presentation layer only — no approval state, no session state.

    The loop owns the "always allow this tool" set; this class just renders
    and collects one decision per call.
    """

    def __init__(self, *, no_color: bool = False) -> None:
        self._no_color = no_color or _detect_no_color()
        self.theme = Theme.no_color() if self._no_color else Theme.dark()
        if sys.platform == "win32":
            # Rich's legacy Windows console path writes through a fixed
            # codepage (cp1252 here) and mangles/crashes on anything outside
            # it — including quote characters models commonly generate.
            # Windows 10 1511+ and Windows Terminal both support ANSI, so
            # force the modern path and UTF-8 output instead of relying on
            # Rich's terminal auto-detection (which is unreliable when
            # stdout isn't a real console, e.g. piped/redirected).
            try:
                # TextIO doesn't expose reconfigure in the base protocol, but
                # the concrete CPython stream implementation has it on all
                # platforms we support.
                getattr(sys.stdout, "reconfigure", lambda **kwargs: None)(
                    encoding="utf-8", errors="replace"
                )
                getattr(sys.stderr, "reconfigure", lambda **kwargs: None)(
                    encoding="utf-8", errors="replace"
                )
            except (AttributeError, ValueError):
                pass
        self.console = Console(
            legacy_windows=False,
            color_system=None if self._no_color else "auto",
            no_color=self._no_color,
        )
        self.mode = "agent"
        self._step_lines: list[str] = []
        self._last_step_lines: list[str] = []
        self._tool_count = 0
        self._thinking: bool = False
        self._thinking_start: float = 0.0
        self._todo: TodoTracker | None = None
        self._last_guardrail: str | None = None
        self._stream_buffer: str = ""
        self._live: Live | None = None
        self._status_provider: Callable[[], dict[str, Any]] | None = None
        self._interrupt_count = 0
        self._input_history = FileHistory(str(_history_path()))
        self._prompt_session: PromptSession[str] | None = None
        self._steps_collapsed: bool = True
        self._guardrail_collapsed: bool = True
        self._build_prompt_style()
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
                "/retry": None,
                "/continue": None,
                "/clear": {"--all": None},
                "/config": {"show": None, "edit": None},
                "/theme": {"dark": None, "light": None, "high-contrast": None, "no-color": None},
                "/edit": None,
                "/last": None,
                "/save": None,
                "/exit": None,
                "/quit": None,
            }
        )

    def _build_prompt_style(self) -> None:
        toolbar_bg = self.theme.toolbar_bg or "#1f1f1f"
        toolbar_fg = self.theme.toolbar_fg or "#c8c8c8"
        self._prompt_style = Style.from_dict(
            {
                "prompt": self.theme.prompt,
                # prompt_toolkit's default toolbar style is `reverse`, which
                # turns a dark-terminal toolbar white unless explicitly reset.
                "bottom-toolbar": f"noreverse bg:{toolbar_bg} {toolbar_fg}",
                "bottom-toolbar.text": f"noreverse bg:{toolbar_bg} {toolbar_fg}",
            }
        )

    def set_theme(self, name: ThemeName) -> None:
        """Switch the active color theme and rebuild dependent UI state."""
        if name == "no-color":
            self.theme = Theme.no_color()
            self._no_color = True
            if hasattr(self, "console"):
                self.console._color_system = None
                self.console.no_color = True
        elif name == "light":
            self.theme = Theme.light()
            self._no_color = False
            if hasattr(self, "console"):
                self.console._color_system = ColorSystem.EIGHT_BIT
                self.console.no_color = False
        elif name == "high-contrast":
            self.theme = Theme.high_contrast()
            self._no_color = False
            if hasattr(self, "console"):
                self.console._color_system = ColorSystem.TRUECOLOR
                self.console.no_color = False
        else:
            self.theme = Theme.dark()
            self._no_color = False
            if hasattr(self, "console"):
                self.console._color_system = ColorSystem.EIGHT_BIT
                self.console.no_color = False
        if hasattr(self, "_build_prompt_style"):
            self._build_prompt_style()

    @property
    def _approval_prompt(self) -> str:
        return (
            f"[{self.theme.approval_prompt}]Allow this tool call? (y)es / (n)o /[/{self.theme.approval_prompt}] "
            f"[{self.theme.approval_always}](a)lways for this session[/{self.theme.approval_always}]: "
        )

    def print_banner(
        self,
        root,
        *,
        model: str,
        session_name: str,
        mode: str = "agent",
        provider_url: str = "",
        profile_email: str | None = None,
        marcus_version: str | None = None,
    ) -> None:
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
        profile = profile_email or "(run /usage login)"
        version_line = (
            f"[bold]Version[/bold] : {escape(marcus_version.ljust(20))} |  "
            if marcus_version
            else ""
        )
        info = Text.from_markup(
            f"[{self.theme.muted}]AI-powered coding agent   (Harness Recipe via Marcus)[/{self.theme.muted}]\n\n"
            f"[bold]Workspace[/bold] : {escape(str(root))}\n"
            f"[bold]Model[/bold]     : {escape(model.ljust(20))} |  "
            f"[bold]Provider[/bold] : {escape(provider_url)}\n"
            f"{version_line}"
            f"[bold]Mode[/bold]      : {escape(mode.ljust(20))} |  "
            f"[bold]Profile[/bold]  : {escape(profile)}\n"
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
                border_style=self.theme.border,
                width=panel_width,
            )
        )
        self.console.print("Type a task or /help for commands.")

    def print_help(self) -> None:
        table = Table(title="Slash Commands", border_style=self.theme.border, show_lines=True)
        table.add_column("Command", style=self.theme.accent, no_wrap=True)
        table.add_column("Arguments", style=self.theme.muted)
        table.add_column("Description", style="default")

        # Map simple commands to their category display order.
        for category, names in command_categories().items():
            table.add_row(f"[bold]{category}[/bold]", "", "", style=self.theme.muted)
            for name in names:
                desc = command_description(name)
                args = ""
                if name == "/model":
                    args = "[name]"
                elif name == "/usage":
                    args = "[login|logout]"
                elif name == "/clear":
                    args = "[--all]"
                elif name == "/mode":
                    args = "[ask|agent|auto|yolo]"
                elif name == "/config":
                    args = "[edit]"
                elif name == "/theme":
                    args = "[dark|light|high-contrast|no-color]"
                elif name == "/save":
                    args = "[path]"
                table.add_row(f"  {name}", args, desc)

        self.console.print(table)
        self.console.print(
            f"[{self.theme.muted}]Otherwise, just type what you want done — Marcus will "
            "read/search/edit files in the working directory and ask before anything risky.["
            + self.theme.muted
            + "]"
        )

    def print_info(self, message: str) -> None:
        self.console.print(f"[{self.theme.info}]{escape(message)}[/{self.theme.info}]")

    def print_error(self, message: str) -> None:
        self.console.print(f"[{self.theme.error}]{escape(message)}[/{self.theme.error}]")

    def clear_screen(self) -> None:
        """Clear the terminal screen (ANSI clear + home cursor)."""
        self.console.clear()

    def print_config(self, settings: Settings) -> None:
        key = settings.llm_api_key
        masked = f"{'*' * 8}{key[-4:]}" if len(key) > 4 else "(not set)"
        self.console.print(
            Panel.fit(
                f"[bold]Base URL[/bold] : {escape(settings.llm_base_url)}\n"
                f"[bold]Model[/bold]    : {escape(settings.llm_model)}\n"
                f"[bold]API key[/bold]  : {escape(masked)}\n\n"
                f"[{self.theme.muted}]Use /config edit to change these for this session "
                "(saved to ~/.marcus/config.toml).[/" + self.theme.muted + "]",
                title="Current Config",
                border_style=self.theme.border,
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
                border_style=self.theme.border,
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
            self.console.print(f"\n[{self.theme.warning}]edit cancelled[/{self.theme.warning}]")
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
                border_style=self.theme.border,
            )
        )

    def print_ollama_cloud_usage(self, usage: "OllamaCloudUsage") -> None:
        self.console.print(
            Panel.fit(
                "\n".join(
                    (
                        _format_cloud_usage_line("Session", usage.session, theme=self.theme),
                        _format_cloud_usage_line("Weekly", usage.weekly, theme=self.theme),
                    )
                ),
                title="Ollama Cloud Usage",
                border_style=self.theme.border,
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
                border_style=self.theme.border,
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
                self.console.print(
                    f"[{self.theme.warning}]no API key entered — skipping setup[/{self.theme.warning}]"
                )
                return None
            model = (
                self.console.input(f"LLM model (default: {escape(default_model)}): ").strip()
                or default_model
            )
        except (KeyboardInterrupt, EOFError):
            self.console.print(f"\n[{self.theme.warning}]setup cancelled[/{self.theme.warning}]")
            return None
        return api_key, base_url, model

    def prompt_multiline(self) -> str | None:
        """Collect multi-line input from the user until a blank line is entered.

        Returns the joined text, or None if the user cancelled with Ctrl+C.
        """
        self.console.print(
            "Entering multi-line mode. Type a blank line to finish, Ctrl+C to cancel."
        )
        lines: list[str] = []
        try:
            while True:
                line = self.console.input("")
                if line == "":
                    break
                lines.append(line)
        except (KeyboardInterrupt, EOFError):
            self.console.print(
                f"[{self.theme.warning}]multi-line input cancelled[/{self.theme.warning}]"
            )
            return None
        return "\n".join(lines)

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
                auto_suggest=SlashCommandAutoSuggest(),
                bottom_toolbar=self._bottom_toolbar if self._status_provider else None,
                refresh_interval=1,
                style=self._prompt_style,
                multiline=False,
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
        bar = self.theme.bar_fill * filled + self.theme.bar_empty * (20 - filled)
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
        self.console.print(f"[{self.theme.warning}]↻ {escape(message)}[/{self.theme.warning}]")

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
            f"[bold {self.theme.error}]YOLO mode executes tools without approval.[/bold {self.theme.error}] "
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
        self._last_guardrail = reason
        self._guardrail_collapsed = True
        self.console.print(
            f"[{self.theme.error}]↳ guardrail stop: {escape(reason)}[/{self.theme.error}] "
            f"[{self.theme.muted}](Ctrl+E expand · Ctrl+R collapse)[/{self.theme.muted}]"
        )

    def print_interrupted(self) -> None:
        self.finish_steps(success=False)
        self.console.print(
            f"[{self.theme.warning}]interrupted - back to prompt[/{self.theme.warning}]"
        )

    def register_interrupt(self) -> bool:
        """Return True on the third consecutive Ctrl+C, signaling CLI exit."""
        self.finish_steps(success=False)
        self._interrupt_count += 1
        remaining = 3 - self._interrupt_count
        if remaining <= 0:
            self.console.print(
                f"[{self.theme.warning}]Ctrl+C pressed 3 times - exiting Marcus.[/{self.theme.warning}]"
            )
            return True
        self.console.print(
            f"[{self.theme.warning}]interrupted - press Ctrl+C {remaining} more "
            f"time{'s' if remaining != 1 else ''} to exit[/{self.theme.warning}]"
        )
        return False

    def begin_turn(self) -> None:
        self._stop_live()
        self._step_lines = []
        self._tool_count = 0
        self._thinking = False
        self._thinking_start = 0.0
        self._todo = None
        self._stream_buffer = ""

    def update_todo(self, todo: TodoTracker) -> None:
        """Update the workflow tracker rendered in the live panel."""
        self._todo = todo
        self.refresh_status()

    def _todo_renderable(self) -> Text:
        """Render the current workflow phase pipeline."""
        text = Text()
        if self._todo is None:
            return text
        order = list(Phase)
        labels = {
            Phase.receive: "รับคำสั่ง",
            Phase.analyze: "วิเคราะห์",
            Phase.plan: "วางแผน",
            Phase.implement: "ดำเนินการ",
            Phase.validate: "ตรวจสอบ",
            Phase.deliver: "ส่งมอบ",
        }
        for index, phase in enumerate(order):
            label = labels.get(phase, phase.value)
            if self._todo.is_done(phase):
                if self._todo.current == phase and not self._todo.finished:
                    text.append(f"[{label}]", style=f"bold {self.theme.accent}")
                else:
                    text.append(f"[{label}]", style=f"{self.theme.muted}")
            else:
                text.append(f"[{label}]", style=self.theme.muted)
            if index < len(order) - 1:
                text.append(" → ", style=self.theme.muted)
        return text

    def start_thinking(self) -> None:
        """Show a live 'think...' indicator while the LLM is working."""
        self._thinking = True
        self._thinking_start = datetime.now().timestamp()
        if self.console.is_terminal and self._step_lines:
            self._refresh_steps()
        elif self.console.is_terminal:
            # No steps yet; just print a transient thinking line that will be
            # replaced by the working box once the first tool call arrives.
            self.console.print(f"[{self.theme.muted}]think...[/{self.theme.muted}]", end="\r")
            self.console.file.flush()
        else:
            self.console.print(f"[{self.theme.muted}]think...[/{self.theme.muted}]", end="")
            self.console.file.flush()

    def stop_thinking(self, elapsed_seconds: float) -> None:
        """Finalize the thinking indicator timing in the step panel."""
        if not self._thinking:
            return
        self._thinking = False
        if self.console.is_terminal:
            self._step_lines.append(f"💭 thought: {elapsed_seconds:.2f}s")
            self._refresh_steps()
        else:
            self.console.print(
                f"\r\x1b[K[{self.theme.muted}]thought: {elapsed_seconds:.2f}s[/{self.theme.muted}]"
            )
            self.console.file.flush()

    def print_last_guardrail(self) -> None:
        """Show the last guardrail stop reason, if any."""
        if self._last_guardrail is None:
            self.print_info("No guardrail stop in this session yet.")
            return
        self.console.print(
            Panel(
                f"[{self.theme.error}]{escape(self._last_guardrail)}[/{self.theme.error}]",
                title="Last Guardrail Stop",
                border_style=self.theme.error,
            )
        )

    def start_stream(self) -> None:
        """Pause live panel and prepare to collect streamed assistant text."""
        self._pause_live()
        self._stream_buffer = ""
        self.console.print(f"[{self.theme.muted}]streaming...[/]", end="")
        self.console.file.flush()

    def stream_delta(self, text: str) -> None:
        """Accumulate a streamed delta for later rendering."""
        self._stream_buffer += text

    def end_stream(self) -> None:
        """Erase the streaming placeholder and render the complete Markdown."""
        # Clear the "streaming..." line using ANSI escape codes.
        self.console.print("\r\x1b[K", end="")
        self.console.file.flush()
        content = self._stream_buffer
        self._stream_buffer = ""
        if content:
            self.console.print(Markdown(content))
        self._resume_live()

    def save_turn(
        self,
        path: Path,
        *,
        user_input: str | None = None,
        final_answer: str = "",
        usage: "UsageStats | None" = None,
        guardrail: str | None = None,
    ) -> None:
        """Write the last turn's details to a Markdown file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Marcus Turn Summary\n",
            f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
        ]
        if user_input:
            lines.extend(["\n## User Request\n", f"```text\n{user_input}\n```\n"])
        if self._last_step_lines:
            lines.extend(
                ["\n## Steps\n"] + [f"- {line}" for line in self._last_step_lines] + ["\n"]
            )
        if final_answer:
            lines.extend(["\n## Final Answer\n", f"{final_answer}\n"])
        if guardrail:
            lines.extend(["\n## Guardrail Stop\n", f"```text\n{guardrail}\n```\n"])
        if usage:
            lines.extend(
                [
                    "\n## Usage\n",
                    f"- LLM calls: {usage.llm_calls}\n",
                    f"- Tokens: {usage.total_tokens:,}\n",
                    f"- Time: {usage.elapsed_seconds:.1f}s\n",
                ]
            )
        path.write_text("".join(lines), encoding="utf-8")

    def finish_steps(self, *, success: bool) -> None:
        if not self._step_lines:
            # Still stop any active live panel so transient thinking lines don't
            # leak into the final output.
            self._stop_live()
            return
        self._last_step_lines = list(self._step_lines)
        self._stop_live()
        if success:
            self._steps_collapsed = True
            self.console.print(
                f"[{self.theme.info}]↳ worked {self._tool_count}x step complete[/{self.theme.info}] "
                f"[{self.theme.muted}](Ctrl+E expand · Ctrl+R collapse)[/{self.theme.muted}]"
            )
        else:
            self._steps_collapsed = True
            self.console.print(
                f"[{self.theme.error}]↳ stopped after {self._tool_count}x step[/{self.theme.error}] "
                f"[{self.theme.muted}](Ctrl+E expand · Ctrl+R collapse)[/{self.theme.muted}]"
            )
        self._step_lines = []
        self._tool_count = 0

    def print_steps(self) -> None:
        lines = self._last_step_lines
        if not lines:
            self.print_info("No completed task steps in this session yet.")
            return
        self._steps_collapsed = False
        # Temporarily swap in the saved lines so _steps_renderable can reuse its
        # truncation-aware layout logic, then restore the running state.
        saved_step_lines = list(self._step_lines)
        saved_tool_count = self._tool_count
        self._step_lines = list(lines)
        self._tool_count = len([line for line in lines if line.startswith(("→ ", "✓ ", "x "))])
        self.console.print(
            self._steps_renderable(title="Last task steps · Ctrl+R to collapse")
        )
        self.console.print(
            f"[{self.theme.muted}]↳ Ctrl+R collapse[/{self.theme.muted}]"
        )
        self._step_lines = saved_step_lines
        self._tool_count = saved_tool_count

    def _steps_renderable(self, *, title: str | None = None) -> Panel:
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
        panel_title = title or f"Working · {self._tool_count} step(s) · Ctrl+R collapse"
        return Panel(
            "\n".join(escape(line) for line in lines),
            title=panel_title,
            border_style=self.theme.border,
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
        bar = self.theme.bar_fill * filled + self.theme.bar_empty * (20 - filled)
        color = self._status_color(ratio)
        text = Text()
        text.append(f"Session {hours:02d}:{minutes:02d}:{seconds:02d} │ Context ")
        text.append(f"[{bar}]", style=color)
        text.append(f" ~{_format_tokens(used)}/{_format_tokens(limit)}\n")
        text.append(
            f"Model {status['model']} │ Used {_format_tokens(status['total_tokens'])} tok │ "
            f"{status['tokens_per_second']:.1f} tok/s │ Mode {status['mode']}\n"
        )
        text.append(f"Workspace {status['workspace']}", style=self.theme.muted)
        return text

    def _status_color(self, ratio: float) -> str:
        if ratio < 0.7:
            return self.theme.status_good
        if ratio < 0.85:
            return self.theme.status_warn
        if ratio < 0.95:
            return self.theme.status_caution
        return self.theme.status_bad

    def _working_renderable(self) -> Group:
        parts: list[Any] = []
        if self._thinking:
            elapsed = datetime.now().timestamp() - self._thinking_start
            parts.append(Text(f"💭 thinking... {elapsed:.1f}s", style=self.theme.muted))
        if self._todo is not None:
            parts.append(self._todo_renderable())
        if self._step_lines:
            parts.append(self._steps_renderable())
        parts.append(self._status_renderable())
        return Group(*parts)

    def _refresh_steps(self) -> None:
        renderable = self._working_renderable()
        # Ensure we never stack multiple Live instances on top of each other.
        if self._live is not None:
            self._live.stop()
            self._live = None
        self._live = Live(
            renderable, console=self.console, refresh_per_second=8, transient=True
        )
        self._live.start()

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
        summary = _summarize_arguments(arguments)
        risk = "low"
        warning: str | None = None
        if tool.name in {RUN_CLI_TOOL_NAME, START_PROCESS_TOOL_NAME}:
            command = str(arguments.get("command", ""))
            risk = risk_level(command)
            warning = command_warning(command)

        content_lines = [
            f"Tool: {tool.name}",
            f"Risk: {risk}",
        ]
        if summary:
            content_lines.append(f"Arguments: {summary}")
        if warning:
            content_lines.append(f"Warning: {warning}")

        border = (
            self.theme.error
            if risk == "high"
            else self.theme.warning
            if risk == "medium"
            else self.theme.border
        )
        self.console.print(
            Panel(
                "\n".join(content_lines),
                title="Allow this tool call? (y)es / (n)o / (a)lways for this session",
                border_style=border,
            )
        )

        if tool.name == EDIT_FILE_TOOL_NAME:
            self._print_edit_diff(arguments)

        while True:
            try:
                answer = self.console.input("Answer: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                self._resume_live()
                return "no"
            if answer in ("y", "yes"):
                self._resume_live()
                return "yes"
            if answer in ("n", "no"):
                self._resume_live()
                return "no"
            if answer in ("a", "always"):
                self._resume_live()
                return "always"
            self.console.print(f"[{self.theme.muted}]please answer y, n, or a[/{self.theme.muted}]")

    def _clear_approval_prompt(self, answer: str) -> None:
        if not self.console.is_terminal:
            return
        plain_prompt = Text.from_markup(self._approval_prompt).plain
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


def _format_cloud_usage_line(label: str, period: "UsagePeriod | None", *, theme: Theme) -> str:
    if period is None or period.percent is None:
        return f"{label:<7} [N/A]"
    percent = min(100.0, max(0.0, period.percent))
    width = 30
    filled = round(percent * width / 100)
    bar = theme.bar_fill * filled + theme.bar_empty * (width - filled)
    reset = f"  reset {period.resets_in}" if period.resets_in else ""
    return f"{label:<7} [{bar}] {percent:g}%{reset}"
