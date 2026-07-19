import base64
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from harness.config import Settings
from harness.llm.types import LLMResponse, Usage
from marcus_code.cli import app as cli


def test_main_update_flag_prints_local_dev_instructions(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("sys.argv", ["marcus", "--update"])
    # The repo marker (pyproject.toml next to marcus_code package) exists in the
    # test checkout, so update should report local-dev instructions.
    monkeypatch.setattr(cli, "_version_string", lambda: "Marcus Code 1.0.0")

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "local source tree" in output
    assert "git pull" in output


def test_main_update_subcommand_prints_local_dev_instructions(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("sys.argv", ["marcus", "update"])
    monkeypatch.setattr(cli, "_version_string", lambda: "Marcus Code 1.0.0")

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "local source tree" in output


def test_main_update_subcommand_accepts_target_version(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("sys.argv", ["marcus", "update", "1.2.3", "--yes"])
    monkeypatch.setattr(cli, "_detect_install_method", lambda: "uv_tool")
    monkeypatch.setattr(cli, "_current_version", lambda: "1.0.0")
    monkeypatch.setattr(cli, "_latest_release_info", lambda **kwargs: {"tag_name": "v1.2.3"})
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli, "_should_defer_windows_update", lambda method: False)
    executed: list[list[str]] = []

    def _fake_run(command, **kwargs):
        executed.append(command)
        return type("R", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(cli.subprocess, "run", _fake_run)

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert any("v1.2.3" in arg for args in executed for arg in args)


def test_main_update_yes_flag_skips_confirmation(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("sys.argv", ["marcus", "--update", "--yes"])
    monkeypatch.setattr(cli, "_detect_install_method", lambda: "uv_tool")
    monkeypatch.setattr(cli, "_current_version", lambda: "1.0.0")
    monkeypatch.setattr(cli, "_latest_release_info", lambda **kwargs: {"tag_name": "v1.2.3"})
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli, "_should_defer_windows_update", lambda method: False)
    executed: list[list[str]] = []

    def _fake_run(command, **kwargs):
        executed.append(command)
        return type("R", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(cli.subprocess, "run", _fake_run)

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert "Updating Marcus from 1.0.0 to 1.2.3" in capsys.readouterr().out
    assert executed


def test_run_update_defers_uv_tool_replacement_on_windows(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "_detect_install_method", lambda: "uv_tool")
    monkeypatch.setattr(cli, "_current_version", lambda: "1.0.0")
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"C:/bin/{name}.exe")
    monkeypatch.setattr(cli, "_should_defer_windows_update", lambda method: True)
    scheduled: list[tuple[list[str], str]] = []

    def _fake_schedule(command: list[str], target: str) -> Path:
        scheduled.append((command, target))
        return tmp_path / "update.log"

    monkeypatch.setattr(cli, "_schedule_windows_update", _fake_schedule)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("a deferred update must not run in Marcus"),
    )

    code = cli._run_update("1.2.3", assume_yes=True)

    assert code == 0
    assert scheduled and scheduled[0][1] == "1.2.3"
    output = capsys.readouterr().out
    assert "Update scheduled" in output
    assert "will now exit" in output


def test_schedule_windows_update_uses_external_hidden_helper(monkeypatch, tmp_path):
    result_file = tmp_path / "update-result.json"
    log_file = tmp_path / "update.log"
    monkeypatch.setattr(cli, "_deferred_update_files", lambda: (result_file, log_file))
    monkeypatch.setattr(cli.shutil, "which", lambda name: "C:/Windows/PowerShell.exe")
    launches: list[tuple[list[str], dict]] = []

    def _fake_popen(command: list[str], **kwargs):
        launches.append((command, kwargs))
        return object()

    monkeypatch.setattr(cli.subprocess, "Popen", _fake_popen)

    command = ["uv", "tool", "install", "--force", "git+repo@v1.2.3"]
    assert cli._schedule_windows_update(command, "1.2.3") == log_file

    pending = json.loads(result_file.read_text(encoding="utf-8"))
    assert pending["status"] == "pending"
    assert pending["target_version"] == "1.2.3"
    launched_command, options = launches[0]
    assert "-NonInteractive" in launched_command
    encoded_script = launched_command[launched_command.index("-EncodedCommand") + 1]
    script = base64.b64decode(encoded_script).decode("utf-16-le")
    assert "Get-Process" in script
    assert "access is denied" in script.lower()
    assert options["stdin"] is cli.subprocess.DEVNULL
    assert options["stdout"] is cli.subprocess.DEVNULL
    assert options["stderr"] is cli.subprocess.DEVNULL


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell integration test")
def test_windows_update_helper_runs_command_after_parent_exits(monkeypatch, tmp_path):
    if cli.shutil.which("powershell.exe") is None and cli.shutil.which("pwsh.exe") is None:
        pytest.skip("PowerShell is not installed")
    result_file = tmp_path / "update-result.json"
    log_file = tmp_path / "update.log"
    tool_scripts = tmp_path / "tool" / "Scripts"
    tool_scripts.mkdir(parents=True)
    blocker_executable = tool_scripts / "marcus-blocker.exe"
    cli.shutil.copy2(cli.shutil.which("cmd.exe") or "cmd.exe", blocker_executable)
    monkeypatch.setattr(cli, "_deferred_update_files", lambda: (result_file, log_file))
    monkeypatch.setattr(cli.os, "getpid", lambda: 2_147_483_647)
    monkeypatch.setattr(cli.sys, "executable", str(tool_scripts / "python.exe"))
    command = [
        cli.shutil.which("cmd.exe") or "cmd.exe",
        "/d",
        "/c",
        "echo normal-native-stderr 1>&2 & exit /b 0",
    ]
    blocker = cli.subprocess.Popen(
        [str(blocker_executable), "/d", "/c", "ping -n 3 127.0.0.1 >nul"],
        stdin=cli.subprocess.DEVNULL,
        stdout=cli.subprocess.DEVNULL,
        stderr=cli.subprocess.DEVNULL,
        creationflags=getattr(cli.subprocess, "CREATE_NO_WINDOW", 0),
    )
    result = {"status": "pending"}

    try:
        assert cli._schedule_windows_update(command, "test") == log_file
        time.sleep(0.5)
        pending = json.loads(result_file.read_text(encoding="utf-8-sig"))
        assert pending["status"] == "pending"

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and result.get("status") == "pending":
            time.sleep(0.1)
            result = json.loads(result_file.read_text(encoding="utf-8-sig"))
    finally:
        with cli.contextlib.suppress(cli.subprocess.TimeoutExpired):
            blocker.wait(timeout=1)
        if blocker.poll() is None:
            blocker.terminate()
    assert result["status"] == "success"
    assert log_file.is_file()
    assert "normal-native-stderr" in log_file.read_text(encoding="utf-8-sig")


def test_report_deferred_update_blocks_while_pending(monkeypatch, capsys, tmp_path):
    result_file = tmp_path / "update-result.json"
    log_file = tmp_path / "update.log"
    monkeypatch.setattr(cli, "_deferred_update_files", lambda: (result_file, log_file))
    result_file.write_text(
        json.dumps(
            {
                "status": "pending",
                "target_version": "1.2.3",
                "started_at": datetime.now().timestamp(),
            }
        ),
        encoding="utf-8",
    )

    assert cli._report_deferred_update_result() is True
    assert result_file.exists()
    assert "still running" in capsys.readouterr().out


def test_report_deferred_update_consumes_success(monkeypatch, capsys, tmp_path):
    result_file = tmp_path / "update-result.json"
    log_file = tmp_path / "update.log"
    monkeypatch.setattr(cli, "_deferred_update_files", lambda: (result_file, log_file))
    result_file.write_text(
        json.dumps({"status": "success", "target_version": "1.2.3"}), encoding="utf-8"
    )

    assert cli._report_deferred_update_result() is False
    assert not result_file.exists()
    assert "updated successfully to 1.2.3" in capsys.readouterr().out


def test_update_command_pin_target_version():
    assert cli._update_command("uv_tool", target_version="1.2.3") == [
        "uv",
        "tool",
        "install",
        "--force",
        "git+https://github.com/lazymarcus005-maker/marcus.git@v1.2.3",
    ]
    assert cli._update_command("uv_tool") == [
        "uv",
        "tool",
        "install",
        "--force",
        "git+https://github.com/lazymarcus005-maker/marcus.git",
    ]
    assert cli._update_command("pipx", target_version="1.2.3") == [
        "pipx",
        "upgrade",
        "--include-injected",
        "marcus==1.2.3",
    ]
    assert cli._update_command("pip", target_version="1.2.3") == [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "marcus==1.2.3",
    ]


def test_confirm_update_respects_yes_flag(monkeypatch, capsys):
    info = {"body": "- Fix bug\n- Add feature"}
    assert cli._confirm_update("1.0.0", "1.1.0", "uv_tool", info, assume_yes=True) is True
    # With assume_yes no input is read.


def test_confirm_update_cancels_on_no(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert cli._confirm_update("1.0.0", "1.1.0", "uv_tool", None, assume_yes=False) is False
    assert "Update cancelled" in capsys.readouterr().out


def test_print_release_notes_trims_long_bodies(capsys):
    info = {"body": "\n".join(f"Line {i}" for i in range(12))}
    cli._print_release_notes("1.1.0", info)
    output = capsys.readouterr().out
    assert "Release notes:" in output
    assert "Line 0" in output
    assert "Line 7" in output
    assert "Line 8" not in output
    assert "4 more line(s)" in output


def test_main_rejects_unknown_subcommand(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("sys.argv", ["marcus", "foo"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    # Typer/Click surface unknown subcommands with exit code 2 and a Rich-
    # rendered error banner. The exact wording ("No such command 'foo'.")
    # is written to stderr but wrapped in a decorative box, so we assert on
    # exit code + the offending argument appearing somewhere in the error
    # output rather than a fixed argparse string.
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "foo" in captured.err


def test_version_cache_is_used_when_fresh(monkeypatch, tmp_path):
    cache_file = tmp_path / "version-check.json"
    monkeypatch.setattr(cli, "_version_cache_file", lambda: cache_file)
    now = datetime.now().timestamp()
    cache_file.write_text(json.dumps({"version": "9.9.9", "checked_at": now}), encoding="utf-8")

    assert cli._latest_release_version() == "9.9.9"

    def test_version_cache_ignores_stale_cache_and_returns_latest(monkeypatch, tmp_path):
        cache_file = tmp_path / "version-check.json"
        monkeypatch.setattr(cli, "_version_cache_file", lambda: cache_file)
        old = (datetime.now() - timedelta(days=2)).timestamp()
        cache_file.write_text(json.dumps({"version": "0.0.1", "checked_at": old}), encoding="utf-8")

        class _Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"tag_name": "v1.2.3"}

        monkeypatch.setattr(cli.httpx, "get", lambda **kwargs: _Response())

        assert cli._latest_release_version() == "1.2.3"
        assert json.loads(cache_file.read_text(encoding="utf-8"))["version"] == "1.2.3"


def test_version_cache_falls_back_to_stale_cache_when_network_fails(monkeypatch, tmp_path):
    cache_file = tmp_path / "version-check.json"
    monkeypatch.setattr(cli, "_version_cache_file", lambda: cache_file)
    old = (datetime.now() - timedelta(days=2)).timestamp()
    cache_file.write_text(json.dumps({"version": "0.0.1", "checked_at": old}), encoding="utf-8")

    def raise_error(**kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(cli.httpx, "get", raise_error)

    assert cli._latest_release_version() == "0.0.1"


def test_update_command_for_uv_tool():
    assert cli._update_command("uv_tool") == [
        "uv",
        "tool",
        "install",
        "--force",
        "git+https://github.com/lazymarcus005-maker/marcus.git",
    ]


def test_update_command_for_pipx():
    assert cli._update_command("pipx") == ["pipx", "upgrade", "--include-injected", "marcus"]


def test_update_command_for_pip():
    command = cli._update_command("pip")
    assert command[:3] == [sys.executable, "-m", "pip"]
    assert command[3:] == ["install", "--upgrade", "marcus"]


def test_update_command_for_unknown_is_empty():
    assert cli._update_command("unknown") == []


def test_version_is_newer_compares_numeric_and_prerelease_suffixes():
    assert cli._version_is_newer("1.1.0", "1.0.0") is True
    assert cli._version_is_newer("1.0.1", "1.0.0") is True
    assert cli._version_is_newer("1.0.0", "1.0.0") is False
    assert cli._version_is_newer("0.9.9", "1.0.0") is False
    assert cli._version_is_newer("1.0.0", "1.0.0a1") is True
    assert cli._version_is_newer("1.0.0b1", "1.0.0a1") is True


def test_notify_if_update_available_prints_when_newer(capsys, monkeypatch):
    monkeypatch.setattr(cli, "_current_version", lambda: "1.0.0")
    monkeypatch.setattr(cli, "_latest_release_version", lambda **kwargs: "1.1.0")

    cli._notify_if_update_available()

    output = capsys.readouterr().out
    assert "[update available]" in output
    assert "1.1.0" in output
    assert "marcus --update" in output


def test_notify_if_update_available_is_silent_when_up_to_date(capsys, monkeypatch):
    monkeypatch.setattr(cli, "_current_version", lambda: "1.1.0")
    monkeypatch.setattr(cli, "_latest_release_version", lambda **kwargs: "1.1.0")

    cli._notify_if_update_available()

    assert capsys.readouterr().out == ""


def test_notify_if_update_available_is_silent_when_check_fails(capsys, monkeypatch):
    monkeypatch.setattr(cli, "_current_version", lambda: "1.0.0")
    monkeypatch.setattr(cli, "_latest_release_version", lambda **kwargs: None)

    cli._notify_if_update_available()

    assert capsys.readouterr().out == ""


def test_run_update_skips_when_already_up_to_date(capsys, monkeypatch):
    monkeypatch.setattr(cli, "_current_version", lambda: "1.0.0")
    monkeypatch.setattr(cli, "_latest_release_info", lambda **kwargs: {"tag_name": "v1.0.0"})
    monkeypatch.setattr(cli, "_detect_install_method", lambda: "uv_tool")

    code = cli._run_update(assume_yes=True)

    assert code == 0
    output = capsys.readouterr().out
    assert "already up to date" in output


def test_run_update_local_dev_ignores_version_check(capsys, monkeypatch):
    monkeypatch.setattr(cli, "_current_version", lambda: "1.0.0")
    monkeypatch.setattr(cli, "_detect_install_method", lambda: "local_dev")

    code = cli._run_update()

    assert code == 0
    assert "local source tree" in capsys.readouterr().out


def test_detect_install_method_finds_uv_tool(monkeypatch):
    fake_path = Path("/home/user/.local/share/uv/tools/marcus/bin/python")
    monkeypatch.setattr(cli.sys, "executable", str(fake_path))
    monkeypatch.setenv("UV_TOOL_DIR", "/home/user/.local/share/uv/tools")
    assert cli._detect_install_method() == "uv_tool"


def test_detect_install_method_finds_pipx(monkeypatch):
    fake_path = Path.home() / ".local" / "pipx" / "venvs" / "marcus" / "bin" / "python"
    monkeypatch.setattr(cli.sys, "executable", str(fake_path))
    assert cli._detect_install_method() == "pipx"


@pytest.mark.parametrize("flag", ["-v", "--version"])
def test_main_prints_version(flag, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["marcus", flag])
    monkeypatch.setattr(cli, "_version_string", lambda: "Marcus Code 1.2.3")

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == "Marcus Code 1.2.3"


class _FakeUI:
    instances: list["_FakeUI"] = []

    def __init__(self, *, no_color: bool = False):
        self.inputs = iter(["/exit"])
        self.setup_result = None
        self.assistant = []
        self.banner_calls = []
        self.__class__.instances.append(self)

    def run_first_time_setup(self, **kwargs):
        return self.setup_result

    def print_banner(self, *args, **kwargs):
        self.banner_calls.append((args, kwargs))

    def prompt_user(self):
        return next(self.inputs)

    def print_assistant(self, text):
        self.assistant.append(text)

    def print_interrupted(self):
        pass


class _FakeGateway:
    instances: list["_FakeGateway"] = []

    def __init__(self, *args, **kwargs):
        self.closed = False
        self.__class__.instances.append(self)

    async def complete(self, *args, **kwargs):
        return LLMResponse("ok", [], "stop", Usage(1, 1, 2), "test", {})

    async def aclose(self):
        self.closed = True


@pytest.fixture
def patched_cli(monkeypatch, tmp_path):
    _FakeUI.instances.clear()
    _FakeGateway.instances.clear()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "TerminalUI", _FakeUI)
    monkeypatch.setattr(cli, "LLMGateway", _FakeGateway)
    monkeypatch.setattr(cli, "build_marcus_tools", lambda root, settings: [])
    monkeypatch.setattr(cli, "load_project_instructions", lambda root: None)
    monkeypatch.setattr(cli, "has_llm_credentials", lambda settings: True)
    monkeypatch.setattr(cli, "resolve_settings", lambda: Settings(llm_api_key="sk-real"))
    return tmp_path


@pytest.mark.asyncio
async def test_amain_prompt_runs_one_turn_and_closes_gateway(patched_cli):
    await cli._amain("hello")

    assert _FakeUI.instances[0].assistant == ["ok"]
    assert _FakeGateway.instances[0].closed is True


@pytest.mark.asyncio
async def test_amain_repl_exit_closes_gateway(patched_cli):
    await cli._amain()
    assert _FakeGateway.instances[0].closed is True


@pytest.mark.asyncio
async def test_amain_cleans_up_background_tools(monkeypatch, tmp_path):
    class _ClosableTools(list):
        closed = False

        async def aclose(self):
            self.closed = True

    tools = _ClosableTools()
    monkeypatch.chdir(tmp_path)
    _FakeUI.instances.clear()
    _FakeGateway.instances.clear()
    monkeypatch.setattr(cli, "TerminalUI", _FakeUI)
    monkeypatch.setattr(cli, "LLMGateway", _FakeGateway)
    monkeypatch.setattr(cli, "build_marcus_tools", lambda root, settings: tools)
    monkeypatch.setattr(cli, "load_project_instructions", lambda root: None)
    monkeypatch.setattr(cli, "has_llm_credentials", lambda settings: True)
    monkeypatch.setattr(cli, "resolve_settings", lambda: Settings(llm_api_key="sk-real"))

    await cli._amain("hello")

    assert tools.closed is True


@pytest.mark.asyncio
async def test_amain_cleans_up_background_processes_after_each_turn(monkeypatch, tmp_path):
    class _ProcessManager:
        def __init__(self):
            self.cleanup_calls = 0

        async def aclose(self):
            self.cleanup_calls += 1

    class _Tools(list):
        def __init__(self):
            super().__init__()
            self.process_manager = _ProcessManager()

        async def aclose(self):
            pass

    tools = _Tools()
    monkeypatch.chdir(tmp_path)
    _FakeUI.instances.clear()
    _FakeGateway.instances.clear()
    monkeypatch.setattr(cli, "TerminalUI", _FakeUI)
    monkeypatch.setattr(cli, "LLMGateway", _FakeGateway)
    monkeypatch.setattr(cli, "build_marcus_tools", lambda root, settings: tools)
    monkeypatch.setattr(cli, "load_project_instructions", lambda root: None)
    monkeypatch.setattr(cli, "has_llm_credentials", lambda settings: True)
    monkeypatch.setattr(cli, "resolve_settings", lambda: Settings(llm_api_key="sk-real"))

    await cli._amain("hello")

    assert tools.process_manager.cleanup_calls == 1


@pytest.mark.asyncio
async def test_amain_closes_gateway_when_turn_raises(patched_cli, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli.MarcusLoop, "run_turn", fail)
    with pytest.raises(RuntimeError, match="boom"):
        await cli._amain("hello")
    assert _FakeGateway.instances[0].closed is True


@pytest.mark.asyncio
async def test_amain_first_time_setup_saves_config(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _FakeUI.instances.clear()
    _FakeGateway.instances.clear()
    ui = _FakeUI()
    ui.setup_result = ("sk-new", "https://api.example.com/v1", "model-new")
    monkeypatch.setattr(cli, "TerminalUI", lambda **kwargs: ui)
    monkeypatch.setattr(cli, "LLMGateway", _FakeGateway)
    monkeypatch.setattr(cli, "build_marcus_tools", lambda root, settings: [])
    monkeypatch.setattr(cli, "load_project_instructions", lambda root: None)
    settings = iter([Settings(llm_api_key="changeme"), Settings(llm_api_key="sk-new")])
    monkeypatch.setattr(cli, "resolve_settings", lambda: next(settings))
    monkeypatch.setattr(cli, "has_llm_credentials", lambda value: value.llm_api_key != "changeme")
    saved = {}
    monkeypatch.setattr(cli, "save_user_config", lambda **kwargs: saved.update(kwargs))

    await cli._amain("setup")

    assert saved == {
        "api_key": "sk-new",
        "base_url": "https://api.example.com/v1",
        "model": "model-new",
    }
    assert _FakeGateway.instances[0].closed is True
