import asyncio
import ipaddress
import re
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from harness.config import Settings
from harness.db.enums import RiskTier
from harness.runtime.native_tools import _strip_html
from harness.runtime.tools import Tool

READ_FILE_TOOL_NAME = "read_file"
WRITE_FILE_TOOL_NAME = "write_file"
EDIT_FILE_TOOL_NAME = "edit_file"
LIST_FILES_TOOL_NAME = "list_files"
GREP_TOOL_NAME = "grep"
RUN_CLI_TOOL_NAME = "run_cli"
FETCH_URL_TOOL_NAME = "fetch_url"

# Directories never walked by list_files/grep — noise, not user code.
_SKIP_DIR_NAMES = frozenset({".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".ruff_cache", ".pytest_cache"})

_MAX_LIST_RESULTS = 200
_MAX_GREP_MATCHES = 200
_MAX_GREP_FILE_BYTES = 2_000_000  # skip files larger than this when grepping

_SECRET_FILE_NAMES = {".env", ".env.local", ".env.production", "credentials.json"}
_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S
)
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)(api[_-]?key|secret|password|token|authorization)(\s*[:=]\s*)(['\"]?)[^\s'\"]+\3"
)


def _redact_secrets(path: str, content: str) -> tuple[str, int]:
    name = Path(path).name.lower()
    redactions = 0
    if name in _SECRET_FILE_NAMES or name.endswith((".pem", ".key")):
        redactions += 1
        return "[REDACTED SENSITIVE FILE CONTENT]", redactions
    redacted = content
    redacted, count = _PRIVATE_KEY_PATTERN.subn("[REDACTED PRIVATE KEY]", redacted)
    redactions += count
    redacted, count = _CREDENTIAL_PATTERN.subn(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", redacted)
    redactions += count
    return redacted, redactions


def _resolve_scoped_path(root: Path, relative_path: str) -> Path:
    """Resolve relative_path under root, refusing any path that escapes it.

    Mirrors harness/runtime/native_tools.py's _resolve_sandboxed_path, but
    scoped to the CLI's working directory rather than a configured server
    sandbox — kept as a separate small copy rather than a shared import so
    marcus_code doesn't couple to harness's server-sandbox semantics.
    """
    base = root.resolve()
    candidate = (base / relative_path).resolve()
    if not candidate.is_relative_to(base):
        raise ValueError(f"path {relative_path!r} escapes the working directory")
    return candidate


def _should_skip_dir(path: Path) -> bool:
    return any(part in _SKIP_DIR_NAMES for part in path.parts)


def build_read_file_tool(root: Path) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        path = arguments.get("path")
        if not path:
            raise ValueError("read_file requires a 'path' field")
        resolved = _resolve_scoped_path(root, path)
        if not resolved.is_file():
            raise ValueError(f"file not found: {path}")
        content = resolved.read_text(encoding="utf-8", errors="replace")
        safe_content, redactions = _redact_secrets(path, content)
        return {"path": path, "content": safe_content, "lines": content.count("\n") + 1, "redacted": redactions > 0}

    return Tool(
        name=READ_FILE_TOOL_NAME,
        description="Read a text file's full contents. Path is relative to the working directory.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path relative to the working directory."}},
            "required": ["path"],
        },
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


def build_write_file_tool(root: Path) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        path = arguments.get("path")
        content = arguments.get("content")
        if not path or content is None:
            raise ValueError("write_file requires 'path' and 'content' fields")
        resolved = _resolve_scoped_path(root, path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return {"path": path, "bytes_written": len(content.encode("utf-8"))}

    return Tool(
        name=WRITE_FILE_TOOL_NAME,
        description=(
            "Create a new file or overwrite an existing one with the given content. "
            "Prefer edit_file for changes to an existing file — write_file replaces "
            "the whole file. Path is relative to the working directory."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the working directory."},
                "content": {"type": "string", "description": "Full file content to write."},
            },
            "required": ["path", "content"],
        },
        handler=handler,
        risk_tier=RiskTier.sensitive_write,
    )


def build_edit_file_tool(root: Path) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        path = arguments.get("path")
        old_string = arguments.get("old_string")
        new_string = arguments.get("new_string")
        if not path or old_string is None or new_string is None:
            raise ValueError("edit_file requires 'path', 'old_string', and 'new_string' fields")
        resolved = _resolve_scoped_path(root, path)
        if not resolved.is_file():
            raise ValueError(f"file not found: {path}")

        original = resolved.read_text(encoding="utf-8", errors="replace")
        occurrences = original.count(old_string)
        if occurrences == 0:
            raise ValueError("old_string not found in file")
        if occurrences > 1:
            raise ValueError(
                f"old_string is not unique in file ({occurrences} occurrences) — "
                "include more surrounding context to make it unique"
            )

        updated = original.replace(old_string, new_string, 1)
        resolved.write_text(updated, encoding="utf-8")
        return {"path": path, "old_string": old_string, "new_string": new_string}

    return Tool(
        name=EDIT_FILE_TOOL_NAME,
        description=(
            "Replace one exact occurrence of old_string with new_string in an existing "
            "file. old_string must match exactly (including whitespace) and appear "
            "exactly once — include enough surrounding context to make it unique. "
            "Path is relative to the working directory."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the working directory."},
                "old_string": {"type": "string", "description": "Exact text to replace, must be unique in the file."},
                "new_string": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_string", "new_string"],
        },
        handler=handler,
        risk_tier=RiskTier.sensitive_write,
    )


