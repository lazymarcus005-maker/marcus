"""Additional embedded tools for the Marcus CLI coding agent.

These tools complement marcus_code/tools.py with web search, git inspection,
code manipulation, testing, and memory capabilities.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import difflib
import json
import os
import re
import subprocess
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

SEARCH_WEB_TOOL_NAME = "search_web"
GIT_STATUS_TOOL_NAME = "git_status"
GIT_DIFF_TOOL_NAME = "git_diff"
GIT_LOG_TOOL_NAME = "git_log"
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


def _resolve_scoped_path(root: Path, relative_path: str) -> Path:
    return resolve_scoped_path(root, relative_path)


# ---------------------------------------------------------------------------
# Web search
# ---------------------------------------------------------------------------


def _web_search_duckduckgo(query: str, max_results: int) -> list[dict[str, str]]:
    """Best-effort DuckDuckGo HTML scrape without external dependencies."""
    url = "https://html.duckduckgo.com/html/"
    data = {"q": query}
    try:
        response = httpx.post(url, data=data, timeout=15, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"search request failed: {exc}") from exc

    text = response.text
    results: list[dict[str, str]] = []
    # DuckDuckGo HTML result blocks
    for block in re.split(r"<div class=\"result \"", text)[1:]:
        title_match = re.search(r"<a[^>]*class=\"result__a\"[^>]*>(.*?)</a>", block, re.S)
        href_match = re.search(r"<a[^>]*class=\"result__a\"[^>]*href=\"([^\"]+)\"", block)
        snippet_match = re.search(r"<a[^>]*class=\"result__snippet\"[^>]*>(.*?)</a>", block, re.S)
        if title_match and href_match:
            title = re.sub(r"<[^>]+>", "", title_match.group(1))
            href = href_match.group(1)
            snippet = re.sub(r"<[^>]+>", "", snippet_match.group(1)) if snippet_match else ""
            results.append({"title": title.strip(), "url": href, "snippet": snippet.strip()})
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
        return {
            "query": query,
            "results": results,
            "count": len(results),
        }

    return Tool(
        name=SEARCH_WEB_TOOL_NAME,
        description=(
            "Search the web with DuckDuckGo and return a list of result titles, "
            "URLs, and short snippets. Use when you need to discover documentation, "
            "articles, or current information rather than a known URL."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "Maximum results to return (default 5).",
                },
            },
            "required": ["query"],
        },
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


# ---------------------------------------------------------------------------
# Git tools
# ---------------------------------------------------------------------------


def _git_command(repo_root: Path, *args: str) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError as exc:
        raise RuntimeError("git executable not found") from exc


def _ensure_git_repo(repo_root: Path) -> None:
    returncode, _, _ = _git_command(repo_root, "rev-parse", "--git-dir")
    if returncode != 0:
        raise ValueError(f"not a git repository: {repo_root}")


def build_git_status_tool(root: Path) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        repo_root = _resolve_scoped_path(root, arguments.get("repo_path", "."))
        _ensure_git_repo(repo_root)
        returncode, stdout, stderr = _git_command(repo_root, "status", "--short", "--branch")
        if returncode != 0:
            raise RuntimeError(stderr or "git status failed")
        return {"repo_path": str(repo_root), "status": stdout.strip()}

    return Tool(
        name=GIT_STATUS_TOOL_NAME,
        description="Show git working tree status (short format with branch).",
        parameters={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to repository root (default: working directory).",
                }
            },
            "required": [],
        },
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


def build_git_diff_tool(root: Path) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        repo_root = _resolve_scoped_path(root, arguments.get("repo_path", "."))
        _ensure_git_repo(repo_root)
        target = arguments.get("target", "")
        args = ["diff", "--no-color"]
        if target:
            args.extend(["--", target])
        returncode, stdout, stderr = _git_command(repo_root, *args)
        if returncode != 0:
            raise RuntimeError(stderr or "git diff failed")
        max_bytes = 50_000
        return {
            "repo_path": str(repo_root),
            "target": target or "all",
            "diff": stdout[:max_bytes],
            "truncated": len(stdout) > max_bytes,
        }

    return Tool(
        name=GIT_DIFF_TOOL_NAME,
        description="Show git diff for the working tree or a specific path.",
        parameters={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to repository root (default: working directory).",
                },
                "target": {
                    "type": "string",
                    "description": "Specific file or directory to diff (default: all).",
                },
            },
            "required": [],
        },
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


def build_git_log_tool(root: Path) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        repo_root = _resolve_scoped_path(root, arguments.get("repo_path", "."))
        _ensure_git_repo(repo_root)
        count = max(1, min(int(arguments.get("count", 10)), 50))
        returncode, stdout, stderr = _git_command(
            repo_root, "log", f"-{count}", "--oneline", "--no-decorate"
        )
        if returncode != 0:
            raise RuntimeError(stderr or "git log failed")
        return {
            "repo_path": str(repo_root),
            "count": count,
            "log": stdout.strip().splitlines(),
        }

    return Tool(
        name=GIT_LOG_TOOL_NAME,
        description="Show recent git commits in one-line format.",
        parameters={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to repository root (default: working directory).",
                },
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Number of commits (default 10, max 50).",
                },
            },
            "required": [],
        },
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


# ---------------------------------------------------------------------------
# Apply diff
# ---------------------------------------------------------------------------


def _apply_unified_diff(root: Path, path: str, diff_text: str) -> dict[str, Any]:
    resolved = _resolve_scoped_path(root, path)
    if not resolved.is_file():
        raise ValueError(f"file not found: {path}")
    original = resolved.read_text(encoding="utf-8", errors="replace")
    # Use Python's difflib to apply patch lines.
    patched_lines = list(difflib.restore(diff_text.splitlines(), 2))
    if not patched_lines:
        # Fallback: try simple per-hunk replacement when restore doesn't work.
        patched = _apply_simple_hunks(original, diff_text)
    else:
        patched = "\n".join(patched_lines)
    result = atomic_write_text(resolved, patched)
    return {"path": path, **result}


def _apply_simple_hunks(original: str, diff_text: str) -> str:
    # This is intentionally simple: it looks for @@ context lines and replaces
    # the first matching block. For production use a proper patch library.
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


def build_apply_diff_tool(root: Path) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        path = arguments.get("path")
        diff = arguments.get("diff")
        if not path or diff is None:
            raise ValueError("apply_diff requires 'path' and 'diff' fields")
        return _apply_unified_diff(root, path, diff)

    return Tool(
        name=APPLY_DIFF_TOOL_NAME,
        description=(
            "Apply a unified diff to an existing file. Prefer edit_file for simple "
            "single replacements; use apply_diff for multi-hunk changes."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to working directory.",
                },
                "diff": {"type": "string", "description": "Unified diff to apply."},
            },
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


def build_read_directory_tree_tool(root: Path) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        target = arguments.get("path", ".")
        max_depth = max(1, min(int(arguments.get("max_depth", 4)), 8))
        resolved = _resolve_scoped_path(root, target)
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
                "path": {
                    "type": "string",
                    "description": "Directory path relative to working directory.",
                },
                "max_depth": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 8,
                    "description": "Max depth (default 4).",
                },
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
    if (root / "pytest.ini").is_file() or (root / "setup.py").is_file():
        return "pytest -q"
    if (root / "package.json").is_file():
        return "npm test"
    if (root / "Cargo.toml").is_file():
        return "cargo test"
    if (root / "go.mod").is_file():
        return "go test ./..."
    return None


def build_run_tests_tool(root: Path, settings: Settings) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        command = arguments.get("command")
        if not command:
            inferred = _infer_test_command(root)
            if not inferred:
                raise ValueError("could not infer test command; provide one explicitly")
            command = inferred
        timeout = max(0.1, min(float(arguments.get("timeout_seconds", 300)), 600))
        metadata = inspect_shell_command(command)

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(root.resolve()),
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
        description=(
            "Run the project test suite. Infers the command from project files "
            "(pyproject.toml, package.json, etc.) if not provided."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Test command (optional, inferred if omitted).",
                },
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 600,
                    "description": "Timeout (default 300, max 600).",
                },
            },
            "required": [],
        },
        handler=handler,
        risk_tier=RiskTier.destructive,
    )


# ---------------------------------------------------------------------------
# Execute Python
# ---------------------------------------------------------------------------


def build_execute_python_tool(root: Path) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        code = arguments.get("code")
        if not code:
            raise ValueError("execute_python requires a 'code' field")
        timeout = max(0.1, min(float(arguments.get("timeout_seconds", 30)), 120))

        # Write to a temporary file so we can use -I (isolated mode) safely.
        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", prefix="marcus_python_", delete=False, dir=str(root.resolve())
        ) as tmp:
            tmp.write(code)
            tmp_path = tmp.name
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                tmp_path,
                cwd=str(root.resolve()),
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
        description=(
            "Execute a snippet of Python code in an isolated subprocess. "
            "Use for calculations, data transformation, or quick prototyping."
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python source code to execute."},
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 120,
                    "description": "Timeout (default 30).",
                },
            },
            "required": ["code"],
        },
        handler=handler,
        risk_tier=RiskTier.destructive,
    )


# ---------------------------------------------------------------------------
# Memory (lightweight key-value store)
# ---------------------------------------------------------------------------


def _memory_dir(root: Path) -> Path:
    return root / ".marcus" / "memory"


def build_memory_read_tool(root: Path) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        key = arguments.get("key")
        if not key:
            raise ValueError("memory_read requires a 'key' field")
        if "/" in key or "\\" in key or ".." in key:
            raise ValueError("invalid memory key")
        memory_file = _memory_dir(root) / f"{key}.json"
        if not memory_file.is_file():
            return {"key": key, "exists": False, "value": None}
        try:
            value = json.loads(memory_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"failed to read memory: {exc}") from exc
        return {"key": key, "exists": True, "value": value}

    return Tool(
        name=MEMORY_READ_TOOL_NAME,
        description="Read a value from the project-local memory store by key.",
        parameters={
            "type": "object",
            "properties": {"key": {"type": "string", "description": "Memory key."}},
            "required": ["key"],
        },
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


def build_memory_write_tool(root: Path) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        key = arguments.get("key")
        value = arguments.get("value")
        if not key or value is None:
            raise ValueError("memory_write requires 'key' and 'value' fields")
        if "/" in key or "\\" in key or ".." in key:
            raise ValueError("invalid memory key")
        memory_dir = _memory_dir(root)
        memory_dir.mkdir(parents=True, exist_ok=True)
        memory_file = memory_dir / f"{key}.json"
        memory_file.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"key": key, "written": True}

    return Tool(
        name=MEMORY_WRITE_TOOL_NAME,
        description="Write a JSON-serializable value to the project-local memory store.",
        parameters={
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Memory key."},
                "value": {"type": "object", "description": "JSON value to store."},
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
        # Placeholder: real UI integration happens via MarcusLoop or server.
        # Returning the question/options lets the caller render the prompt.
        question = arguments.get("question")
        options = arguments.get("options")
        if not question or not options:
            raise ValueError("ask_user_choice requires 'question' and 'options' fields")
        if not isinstance(options, list) or len(options) < 2:
            raise ValueError("options must be a list with at least 2 items")
        return {
            "question": question,
            "options": options,
            "note": "waiting for user selection (render in UI)",
        }

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
    # Naive extractive summary: first sentence of each paragraph up to limit.
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
        description=(
            "Summarize a long text into a few sentences. Useful for condensing "
            "tool output before feeding it back to the model."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "max_sentences": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "Default 3.",
                },
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
        # Best-effort cross-platform process listing.
        if sys.platform == "win32":
            cmd = "tasklist /fo csv /nh"
        else:
            cmd = "ps -eo pid,comm,args"
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
            os.kill(int(pid), 9 if sys.platform != "win32" else 9)
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
        description=(
            "Check whether an HTTP(S) URL is reachable. Public URLs only; "
            "loopback is allowed. Uses HEAD by default."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {
                    "type": "string",
                    "enum": ["HEAD", "GET"],
                    "description": "Default HEAD.",
                },
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


def _todo_file(root: Path) -> Path:
    return root / ".marcus" / "todos.json"


def _load_todos(root: Path) -> list[dict[str, Any]]:
    path = _todo_file(root)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_todos(root: Path, todos: list[dict[str, Any]]) -> None:
    _todo_file(root).parent.mkdir(parents=True, exist_ok=True)
    _todo_file(root).write_text(json.dumps(todos, ensure_ascii=False, indent=2), encoding="utf-8")


def build_todo_create_tool(root: Path) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        title = arguments.get("title")
        if not title:
            raise ValueError("todo_create requires a 'title' field")
        todos = _load_todos(root)
        todo = {
            "id": uuid.uuid4().hex[:12],
            "title": title,
            "status": arguments.get("status", "pending"),
            "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        todos.append(todo)
        _save_todos(root, todos)
        return {"todo": todo}

    return Tool(
        name=TODO_CREATE_TOOL_NAME,
        description="Create a new todo item for the current session/project.",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress"],
                    "description": "Default pending.",
                },
            },
            "required": ["title"],
        },
        handler=handler,
        risk_tier=RiskTier.sensitive_write,
    )


def build_todo_update_tool(root: Path) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        todo_id = arguments.get("id")
        status = arguments.get("status")
        if not todo_id or not status:
            raise ValueError("todo_update requires 'id' and 'status' fields")
        todos = _load_todos(root)
        for todo in todos:
            if todo.get("id") == todo_id:
                todo["status"] = status
                todo["updated_at"] = datetime.datetime.now(datetime.UTC).isoformat()
                _save_todos(root, todos)
                return {"todo": todo}
        raise ValueError(f"todo not found: {todo_id}")

    return Tool(
        name=TODO_UPDATE_TOOL_NAME,
        description="Update the status of a todo item.",
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


def build_todo_list_tool(root: Path) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        status = arguments.get("status")
        todos = _load_todos(root)
        if status:
            todos = [t for t in todos if t.get("status") == status]
        return {"todos": todos, "count": len(todos)}

    return Tool(
        name=TODO_LIST_TOOL_NAME,
        description="List todo items for the current project.",
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


def build_compare_files_tool(root: Path) -> Tool:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        path_a = arguments.get("path_a")
        path_b = arguments.get("path_b")
        if not path_a or not path_b:
            raise ValueError("compare_files requires 'path_a' and 'path_b' fields")
        resolved_a = _resolve_scoped_path(root, path_a)
        resolved_b = _resolve_scoped_path(root, path_b)
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
        description="Compare two files and return a unified diff.",
        parameters={
            "type": "object",
            "properties": {
                "path_a": {"type": "string"},
                "path_b": {"type": "string"},
            },
            "required": ["path_a", "path_b"],
        },
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------


def build_marcus_extra_tools(root: Path, settings: Settings) -> list[Tool]:
    """Return all additional CLI tools defined in this module."""
    return [
        build_search_web_tool(),
        build_git_status_tool(root),
        build_git_diff_tool(root),
        build_git_log_tool(root),
        build_apply_diff_tool(root),
        build_read_directory_tree_tool(root),
        build_run_tests_tool(root, settings),
        build_execute_python_tool(root),
        build_memory_read_tool(root),
        build_memory_write_tool(root),
        build_ask_user_choice_tool(),
        build_summarize_text_tool(),
        build_list_processes_tool(),
        build_kill_process_tool(),
        build_check_url_health_tool(),
        build_todo_create_tool(root),
        build_todo_update_tool(root),
        build_todo_list_tool(root),
        build_compare_files_tool(root),
    ]
