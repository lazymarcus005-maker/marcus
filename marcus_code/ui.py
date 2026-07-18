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
    """Design tokens for TerminalUI — semantic colors + a small glyph
    vocabulary. All surfaces pull styling from here so switching themes
    (dark/light/high-contrast/no-color) stays visually consistent.
    """

    # Semantic colors
    info: str = "cyan"
    error: str = "red"
    success: str = "green"
    warning: str = "yellow"
    accent: str = "bold cyan"
    muted: str = "dim"
    prompt: str = "bold #5fd7ff"
    # Softer border for surrounding panels — the previous "cyan" competed
    # with content colors; grey37 recedes so eyes land on data first.
    border: str = "grey37"
    toolbar_bg: str = "#1f1f1f"
    toolbar_fg: str = "#c8c8c8"
    approval_prompt: str = "#F2B880"
    approval_always: str = "#F28B82"
    status_good: str = "green"
    status_warn: str = "yellow"
    status_caution: str = "#F2B880"
    status_bad: str = "#F28B82"

    # Glyph vocabulary — all UI ideograms come from here so no-color mode
    # falls back cleanly to ASCII without per-callsite branching.
    bar_fill: str = "█"
    bar_empty: str = "░"
    success_glyph: str = "✓"
    fail_glyph: str = "×"
    arrow: str = "▸"
    bullet: str = "•"
    active_dot: str = "●"
    pending_dot: str = "○"
    spinner: str = "⋯"
    sep: str = "·"
    rule: str = "─"

    @classmethod
    def dark(cls) -> "Theme":
        return cls()

    @classmethod
    def light(cls) -> "Theme":
        return cls(
            prompt="bold #0066cc",
            border="grey58",
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
            arrow=">",
            bullet="-",
            active_dot="*",
            pending_dot=".",
            spinner="...",
            sep="|",
            rule="-",
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
        self._working_lines: list[str] = []
        self._last_step_lines: list[str] = []
        self._tool_count = 0
        self._thinking: bool = False
        self._thinking_start: float = 0.0
        self._last_thought_seconds: float | None = None
        self._todo: TodoTracker | None = None
        self._last_guardrail: str | None = None
        self._stream_buffer: str = ""
        self._streaming: bool = False
        self._turn_active: bool = False
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
        logo_padded = Padding(logo, (1, 2))
        profile = profile_email or "(run /usage login)"

        # Single-column key/value block, aligned by longest label. Reads
        # top-to-bottom instead of the old two-column table that broke on
        # narrow terminals and forced awkward ljust(20) padding.
        rows: list[tuple[str, str]] = [
            ("Workspace", str(root)),
            ("Model", model),
            ("Provider", provider_url),
            ("Mode", mode),
            ("Profile", profile),
            ("Session", session_name),
        ]
        label_width = max(len(label) for label, _ in rows)
        info_lines = [
            f"[bold]{label.ljust(label_width)}[/bold]  {escape(value)}"
            for label, value in rows
        ]
        info = Text.from_markup(
            f"[{self.theme.muted}]AI-powered coding agent {self.theme.sep} "
            f"Harness Recipe via Marcus[/{self.theme.muted}]\n\n"
            + "\n".join(info_lines)
        )
        body = Group(logo_padded, info)
        panel_title = "Marcus Code"
        if marcus_version:
            panel_title = f"Marcus Code (V{marcus_version})"

        # Fit-then-nudge: measure natural width, add breathing room, and
        # never overflow the terminal. Prior implementation multiplied by
        # 1.1 which added padding proportional to logo width; a fixed
        # +4 gutter reads the same on wide/narrow terminals.
        measurement = Measurement.get(self.console, self.console.options, body)
        panel_width = min(measurement.maximum + 8, self.console.width)
        self.console.print(
            Panel(
                body,
                title=panel_title,
                title_align="left",
                border_style=self.theme.border,
                width=panel_width,
                padding=(0, 2),
            )
        )
        self.console.print(
            f"[{self.theme.muted}]  Type a task, or /help for commands.[/{self.theme.muted}]"
        )

    def print_help(self) -> None:
        arg_hints: dict[str, str] = {
            "/model": "[name]",
            "/usage": "[login|logout]",
            "/clear": "[--all]",
            "/mode": "[ask|agent|auto|yolo]",
            "/config": "[edit]",
            "/theme": "[dark|light|high-contrast|no-color]",
            "/save": "[path]",
        }
        categories = command_categories()

        # Rich Table with no visible borders — gives us adaptive column
        # widths and clean wrap behavior when the description overflows,
        # instead of the previous ljust approach that only worked when the
        # widest hint fit inside the terminal.
        table = Table.grid(padding=(0, 3), pad_edge=False)
        # Signature column stays on one line so each command keeps its own
        # row; the description column takes the remaining terminal width and
        # wraps with a hanging indent.
        table.add_column(justify="left", no_wrap=True)
        table.add_column(justify="left", overflow="fold", ratio=1)

        first_category = True
        for category, names in categories.items():
            if not first_category:
                table.add_row("", "")
            first_category = False
            table.add_row(
                Text.from_markup(f"[{self.theme.accent}]{category}[/{self.theme.accent}]"),
                "",
            )
            for name in names:
                hint = arg_hints.get(name, "")
                signature = f"{name} {hint}" if hint else name
                desc = command_description(name)
                table.add_row(
                    Text.from_markup(f"  {escape(signature)}"),
                    Text.from_markup(
                        f"[{self.theme.muted}]{escape(desc)}[/{self.theme.muted}]"
                    ),
                )

        blocks: list[Any] = [
            Text.from_markup(
                f"[bold]Slash commands[/bold] "
                f"[{self.theme.muted}]{self.theme.sep} "
                f"prefix any input with / to run a command[/{self.theme.muted}]"
            ),
            Text(""),
            table,
            Text(""),
            Text.from_markup(
                f"[{self.theme.muted}]Otherwise, type a task — Marcus will read, search, "
                f"and edit files in the working directory, asking before anything "
                f"risky.[/{self.theme.muted}]"
            ),
        ]
        self.console.print(Group(*blocks))

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
        rows = [
            ("Base URL", settings.llm_base_url),
            ("Model", settings.llm_model),
            ("API key", masked),
        ]
        label_width = max(len(label) for label, _ in rows)
        body = "\n".join(
            f"[bold]{label.ljust(label_width)}[/bold]  {escape(value)}"
            for label, value in rows
        )
        hint = (
            f"\n\n[{self.theme.muted}]Use /config edit to change these for this session "
            f"(saved to ~/.marcus/config.toml).[/{self.theme.muted}]"
        )
        self.console.print(
            Panel.fit(
                body + hint,
                title="Current config",
                title_align="left",
                border_style=self.theme.border,
                padding=(0, 2),
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
        rows = [
            ("LLM calls", str(stats.llm_calls)),
            ("Prompt tokens", f"{stats.prompt_tokens:,}"),
            ("Completion tokens", f"{stats.completion_tokens:,}"),
            ("Total tokens", f"{stats.total_tokens:,}"),
            ("Token budget", budget),
            ("Remaining tokens", remaining),
            ("LLM time", f"{stats.elapsed_seconds:.1f}s"),
            ("Throughput", f"{stats.tokens_per_second:.1f} tok/s"),
            ("Session time", f"{elapsed:.0f}s"),
        ]
        label_width = max(len(label) for label, _ in rows)
        body = "\n".join(
            f"[bold]{label.ljust(label_width)}[/bold]  {escape(value)}"
            for label, value in rows
        )
        self.console.print(
            Panel.fit(
                body,
                title="Usage",
                title_align="left",
                border_style=self.theme.border,
                padding=(0, 2),
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
                title_align="left",
                border_style=self.theme.border,
                padding=(0, 2),
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
        # Narrower bar (14 cells vs 20) — the numeric ratio next to it
        # already carries the precision, so the visual bar just has to say
        # "roughly how full."
        filled = round(ratio * 14)
        bar = self.theme.bar_fill * filled + self.theme.bar_empty * (14 - filled)
        sep = "  "
        # Two lines instead of three. Session + context + mode on top,
        # model + throughput + workspace below. Same information density,
        # fewer rows anchored to the prompt.
        line_one = (
            f"Session {hours:02d}:{minutes:02d}:{seconds:02d}{sep}"
            f"Context [{bar}] ~{_format_tokens(used)}/{_format_tokens(limit)}{sep}"
            f"Mode {status['mode']}"
        )
        line_two = (
            f"Model {status['model']}{sep}"
            f"Used {_format_tokens(status['total_tokens'])} tok{sep}"
            f"{status['tokens_per_second']:.1f} tok/s{sep}"
            f"Workspace {status['workspace']}"
        )
        return [
            (
                "class:bottom-toolbar.text",
                f"\n{line_one}\n{line_two}",
            )
        ]

    def print_recovery(self, message: str) -> None:
        glyph = "↻" if not self._no_color else "~"
        if self._turn_active:
            self._working_lines.append(f"{glyph} {self._compact_line(message, 180)}")
            self._refresh_steps()
            return
        self.console.print(
            f"[{self.theme.warning}]{glyph} {escape(message)}[/{self.theme.warning}]"
        )

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
        if self._turn_active:
            label = "plan" if text.lstrip().lower().startswith("plan") else "note"
            compact = self._compact_line(text, 180)
            self._working_lines.append(f"{self.theme.bullet} {label}: {compact}")
            self._step_lines.append(f"{label}: {text}")
            self._refresh_steps()
            return
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
        arrow = self.theme.arrow
        self._step_lines.extend([f"{arrow} {action}", f"  {tool_name}({summary})"])
        compact_args = self._compact_line(summary, 120)
        suffix = f" {self.theme.sep} {compact_args}" if compact_args else ""
        self._working_lines.append(f"{self._tool_count}. {action}{suffix}")
        self._refresh_steps()

    def print_tool_result(self, tool_name: str, result: dict) -> None:
        summary = _tool_result_summary(tool_name, result)
        glyph = self.theme.success_glyph
        self._step_lines.append(f"{glyph} {summary}")
        self._working_lines.append(f"   {glyph} {self._compact_line(summary, 160)}")
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
        glyph = self.theme.fail_glyph
        self._step_lines.append(f"{glyph} {tool_name}: {error}")
        self._working_lines.append(
            f"   {glyph} {tool_name}: {self._compact_line(error, 140)}"
        )
        self._refresh_steps()

    def print_tool_declined(self, tool_name: str) -> None:
        glyph = self.theme.fail_glyph
        self._step_lines.append(f"{glyph} {tool_name} declined by user")
        self._working_lines.append(f"   {glyph} {tool_name} declined")
        self._refresh_steps()

    def print_guardrail_stop(self, reason: str) -> None:
        self.finish_steps(success=False)
        self._last_guardrail = reason
        self._guardrail_collapsed = True
        glyph = self.theme.fail_glyph
        self.console.print(
            f"[{self.theme.error}]{glyph} guardrail {self.theme.sep} "
            f"{escape(reason)}[/{self.theme.error}]"
        )

    def print_interrupted(self) -> None:
        self.finish_steps(success=False)
        glyph = self.theme.fail_glyph
        self.console.print(
            f"[{self.theme.warning}]{glyph} interrupted {self.theme.sep} "
            f"back to prompt[/{self.theme.warning}]"
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
        self._working_lines = []
        self._tool_count = 0
        self._thinking = False
        self._thinking_start = 0.0
        self._last_thought_seconds = None
        self._todo = None
        self._stream_buffer = ""
        self._streaming = False
        self._turn_active = True

    def update_todo(self, todo: TodoTracker) -> None:
        """Update the workflow tracker rendered in the live panel."""
        self._todo = todo
        if self._turn_active:
            self._refresh_steps()

    def _todo_renderable(self) -> Text:
        """Phase pipeline as a subtle breadcrumb.

        Done phases render in success color; the current phase is
        accent-highlighted; upcoming phases stay muted. The separator uses
        the theme's ``sep`` glyph so it degrades gracefully in no-color mode.
        """
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
        sep = f" {self.theme.sep} "
        for index, phase in enumerate(order):
            label = labels.get(phase, phase.value)
            if self._todo.current == phase and not self._todo.finished:
                text.append(label, style=self.theme.accent)
            elif self._todo.is_done(phase):
                text.append(label, style=self.theme.success)
            else:
                text.append(label, style=self.theme.muted)
            if index < len(order) - 1:
                text.append(sep, style=self.theme.muted)
        return text

    def start_thinking(self) -> None:
        """Show thinking inside the single live working panel."""
        self._thinking = True
        self._thinking_start = datetime.now().timestamp()
        self._refresh_steps()

    def stop_thinking(self, elapsed_seconds: float) -> None:
        """Finalize the thinking indicator timing in the step panel."""
        if not self._thinking:
            return
        self._thinking = False
        self._last_thought_seconds = elapsed_seconds
        self._refresh_steps()

    def print_last_guardrail(self) -> None:
        """Show the last guardrail stop reason, if any."""
        if self._last_guardrail is None:
            self.print_info("No guardrail stop in this session yet.")
            return
        self.console.print(
            Panel(
                f"[{self.theme.error}]{escape(self._last_guardrail)}[/{self.theme.error}]",
                title=f"{self.theme.fail_glyph} Last guardrail stop",
                title_align="left",
                border_style=self.theme.error,
                padding=(0, 1),
            )
        )

    def start_stream(self) -> None:
        """Collect streamed text while keeping status inside the working panel."""
        self._stream_buffer = ""
        self._streaming = True
        self._refresh_steps()

    def stream_delta(self, text: str) -> None:
        """Accumulate a streamed delta for later rendering."""
        self._stream_buffer += text

    def end_stream(self) -> None:
        """Finish streaming; the loop renders the response exactly once."""
        self._stream_buffer = ""
        self._streaming = False
        self._refresh_steps()

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
        if not self._step_lines and not self._working_lines:
            # Still stop any active live panel so transient thinking lines don't
            # leak into the final output.
            self._stop_live()
            self._turn_active = False
            return
        self._last_step_lines = list(self._step_lines)
        self._stop_live()
        self._turn_active = False
        sep = self.theme.sep
        detail_hint = f" {sep} /steps" if self._last_step_lines else ""
        count = f" {sep} {self._tool_count} tool(s)" if self._tool_count else ""
        self._steps_collapsed = True
        if success:
            glyph = self.theme.success_glyph
            self.console.print(
                f"[{self.theme.success}]{glyph} done{count}[/{self.theme.success}]"
                f"[{self.theme.muted}]{detail_hint}[/{self.theme.muted}]"
            )
        else:
            glyph = self.theme.fail_glyph
            self.console.print(
                f"[{self.theme.error}]{glyph} stopped{count}[/{self.theme.error}]"
                f"[{self.theme.muted}]{detail_hint}[/{self.theme.muted}]"
            )
        self._step_lines = []
        self._working_lines = []
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
        step_prefixes = (
            f"{self.theme.arrow} ",
            f"{self.theme.success_glyph} ",
            f"{self.theme.fail_glyph} ",
        )
        self._tool_count = len(
            [line for line in lines if line.startswith(step_prefixes)]
        )
        self.console.print(self._steps_renderable(title="Last steps"))
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
            lines = [
                f"{self.theme.spinner} {hidden} earlier line(s) hidden; use /steps"
            ] + lines[-(max_lines - 1) :]
        panel_title = title or f"Steps {self.theme.sep} {self._tool_count} tool(s)"
        return Panel(
            "\n".join(escape(line) for line in lines),
            title=panel_title,
            title_align="left",
            border_style=self.theme.border,
            padding=(0, 1),
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
        sep = f" {self.theme.sep} "
        text = Text()
        text.append(f"Session {hours:02d}:{minutes:02d}:{seconds:02d}{sep}Context ")
        text.append(f"[{bar}]", style=color)
        text.append(f" ~{_format_tokens(used)}/{_format_tokens(limit)}{sep}Mode {status['mode']}\n")
        text.append(
            f"Model {status['model']}{sep}"
            f"Used {_format_tokens(status['total_tokens'])} tok{sep}"
            f"{status['tokens_per_second']:.1f} tok/s\n"
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

    def _working_renderable(self) -> Panel:
        lines = list(self._working_lines)
        if self._thinking:
            elapsed = datetime.now().timestamp() - self._thinking_start
            activity = "streaming" if self._streaming else "thinking"
            lines.append(f"{self.theme.spinner} {activity} {elapsed:.1f}s")
        elif self._streaming:
            lines.append(f"{self.theme.spinner} streaming")
        elif not lines:
            lines.append(f"{self.theme.spinner} preparing")

        # Extra reserved rows account for panel border, phase breadcrumb,
        # a blank spacer, the subtitle, and space for the prompt below.
        max_lines = max(3, self.console.size.height - 10)
        hidden = max(0, len(lines) - max_lines)
        if hidden:
            lines = [
                f"{self.theme.spinner} {hidden} earlier step(s) {self.theme.sep} /steps"
            ] + lines[-(max_lines - 1) :]

        step_body = Text.from_markup("\n".join(escape(line) for line in lines))
        if self._todo is not None:
            # Breadcrumb-then-rule-then-steps gives the eye a stable header
            # regardless of how much churn is happening in the step list.
            body: Group | Text = Group(self._todo_renderable(), Text(""), step_body)
        else:
            body = step_body

        count = f" {self.theme.sep} {self._tool_count} tool(s)" if self._tool_count else ""
        title = f"Marcus Code{count}"
        subtitle = self._working_status_line()
        return Panel(
            body,
            title=title,
            title_align="left",
            subtitle=subtitle or None,
            border_style=self.theme.border,
            padding=(0, 1),
        )

    def _working_status_line(self) -> str:
        if self._status_provider is None:
            thought = (
                f"thought {self._last_thought_seconds:.1f}s"
                if self._last_thought_seconds is not None
                else ""
            )
            return thought
        status = self._status_provider()
        used = status["context_tokens"]
        limit = max(1, status["context_limit"])
        context_percent = min(100, round(used * 100 / limit))
        parts = [
            str(status["model"]),
            f"ctx {context_percent}%",
            str(status["mode"]),
        ]
        if self._last_thought_seconds is not None:
            parts.append(f"thought {self._last_thought_seconds:.1f}s")
        return " · ".join(parts)

    @staticmethod
    def _compact_line(text: str, limit: int) -> str:
        compact = " ".join(str(text).split())
        if len(compact) <= limit:
            return compact
        return compact[: max(1, limit - 1)].rstrip() + "…"

    def _refresh_steps(self) -> None:
        if not self.console.is_terminal or not self._turn_active:
            return
        renderable = self._working_renderable()
        if self._live is not None:
            self._live.update(renderable, refresh=True)
            return
        self._live = Live(renderable, console=self.console, refresh_per_second=8, transient=True)
        self._live.start()

    def _pause_live(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def _resume_live(self) -> None:
        if self._turn_active:
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

        # Inline layout instead of a bordered panel: risk pill on the left,
        # tool name and arguments to the right, so the eye lands on what's
        # about to happen without decoding a boxed table first.
        risk_style, risk_glyph = self._risk_style(risk)
        header = (
            f"[{risk_style}]{risk_glyph} risk: {risk}[/{risk_style}]"
            f"  [bold]{escape(tool.name)}[/bold]"
        )
        if summary:
            header += f"  [{self.theme.muted}]{self.theme.sep} {escape(summary)}[/{self.theme.muted}]"
        self.console.print(header)
        if warning:
            self.console.print(
                f"  [{self.theme.warning}]{self.theme.arrow} warning: "
                f"{escape(warning)}[/{self.theme.warning}]"
            )

        if tool.name == EDIT_FILE_TOOL_NAME:
            self._print_edit_diff(arguments)

        while True:
            try:
                answer = self.console.input(self._approval_prompt).strip().lower()
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
            self.console.print(
                f"[{self.theme.muted}]please answer y, n, or a[/{self.theme.muted}]"
            )

    def _risk_style(self, risk: str) -> tuple[str, str]:
        """Colour + glyph for the risk pill on the approval header."""
        if risk == "high":
            return self.theme.error, self.theme.fail_glyph
        if risk == "medium":
            return self.theme.warning, self.theme.arrow
        return self.theme.success, self.theme.success_glyph

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
