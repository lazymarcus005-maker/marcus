import asyncio
import re
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

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
from harness.runtime.tools import Tool
from harness.runtime.url_validation import validate_public_url

READ_FILE_TOOL_NAME = "read_file"
WRITE_FILE_TOOL_NAME = "write_file"
EDIT_FILE_TOOL_NAME = "edit_file"
LIST_FILES_TOOL_NAME = "list_files"
GREP_TOOL_NAME = "grep"
RUN_CLI_TOOL_NAME = "run_cli"
FETCH_URL_TOOL_NAME = "fetch_url"
START_PROCESS_TOOL_NAME = "start_process"
READ_PROCESS_OUTPUT_TOOL_NAME = "read_process_output"
STOP_PROCESS_TOOL_NAME = "stop_process"
WAIT_FOR_HTTP_TOOL_NAME = "wait_for_http"

DEFAULT_READ_FILE_MAX_CHARS = 4000

# Directories never walked by list_files/grep — noise, not user code.
_SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
    }
)

_MAX_LIST_RESULTS = 200
_MAX_GREP_MATCHES = 200
_MAX_GREP_FILE_BYTES = 2_000_000  # skip files larger than this when grepping


def _resolve_scoped_path(root: Path, relative_path: str) -> Path:
    """Resolve relative_path under root, refusing any path that escapes it."""
    return resolve_scoped_path(root, relative_path)


def _should_skip_dir(path: Path) -> bool:
    return any(part in _SKIP_DIR_NAMES for part in path.parts)


def build_read_file_tool(root: Path, *, max_chars: int = DEFAULT_READ_FILE_MAX_CHARS) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        path = arguments.get("path")
        if not path:
            raise ValueError("read_file requires a 'path' field")
        resolved = _resolve_scoped_path(root, path)
        if not resolved.is_file():
            raise ValueError(f"file not found: {path}")

        raw = resolved.read_text(encoding="utf-8", errors="replace")
        total_lines = raw.count("\n") + 1

        offset = max(1, int(arguments.get("offset", 1)))
        limit = arguments.get("limit")
        if limit is not None:
            limit = max(1, int(limit))

        if limit is not None or offset > 1:
            lines = raw.splitlines(keepends=True)
            start = offset - 1
            end = len(lines) if limit is None else min(start + limit, len(lines))
            selected = lines[start:end]
            content = "".join(selected)
            chunk_lines = len(selected)
        else:
            content = raw
            chunk_lines = total_lines

        safe_content, redactions = redact_secrets(path, content)
        truncated = len(safe_content) > max_chars
        if truncated:
            safe_content = safe_content[:max_chars]

        result: dict[str, Any] = {
            "path": path,
            "content": safe_content,
            "lines": chunk_lines,
            "total_lines": total_lines,
            "offset": offset,
            "redacted": redactions > 0,
            "truncated": truncated,
        }
        if limit is not None:
            result["limit"] = limit
        return result

    return Tool(
        name=READ_FILE_TOOL_NAME,
        description=(
            "Read a text file's contents. Path is relative to the working directory. "
            "Use offset (1-based) and limit to read a specific chunk of a large file."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the working directory.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "1-based starting line.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum lines to return.",
                },
            },
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
        return {"path": path, **atomic_write_text(resolved, content)}

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
                "path": {
                    "type": "string",
                    "description": "Path relative to the working directory.",
                },
                "content": {"type": "string", "description": "Full file content to write."},
            },
            "required": ["path", "content"],
        },
        handler=handler,
        risk_tier=RiskTier.sensitive_write,
        mutates_workspace=True,
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
        return {
            "path": path,
            "old_string": old_string,
            "new_string": new_string,
            **atomic_write_text(resolved, updated),
        }

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
                "path": {
                    "type": "string",
                    "description": "Path relative to the working directory.",
                },
                "old_string": {
                    "type": "string",
                    "description": "Exact text to replace, must be unique in the file.",
                },
                "new_string": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_string", "new_string"],
        },
        handler=handler,
        risk_tier=RiskTier.sensitive_write,
        mutates_workspace=True,
    )


