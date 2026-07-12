import difflib
import sys
from typing import Literal

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax

from harness.runtime.tools import Tool
from marcus_code.tools import EDIT_FILE_TOOL_NAME

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

    def print_banner(self, root) -> None:
        self.console.print(
            Panel.fit(
                f"[bold]Marcus Code[/bold]\nWorking directory: {escape(str(root))}\n"
                "Type your request, or /exit to quit.",
                border_style="cyan",
            )
        )

    def prompt_user(self) -> str | None:
        try:
            return self.console.input("[bold cyan]> [/bold cyan]")
        except EOFError:
            return None

    def print_assistant(self, text: str) -> None:
        self.console.print(Markdown(text))

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
