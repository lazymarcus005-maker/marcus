"""Textual dashboard for Marcus — the ``marcus tui`` subcommand.

The TUI is intentionally minimal and shares the agent's ``EventBus``: the
same events the terminal renderer converts into log lines are dispatched
here into a Textual ``RichLog``. Tool approvals surface as a modal screen
rather than the inline y/n/a prompt.

Layout (top → bottom):

    ┌ status line ─────────────────────────────────────────┐
    │ model · mode · ctx% · session                        │
    ├──────────────────────────────────────────────────────┤
    │ RichLog (append-only event timeline)                 │
    │                                                      │
    ├──────────────────────────────────────────────────────┤
    │ Input                                                │
    └──────────────────────────────────────────────────────┘

Slash commands (/help, /exit, ...) are dispatched through the existing
``marcus_code.cli.commands`` handler, so behavior stays consistent with
the terminal REPL.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any

from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, RichLog, Static

from harness.runtime.tools import Tool
from marcus_code.runtime.event_bus import EventBus
from marcus_code.runtime.events import (
    AssistantMessage,
    Event,
    FinalAnswer,
    GuardrailStop,
    Interrupted,
    Recovery,
    ThinkingStarted,
    ThinkingStopped,
    TodoUpdated,
    ToolCallCompleted,
    ToolCallDeclined,
    ToolCallFailed,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
)
from marcus_code.runtime.todo_tracker import Phase
from marcus_code.ui.warnings import command_warning, risk_level


def _summarize(arguments: dict[str, Any], max_len: int = 120) -> str:
    parts = []
    for key, value in arguments.items():
        text = str(value)
        if len(text) > max_len:
            text = text[:max_len] + "..."
        parts.append(f"{key}={text!r}")
    return ", ".join(parts)


_PHASE_LABELS = {
    Phase.receive: "Receive",
    Phase.analyze: "Analyze",
    Phase.plan: "Plan",
    Phase.implement: "Implement",
    Phase.validate: "Validate",
    Phase.deliver: "Deliver",
}


class ApprovalScreen(ModalScreen[str]):
    """Modal dialog surfaced whenever the agent needs a tool-call approval."""

    CSS = """
    ApprovalScreen {
        align: center middle;
    }
    #dialog {
        width: 70;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: solid $primary;
    }
    #buttons {
        height: 3;
        align: center middle;
        margin-top: 1;
    }
    Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("y", "decide('yes')", "Apply once", show=True),
        Binding("a", "decide('always')", "Always", show=True),
        Binding("n,escape", "decide('no')", "Reject", show=True),
    ]

    def __init__(self, tool_name: str, summary: str, risk: str, warning: str | None) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._summary = summary
        self._risk = risk
        self._warning = warning

    def compose(self) -> ComposeResult:
        risk_colour = {"high": "red", "medium": "yellow"}.get(self._risk, "green")
        header = Text.from_markup(
            f"[{risk_colour}]risk: {self._risk}[/{risk_colour}]  "
            f"[bold]{escape(self._tool_name)}[/bold]"
        )
        with Vertical(id="dialog"):
            yield Static("Allow this tool call?")
            yield Static(header)
            if self._summary:
                yield Static(f"[dim]{escape(self._summary)}[/dim]")
            if self._warning:
                yield Static(f"[yellow]▸ warning: {escape(self._warning)}[/yellow]")
            with Vertical(id="buttons"):
                yield Button("Apply once (y)", id="yes", variant="success")
                yield Button("Always allow this tool (a)", id="always", variant="primary")
                yield Button("Reject (n)", id="no", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id or "no")

    def action_decide(self, decision: str) -> None:
        self.dismiss(decision)


