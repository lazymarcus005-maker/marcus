"""Additional native tools for the Harness server agent runtime.

These mirror the extra CLI capabilities where appropriate and add server-side
code-editing tools.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import difflib
import json
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from harness.config import Settings
from harness.db.enums import RiskTier
from harness.runtime.command_policy import inspect_shell_command
from harness.runtime.file_writes import atomic_write_text
from harness.runtime.path_utils import resolve_scoped_path
from harness.runtime.processes import process_group_kwargs
from harness.runtime.tools import Tool
from harness.runtime.url_validation import validate_public_url


def _resolve_sandboxed_path(root: str, relative_path: str) -> Path:
    return resolve_scoped_path(Path(root), relative_path)


SEARCH_WEB_TOOL_NAME = "search_web"
EDIT_FILE_TOOL_NAME = "edit_file"
APPLY_DIFF_TOOL_NAME = "apply_diff"
READ_DIRECTORY_TREE_TOOL_NAME = "read_directory_tree"
RUN_TESTS_TOOL_NAME = "run_tests"
EXECUTE_PYTHON_TOOL_NAME = "execute_python"
MEMORY_READ_TOOL_NAME = "memory_read"
MEMORY_WRITE_TOOL_NAME = "memory_write"
ASK_USER_CHOICE_TOOL_NAME = "ask_user_choice"
SUMMARIZE_TEXT_TOOL_NAME = "summarize_text"
LIST_PROCESSES_TOOL_NAME = "list_processes"
KILL_PROCESS_TOOL_NAME = "kill_process"
CHECK_URL_HEALTH_TOOL_NAME = "check_url_health"
TODO_CREATE_TOOL_NAME = "todo_create"
TODO_UPDATE_TOOL_NAME = "todo_update"
TODO_LIST_TOOL_NAME = "todo_list"
COMPARE_FILES_TOOL_NAME = "compare_files"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _memory_dir(root: str) -> Path:
    return Path(root).resolve() / ".marcus" / "memory"


def _todo_file(root: str) -> Path:
    return Path(root).resolve() / ".marcus" / "todos.json"


# ---------------------------------------------------------------------------
# Web search
# ---------------------------------------------------------------------------


def _web_search_duckduckgo(query: str, max_results: int) -> list[dict[str, str]]:
    url = "https://html.duckduckgo.com/html/"
    try:
        response = httpx.post(url, data={"q": query}, timeout=15, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"search request failed: {exc}") from exc

    text = response.text
    results: list[dict[str, str]] = []
    for block in re.split(r"<div class=\"result \"", text)[1:]:
        title_match = re.search(r"<a[^>]*class=\"result__a\"[^>]*>(.*?)</a>", block, re.S)
        href_match = re.search(r"<a[^>]*class=\"result__a\"[^>]*href=\"([^\"]+)\"", block)
        snippet_match = re.search(r"<a[^>]*class=\"result__snippet\"[^>]*>(.*?)</a>", block, re.S)
        if title_match and href_match:
            title = re.sub(r"<[^>]+>", "", title_match.group(1))
            snippet = re.sub(r"<[^>]+>", "", snippet_match.group(1)) if snippet_match else ""
            results.append(
                {"title": title.strip(), "url": href_match.group(1), "snippet": snippet.strip()}
            )
        if len(results) >= max_results:
            break
    return results


def build_search_web_tool() -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        query = arguments.get("query")
        if not query:
            raise ValueError("search_web requires a 'query' field")
        max_results = max(1, min(int(arguments.get("max_results", 5)), 10))
        results = await asyncio.to_thread(_web_search_duckduckgo, query, max_results)
        return {"query": query, "results": results, "count": len(results)}

    return Tool(
        name=SEARCH_WEB_TOOL_NAME,
        description="Search the web with DuckDuckGo and return result titles, URLs, and snippets.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
        },
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


# ---------------------------------------------------------------------------
# Edit file (server sandboxed)
# ---------------------------------------------------------------------------


def build_edit_file_tool(settings: Settings) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        path = arguments.get("path")
        old_string = arguments.get("old_string")
        new_string = arguments.get("new_string")
        if not path or old_string is None or new_string is None:
            raise ValueError("edit_file requires 'path', 'old_string', and 'new_string' fields")
        resolved = _resolve_sandboxed_path(settings.tools_fs_root, path)
        if not resolved.is_file():
            raise ValueError(f"file not found: {path}")
        original = resolved.read_text(encoding="utf-8", errors="replace")
        occurrences = original.count(old_string)
        if occurrences == 0:
            raise ValueError("old_string not found in file")
        if occurrences > 1:
            raise ValueError(f"old_string is not unique ({occurrences} occurrences)")
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
            "file inside the sandboxed tools directory. Requires human approval."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        handler=handler,
        risk_tier=RiskTier.sensitive_write,
    )


# ---------------------------------------------------------------------------
# Apply diff (server sandboxed)
# ---------------------------------------------------------------------------


def _apply_simple_hunks(original: str, diff_text: str) -> str:
    hunks = re.split(r"(?=^@@ .*? @@)", diff_text, flags=re.M)
    for hunk in hunks:
        if not hunk.strip() or not hunk.startswith("@@"):
            continue
        body = re.sub(r"^@@ .*? @@\n?", "", hunk, flags=re.M)
        old_block: list[str] = []
        new_block: list[str] = []
        for raw_line in body.splitlines(keepends=True):
            if not raw_line:
                continue
            if raw_line.startswith("-"):
                old_block.append(raw_line[1:])
            elif raw_line.startswith("+"):
                new_block.append(raw_line[1:])
            elif raw_line.startswith(" "):
                old_block.append(raw_line[1:])
                new_block.append(raw_line[1:])
        if not old_block:
            continue
        old_joined = "".join(old_block)
        if old_joined in original:
            original = original.replace(old_joined, "".join(new_block), 1)
    return original


def build_apply_diff_tool(settings: Settings) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        path = arguments.get("path")
        diff = arguments.get("diff")
        if not path or diff is None:
            raise ValueError("apply_diff requires 'path' and 'diff' fields")
        resolved = _resolve_sandboxed_path(settings.tools_fs_root, path)
        if not resolved.is_file():
            raise ValueError(f"file not found: {path}")
        original = resolved.read_text(encoding="utf-8", errors="replace")
        patched_lines = list(difflib.restore(diff.splitlines(), 2))
        patched = "\n".join(patched_lines) if patched_lines else _apply_simple_hunks(original, diff)
        return {"path": path, **atomic_write_text(resolved, patched)}

    return Tool(
        name=APPLY_DIFF_TOOL_NAME,
        description="Apply a unified diff to a sandboxed file. Requires human approval.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "diff": {"type": "string"}},
            "required": ["path", "diff"],
        },
        handler=handler,
        risk_tier=RiskTier.sensitive_write,
    )


# ---------------------------------------------------------------------------
# Directory tree
# ---------------------------------------------------------------------------


def _build_tree(path: Path, base: Path, max_depth: int, current_depth: int = 0) -> dict[str, Any]:
    rel = path.relative_to(base)
    entry: dict[str, Any] = {"name": path.name, "path": str(rel)}
    if path.is_file():
        entry["type"] = "file"
        return entry
    entry["type"] = "directory"
    if current_depth >= max_depth:
        entry["children"] = []
        return entry
    children: list[dict[str, Any]] = []
    try:
        for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name in {
                ".git",
                "__pycache__",
                "node_modules",
                ".venv",
                "venv",
                ".mypy_cache",
                ".ruff_cache",
                ".pytest_cache",
            }:
                continue
            children.append(_build_tree(child, base, max_depth, current_depth + 1))
    except PermissionError:
        pass
    entry["children"] = children
    return entry


def build_read_directory_tree_tool(settings: Settings) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        target = arguments.get("path", ".")
        max_depth = max(1, min(int(arguments.get("max_depth", 4)), 8))
        resolved = _resolve_sandboxed_path(settings.tools_fs_root, target)
        if not resolved.is_dir():
            raise ValueError(f"directory not found: {target}")
        tree = _build_tree(resolved, resolved, max_depth)
        return {"root": str(tree.get("path", target)), "max_depth": max_depth, "tree": tree}

    return Tool(
        name=READ_DIRECTORY_TREE_TOOL_NAME,
        description="Return a nested directory tree as JSON, skipping noise directories.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_depth": {"type": "integer", "minimum": 1, "maximum": 8},
            },
            "required": [],
        },
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


# ---------------------------------------------------------------------------
# Run tests wrapper
# ---------------------------------------------------------------------------


def _infer_test_command(root: Path) -> str | None:
    if (root / "pyproject.toml").is_file():
        return "uv run pytest -q"
    if (root / "package.json").is_file():
        return "npm test"
    if (root / "Cargo.toml").is_file():
        return "cargo test"
    if (root / "go.mod").is_file():
        return "go test ./..."
    return None


def build_run_tests_tool(settings: Settings) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        root = Path(settings.tools_fs_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        command = arguments.get("command")
        if not command:
            inferred = _infer_test_command(root)
            if not inferred:
                raise ValueError("could not infer test command")
            command = inferred
        timeout = max(0.1, min(float(arguments.get("timeout_seconds", 300)), 600))
        metadata = inspect_shell_command(command)

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_group_kwargs(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError as exc:
            raise ValueError(f"tests timed out after {timeout:g}s") from exc

        max_bytes = settings.tools_run_cli_max_output_bytes
        return {
            "command": command,
            "exit_code": proc.returncode,
            "stdout": stdout[:max_bytes].decode("utf-8", errors="replace"),
            "stderr": stderr[:max_bytes].decode("utf-8", errors="replace"),
            **metadata.as_dict(),
            "timeout_seconds": timeout,
        }

    return Tool(
        name=RUN_TESTS_TOOL_NAME,
        description="Run the project test suite, inferring the command from project files if needed.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 600},
            },
            "required": [],
        },
        handler=handler,
        risk_tier=RiskTier.destructive,
    )


# ---------------------------------------------------------------------------
# Execute Python
# ---------------------------------------------------------------------------


def build_execute_python_tool(settings: Settings) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        code = arguments.get("code")
        if not code:
            raise ValueError("execute_python requires a 'code' field")
        timeout = max(0.1, min(float(arguments.get("timeout_seconds", 30)), 120))
        root = Path(settings.tools_fs_root).resolve()
        root.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", prefix="marcus_python_", delete=False, dir=str(root)
        ) as tmp:
            tmp.write(str(code))
            tmp_path = tmp.name
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                tmp_path,
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError as exc:
                raise ValueError(f"python execution timed out after {timeout:g}s") from exc
            return {
                "exit_code": proc.returncode,
                "stdout": stdout[:50_000].decode("utf-8", errors="replace"),
                "stderr": stderr[:50_000].decode("utf-8", errors="replace"),
            }
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)

    return Tool(
        name=EXECUTE_PYTHON_TOOL_NAME,
        description="Execute a Python snippet in an isolated subprocess inside the sandbox.",
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 120},
            },
            "required": ["code"],
        },
        handler=handler,
        risk_tier=RiskTier.destructive,
    )


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


def build_memory_read_tool(settings: Settings) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        key = arguments.get("key")
        if not key:
            raise ValueError("memory_read requires a 'key' field")
        if "/" in key or "\\" in key or ".." in key:
            raise ValueError("invalid memory key")
        memory_file = _memory_dir(settings.tools_fs_root) / f"{key}.json"
        if not memory_file.is_file():
            return {"key": key, "exists": False, "value": None}
        try:
            value = json.loads(memory_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"failed to read memory: {exc}") from exc
        return {"key": key, "exists": True, "value": value}

    return Tool(
        name=MEMORY_READ_TOOL_NAME,
        description="Read a value from the sandbox-local memory store by key.",
        parameters={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


def build_memory_write_tool(settings: Settings) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        key = arguments.get("key")
        value = arguments.get("value")
        if not key or value is None:
            raise ValueError("memory_write requires 'key' and 'value' fields")
        if "/" in key or "\\" in key or ".." in key:
            raise ValueError("invalid memory key")
        memory_dir = _memory_dir(settings.tools_fs_root)
        memory_dir.mkdir(parents=True, exist_ok=True)
        memory_file = memory_dir / f"{key}.json"
        memory_file.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"key": key, "written": True}

    return Tool(
        name=MEMORY_WRITE_TOOL_NAME,
        description="Write a JSON-serializable value to the sandbox-local memory store.",
        parameters={
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"type": "object"},
            },
            "required": ["key", "value"],
        },
        handler=handler,
        risk_tier=RiskTier.sensitive_write,
    )


# ---------------------------------------------------------------------------
# Ask user choice
# ---------------------------------------------------------------------------


def build_ask_user_choice_tool() -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        question = arguments.get("question")
        options = arguments.get("options")
        if not question or not options:
            raise ValueError("ask_user_choice requires 'question' and 'options' fields")
        if not isinstance(options, list) or len(options) < 2:
            raise ValueError("options must be a list with at least 2 items")
        return {"question": question, "options": options, "note": "waiting for user selection"}

    return Tool(
        name=ASK_USER_CHOICE_TOOL_NAME,
        description="Ask the user to choose one option from a list.",
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "string"}, "minItems": 2},
            },
            "required": ["question", "options"],
        },
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


# ---------------------------------------------------------------------------
# Summarize text
# ---------------------------------------------------------------------------


def _simple_summarize(text: str, max_sentences: int) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text.replace("\n", " ").strip())
    return " ".join(sentences[:max_sentences])


def build_summarize_text_tool() -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        text = arguments.get("text")
        if not text:
            raise ValueError("summarize_text requires a 'text' field")
        max_sentences = max(1, min(int(arguments.get("max_sentences", 3)), 20))
        summary = _simple_summarize(str(text), max_sentences)
        return {"summary": summary, "max_sentences": max_sentences, "original_length": len(text)}

    return Tool(
        name=SUMMARIZE_TEXT_TOOL_NAME,
        description="Summarize a long text into a few sentences.",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "max_sentences": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["text"],
        },
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------


def build_list_processes_tool() -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        cmd = "tasklist /fo csv /nh" if sys.platform == "win32" else "ps -eo pid,comm,args"
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = stdout[:50_000].decode("utf-8", errors="replace")
        return {"processes": output.splitlines()[:100]}

    return Tool(
        name=LIST_PROCESSES_TOOL_NAME,
        description="List running OS processes (best-effort, truncated).",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


def build_kill_process_tool() -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        pid = arguments.get("pid")
        if pid is None:
            raise ValueError("kill_process requires a 'pid' field")
        try:
            os.kill(int(pid), 9)
        except ProcessLookupError:
            return {"pid": pid, "killed": False, "reason": "process not found"}
        except OSError as exc:
            raise RuntimeError(f"failed to kill process: {exc}") from exc
        return {"pid": pid, "killed": True}

    return Tool(
        name=KILL_PROCESS_TOOL_NAME,
        description="Kill a process by PID.",
        parameters={
            "type": "object",
            "properties": {"pid": {"type": "integer"}},
            "required": ["pid"],
        },
        handler=handler,
        risk_tier=RiskTier.destructive,
    )


# ---------------------------------------------------------------------------
# URL health check
# ---------------------------------------------------------------------------


def build_check_url_health_tool() -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        url = arguments.get("url")
        if not url:
            raise ValueError("check_url_health requires a 'url' field")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("only http(s) URLs supported")
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            validate_public_url(url)
        timeout = max(0.1, min(float(arguments.get("timeout_seconds", 10)), 60))
        method = arguments.get("method", "HEAD").upper()
        if method not in {"GET", "HEAD"}:
            raise ValueError("method must be GET or HEAD")

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            try:
                response = await client.request(method, url)
                return {
                    "url": str(response.url),
                    "status": response.status_code,
                    "healthy": response.status_code < 500,
                    "response_time_ms": int(response.elapsed.total_seconds() * 1000),
                }
            except httpx.HTTPError as exc:
                return {"url": url, "status": None, "healthy": False, "error": str(exc)}

    return Tool(
        name=CHECK_URL_HEALTH_TOOL_NAME,
        description="Check whether an HTTP(S) URL is reachable. Public URLs only; loopback allowed.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "enum": ["HEAD", "GET"]},
                "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 60},
            },
            "required": ["url"],
        },
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


# ---------------------------------------------------------------------------
# Todo management
# ---------------------------------------------------------------------------


def _load_todos(root: str) -> list[dict[str, Any]]:
    path = _todo_file(root)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_todos(root: str, todos: list[dict[str, Any]]) -> None:
    path = _todo_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(todos, ensure_ascii=False, indent=2), encoding="utf-8")


def build_todo_create_tool(settings: Settings) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        title = arguments.get("title")
        if not title:
            raise ValueError("todo_create requires a 'title' field")
        todos = _load_todos(settings.tools_fs_root)
        todo = {
            "id": uuid.uuid4().hex[:12],
            "title": title,
            "status": arguments.get("status", "pending"),
            "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        todos.append(todo)
        _save_todos(settings.tools_fs_root, todos)
        return {"todo": todo}

    return Tool(
        name=TODO_CREATE_TOOL_NAME,
        description="Create a new todo item in the sandbox-local todo list.",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "in_progress"]},
            },
            "required": ["title"],
        },
        handler=handler,
        risk_tier=RiskTier.sensitive_write,
    )


def build_todo_update_tool(settings: Settings) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        todo_id = arguments.get("id")
        status = arguments.get("status")
        if not todo_id or not status:
            raise ValueError("todo_update requires 'id' and 'status' fields")
        todos = _load_todos(settings.tools_fs_root)
        for todo in todos:
            if todo.get("id") == todo_id:
                todo["status"] = status
                todo["updated_at"] = datetime.datetime.now(datetime.UTC).isoformat()
                _save_todos(settings.tools_fs_root, todos)
                return {"todo": todo}
        raise ValueError(f"todo not found: {todo_id}")

    return Tool(
        name=TODO_UPDATE_TOOL_NAME,
        description="Update the status of a sandbox-local todo item.",
        parameters={
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "done", "cancelled"],
                },
            },
            "required": ["id", "status"],
        },
        handler=handler,
        risk_tier=RiskTier.sensitive_write,
    )


def build_todo_list_tool(settings: Settings) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        status = arguments.get("status")
        todos = _load_todos(settings.tools_fs_root)
        if status:
            todos = [t for t in todos if t.get("status") == status]
        return {"todos": todos, "count": len(todos)}

    return Tool(
        name=TODO_LIST_TOOL_NAME,
        description="List sandbox-local todo items.",
        parameters={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "done", "cancelled"],
                }
            },
            "required": [],
        },
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


# ---------------------------------------------------------------------------
# Compare files
# ---------------------------------------------------------------------------


def build_compare_files_tool(settings: Settings) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        path_a = arguments.get("path_a")
        path_b = arguments.get("path_b")
        if not path_a or not path_b:
            raise ValueError("compare_files requires 'path_a' and 'path_b' fields")
        resolved_a = _resolve_sandboxed_path(settings.tools_fs_root, path_a)
        resolved_b = _resolve_sandboxed_path(settings.tools_fs_root, path_b)
        if not resolved_a.is_file():
            raise ValueError(f"file not found: {path_a}")
        if not resolved_b.is_file():
            raise ValueError(f"file not found: {path_b}")
        text_a = resolved_a.read_text(encoding="utf-8", errors="replace").splitlines()
        text_b = resolved_b.read_text(encoding="utf-8", errors="replace").splitlines()
        diff = list(
            difflib.unified_diff(text_a, text_b, fromfile=path_a, tofile=path_b, lineterm="")
        )
        return {
            "path_a": path_a,
            "path_b": path_b,
            "identical": text_a == text_b,
            "diff": "\n".join(diff),
            "lines_a": len(text_a),
            "lines_b": len(text_b),
        }

    return Tool(
        name=COMPARE_FILES_TOOL_NAME,
        description="Compare two sandboxed files and return a unified diff.",
        parameters={
            "type": "object",
            "properties": {"path_a": {"type": "string"}, "path_b": {"type": "string"}},
            "required": ["path_a", "path_b"],
        },
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------


def build_native_extra_tools(settings: Settings) -> list[Tool]:
    """Return all additional server native tools."""
    return [
        build_search_web_tool(),
        build_edit_file_tool(settings),
        build_apply_diff_tool(settings),
        build_read_directory_tree_tool(settings),
        build_run_tests_tool(settings),
        build_execute_python_tool(settings),
        build_memory_read_tool(settings),
        build_memory_write_tool(settings),
        build_ask_user_choice_tool(),
        build_summarize_text_tool(),
        build_list_processes_tool(),
        build_kill_process_tool(),
        build_check_url_health_tool(),
        build_todo_create_tool(settings),
        build_todo_update_tool(settings),
        build_todo_list_tool(settings),
        build_compare_files_tool(settings),
    ]