def build_list_files_tool(root: Path) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        pattern = arguments.get("pattern") or "**/*"
        base = root.resolve()
        matches = []
        for candidate in base.glob(pattern):
            if _should_skip_dir(candidate.relative_to(base)):
                continue
            if candidate.is_file():
                matches.append(str(candidate.relative_to(base)))
            if len(matches) >= _MAX_LIST_RESULTS:
                break
        matches.sort()
        return {
            "pattern": pattern,
            "files": matches,
            "truncated": len(matches) >= _MAX_LIST_RESULTS,
        }

    return Tool(
        name=LIST_FILES_TOOL_NAME,
        description=(
            "List files under the working directory matching a glob pattern "
            "(e.g. '**/*.py', 'src/**/*'). Defaults to all files. "
            f"Results capped at {_MAX_LIST_RESULTS}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern, default '**/*'."},
            },
            "required": [],
        },
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


def build_grep_tool(root: Path) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        pattern = arguments.get("pattern")
        if not pattern:
            raise ValueError("grep requires a 'pattern' field")
        glob_pattern = arguments.get("glob") or "**/*"
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc

        base = root.resolve()
        matches: list[dict[str, Any]] = []
        for candidate in base.glob(glob_pattern):
            if len(matches) >= _MAX_GREP_MATCHES:
                break
            if not candidate.is_file() or _should_skip_dir(candidate.relative_to(base)):
                continue
            try:
                if candidate.stat().st_size > _MAX_GREP_FILE_BYTES:
                    continue
                text = candidate.read_text(encoding="utf-8", errors="strict")
            except (UnicodeDecodeError, OSError):
                continue  # binary or unreadable file — skip, not an error
            rel = str(candidate.relative_to(base))
            for line_no, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append({"path": rel, "line": line_no, "text": line.strip()[:300]})
                    if len(matches) >= _MAX_GREP_MATCHES:
                        break

        return {
            "pattern": pattern,
            "matches": matches,
            "truncated": len(matches) >= _MAX_GREP_MATCHES,
        }

    return Tool(
        name=GREP_TOOL_NAME,
        description=(
            "Search file contents for a regex pattern under the working directory. "
            f"Skips binary files and common noise directories. Results capped at {_MAX_GREP_MATCHES}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression to search for."},
                "glob": {"type": "string", "description": "Glob to restrict which files are searched, default '**/*'."},
            },
            "required": ["pattern"],
        },
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


def build_run_cli_tool(root: Path, settings: Settings) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        command = arguments.get("command")
        if not command:
            raise ValueError("run_cli requires a 'command' field")

        cwd = root.resolve()
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=settings.tools_run_cli_timeout_seconds
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise ValueError(
                f"command timed out after {settings.tools_run_cli_timeout_seconds}s"
            ) from exc

        max_bytes = settings.tools_run_cli_max_output_bytes
        return {
            "command": command,
            "cwd": str(cwd),
            "exit_code": proc.returncode,
            "stdout": stdout[:max_bytes].decode("utf-8", errors="replace"),
            "stderr": stderr[:max_bytes].decode("utf-8", errors="replace"),
        }

    return Tool(
        name=RUN_CLI_TOOL_NAME,
        description=(
            "Run a shell command in the working directory. DESTRUCTIVE — requires "
            "approval every call. Prefer a narrower tool when one suffices."
        ),
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The shell command to run."}},
            "required": ["command"],
        },
        handler=handler,
        risk_tier=RiskTier.destructive,
    )


def build_fetch_url_tool(settings: Settings) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        url = arguments.get("url")
        if not url:
            raise ValueError("fetch_url requires a 'url' field")
        _validate_public_url(url)

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
                    _validate_public_url(url)
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
            "Fetch a web page or URL over HTTP(S) and return its text content "
            "(HTML tags stripped). Use for documentation, changelogs, or API docs."
        ),
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "The absolute http(s) URL to fetch."}},
            "required": ["url"],
        },
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


def _validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("fetch_url only supports public http(s) URLs")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise ValueError(f"could not resolve URL host: {parsed.hostname}") from exc
    if not addresses:
        raise ValueError("URL host has no addresses")
    if len(addresses) != 1:
        raise ValueError("fetch_url refuses hosts with multiple DNS addresses")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("fetch_url refuses loopback, private, link-local, or reserved addresses")


def build_marcus_tools(root: Path, settings: Settings) -> list[Tool]:
    from marcus_code.skills import build_load_skill_tool

    return [
        build_read_file_tool(root),
        build_write_file_tool(root),
        build_edit_file_tool(root),
        build_list_files_tool(root),
        build_grep_tool(root),
        build_run_cli_tool(root, settings),
        build_fetch_url_tool(settings),
        build_load_skill_tool(root),
    ]
