"""In-process synchronous event bus.

The bus is deliberately simple: subscribers register a callable and each
``emit`` fans out to all of them in registration order. This is enough for
the current single-process, single-UI setup — swapping to an async queue
would only matter when a subscriber (e.g. a Textual dashboard running on
its own event loop) needs backpressure or thread-safety.

Subscriber failures are swallowed so a broken renderer can't take down
the agent loop. If you need to debug a renderer, subscribe a logging
handler that re-raises.
"""

from __future__ import annotations

from collections.abc import Callable

from marcus_code.runtime.events import Event

EventHandler = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    def emit(self, event: Event) -> None:
        # Snapshot the handler list so a subscriber that resubscribes during
        # dispatch doesn't affect this fan-out.
        for handler in list(self._handlers):
            try:
                handler(event)
            except Exception:  # noqa: BLE001 - never let a renderer bug crash the agent
                pass
