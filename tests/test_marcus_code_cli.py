import pytest

from harness.config import Settings
from harness.llm.types import LLMResponse, Usage
from marcus_code import cli


class _FakeUI:
    instances = []

    def __init__(self):
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
    instances = []

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
    monkeypatch.setattr(cli, "TerminalUI", lambda: ui)
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
