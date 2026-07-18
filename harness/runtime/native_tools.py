import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from harness.config import Settings
from harness.db.enums import RiskTier
from harness.runtime.command_policy import inspect_shell_command
from harness.runtime.file_writes import atomic_write_text
from harness.runtime.html_utils import strip_html as _strip_html
from harness.runtime.path_utils import resolve_scoped_path
from harness.runtime.processes import (
    close_process_transport,
    process_group_kwargs,
    terminate_process_tree,
)
from harness.runtime.redaction import redact_secrets
from harness.runtime.tools import Tool, ToolHandler
from harness.runtime.url_validation import validate_public_url

FINISH_TOOL_NAME = "finish"
ASK_USER_TOOL_NAME = "ask_user"
LIST_TOOL_DOMAINS_NAME = "list_tool_domains"
LIST_DOMAIN_TOOLS_NAME = "list_domain_tools"
LOAD_TOOL_NAME = "load_tool"
USE_SKILL_NAME = "use_skill"

# Built-in tools (not from an MCP server) exposed as a single progressive-
# disclosure domain alongside each registered MCP server's domain.
BUILTIN_DOMAIN_NAME = "builtin"
FETCH_URL_TOOL_NAME = "fetch_url"
READ_FILE_TOOL_NAME = "read_file"
WRITE_FILE_TOOL_NAME = "write_file"
RUN_CLI_TOOL_NAME = "run_cli"

FINISH_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "result": {"description": "The final result or answer for the goal."},
        "summary": {"type": "string", "description": "Brief summary of what was done."},
        "outcome": {
            "type": "string",
            "enum": ["succeeded", "failed"],
            "description": "Whether the requested goal actually succeeded. Defaults to succeeded.",
        },
    },
    "required": ["result"],
}

ASK_USER_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "description": "The question to ask the user."},
    },
    "required": ["question"],
}

LIST_TOOL_DOMAINS_SCHEMA = {"type": "object", "properties": {}, "required": []}

LIST_DOMAIN_TOOLS_SCHEMA = {
    "type": "object",
    "properties": {
        "domain": {
            "type": "string",
            "description": "A domain name returned by list_tool_domains.",
        },
    },
    "required": ["domain"],
}

LOAD_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "A tool name returned by list_domain_tools.",
        },
    },
    "required": ["name"],
}

USE_SKILL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "A skill name from the published skill catalog.",
        },
    },
    "required": ["name"],
}


async def _finish_handler(arguments: dict[str, Any]) -> dict[str, Any]:
    if "result" not in arguments:
        raise ValueError("finish requires a 'result' field")
    return arguments


async def _ask_user_handler(arguments: dict[str, Any]) -> dict[str, Any]:
    if "question" not in arguments:
        raise ValueError("ask_user requires a 'question' field")
    return arguments


