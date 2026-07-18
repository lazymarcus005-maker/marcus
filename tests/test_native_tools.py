from unittest.mock import patch

import httpx
import pytest

from harness.config import Settings
from harness.runtime.native_tools import (
    build_builtin_tools,
    build_fetch_url_tool,
    build_read_file_tool,
    build_run_cli_tool,
    build_write_file_tool,
)


class _FakeResponse:
    def __init__(self, *, content: bytes, status_code: int = 200, headers=None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}
        self.encoding = "utf-8"
        self.url = "https://example.com/page"

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", self.url)
            raise httpx.HTTPStatusError("error", request=request, response=self)


class _FakeAsyncClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url):
        return self._response


@pytest.mark.asyncio
async def test_fetch_url_strips_html_and_returns_text():
    settings = Settings()
    body = b"<html><head><style>.x{}</style></head><body><h1>Hi</h1><p>World</p></body></html>"
    with (
        patch(
            "harness.runtime.native_tools.httpx.AsyncClient",
            return_value=_FakeAsyncClient(
                _FakeResponse(content=body, headers={"content-type": "text/html"})
            ),
        ),
        patch(
            "harness.runtime.url_validation.socket.getaddrinfo",
            return_value=[(2, 1, 0, "", ("93.184.216.34", 443))],
        ),
    ):
        tool = build_fetch_url_tool(settings)
        result = await tool.handler({"url": "https://example.com/page"})

    assert result["status"] == 200
    assert "Hi" in result["text"]
    assert "World" in result["text"]
    assert "<h1>" not in result["text"]
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_fetch_url_rejects_non_http_scheme():
    settings = Settings()
    tool = build_fetch_url_tool(settings)
    with pytest.raises(ValueError, match="http"):
        await tool.handler({"url": "file:///etc/passwd"})


@pytest.mark.asyncio
async def test_fetch_url_truncates_large_content():
    settings = Settings(tools_fetch_url_max_bytes=10)
    with (
        patch(
            "harness.runtime.native_tools.httpx.AsyncClient",
            return_value=_FakeAsyncClient(
                _FakeResponse(content=b"0123456789ABCDEF", headers={"content-type": "text/plain"})
            ),
        ),
        patch(
            "harness.runtime.url_validation.socket.getaddrinfo",
            return_value=[(2, 1, 0, "", ("93.184.216.34", 443))],
        ),
    ):
        tool = build_fetch_url_tool(settings)
        result = await tool.handler({"url": "https://example.com/page"})

    assert result["truncated"] is True
    assert len(result["text"]) == 10


@pytest.mark.asyncio
async def test_read_file_reads_existing_file(tmp_path):
    (tmp_path / "notes.txt").write_text("hello world", encoding="utf-8")
    settings = Settings(tools_fs_root=str(tmp_path))
    tool = build_read_file_tool(settings)

    result = await tool.handler({"path": "notes.txt"})

    assert result["content"] == "hello world"
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_read_file_rejects_path_traversal(tmp_path):
    settings = Settings(tools_fs_root=str(tmp_path))
    tool = build_read_file_tool(settings)

    with pytest.raises(ValueError, match="escapes"):
        await tool.handler({"path": "../outside.txt"})


@pytest.mark.asyncio
async def test_read_file_missing_file_raises(tmp_path):
    settings = Settings(tools_fs_root=str(tmp_path))
    tool = build_read_file_tool(settings)

    with pytest.raises(ValueError, match="not found"):
        await tool.handler({"path": "does_not_exist.txt"})


@pytest.mark.asyncio
async def test_write_file_creates_file_and_parent_dirs(tmp_path):
    settings = Settings(tools_fs_root=str(tmp_path))
    tool = build_write_file_tool(settings)

    result = await tool.handler({"path": "nested/dir/out.txt", "content": "written"})

    assert result["bytes_written"] == len(b"written")
    assert (tmp_path / "nested" / "dir" / "out.txt").read_text(encoding="utf-8") == "written"


@pytest.mark.asyncio
async def test_write_file_rejects_path_traversal(tmp_path):
    settings = Settings(tools_fs_root=str(tmp_path))
    tool = build_write_file_tool(settings)

    with pytest.raises(ValueError, match="escapes"):
        await tool.handler({"path": "../escape.txt", "content": "x"})

    assert not (tmp_path.parent / "escape.txt").exists()


@pytest.mark.asyncio
async def test_run_cli_executes_command_and_captures_output(tmp_path):
    settings = Settings(tools_fs_root=str(tmp_path), tools_run_cli_enabled=True)
    tool = build_run_cli_tool(settings)

    result = await tool.handler({"command": "echo hello"})

    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]


@pytest.mark.asyncio
async def test_run_cli_disabled_raises(tmp_path):
    settings = Settings(tools_fs_root=str(tmp_path), tools_run_cli_enabled=False)
    tool = build_run_cli_tool(settings)

    with pytest.raises(ValueError, match="disabled"):
        await tool.handler({"command": "echo hello"})


@pytest.mark.asyncio
async def test_run_cli_times_out(tmp_path):
    settings = Settings(
        tools_fs_root=str(tmp_path), tools_run_cli_enabled=True, tools_run_cli_timeout_seconds=0.01
    )
    tool = build_run_cli_tool(settings)
    sleep_command = 'python -c "import time; time.sleep(2)"'

    with pytest.raises(ValueError, match="timed out"):
        await tool.handler({"command": sleep_command})


def test_build_builtin_tools_excludes_run_cli_when_disabled(tmp_path):
    settings = Settings(tools_fs_root=str(tmp_path), tools_run_cli_enabled=False)
    names = {t.name for t in build_builtin_tools(settings)}
    assert {"fetch_url", "read_file", "write_file"}.issubset(names)
    assert "run_cli" not in names


def test_build_builtin_tools_includes_run_cli_when_enabled(tmp_path):
    settings = Settings(tools_fs_root=str(tmp_path), tools_run_cli_enabled=True)
    names = {t.name for t in build_builtin_tools(settings)}
    assert {"fetch_url", "read_file", "write_file", "run_cli"}.issubset(names)