def build_list_files_tool(root: Path) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        pattern = arguments.get("pattern") or "**/*"
        max_depth = arguments.get("max_depth")
        if max_depth is not None:
            max_depth = max(1, int(max_depth))
        base = root.resolve()
        matches = []
        for candidate in base.glob(pattern):
            rel = candidate.relative_to(base)
            if _should_skip_dir(rel):
                continue
            if max_depth is not None and len(rel.parts) > max_depth:
                continue
            if candidate.is_file():
                matches.append(str(rel))
            if len(matches) >= _MAX_LIST_RESULTS:
                break
        matches.sort()
        return {
            "pattern": pattern,
            "max_depth": max_depth,
            "files": matches,
            "truncated": len(matches) >= _MAX_LIST_RESULTS,
        }

    return Tool(
        name=LIST_FILES_TOOL_NAME,
        description=(
            "List files under the working directory matching a glob pattern "
            "(e.g. '**/*.py', 'src/**/*'). Defaults to all files. "
            f"Results capped at {_MAX_LIST_RESULTS}. Use max_depth to avoid deep scans."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern, default '**/*'."},
                "max_depth": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum directory depth to include.",
                },
            },
            "required": [],
        },
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


def _is_text_file(path: Path) -> bool:
    """Best-effort binary detection by sampling bytes for null bytes / non-text."""
    try:
        sample = path.read_bytes()[:8192]
    except OSError:
        return False
    if not sample:
        return True
    if b"\x00" in sample:
        return False
    # Simple heuristic: if more than 30% of bytes are outside printable ASCII
    # plus common whitespace, treat as binary.
    non_text = sum(1 for b in sample if b < 9 or (13 < b < 32 and b not in {9, 10, 13}))
    return non_text / len(sample) <= 0.30


def build_grep_tool(root: Path) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        pattern = arguments.get("pattern")
        if not pattern:
            raise ValueError("grep requires a 'pattern' field")
        glob_pattern = arguments.get("glob") or "**/*"
        max_results = arguments.get("max_results")
        if max_results is None:
            max_results = 50
        else:
            max_results = max(1, min(int(max_results), _MAX_GREP_MATCHES))
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc

        base = root.resolve()
        matches: list[dict[str, Any]] = []
        for candidate in base.glob(glob_pattern):
            if len(matches) >= max_results:
                break
            if not candidate.is_file() or _should_skip_dir(candidate.relative_to(base)):
                continue
            if candidate.stat().st_size > _MAX_GREP_FILE_BYTES:
                continue
            if not _is_text_file(candidate):
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="strict")
            except (UnicodeDecodeError, OSError):
                continue
            rel = str(candidate.relative_to(base))
            for line_no, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append({"path": rel, "line": line_no, "text": line.strip()[:300]})
                    if len(matches) >= max_results:
                        break

        # Sort by filename/path relevance: matches in shorter paths first.
        matches.sort(key=lambda m: (len(m["path"].split("/")), m["path"], m["line"]))

        return {
            "pattern": pattern,
            "matches": matches,
            "truncated": len(matches) >= max_results,
        }

    return Tool(
        name=GREP_TOOL_NAME,
        description=(
            "Search file contents for a regex pattern under the working directory. "
            "Skips binary files and common noise directories. "
            "Default max_results is 50; use max_results up to {_MAX_GREP_MATCHES}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression to search for."},
                "glob": {
                    "type": "string",
                    "description": "Glob to restrict which files are searched, default '**/*'.",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_GREP_MATCHES,
                    "description": "Maximum matches to return.",
                },
            },
            "required": ["pattern"],
        },
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


def _run_cli_timeout_for(command: str, default: float) -> float:
    """Select a longer timeout for known slow commands while keeping a cap."""
    lowered = command.lower()
    slow_markers = ("pytest", "npm test", "dotnet build", "cargo test", "mvn ", "gradle ")
    build_markers = ("build", "compile", "webpack", "tsc", "npm run build")
    if any(marker in lowered for marker in slow_markers):
        return max(default, 180.0)
    if any(marker in lowered for marker in build_markers):
        return max(default, 120.0)
    return default


