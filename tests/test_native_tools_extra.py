"""Tests for additional server native tools in harness.runtime.native_tools_extra."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from harness.config import Settings
from harness.runtime.native_tools_extra import build_native_extra_tools


@pytest.fixture
def extra_tools(tmp_path: Path):
    settings = Settings(tools_fs_root=str(tmp_path))
    return {t.name: t for t in build_native_extra_tools(settings)}


@pytest.mark.asyncio
async def test_search_web_returns_results(extra_tools):
    tool = extra_tools["search_web"]
    with patch(
        "harness.runtime.native_tools_extra._web_search_duckduckgo",
        return_value=[{"title": "Test", "url": "https://example.com", "snippet": "snippet"}],
    ):
        result = await tool.handler({"query": "python async"})
    assert result["count"] == 1
    assert result["results"][0]["title"] == "Test"


@pytest.mark.asyncio
async def test_edit_file_sandboxed(tmp_path):
    (tmp_path / "notes.txt").write_text("hello world", encoding="utf-8")
    tools = {t.name: t for t in build_native_extra_tools(Settings(tools_fs_root=str(tmp_path)))}
    result = await tools["edit_file"].handler(
        {"path": "notes.txt", "old_string": "world", "new_string": "universe"}
    )
    assert result["bytes_written"] == len(b"hello universe")
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "hello universe"


@pytest.mark.asyncio
async def test_read_directory_tree_skips_noise_dirs(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "junk.pyc").write_text("x")
    tools = {t.name: t for t in build_native_extra_tools(Settings(tools_fs_root=str(tmp_path)))}
    result = await tools["read_directory_tree"].handler({"path": ".", "max_depth": 2})
    names = _collect_names(result["tree"])
    assert "main.py" in names
    assert "__pycache__" not in names


def _collect_names(tree: dict) -> set[str]:
    names = {tree["name"]}
    for child in tree.get("children", []):
        names.update(_collect_names(child))
    return names


@pytest.mark.asyncio
async def test_summarize_text_truncates(extra_tools):
    tool = extra_tools["summarize_text"]
    text = "First sentence here. Second sentence here. Third sentence here. Fourth here."
    result = await tool.handler({"text": text, "max_sentences": 2})
    assert result["max_sentences"] == 2


@pytest.mark.asyncio
async def test_compare_files_detects_differences(tmp_path):
    (tmp_path / "a.txt").write_text("line1\nline2\n")
    (tmp_path / "b.txt").write_text("line1\nmodified\n")
    tools = {t.name: t for t in build_native_extra_tools(Settings(tools_fs_root=str(tmp_path)))}
    result = await tools["compare_files"].handler({"path_a": "a.txt", "path_b": "b.txt"})
    assert result["identical"] is False
    assert "modified" in result["diff"]


@pytest.mark.asyncio
async def test_memory_write_and_read(tmp_path):
    tools = {t.name: t for t in build_native_extra_tools(Settings(tools_fs_root=str(tmp_path)))}
    await tools["memory_write"].handler({"key": "prefs", "value": {"theme": "dark"}})
    result = await tools["memory_read"].handler({"key": "prefs"})
    assert result["exists"] is True
    assert result["value"] == {"theme": "dark"}


@pytest.mark.asyncio
async def test_check_url_health_reaches_public_url(extra_tools):
    tool = extra_tools["check_url_health"]
    with (
        patch(
            "harness.runtime.url_validation.socket.getaddrinfo",
            return_value=[(2, 1, 0, "", ("93.184.216.34", 443))],
        ),
        patch(
            "httpx.AsyncClient.request",
            return_value=_FakeResponse(status_code=200),
        ),
    ):
        result = await tool.handler({"url": "https://example.com"})
    assert result["healthy"] is True


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.url = "https://example.com"
        self.elapsed = __import__("datetime").timedelta(seconds=0.1)