class TuiPrompter:
    """Adapter passed to ``MarcusLoop`` in place of ``TerminalUI``.

    The agent's one-way notifications flow through the ``EventBus`` and land
    on the TUI's ``handle_event`` method, so this class only needs to cover
    two things:

    - The synchronous ``hasattr`` gates that the loop uses to decide whether
      to emit some events (``begin_turn``, ``update_todo``, ``print_*``,
      ``finish_steps``, ...). We expose them as no-ops so those gates pass.
    - The truly two-way calls (``confirm_tool_call``, ``refresh_status``)
      which need to bridge into the Textual app.
    """

    _STUB_METHODS = (
        "begin_turn",
        "update_todo",
        "start_thinking",
        "stop_thinking",
        "print_tool_call",
        "print_tool_result",
        "print_tool_error",
        "print_tool_declined",
        "print_recovery",
        "print_assistant",
        "print_final_answer",
        "print_guardrail_stop",
        "print_interrupted",
        "finish_steps",
    )

    def __init__(self, app: "MarcusTuiApp") -> None:
        self._app = app
        # Install no-op stubs so ``hasattr(ui, name)`` in agent.py returns True.
        # We deliberately do NOT expose ``start_stream``/``stream_delta``/
        # ``end_stream`` — that keeps the loop on the non-streaming code path,
        # which is much simpler to render into a discrete RichLog.
        for name in self._STUB_METHODS:
            setattr(self, name, self._noop)

    @staticmethod
    def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def confirm_tool_call(self, tool: Tool, arguments: dict[str, Any]) -> str:
        summary = _summarize(arguments)
        risk = "low"
        warning: str | None = None
        if tool.name in {"run_cli", "start_process"}:
            command = str(arguments.get("command", ""))
            risk = risk_level(command)
            warning = command_warning(command)
        screen = ApprovalScreen(tool.name, summary, risk, warning)
        decision = await self._app.push_screen_wait(screen)
        return decision if decision in {"yes", "no", "always"} else "no"

    def refresh_status(self) -> None:
        self._app.refresh_status()


class TuiRenderer:
    """Subscribes to the agent ``EventBus`` and appends lines to the app's
    ``RichLog``. Mirrors the terminal renderer's structure so both surfaces
    stay in sync when we add a new event type."""

    def __init__(self, app: "MarcusTuiApp") -> None:
        self._app = app
        self._tool_count = 0
        self._last_phase: Phase | None = None

    def handle(self, event: Event) -> None:
        if isinstance(event, TurnStarted):
            self._tool_count = 0
            self._last_phase = None
        elif isinstance(event, TurnFinished):
            if self._tool_count:
                glyph = "✓" if event.success else "×"
                colour = "green" if event.success else "red"
                verb = "done" if event.success else "stopped"
                self._app.write_log(
                    f"[{colour}]{glyph} {verb} · {self._tool_count} tool(s)[/{colour}]"
                )
        elif isinstance(event, TodoUpdated):
            tracker = event.tracker
            current = tracker.current if not tracker.finished else None
            if current is not None and current != self._last_phase:
                self._last_phase = current
                label = _PHASE_LABELS.get(current, current.value)
                self._app.write_log(f"[dim]· {escape(label)}[/dim]")
        elif isinstance(event, ThinkingStarted):
            self._app.set_thinking(True)
        elif isinstance(event, ThinkingStopped):
            self._app.set_thinking(False)
            if event.elapsed_seconds > 0:
                self._app.write_log(
                    f"[dim]⋯ thought {event.elapsed_seconds:.1f}s[/dim]"
                )
        elif isinstance(event, ToolCallStarted):
            self._tool_count += 1
            summary = _summarize(event.arguments)
            self._app.write_log(
                f"[bold cyan]▸ {self._tool_count}. {escape(event.tool_name)}[/bold cyan]"
                + (f"  [dim]· {escape(summary)}[/dim]" if summary else "")
            )
        elif isinstance(event, ToolCallCompleted):
            summary = _tool_result_summary(event.tool_name, event.result)
            self._app.write_log(f"   [green]✓[/green] {escape(summary)}")
        elif isinstance(event, ToolCallFailed):
            self._app.write_log(
                f"   [red]×[/red] {escape(event.tool_name)}: {escape(event.error)}"
            )
        elif isinstance(event, ToolCallDeclined):
            self._app.write_log(f"   [dim]× {escape(event.tool_name)} declined[/dim]")
        elif isinstance(event, AssistantMessage):
            self._app.write_log(escape(event.text))
        elif isinstance(event, FinalAnswer):
            self._app.write_log("")
            self._app.write_log(f"[bold]{escape(event.text)}[/bold]")
        elif isinstance(event, Recovery):
            self._app.write_log(f"[yellow]↻ {escape(event.message)}[/yellow]")
        elif isinstance(event, GuardrailStop):
            self._app.write_log(
                f"[red]× guardrail · {escape(event.reason)}[/red]"
            )
        elif isinstance(event, Interrupted):
            self._app.write_log("[yellow]× interrupted · back to prompt[/yellow]")