def build_run_cli_tool(root: Path, settings: Settings) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        command = arguments.get("command")
        if not command:
            raise ValueError("run_cli requires a 'command' field")
        metadata = inspect_shell_command(command)

        requested_timeout = arguments.get("timeout_seconds")
        if requested_timeout is not None:
            timeout = max(0.1, min(float(requested_timeout), 300.0))
        else:
            timeout = _run_cli_timeout_for(command, settings.tools_run_cli_timeout_seconds)

        cwd = root.resolve()
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_group_kwargs(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError as exc:
            await terminate_process_tree(proc, drain_pipes=True)
            close_process_transport(proc)
            raise ValueError(f"command timed out after {timeout:g}s") from exc

        max_bytes = settings.tools_run_cli_max_output_bytes
        result = {
            "command": command,
            "cwd": str(cwd),
            "exit_code": proc.returncode,
            "stdout": stdout[:max_bytes].decode("utf-8", errors="replace"),
            "stderr": stderr[:max_bytes].decode("utf-8", errors="replace"),
            **metadata.as_dict(),
            "timeout_seconds": timeout,
        }
        close_process_transport(proc)
        return result

    return Tool(
        name=RUN_CLI_TOOL_NAME,
        description=(
            "Run a shell command in the working directory. DESTRUCTIVE — requires "
            "approval every call. Prefer a narrower tool when one suffices. "
            "Optional timeout_seconds up to 300."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run."},
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 300,
                    "description": "Optional timeout in seconds (default varies by command).",
                },
            },
            "required": ["command"],
        },
        handler=handler,
        risk_tier=RiskTier.destructive,
        # An arbitrary shell command can change files. Treat it conservatively
        # and record verification after the revision bump when it is a check.
        mutates_workspace=True,
        evidence_type="command",
    )


@dataclass
class _ManagedProcess:
    command: str
    process: asyncio.subprocess.Process
    stdout: deque[str] = field(default_factory=deque)
    stderr: deque[str] = field(default_factory=deque)
    output_chars: int = 0
    reader_tasks: list[asyncio.Task[None]] = field(default_factory=list)


class BackgroundProcessManager:
    """Owns long-running child processes for one Marcus CLI session."""

    def __init__(self, root: Path, *, max_output_chars: int = 50_000) -> None:
        self.root = root.resolve()
        self.max_output_chars = max_output_chars
        self._processes: dict[str, _ManagedProcess] = {}

    async def start(self, command: str) -> dict[str, Any]:
        metadata = inspect_shell_command(command)
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(self.root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_group_kwargs(),
        )
        process_id = uuid.uuid4().hex[:12]
        managed = _ManagedProcess(command=command, process=proc)
        self._processes[process_id] = managed
        assert proc.stdout is not None and proc.stderr is not None
        managed.reader_tasks = [
            asyncio.create_task(self._capture(managed, proc.stdout, managed.stdout)),
            asyncio.create_task(self._capture(managed, proc.stderr, managed.stderr)),
        ]
        await asyncio.sleep(0)
        return {
            "process_id": process_id,
            "pid": proc.pid,
            "command": command,
            "cwd": str(self.root),
            "status": self._status(proc),
            **metadata.as_dict(),
        }

    async def _capture(
        self,
        managed: _ManagedProcess,
        stream: asyncio.StreamReader,
        destination: deque[str],
    ) -> None:
        while chunk := await stream.readline():
            text = chunk.decode("utf-8", errors="replace")
            destination.append(text)
            managed.output_chars += len(text)
            while managed.output_chars > self.max_output_chars:
                candidates = [buffer for buffer in (managed.stdout, managed.stderr) if buffer]
                if not candidates:
                    break
                removed = candidates[0].popleft()
                managed.output_chars -= len(removed)

    def output(self, process_id: str) -> dict[str, Any]:
        canonical_id, managed = self._get(process_id)
        return {
            "process_id": canonical_id,
            "status": self._status(managed.process),
            "exit_code": managed.process.returncode,
            "stdout": "".join(managed.stdout),
            "stderr": "".join(managed.stderr),
        }

    async def stop(self, process_id: str) -> dict[str, Any]:
        canonical_id, managed = self._get(process_id)
        await terminate_process_tree(managed.process)
        await asyncio.gather(*managed.reader_tasks, return_exceptions=True)
        close_process_transport(managed.process)
        return {
            "process_id": canonical_id,
            "status": "stopped",
            "exit_code": managed.process.returncode,
        }

    async def aclose(self) -> None:
        await asyncio.gather(
            *(self.stop(process_id) for process_id in list(self._processes)),
            return_exceptions=True,
        )
        self._processes.clear()

    def _get(self, process_id: str) -> tuple[str, _ManagedProcess]:
        if process_id in self._processes:
            return process_id, self._processes[process_id]

        # Models sometimes repeat an identifier as the visible abbreviated
        # form (for example "4fe927..."). Accept it only when a sufficiently
        # long prefix identifies exactly one session-owned process.
        prefix = process_id.rstrip(".")
        matches = [key for key in self._processes if len(prefix) >= 6 and key.startswith(prefix)]
        if len(matches) == 1:
            canonical_id = matches[0]
            return canonical_id, self._processes[canonical_id]
        if len(matches) > 1:
            raise ValueError(f"ambiguous process_id prefix: {process_id}")
        raise ValueError(f"unknown process_id: {process_id}")

    @staticmethod
    def _status(proc: asyncio.subprocess.Process) -> str:
        return "running" if proc.returncode is None else "exited"