def build_finish_tool() -> Tool:
    return Tool(
        name=FINISH_TOOL_NAME,
        description=(
            "Call this when the goal has been achieved. Pass the final result and a "
            "brief summary of what was done."
        ),
        parameters=FINISH_TOOL_SCHEMA,
        handler=_finish_handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


def build_ask_user_tool() -> Tool:
    return Tool(
        name=ASK_USER_TOOL_NAME,
        description="Call this to ask the user a clarifying question before continuing.",
        parameters=ASK_USER_TOOL_SCHEMA,
        handler=_ask_user_handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


# Progressive tool disclosure (issue #15, idea.md §4): these three meta-tools
# expose the tool catalog in three levels (domain -> tool summary -> full
# schema) instead of sending every MCP tool's full schema to the LLM up
# front. Their real logic needs DB access the tenant's MCP registry, which a
# plain ToolHandler (arguments-only) doesn't have — RunEngine builds the
# actual handler per call (closed over the run's tenant) and passes it in
# here, mirroring the shape of build_finish_tool/build_ask_user_tool so the
# resulting Tool still flows through the normal write-ahead/idempotency path.


def build_list_tool_domains_tool(handler: ToolHandler) -> Tool:
    return Tool(
        name=LIST_TOOL_DOMAINS_NAME,
        description=(
            "List the available tool domains (one per registered external system, "
            "e.g. an MCP server). Call this first to discover what capabilities "
            "exist before you know which tool you need."
        ),
        parameters=LIST_TOOL_DOMAINS_SCHEMA,
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


def build_list_domain_tools_tool(handler: ToolHandler) -> Tool:
    return Tool(
        name=LIST_DOMAIN_TOOLS_NAME,
        description=(
            "List the tools available within one domain: name and a short summary "
            "for each, without full parameter schemas. Call load_tool on one of "
            "them to get its schema before calling it."
        ),
        parameters=LIST_DOMAIN_TOOLS_SCHEMA,
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


def build_load_tool_tool(handler: ToolHandler) -> Tool:
    return Tool(
        name=LOAD_TOOL_NAME,
        description=(
            "Unlock a specific tool by name: returns its full parameter schema and "
            "makes it callable starting next turn. You must call this before "
            "calling any tool other than finish, ask_user, or these meta-tools."
        ),
        parameters=LOAD_TOOL_SCHEMA,
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


def build_use_skill_tool(handler: ToolHandler) -> Tool:
    return Tool(
        name=USE_SKILL_NAME,
        description=(
            "Load a published skill by name when it matches the user's request. "
            "This persists the skill revision for the run, injects its full "
            "instruction starting next turn, and unlocks the skill's required tools."
        ),
        parameters=USE_SKILL_SCHEMA,
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


# --- Built-in capability tools (fetch_url, read_file, write_file, run_cli) ---
#
# Unlike the meta-tools above, these are real capabilities exposed directly
# (not proxied through an MCP server). They're merged into RunEngine's
# tools_by_name at construction (see build_builtin_tools) and surfaced
# through the same progressive-disclosure domain mechanism as MCP servers,
# under the synthetic domain name BUILTIN_DOMAIN_NAME — so they don't bloat
# every LLM call with 4 more always-visible schemas.

FETCH_URL_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "The absolute http(s) URL to fetch."},
    },
    "required": ["url"],
}

READ_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "File path relative to the sandboxed tools directory.",
        },
    },
    "required": ["path"],
}

WRITE_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "File path relative to the sandboxed tools directory.",
        },
        "content": {"type": "string", "description": "Text content to write."},
    },
    "required": ["path", "content"],
}

RUN_CLI_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "The shell command to run."},
    },
    "required": ["command"],
}


def _resolve_sandboxed_path(root: str, relative_path: str) -> Path:
    return resolve_scoped_path(Path(root), relative_path)


# Re-export for extra tools to avoid circular imports.
resolve_sandboxed_path = _resolve_sandboxed_path