def _tool_result_summary(tool_name: str, result: dict[str, Any]) -> str:
    # Local copy of the summary rules used by the terminal renderer — a
    # shared helper would be cleaner but pulling it into the TUI keeps the
    # optional dependency graph shallow.
    if result.get("_truncated"):
        return (
            f"Result captured (truncated from "
            f"{result.get('_original_length', '?')} characters)"
        )
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
    if tool_name == "stop_process":
        return f"Service stopped (process {result.get('process_id', '?')})"
    if tool_name == "fetch_url":
        return f"Fetched URL (status {result.get('status', '?')})"
    return "Tool completed"


class MarcusTuiApp(App):
    """Full-screen Textual app hosting the Marcus REPL.

    The app itself is only a shell — everything meaningful (turn execution,
    tool dispatch, event emission) happens inside ``MarcusLoop``. This class
    just:
    - collects user input,
    - triggers turns and slash commands,
    - shows agent events, and
    - proxies approvals through a modal.
    """

    CSS = """
    Screen { layout: vertical; }
    #status {
        height: 1;
        background: $panel;
        color: $text;
        padding: 0 1;
    }
    #log {
        height: 1fr;
        padding: 0 1;
    }
    #input {
        height: 3;
        border: solid $primary;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "request_quit", "Quit", show=True),
        Binding("ctrl+d", "request_quit", "Quit"),
    ]

    def __init__(
        self,
        events: EventBus,
        *,
        on_submit: Callable[[str], "asyncio.Future[None]"],
        status_provider: Callable[[], dict[str, Any]] | None = None,
        session_name: str = "",
    ) -> None:
        super().__init__()
        self._events = events
        self._on_submit = on_submit
        self._status_provider = status_provider
        self._session_name = session_name
        self._thinking = False
        self._turn_task: asyncio.Task[None] | None = None
        self._renderer = TuiRenderer(self)

    def compose(self) -> ComposeResult:
        yield Static(self._render_status(), id="status")
        yield RichLog(id="log", highlight=False, markup=True, wrap=True)
        yield Input(placeholder="Type a task, /help for commands, Ctrl+C to quit", id="input")

    def on_mount(self) -> None:
        self._events.subscribe(self._renderer.handle)
        self.set_interval(1.0, self.refresh_status)
        self.query_one("#input", Input).focus()
        self.write_log(
            f"[bold]Marcus TUI[/bold]  [dim]· session {escape(self._session_name)}[/dim]"
        )
        self.write_log(
            "[dim]Type a task, /help for commands, Ctrl+C to quit.[/dim]"
        )

    def write_log(self, text: str) -> None:
        try:
            self.query_one("#log", RichLog).write(Text.from_markup(text))
        except Exception:  # noqa: BLE001 - never let a render bug crash the app
            pass

    def set_thinking(self, active: bool) -> None:
        self._thinking = active
        self.refresh_status()

    def refresh_status(self) -> None:
        try:
            self.query_one("#status", Static).update(self._render_status())
        except Exception:  # noqa: BLE001
            pass

    def _render_status(self) -> str:
        status = self._status_provider() if self._status_provider else None
        parts: list[str] = []
        if status:
            used = int(status.get("context_tokens", 0))
            limit = max(1, int(status.get("context_limit", 1)))
            pct = min(100, round(used * 100 / limit))
            parts.extend(
                [
                    f"[bold]{escape(str(status.get('model', '')))}[/bold]",
                    f"mode {escape(str(status.get('mode', '')))}",
                    f"ctx {pct}%",
                    f"used {_format_tokens(int(status.get('total_tokens', 0)))} tok",
                ]
            )
        if self._thinking:
            parts.append("[cyan]⋯ thinking[/cyan]")
        if self._session_name:
            parts.append(f"[dim]{escape(self._session_name)}[/dim]")
        if not parts:
            parts.append("Marcus TUI")
        return "  ·  ".join(parts)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        input_widget = self.query_one("#input", Input)
        input_widget.value = ""
        if not text:
            return
        if self._turn_task is not None and not self._turn_task.done():
            self.write_log("[yellow]A turn is already running; wait for it to finish.[/yellow]")
            return
        # Echo the user's input so scrollback shows both sides of the
        # conversation (Rich renders bold prompt-style prefix).
        self.write_log(f"[bold green]>[/bold green] {escape(text)}")
        self._turn_task = asyncio.create_task(self._run_submission(text))

    async def _run_submission(self, text: str) -> None:
        try:
            await self._on_submit(text)
        except Exception as exc:  # noqa: BLE001 - surface unexpected errors in the log
            self.write_log(f"[red]× error · {escape(type(exc).__name__)}: {escape(str(exc))}[/red]")

    def action_request_quit(self) -> None:
        self.exit()


def _format_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)
