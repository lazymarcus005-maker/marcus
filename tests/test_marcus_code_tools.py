import asyncio
import socket
import sys
from unittest.mock import patch

import httpx
import pytest

from harness.config import Settings
from marcus_code.tools.base import (
    BackgroundProcessManager,
    build_background_process_tools,
    build_edit_file_tool,
    build_fetch_url_tool,
    build_grep_tool,
    build_list_files_tool,
    build_read_file_tool,
    build_run_cli_tool,
    build_write_file_tool,
)


@pytest.mark.asyncio
async def test_read_file_reads_existing_file(tmp_path):
    (tmp_path / "notes.txt").write_text("hello world", encoding="utf-8")
    tool = build_read_file_tool(tmp_path)

    result = await tool.handler({"path": "notes.txt"})

    assert result["content"] == "hello world"


@pytest.mark.asyncio
async def test_read_file_rejects_path_traversal(tmp_path):
    tool = build_read_file_tool(tmp_path)

    with pytest.raises(ValueError, match="escapes"):
        await tool.handler({"path": "../outside.txt"})


@pytest.mark.asyncio
async def test_read_file_missing_file_raises(tmp_path):
    tool = build_read_file_tool(tmp_path)

    with pytest.raises(ValueError, match="not found"):
        await tool.handler({"path": "missing.txt"})


@pytest.mark.asyncio
async def test_read_file_redacts_sensitive_filename(tmp_path):
    (tmp_path / ".env").write_text("API_KEY=super-secret", encoding="utf-8")
    result = await build_read_file_tool(tmp_path).handler({"path": ".env"})
    assert result["redacted"] is True
    assert "super-secret" not in result["content"]


@pytest.mark.asyncio
async def test_read_file_redacts_credential_patterns(tmp_path):
    (tmp_path / "config.txt").write_text("token: abc123\nname: marcus", encoding="utf-8")
    result = await build_read_file_tool(tmp_path).handler({"path": "config.txt"})
    assert "abc123" not in result["content"]
    assert "name: marcus" in result["content"]


@pytest.mark.asyncio
async def test_write_file_creates_file_and_parent_dirs(tmp_path):
    tool = build_write_file_tool(tmp_path)

    result = await tool.handler({"path": "nested/out.txt", "content": "written"})

    assert result["bytes_written"] == len(b"written")
    assert (tmp_path / "nested" / "out.txt").read_text(encoding="utf-8") == "written"


@pytest.mark.asyncio
async def test_write_file_rejects_path_traversal(tmp_path):
    tool = build_write_file_tool(tmp_path)

    with pytest.raises(ValueError, match="escapes"):
        await tool.handler({"path": "../escape.txt", "content": "x"})

    assert not (tmp_path.parent / "escape.txt").exists()