def build_fetch_url_tool(settings: Settings) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        url = arguments.get("url")
        if not url:
            raise ValueError("fetch_url requires a 'url' field")
        validate_public_url(url)

        async with httpx.AsyncClient(
            follow_redirects=False, timeout=settings.tools_fetch_url_timeout_seconds
        ) as client:
            for _ in range(5):
                response = await client.get(url)
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        break
                    url = urljoin(url, location)
                    validate_public_url(url)
                    continue
                break
        response.raise_for_status()

        max_bytes = settings.tools_fetch_url_max_bytes
        raw = response.content[:max_bytes]
        text = raw.decode(response.encoding or "utf-8", errors="replace")
        content_type = response.headers.get("content-type", "")
        if "html" in content_type:
            text = _strip_html(text)

        return {
            "url": str(response.url),
            "status": response.status_code,
            "content_type": content_type,
            "text": text,
            "truncated": len(response.content) > max_bytes,
        }

    return Tool(
        name=FETCH_URL_TOOL_NAME,
        description=(
            "Fetch a public web page or URL over HTTP(S) and return its text content "
            "(HTML tags stripped). Use this to read documentation, articles, or "
            "API responses from the web. Loopback/private addresses are blocked."
        ),
        parameters=FETCH_URL_SCHEMA,
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


def build_read_file_tool(settings: Settings) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        path = arguments.get("path")
        if not path:
            raise ValueError("read_file requires a 'path' field")
        resolved = _resolve_sandboxed_path(settings.tools_fs_root, path)
        if not resolved.is_file():
            raise ValueError(f"file not found: {path}")

        max_bytes = settings.tools_read_file_max_bytes
        data = resolved.read_bytes()
        content = data[:max_bytes].decode("utf-8", errors="replace")
        safe_content, redactions = redact_secrets(path, content)
        return {
            "path": path,
            "content": safe_content,
            "truncated": len(data) > max_bytes,
            "redacted": redactions > 0,
        }

    return Tool(
        name=READ_FILE_TOOL_NAME,
        description=(
            f"Read a text file from the sandboxed tools directory ({settings.tools_fs_root}). "
            "The path is relative to that root and cannot escape it. "
            "Credentials and private keys are redacted from the returned content."
        ),
        parameters=READ_FILE_SCHEMA,
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


def build_write_file_tool(settings: Settings) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        path = arguments.get("path")
        content = arguments.get("content")
        if not path or content is None:
            raise ValueError("write_file requires 'path' and 'content' fields")
        resolved = _resolve_sandboxed_path(settings.tools_fs_root, path)
        return {"path": path, **atomic_write_text(resolved, content)}

    return Tool(
        name=WRITE_FILE_TOOL_NAME,
        description=(
            f"Write a text file into the sandboxed tools directory ({settings.tools_fs_root}), "
            "creating parent directories as needed. Overwrites an existing file at that path. "
            "The path is relative to that root and cannot escape it. Requires human approval."
        ),
        parameters=WRITE_FILE_SCHEMA,
        handler=handler,
        risk_tier=RiskTier.sensitive_write,
    )


def build_run_cli_tool(settings: Settings) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        if not settings.tools_run_cli_enabled:
            raise ValueError("run_cli is disabled on this deployment")
        command = arguments.get("command")
        if not command:
            raise ValueError("run_cli requires a 'command' field")
        metadata = inspect_shell_command(command)

        cwd = Path(settings.tools_fs_root).resolve()
        cwd.mkdir(parents=True, exist_ok=True)

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_group_kwargs(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=settings.tools_run_cli_timeout_seconds
            )
        except TimeoutError as exc:
            await terminate_process_tree(proc, drain_pipes=True)
            close_process_transport(proc)
            raise ValueError(
                f"command timed out after {settings.tools_run_cli_timeout_seconds}s"
            ) from exc

        max_bytes = settings.tools_run_cli_max_output_bytes
        result = {
            "command": command,
            "cwd": str(cwd),
            "exit_code": proc.returncode,
            "stdout": stdout[:max_bytes].decode("utf-8", errors="replace"),
            "stderr": stderr[:max_bytes].decode("utf-8", errors="replace"),
            **metadata.as_dict(),
        }
        close_process_transport(proc)
        return result

    return Tool(
        name=RUN_CLI_TOOL_NAME,
        description=(
            f"Run a shell command in the sandboxed tools directory ({settings.tools_fs_root}). "
            "DESTRUCTIVE — requires human approval every single call, and can affect "
            "anything the server process has permission to touch. Prefer a narrower "
            "tool (fetch_url, read_file, write_file) whenever one suffices."
        ),
        parameters=RUN_CLI_SCHEMA,
        handler=handler,
        risk_tier=RiskTier.destructive,
    )


def build_builtin_tools(settings: Settings) -> list[Tool]:
    from harness.runtime.native_tools_extra import build_native_extra_tools

    tools = [
        build_fetch_url_tool(settings),
        build_read_file_tool(settings),
        build_write_file_tool(settings),
    ]
    if settings.tools_run_cli_enabled:
        tools.append(build_run_cli_tool(settings))
    tools.extend(build_native_extra_tools(settings))
    return tools
