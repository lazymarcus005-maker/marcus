import asyncio
import contextlib
import os
import signal
import subprocess
from typing import Any


def process_group_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
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
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
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
