import difflib
import re
import sys
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from rich.console import Console, Group
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
from marcus_code.tools import EDIT_FILE_TOOL_NAME, RUN_CLI_TOOL_NAME

if TYPE_CHECKING:
    from marcus_code.loop import UsageStats

ApprovalDecision = Literal["yes", "no", "always"]


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

    def print_banner(self, root, *, model: str, session_name: str) -> None:
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
        remaining = "unlimited" if max_total_tokens is None else f"{max(0, max_total_tokens - stats.total_tokens):,}"
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

    def run_first_time_setup(self, *, default_base_url: str, default_model: str) -> tuple[str, str, str] | None:
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
            base_url = self.console.input(
                f"LLM base URL (default: {escape(default_base_url)}): "
            ).strip() or default_base_url
            api_key = self.console.input("LLM API key: ").strip()
            if not api_key:
                self.console.print("[yellow]no API key entered — skipping setup[/yellow]")
                return None
            model = self.console.input(f"LLM model (default: {escape(default_model)}): ").strip() or default_model
        except (KeyboardInterrupt, EOFError):
            self.console.print("\n[yellow]setup cancelled[/yellow]")
            return None
        return api_key, base_url, model

    def prompt_user(self) -> str | None:
        try:
            return self.console.input("[bold cyan]> [/bold cyan]")
        except EOFError:
            return None

    def print_assistant(self, text: str) -> None:
        self.console.print(Markdown(text))

    def print_assistant_delta(self, text: str) -> None:
        self.console.print(text, end="")

    def print_tool_call(self, tool_name: str, arguments: dict) -> None:
        summary = _summarize_arguments(arguments)
        self.console.print(f"[dim]> {escape(tool_name)}({escape(summary)})[/dim]")

    def print_tool_error(self, tool_name: str, error: str) -> None:
        self.console.print(f"[red]x {escape(tool_name)}: {escape(error)}[/red]")

    def print_tool_declined(self, tool_name: str) -> None:
        self.console.print(f"[yellow]x {escape(tool_name)} declined by user[/yellow]")

    def print_guardrail_stop(self, reason: str) -> None:
        self.console.print(f"[red]stopped: {escape(reason)}[/red]")

    def print_interrupted(self) -> None:
        self.console.print("[yellow]interrupted - back to prompt[/yellow]")

    def confirm_tool_call(self, tool: Tool, arguments: dict) -> ApprovalDecision:
        if tool.name == EDIT_FILE_TOOL_NAME:
            self._print_edit_diff(arguments)
        else:
            summary = _summarize_arguments(arguments)
            self.console.print(f"[bold]{escape(tool.name)}[/bold]({escape(summary)})")
        if tool.name == RUN_CLI_TOOL_NAME:
            warning = _command_warning(str(arguments.get("command", "")))
            if warning:
                self.console.print(f"[bold red]WARNING:[/bold red] {escape(warning)}")

        while True:
            # Note: avoid literal square brackets in text passed through
            # Console.input/print — Rich parses "[...]" as markup tags, so
            # e.g. "[y]es" silently vanishes instead of printing.
            answer = self.console.input(
                "[bold]Allow this tool call?[/bold] (y)es / (n)o / (a)lways for this session: "
            ).strip().lower()
            if answer in ("y", "yes"):
                return "yes"
            if answer in ("n", "no"):
                return "no"
            if answer in ("a", "always"):
                return "always"
            self.console.print("[dim]please answer y, n, or a[/dim]")

    def _print_edit_diff(self, arguments: dict) -> None:
        path = arguments.get("path", "<unknown>")
        old = arguments.get("old_string", "")
        new = arguments.get("new_string", "")
        diff = "\n".join(
            difflib.unified_diff(
                old.splitlines(), new.splitlines(), fromfile=path, tofile=path, lineterm=""
            )
        )
        self.console.print(f"[bold]edit_file[/bold]({escape(str(path))})")
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


def _command_warning(command: str) -> str | None:
    lowered = command.lower()
    if re.search(r"rm\s+(-\w*f|--force).*\s-r|rm\s+-r[f\s]|rmdir\s+/s", lowered):
        return "recursive deletion or forced removal"
    if re.search(r"git\s+push\b.*--force|git\s+reset\b.*--hard", lowered):
        return "irreversible or remote Git history rewrite"
    if re.search(r"\b(drop|truncate)\s+(table|database)\b|delete\s+from\b", lowered):
        return "destructive database operation"
    return None
