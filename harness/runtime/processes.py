import asyncio
import contextlib
import os
import signal
import subprocess
from typing import Any


def process_group_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        # subprocess.CREATE_NEW_PROCESS_GROUP only exists in the Windows
        # stdlib stub; mypy runs against the Linux stub in CI, so look it up
        # dynamically rather than referencing the attribute directly.
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


async def terminate_process_tree(
    proc: asyncio.subprocess.Process, *, drain_pipes: bool = False
) -> None:
    """Terminate a shell/process and descendants, then wait for transport completion."""
    if proc.returncode is None:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.communicate()
            close_process_transport(killer)
        else:
            # os.killpg/signal.SIGKILL are POSIX-only stdlib attributes; mypy
            # checks against whichever platform's stub it's configured for
            # (which differs between local Windows dev and Linux CI), so look
            # them up dynamically rather than referencing them directly —
            # same reasoning as CREATE_NEW_PROCESS_GROUP above.
            killpg = getattr(os, "killpg", None)
            sigkill = getattr(signal, "SIGKILL", None)
            if killpg is not None and sigkill is not None:
                with contextlib.suppress(ProcessLookupError):
                    killpg(proc.pid, sigkill)
    try:
        if drain_pipes:
            await asyncio.wait_for(proc.communicate(), timeout=2)
        else:
            await asyncio.wait_for(proc.wait(), timeout=2)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=2)


def close_process_transport(proc: asyncio.subprocess.Process) -> None:
    """Release asyncio's platform transport before the event loop is closed."""
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        transport.close()