class MarcusTools(list[Tool]):
    def __init__(self, tools: list[Tool], process_manager: BackgroundProcessManager) -> None:
        super().__init__(tools)
        self.process_manager = process_manager

    async def aclose(self) -> None:
        await self.process_manager.aclose()


def build_background_process_tools(manager: BackgroundProcessManager) -> list[Tool]:
    async def start_handler(arguments: dict[str, Any]) -> dict[str, Any]:
        command = arguments.get("command")
        if not command:
            raise ValueError("start_process requires a 'command' field")
        return await manager.start(command)

    async def output_handler(arguments: dict[str, Any]) -> dict[str, Any]:
        process_id = arguments.get("process_id")
        if not process_id:
            raise ValueError("read_process_output requires a 'process_id' field")
        return manager.output(process_id)

    async def stop_handler(arguments: dict[str, Any]) -> dict[str, Any]:
        process_id = arguments.get("process_id")
        if not process_id:
            raise ValueError("stop_process requires a 'process_id' field")
        return await manager.stop(process_id)

    async def wait_handler(arguments: dict[str, Any]) -> dict[str, Any]:
        url = arguments.get("url")
        if not url:
            raise ValueError("wait_for_http requires a 'url' field")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("wait_for_http only permits localhost HTTP(S) URLs")
        timeout = min(max(float(arguments.get("timeout_seconds", 30)), 0.1), 120)
        deadline = asyncio.get_running_loop().time() + timeout
        last_error = "service not ready"
        async with httpx.AsyncClient(timeout=1.0) as client:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    response = await client.get(url)
                    if response.status_code < 500:
                        return {"url": url, "ready": True, "status": response.status_code}
                    last_error = f"HTTP {response.status_code}"
                except httpx.HTTPError as exc:
                    last_error = str(exc)
                await asyncio.sleep(0.1)
        raise ValueError(f"service was not ready after {timeout:g}s: {last_error}")

    process_id_schema = {
        "type": "object",
        "properties": {"process_id": {"type": "string"}},
        "required": ["process_id"],
    }
    return [
        Tool(
            name=START_PROCESS_TOOL_NAME,
            description="Start a long-running service in the background and return its process_id.",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            handler=start_handler,
            risk_tier=RiskTier.destructive,
            volatile=True,
        ),
        Tool(
            name=READ_PROCESS_OUTPUT_TOOL_NAME,
            description="Read current stdout, stderr, and status of a background process.",
            parameters=process_id_schema,
            handler=output_handler,
            risk_tier=RiskTier.read_only,
            idempotent=True,
            volatile=True,
        ),
        Tool(
            name=STOP_PROCESS_TOOL_NAME,
            description="Stop a background process and all of its descendants.",
            parameters=process_id_schema,
            handler=stop_handler,
            risk_tier=RiskTier.sensitive_write,
            idempotent=True,
            volatile=True,
        ),
        Tool(
            name=WAIT_FOR_HTTP_TOOL_NAME,
            description="Wait until a localhost HTTP service is ready to accept requests.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 120},
                },
                "required": ["url"],
            },
            handler=wait_handler,
            risk_tier=RiskTier.read_only,
            idempotent=True,
            evidence_type="http",
            volatile=True,
        ),
    ]


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
            "Fetch a web page or URL over HTTP(S) and return its text content "
            "(HTML tags stripped). Use for documentation, changelogs, or API docs."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The absolute http(s) URL to fetch."}
            },
            "required": ["url"],
        },
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


def build_marcus_tools(root: Path, settings: Settings) -> MarcusTools:
    from marcus_code.runtime.skills import build_load_skill_tool
    from marcus_code.tools.extra import build_marcus_extra_tools

    process_manager = BackgroundProcessManager(
        root, max_output_chars=settings.tools_run_cli_max_output_bytes
    )
    tools = [
        build_read_file_tool(root),
        build_write_file_tool(root),
        build_edit_file_tool(root),
        build_list_files_tool(root),
        build_grep_tool(root),
        build_run_cli_tool(root, settings),
        build_fetch_url_tool(settings),
        build_load_skill_tool(root),
    ]
    tools.extend(build_background_process_tools(process_manager))
    tools.extend(build_marcus_extra_tools(root, settings))
    return MarcusTools(tools, process_manager)
