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
from rich.markdown import Markdown
from rich.markup import escape
from rich.measure import Measurement
from rich.padding import Padding
from rich.panel import Panel
from rich.status import Status
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from harness.config import Settings
from harness.runtime.tools import Tool
from marcus_code.ui.banner import render_banner
from marcus_code.ui.command_info import all_commands, command_categories, command_description
from marcus_code.ui.warnings import command_warning, risk_level
from marcus_code.state.config import USER_CONFIG_DIR
from marcus_code.runtime.todo_tracker import Phase, TodoTracker
from marcus_code.tools.base import EDIT_FILE_TOOL_NAME, RUN_CLI_TOOL_NAME, START_PROCESS_TOOL_NAME

if TYPE_CHECKING:
    from marcus_code.runtime.agent import UsageStats
    from marcus_code.runtime.ollama_usage import OllamaCloudUsage, UsagePeriod

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
    """Append-only terminal renderer.

    All chat surfaces (tool calls, results, thinking, guardrails, final
    answer) print inline once, in the order they happen — no live-refreshing
    panel, no boxes. Scrollback and copy/paste behave like a normal shell,
    which matches Claude Code / Codex CLI style.

    The banner (one-shot at startup) and the prompt_toolkit bottom toolbar
    (persistent status line, not a panel) are the only remaining
    non-linear surfaces.
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
        self._tool_count = 0
        self._thinking: bool = False
        self._thinking_start: float = 0.0
        self._thinking_status: Status | None = None
        self._last_thought_seconds: float | None = None
        self._todo: TodoTracker | None = None
        self._last_phase: Phase | None = None
        self._last_guardrail: str | None = None
        self._streaming: bool = False
        self._turn_active: bool = False
        self._last_turn_lines: list[str] = []
        self._status_provider: Callable[[], dict[str, Any]] | None = None
        self._interrupt_count = 0
        self._input_history = FileHistory(str(_history_path()))
        self._prompt_session: PromptSession[str] | None = None
        self._build_prompt_style()
        self._completer = NestedCompleter.from_nested_dict(
            {
                "/help": None,
                "/?": None,
                "/model": None,
                "/usage": None,
                "/mode": {"ask": None, "agent": None, "auto": None, "yolo": None},
                "/status": None,
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

        measurement = Measurement.get(self.console, self.console.options, body)
        panel_width = min(measurement.maximum + 8, self.console.width)
        # Banner is one-shot at startup, not a live surface — keeping the
        # panel here doesn't interfere with scrollback and gives the app a
        # recognizable header.
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

    def _print_heading(self, text: str) -> None:
        self.console.print(f"[{self.theme.accent}]{escape(text)}[/{self.theme.accent}]")

    def _print_kv_block(self, rows: list[tuple[str, str]]) -> None:
        if not rows:
            return
        label_width = max(len(label) for label, _ in rows)
        for label, value in rows:
            self.console.print(
                f"[bold]{label.ljust(label_width)}[/bold]  {escape(value)}"
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

        table = Table.grid(padding=(0, 3), pad_edge=False)
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

        self.console.print(
            Text.from_markup(
                f"[bold]Slash commands[/bold] "
                f"[{self.theme.muted}]{self.theme.sep} "
                f"prefix any input with / to run a command[/{self.theme.muted}]"
            )
        )
        self.console.print()
        self.console.print(table)
        self.console.print()
        self.console.print(
            Text.from_markup(
                f"[{self.theme.muted}]Otherwise, type a task — Marcus will read, search, "
                f"and edit files in the working directory, asking before anything "
                f"risky.[/{self.theme.muted}]"
            )
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
        self._print_heading("Current config")
        self._print_kv_block(
            [
                ("Base URL", settings.llm_base_url),
                ("Model", settings.llm_model),
                ("API key", masked),
            ]
        )
        self.console.print(
            f"[{self.theme.muted}]Use /config edit to change these for this session "
            f"(saved to ~/.marcus/config.toml).[/{self.theme.muted}]"
        )

    def run_config_edit(
        self, *, current_base_url: str, current_model: str, has_existing_key: bool
    ) -> tuple[str | None, str, str] | None:
        """Prompt to edit LLM config, prefilled with current values. Returns
        (api_key_or_None, base_url, model); api_key is None when the user
        left it blank to keep the existing key. Returns None if cancelled.
        """
        self._print_heading("Edit config")
        self.console.print(
            f"[{self.theme.muted}]Press Enter to keep the current value.[/{self.theme.muted}]"
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
        self._print_heading("Usage")
        self._print_kv_block(
            [
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
        )

    def print_ollama_cloud_usage(self, usage: "OllamaCloudUsage") -> None:
        self._print_heading("Ollama Cloud Usage")
        self.console.print(_format_cloud_usage_line("Session", usage.session, theme=self.theme))
        self.console.print(_format_cloud_usage_line("Weekly", usage.weekly, theme=self.theme))

    def run_first_time_setup(
        self, *, default_base_url: str, default_model: str
    ) -> tuple[str, str, str] | None:
        """Prompt for LLM credentials on first run. Returns (api_key, base_url,
        model) to be saved to ~/.marcus/config.toml, or None if the user
        cancelled (Ctrl+C/EOF/blank key) — the caller proceeds without saving
        and lets the first LLM call fail with a clear error instead.
        """
        self._print_heading("First-time setup")
        self.console.print(
            f"[{self.theme.muted}]No LLM API key found. Let's set one up — "
            f"saved to ~/.marcus/config.toml for future runs. "
            f"Press Ctrl+C to skip and rely on env vars instead.[/{self.theme.muted}]"
        )
        try:
            # Avoid literal square brackets in Console.input/print text —
            # Rich parses "[...]" as markup, so "[default]" silently vanishes.
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
        if self._prompt_session is None:
            if not (sys.stdin.isatty() and sys.stdout.isatty()):
                try:
                    result = input(f"{self.mode.upper()} > ")
                except EOFError:
                    return None
                except KeyboardInterrupt:
                    return None if self.register_interrupt() else ""
                self._interrupt_count = 0
                return result
            try:
                self._prompt_session = PromptSession(history=self._input_history)
            except Exception as exc:  # noqa: BLE001 - terminal capability fallback
                self.print_recovery(
                    f"Advanced prompt unavailable ({type(exc).__name__}); using basic input."
                )
                try:
                    result = input(f"{self.mode.upper()} > ")
                except EOFError:
                    return None
                except KeyboardInterrupt:
                    return None if self.register_interrupt() else ""
                self._interrupt_count = 0
                return result
        try:
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
        filled = round(ratio * 14)
        bar = self.theme.bar_fill * filled + self.theme.bar_empty * (14 - filled)
        sep = "  "
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
        self._stop_thinking_status()
        line = f"[{self.theme.warning}]{glyph} {escape(message)}[/{self.theme.warning}]"
        self.console.print(line)
        if self._turn_active:
            self._last_turn_lines.append(f"{glyph} {message}")

    async def aclose(self) -> None:
        self._stop_thinking_status()
        if self._prompt_session is not None:
            await self._prompt_session.app.cancel_and_wait_for_background_tasks()

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.refresh_status()

    def bind_status(self, provider: Callable[[], dict[str, Any]]) -> None:
        self._status_provider = provider

    def refresh_status(self) -> None:
        # Status is now shown only in the prompt_toolkit bottom toolbar
        # (refreshed by prompt_toolkit itself) and via /status. Nothing to
        # push during a turn — this stays as a no-op so callers in the loop
        # don't need to feature-detect.
        return

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
        self._stop_thinking_status()
        self.console.print(Markdown(text))
        if self._turn_active:
            self._last_turn_lines.append(text)

    def print_final_answer(self, text: str) -> None:
        had_tool_steps = self._tool_count > 0
        self.finish_steps(success=True)
        if had_tool_steps:
            self.console.print()
        self.console.print(Markdown(text))
        if self._turn_active:
            self._last_turn_lines.append(text)

    def print_assistant_delta(self, text: str) -> None:
        self.console.print(text, end="")

    def print_tool_call(self, tool_name: str, arguments: dict) -> None:
        self._stop_thinking_status()
        summary = _summarize_arguments(arguments)
        action = _tool_action(tool_name)
        self._tool_count += 1
        compact_args = self._compact_line(summary, 140)
        suffix = f" [{self.theme.muted}]{self.theme.sep} {escape(compact_args)}[/{self.theme.muted}]" if compact_args else ""
        line = (
            f"[{self.theme.accent}]{self.theme.arrow} {self._tool_count}. "
            f"{escape(action)}[/{self.theme.accent}]{suffix}"
        )
        self.console.print(line)
        if self._turn_active:
            self._last_turn_lines.append(f"{self.theme.arrow} {self._tool_count}. {action}")
            if compact_args:
                self._last_turn_lines.append(f"    {tool_name}({summary})")

    def print_tool_result(self, tool_name: str, result: dict) -> None:
        self._stop_thinking_status()
        summary = _tool_result_summary(tool_name, result)
        compact = self._compact_line(summary, 200)
        self.console.print(
            f"   [{self.theme.success}]{self.theme.success_glyph}[/{self.theme.success}] "
            f"{escape(compact)}"
        )
        if self._turn_active:
            self._last_turn_lines.append(f"  {self.theme.success_glyph} {summary}")
        for label in ("stdout", "stderr"):
            output = result.get(label)
            if not output:
                continue
            text = str(output).strip()
            if not text:
                continue
            if self._turn_active:
                self._last_turn_lines.append(f"  {label}:")
                self._last_turn_lines.append(text[:2000])

    def print_tool_error(self, tool_name: str, error: str) -> None:
        self._stop_thinking_status()
        compact = self._compact_line(error, 200)
        self.console.print(
            f"   [{self.theme.error}]{self.theme.fail_glyph}[/{self.theme.error}] "
            f"{escape(tool_name)}: {escape(compact)}"
        )
        if self._turn_active:
            self._last_turn_lines.append(f"  {self.theme.fail_glyph} {tool_name}: {error}")

    def print_tool_declined(self, tool_name: str) -> None:
        self._stop_thinking_status()
        self.console.print(
            f"   [{self.theme.muted}]{self.theme.fail_glyph} "
            f"{escape(tool_name)} declined[/{self.theme.muted}]"
        )
        if self._turn_active:
            self._last_turn_lines.append(f"  {self.theme.fail_glyph} {tool_name} declined")

    def print_guardrail_stop(self, reason: str) -> None:
        self._stop_thinking_status()
        self.finish_steps(success=False)
        self._last_guardrail = reason
        glyph = self.theme.fail_glyph
        self.console.print(
            f"[{self.theme.error}]{glyph} guardrail {self.theme.sep} "
            f"{escape(reason)}[/{self.theme.error}]"
        )

    def print_interrupted(self) -> None:
        self._stop_thinking_status()
        self.finish_steps(success=False)
        glyph = self.theme.fail_glyph
        self.console.print(
            f"[{self.theme.warning}]{glyph} interrupted {self.theme.sep} "
            f"back to prompt[/{self.theme.warning}]"
        )

    def register_interrupt(self) -> bool:
        """Return True on the third consecutive Ctrl+C, signaling CLI exit."""
        self._stop_thinking_status()
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
        self._stop_thinking_status()
        self._tool_count = 0
        self._thinking = False
        self._thinking_start = 0.0
        self._last_thought_seconds = None
        self._todo = None
        self._last_phase = None
        self._streaming = False
        self._turn_active = True
        self._last_turn_lines = []

    def update_todo(self, todo: TodoTracker) -> None:
        """Print a subtle breadcrumb line whenever the workflow phase advances.

        Instead of a live-refreshing panel, we emit one inline line per
        transition so the terminal scrollback shows the phase history.
        """
        self._todo = todo
        if not self._turn_active:
            return
        labels = {
            Phase.receive: "Receive",
            Phase.analyze: "Analyze",
            Phase.plan: "Plan",
            Phase.implement: "Implement",
            Phase.validate: "Validate",
            Phase.deliver: "Deliver",
        }
        current = todo.current if not todo.finished else None
        if current is None or current == self._last_phase:
            return
        self._last_phase = current
        label = labels.get(current, current.value)
        self._stop_thinking_status()
        self.console.print(
            f"[{self.theme.muted}]{self.theme.sep} {escape(label)}[/{self.theme.muted}]"
        )
        if self._turn_active:
            self._last_turn_lines.append(f"{self.theme.sep} {label}")

    def start_thinking(self) -> None:
        """Show a transient spinner while the model is thinking."""
        self._thinking = True
        self._thinking_start = datetime.now().timestamp()
        if self._thinking_status is not None:
            return
        if not self.console.is_terminal:
            # In non-terminal mode (piped output, tests) Rich's Status uses
            # a Live under the hood which would clutter captured output.
            return
        try:
            self._thinking_status = self.console.status(
                f"[{self.theme.muted}]thinking...[/{self.theme.muted}]",
                spinner="dots",
            )
            self._thinking_status.start()
        except Exception:  # noqa: BLE001 - status is decorative; never block the turn
            self._thinking_status = None

    def stop_thinking(self, elapsed_seconds: float) -> None:
        """Finalize the thinking indicator; print a compact "thought Xs" line."""
        if not self._thinking:
            return
        self._thinking = False
        self._last_thought_seconds = elapsed_seconds
        self._stop_thinking_status()
        if elapsed_seconds > 0:
            self.console.print(
                f"[{self.theme.muted}]{self.theme.spinner} thought "
                f"{elapsed_seconds:.1f}s[/{self.theme.muted}]"
            )
            if self._turn_active:
                self._last_turn_lines.append(
                    f"{self.theme.spinner} thought {elapsed_seconds:.1f}s"
                )

    def _stop_thinking_status(self) -> None:
        if self._thinking_status is not None:
            try:
                self._thinking_status.stop()
            except Exception:  # noqa: BLE001 - stopping a stale status must not raise
                pass
            self._thinking_status = None

    def print_last_guardrail(self) -> None:
        """Show the last guardrail stop reason, if any."""
        if self._last_guardrail is None:
            self.print_info("No guardrail stop in this session yet.")
            return
        glyph = self.theme.fail_glyph
        self.console.print(
            f"[{self.theme.error}]{glyph} last guardrail {self.theme.sep} "
            f"{escape(self._last_guardrail)}[/{self.theme.error}]"
        )

    def start_stream(self) -> None:
        """Streaming is now internal book-keeping only; the loop prints the
        final content via ``print_assistant`` / ``print_final_answer``.
        """
        self._streaming = True

    def stream_delta(self, text: str) -> None:
        # Deltas are ignored — the loop renders the assembled response once
        # via print_assistant/print_final_answer to keep scrollback clean.
        return

    def end_stream(self) -> None:
        self._streaming = False

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
        if self._last_turn_lines:
            lines.extend(
                ["\n## Steps\n"] + [f"- {line}" for line in self._last_turn_lines] + ["\n"]
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
        self._stop_thinking_status()
        if not self._tool_count:
            self._turn_active = False
            return
        count = f" {self.theme.sep} {self._tool_count} tool(s)"
        if success:
            glyph = self.theme.success_glyph
            self.console.print(
                f"[{self.theme.success}]{glyph} done{count}[/{self.theme.success}]"
            )
        else:
            glyph = self.theme.fail_glyph
            self.console.print(
                f"[{self.theme.error}]{glyph} stopped{count}[/{self.theme.error}]"
            )
        self._turn_active = False
        self._tool_count = 0

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

    @staticmethod
    def _compact_line(text: str, limit: int) -> str:
        compact = " ".join(str(text).split())
        if len(compact) <= limit:
            return compact
        return compact[: max(1, limit - 1)].rstrip() + "…"

    def confirm_tool_call(self, tool: Tool, arguments: dict) -> ApprovalDecision:
        self._stop_thinking_status()
        summary = _summarize_arguments(arguments)
        risk = "low"
        warning: str | None = None
        if tool.name in {RUN_CLI_TOOL_NAME, START_PROCESS_TOOL_NAME}:
            command = str(arguments.get("command", ""))
            risk = risk_level(command)
            warning = command_warning(command)

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

        decision = self._approval_menu(tool.name)
        if decision is not None:
            return decision
        return self._approval_text_prompt()

    def _approval_menu(self, tool_name: str) -> ApprovalDecision | None:
        """Show an arrow-key select menu for approving a tool call.

        Returns None to signal "no menu available" (non-TTY, InquirerPy
        import failure, etc.) so the caller can fall back to the text prompt.
        InquirerPy uses prompt_toolkit internally; that plays fine here
        because our outer PromptSession is idle during a tool call.
        """
        if not (self.console.is_terminal and sys.stdin.isatty() and sys.stdout.isatty()):
            return None
        try:
            from InquirerPy import inquirer
        except ImportError:
            return None
        try:
            choice = inquirer.select(
                message=f"Allow {tool_name}?",
                choices=[
                    {"name": "Apply once", "value": "yes"},
                    {"name": "Always allow this tool this session", "value": "always"},
                    {"name": "Reject", "value": "no"},
                ],
                default="yes",
                qmark="›",
                amark="›",
            ).execute()
        except (KeyboardInterrupt, EOFError):
            return "no"
        except Exception:  # noqa: BLE001 - fall back to text on unexpected TTY errors
            return None
        if choice in {"yes", "no", "always"}:
            return choice  # type: ignore[return-value]
        return "no"

    def _approval_text_prompt(self) -> ApprovalDecision:
        """Legacy y/n/a text prompt — used in non-TTY environments (tests,
        piped stdin) where an arrow-key menu wouldn't render.
        """
        while True:
            try:
                answer = self.console.input(self._approval_prompt).strip().lower()
            except (KeyboardInterrupt, EOFError):
                return "no"
            if answer in ("y", "yes"):
                return "yes"
            if answer in ("n", "no"):
                return "no"
            if answer in ("a", "always"):
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