@pytest.mark.asyncio
async def test_edit_file_replaces_unique_occurrence(tmp_path):
    (tmp_path / "app.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    tool = build_edit_file_tool(tmp_path)

    await tool.handler({"path": "app.py", "old_string": "return 1", "new_string": "return 2"})

    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "def foo():\n    return 2\n"


@pytest.mark.asyncio
async def test_edit_file_rejects_missing_old_string(tmp_path):
    (tmp_path / "app.py").write_text("def foo(): pass\n", encoding="utf-8")
    tool = build_edit_file_tool(tmp_path)

    with pytest.raises(ValueError, match="not found"):
        await tool.handler({"path": "app.py", "old_string": "bar", "new_string": "baz"})


@pytest.mark.asyncio
async def test_edit_file_rejects_non_unique_old_string(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    tool = build_edit_file_tool(tmp_path)

    with pytest.raises(ValueError, match="not unique"):
        await tool.handler({"path": "app.py", "old_string": "x = 1", "new_string": "x = 2"})


@pytest.mark.asyncio
async def test_edit_file_rejects_path_traversal(tmp_path):
    tool = build_edit_file_tool(tmp_path)

    with pytest.raises(ValueError, match="escapes"):
        await tool.handler({"path": "../app.py", "old_string": "a", "new_string": "b"})


@pytest.mark.asyncio
async def test_list_files_finds_matching_files(tmp_path):
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "b.txt").write_text("", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.py").write_text("", encoding="utf-8")

    tool = build_list_files_tool(tmp_path)
    result = await tool.handler({"pattern": "**/*.py"})

    assert set(result["files"]) == {"a.py", "sub\\c.py"} or set(result["files"]) == {
        "a.py",
        "sub/c.py",
    }


@pytest.mark.asyncio
async def test_list_files_skips_noise_directories(tmp_path):
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "cache.pyc").write_text("", encoding="utf-8")
    (tmp_path / "real.py").write_text("", encoding="utf-8")

    tool = build_list_files_tool(tmp_path)
    result = await tool.handler({"pattern": "**/*"})

    assert result["files"] == ["real.py"]


@pytest.mark.asyncio
async def test_grep_finds_matching_lines(tmp_path):
    (tmp_path / "app.py").write_text("def foo():\n    raise ValueError('x')\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("print('hello')\n", encoding="utf-8")

    tool = build_grep_tool(tmp_path)
    result = await tool.handler({"pattern": "ValueError"})

    assert len(result["matches"]) == 1
    assert result["matches"][0]["path"] == "app.py"
    assert result["matches"][0]["line"] == 2


@pytest.mark.asyncio
async def test_grep_rejects_invalid_regex(tmp_path):
    tool = build_grep_tool(tmp_path)

    with pytest.raises(ValueError, match="invalid regex"):
        await tool.handler({"pattern": "("})


@pytest.mark.asyncio
async def test_run_cli_executes_command_in_working_directory(tmp_path):
    settings = Settings(tools_run_cli_timeout_seconds=10)
    tool = build_run_cli_tool(tmp_path, settings)

    result = await tool.handler({"command": "echo hello"})

    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]
    assert result["cwd"] == str(tmp_path.resolve())


@pytest.mark.asyncio
async def test_run_cli_times_out(tmp_path):
    settings = Settings(tools_run_cli_timeout_seconds=0.01)
    tool = build_run_cli_tool(tmp_path, settings)
    sleep_command = 'python -c "import time; time.sleep(2)"'

    with pytest.raises(ValueError, match="timed out"):
        await tool.handler({"command": sleep_command})


@pytest.mark.asyncio
async def test_run_cli_timeout_returns_promptly_when_child_inherits_pipes(tmp_path):
    settings = Settings(tools_run_cli_timeout_seconds=0.05)
    tool = build_run_cli_tool(tmp_path, settings)
    command = (
        'python -c "import subprocess,sys,time; '
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        'time.sleep(30)"'
    )

    with pytest.raises(ValueError, match="timed out"):
        await asyncio.wait_for(tool.handler({"command": command}), timeout=5)


def _unused_local_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.asyncio
async def test_background_service_start_wait_read_and_stop(tmp_path):
    port = _unused_local_port()
    manager = BackgroundProcessManager(tmp_path)
    tools = {tool.name: tool for tool in build_background_process_tools(manager)}
    run_cli = build_run_cli_tool(tmp_path, Settings(tools_run_cli_timeout_seconds=5))
    command = f'"{sys.executable}" -u -m http.server {port} --bind 127.0.0.1'

    started = await tools["start_process"].handler({"command": command})
    process_id = started["process_id"]
    try:
        ready = await tools["wait_for_http"].handler(
            {"url": f"http://127.0.0.1:{port}/", "timeout_seconds": 5}
        )
        client_result = await run_cli.handler(
            {
                "command": (
                    f'"{sys.executable}" -c "import urllib.request; '
                    f"print(urllib.request.urlopen('http://127.0.0.1:{port}/').status)\""
                )
            }
        )
        output = await tools["read_process_output"].handler({"process_id": process_id})

        assert ready == {
            "url": f"http://127.0.0.1:{port}/",
            "ready": True,
            "status": 200,
        }
        assert client_result["exit_code"] == 0
        assert client_result["stdout"].strip() == "200"
        assert output["status"] == "running"
    finally:
        stopped = await tools["stop_process"].handler({"process_id": process_id})
        await manager.aclose()

    assert stopped["status"] == "stopped"


@pytest.mark.asyncio
async def test_wait_for_http_rejects_non_local_url(tmp_path):
    manager = BackgroundProcessManager(tmp_path)
    tools = {tool.name: tool for tool in build_background_process_tools(manager)}

    with pytest.raises(ValueError, match="localhost"):
        await tools["wait_for_http"].handler({"url": "https://example.com"})


@pytest.mark.asyncio
async def test_process_manager_close_releases_registry(tmp_path):
    manager = BackgroundProcessManager(tmp_path)
    await manager.start(f'"{sys.executable}" -c "import time; time.sleep(30)"')

    await manager.aclose()

    assert manager._processes == {}


@pytest.mark.asyncio
async def test_stop_process_accepts_unique_abbreviated_id(tmp_path):
    manager = BackgroundProcessManager(tmp_path)
    started = await manager.start(f'"{sys.executable}" -c "import time; time.sleep(30)"')
    process_id = started["process_id"]

    result = await manager.stop(f"{process_id[:6]}...")

    assert result["process_id"] == process_id
    assert result["status"] == "stopped"


@pytest.mark.asyncio
async def test_process_id_prefix_must_be_at_least_six_characters(tmp_path):
    manager = BackgroundProcessManager(tmp_path)
    started = await manager.start(f'"{sys.executable}" -c "import time; time.sleep(30)"')
    try:
        with pytest.raises(ValueError, match="unknown process_id"):
            await manager.stop(f"{started['process_id'][:5]}...")
    finally:
        await manager.aclose()


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
    body = b"<html><body><h1>Hi</h1></body></html>"
    with (
        patch(
            "harness.runtime.url_validation.socket.getaddrinfo",
            return_value=[(0, 0, 0, "", ("93.184.216.34", 443))],
        ),
        patch(
            "marcus_code.tools.base.httpx.AsyncClient",
            return_value=_FakeAsyncClient(
                _FakeResponse(content=body, headers={"content-type": "text/html"})
            ),
        ),
    ):
        tool = build_fetch_url_tool(settings)
        result = await tool.handler({"url": "https://example.com/page"})

    assert "Hi" in result["text"]
    assert "<h1>" not in result["text"]


@pytest.mark.asyncio
async def test_fetch_url_rejects_non_http_scheme():
    settings = Settings()
    tool = build_fetch_url_tool(settings)

    with pytest.raises(ValueError, match="http"):
        await tool.handler({"url": "file:///etc/passwd"})


@pytest.mark.asyncio
async def test_fetch_url_rejects_loopback(monkeypatch):
    monkeypatch.setattr(
        "harness.runtime.url_validation.socket.getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("127.0.0.1", 80))],
    )
    with pytest.raises(ValueError, match="refuses"):
        await build_fetch_url_tool(Settings()).handler({"url": "http://localhost/"})


@pytest.mark.asyncio
async def test_fetch_url_rejects_multiple_dns_addresses(monkeypatch):
    monkeypatch.setattr(
        "harness.runtime.url_validation.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (0, 0, 0, "", ("93.184.216.34", 80)),
            (0, 0, 0, "", ("93.184.216.35", 80)),
        ],
    )
    with pytest.raises(ValueError, match="multiple DNS"):
        await build_fetch_url_tool(Settings()).handler({"url": "http://example.com/"})
