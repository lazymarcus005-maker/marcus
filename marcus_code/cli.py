import asyncio
import contextlib
from pathlib import Path

from harness.config import get_settings
from harness.llm.gateway import LLMGateway
from marcus_code.loop import MarcusLoop
from marcus_code.prompt import build_system_prompt
from marcus_code.tools import build_marcus_tools
from marcus_code.ui import TerminalUI

EXIT_COMMANDS = {"/exit", "/quit"}


async def _amain() -> None:
    settings = get_settings()
    root = Path.cwd()
    ui = TerminalUI()
    ui.print_banner(root)

    llm = LLMGateway(settings=settings)
    tools = build_marcus_tools(root, settings)
    loop = MarcusLoop(llm, tools, ui, system_prompt=build_system_prompt(root))

    try:
        while True:
            user_input = ui.prompt_user()
            if user_input is None:
                break
            stripped = user_input.strip()
            if not stripped:
                continue
            if stripped in EXIT_COMMANDS:
                break
            try:
                await loop.run_turn(user_input)
            except KeyboardInterrupt:
                ui.print_interrupted()
    finally:
        await llm.aclose()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_amain())


if __name__ == "__main__":
    main()
