import asyncio

import pytest

from harness.db.enums import RiskTier
from harness.llm.gateway import LLMTransientError
from harness.llm.types import LLMMessage, LLMResponse, ToolCall, Usage
from harness.runtime.tools import Tool
from marcus_code.loop import MarcusLoop
from marcus_code.modes import AgentMode
from tests.fakes import ScriptedLLMGateway, text_response, tool_call_response


class _FakeUI:
    def __init__(self, decisions=None):
        self._decisions = iter(decisions or [])
        self.assistant_messages: list[str] = []
        self.tool_calls: list[tuple[str, dict]] = []
        self.errors: list[tuple[str, str]] = []
        self.declined: list[str] = []
        self.guardrail_stops: list[str] = []
        self.finished_steps: list[bool] = []

    def print_assistant(self, text):
        self.assistant_messages.append(text)

    def print_tool_call(self, tool_name, arguments):
        self.tool_calls.append((tool_name, arguments))

    def print_tool_error(self, tool_name, error):
        self.errors.append((tool_name, error))

    def print_tool_declined(self, tool_name):
        self.declined.append(tool_name)

    def print_guardrail_stop(self, reason):
        self.guardrail_stops.append(reason)

    def print_interrupted(self):
        pass

    def finish_steps(self, *, success):
        self.finished_steps.append(success)

    def confirm_tool_call(self, tool, arguments):
        return next(self._decisions)


def _read_only_tool(name="peek", result=None):
    async def handler(arguments):
        return result if result is not None else {"ok": True}

    return Tool(
        name=name,
        description="d",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        risk_tier=RiskTier.read_only,
    )


def _sensitive_tool(name="mutate", *, raises=None):
    async def handler(arguments):
        if raises is not None:
            raise raises
        return {"done": True}

    return Tool(
        name=name,
        description="d",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        risk_tier=RiskTier.sensitive_write,
    )


@pytest.mark.asyncio
async def test_plain_text_reply_ends_turn_immediately():
    llm = ScriptedLLMGateway([text_response("all done")])
    ui = _FakeUI()
    loop = MarcusLoop(llm, [], ui)

    await loop.run_turn("do something")

    assert ui.assistant_messages == ["all done"]


@pytest.mark.asyncio
async def test_read_only_tool_executes_without_approval_prompt():
    llm = ScriptedLLMGateway([tool_call_response("peek", {}), text_response("done")])
    ui = _FakeUI(decisions=[])  # no decisions consumed — would raise StopIteration if asked
    loop = MarcusLoop(llm, [_read_only_tool()], ui)

    await loop.run_turn("look around")

    assert ui.tool_calls == [("peek", {})]
    assert ui.assistant_messages == ["done"]


@pytest.mark.asyncio
async def test_only_first_tool_call_in_a_batch_executes():
    batched = LLMResponse(
        content=None,
        tool_calls=[
            ToolCall(id="first", name="peek", arguments={}),
            ToolCall(id="second", name="peek_two", arguments={}),
        ],
        finish_reason="tool_calls",
        usage=Usage(10, 5, 15),
        model="test-model",
        raw={},
    )
    llm = ScriptedLLMGateway([batched, text_response("done")])
    ui = _FakeUI()
    loop = MarcusLoop(llm, [_read_only_tool(), _read_only_tool("peek_two")], ui)

    await loop.run_turn("inspect sequentially")

    assert ui.tool_calls == [("peek", {})]
    assert any("one tool call" in error for _name, error in ui.errors)
    policy_message = next(
        message for message in loop.state.history if message.tool_call_id == "second"
    )
    assert "POLICY_DENIED" in (policy_message.content or "")


@pytest.mark.asyncio
async def test_invalid_argument_repair_is_bounded():
    tool = Tool(
        name="typed",
        description="d",
        parameters={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        },
        handler=lambda arguments: None,
        risk_tier=RiskTier.read_only,
    )
    llm = ScriptedLLMGateway(
        [
            tool_call_response("typed", {"count": "bad"}),
            tool_call_response("typed", {"count": "still-bad"}),
        ]
    )
    ui = _FakeUI()
    loop = MarcusLoop(llm, [tool], ui, max_argument_repairs=1)

    await loop.run_turn("use typed tool")

    assert any("argument repair budget exhausted" in reason for reason in ui.guardrail_stops)
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_cosmetic_argument_changes_with_same_error_stop_as_no_progress():
    async def fail(arguments):
        raise RuntimeError("same failure")

    tool = Tool(
        name="probe",
        description="d",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        handler=fail,
        risk_tier=RiskTier.read_only,
    )
    llm = ScriptedLLMGateway(
        [
            tool_call_response("probe", {"query": "a"}),
            tool_call_response("probe", {"query": "b"}),
            tool_call_response("probe", {"query": "c"}),
        ]
    )
    ui = _FakeUI()
    loop = MarcusLoop(llm, [tool], ui, max_safe_tool_retries=0)

    await loop.run_turn("investigate")

    assert any("no progress" in reason for reason in ui.guardrail_stops)


@pytest.mark.asyncio
async def test_requested_verification_blocks_unsupported_success_claim():
    llm = ScriptedLLMGateway([text_response("tests passed"), text_response("still passed")])
    ui = _FakeUI()
    loop = MarcusLoop(llm, [], ui)

    await loop.run_turn("run tests and verify the result")

    assert any("no successful evidence" in reason for reason in ui.guardrail_stops)
    assert ui.assistant_messages == []


@pytest.mark.asyncio
async def test_successful_test_command_allows_final_answer():
    llm = ScriptedLLMGateway(
        [tool_call_response("run_cli", {"command": "pytest -q"}), text_response("passed")]
    )
    ui = _FakeUI()
    loop = MarcusLoop(
        llm,
        [_read_only_tool("run_cli", {"exit_code": 0, "stdout": "2 passed"})],
        ui,
    )

    await loop.run_turn("run tests and verify the result")

    assert ui.assistant_messages[-1] == "passed"
    assert ui.assistant_messages[0].startswith("Plan:")
    assert ui.guardrail_stops == []


@pytest.mark.asyncio
async def test_sensitive_tool_approved_executes():
    llm = ScriptedLLMGateway([tool_call_response("mutate", {"x": 1}), text_response("done")])
    ui = _FakeUI(decisions=["yes"])
    loop = MarcusLoop(llm, [_sensitive_tool()], ui)

    await loop.run_turn("change something")

    assert ui.assistant_messages[-1] == "done"
    assert ui.declined == []
    tool_message = loop.state.history[-2]
    assert tool_message.role == "tool"
    assert "done" in tool_message.content


@pytest.mark.asyncio
async def test_sensitive_tool_declined_feeds_error_observation():
    llm = ScriptedLLMGateway(
        [tool_call_response("mutate", {"x": 1}), text_response("ok, skipping")]
    )
    ui = _FakeUI(decisions=["no"])
    loop = MarcusLoop(llm, [_sensitive_tool()], ui)

    await loop.run_turn("change something")

    assert ui.declined == ["mutate"]
    tool_message = loop.state.history[-2]
    assert "declined" in tool_message.content


@pytest.mark.asyncio
async def test_always_decision_skips_future_prompts_for_that_tool():
    llm = ScriptedLLMGateway(
        [
            tool_call_response("mutate", {"x": 1}),
            tool_call_response("mutate", {"x": 2}),
            text_response("done"),
        ]
    )
    ui = _FakeUI(decisions=["always"])  # only one decision needed
    loop = MarcusLoop(llm, [_sensitive_tool()], ui)

    await loop.run_turn("change things twice")

    assert "mutate" in loop.state.always_allowed
    assert ui.assistant_messages[-1] == "done"


@pytest.mark.asyncio
async def test_tool_handler_exception_becomes_error_observation_not_crash():
    llm = ScriptedLLMGateway([tool_call_response("mutate", {}), text_response("handled the error")])
    ui = _FakeUI(decisions=["yes"])
    loop = MarcusLoop(llm, [_sensitive_tool(raises=ValueError("boom"))], ui)

    await loop.run_turn("do it")

    assert ui.errors == [("mutate", "boom")]
    assert ui.assistant_messages == ["handled the error"]


@pytest.mark.asyncio
async def test_unknown_tool_call_returns_error_without_crashing():
    llm = ScriptedLLMGateway([tool_call_response("does_not_exist", {}), text_response("done")])
    ui = _FakeUI()
    loop = MarcusLoop(llm, [], ui)

    await loop.run_turn("call something weird")

    assert ui.errors == [("does_not_exist", "unknown tool: does_not_exist")]


@pytest.mark.asyncio
async def test_repeated_identical_call_stops_the_loop():
    llm = ScriptedLLMGateway(
        [
            tool_call_response("peek", {"q": "x"}),
            tool_call_response("peek", {"q": "x"}),
            tool_call_response("peek", {"q": "x"}),
        ]
    )
    ui = _FakeUI()
    loop = MarcusLoop(llm, [_read_only_tool()], ui)

    await loop.run_turn("keep looking")

    assert len(ui.guardrail_stops) == 1
    assert "identical arguments" in ui.guardrail_stops[0]


@pytest.mark.asyncio
async def test_max_steps_guardrail_stops_the_loop():
    responses = [tool_call_response("peek", {"i": i}) for i in range(5)]
    llm = ScriptedLLMGateway(responses)
    ui = _FakeUI()

    async def changing_result(arguments):
        return {"seen": arguments["i"]}

    tool = Tool(
        name="peek",
        description="d",
        parameters={"type": "object", "properties": {"i": {"type": "integer"}}},
        handler=changing_result,
        risk_tier=RiskTier.read_only,
    )
    loop = MarcusLoop(llm, [tool], ui, max_steps=3)

    await loop.run_turn("keep going forever")

    assert any("max steps" in stop for stop in ui.guardrail_stops)


@pytest.mark.asyncio
async def test_history_is_capped_while_preserving_system_prompt():
    llm = ScriptedLLMGateway([text_response("done")])
    ui = _FakeUI()
    loop = MarcusLoop(llm, [], ui, max_history_messages=3, system_prompt="system")
    loop.state.history.extend([LLMMessage(role="user", content=f"old-{i}") for i in range(5)])
    await loop.run_turn("latest")
    assert loop.state.history[0].role == "system"
    assert len(loop.state.history) <= 3


@pytest.mark.asyncio
async def test_token_budget_stops_before_next_call():
    llm = ScriptedLLMGateway([text_response("unused")])
    ui = _FakeUI()
    loop = MarcusLoop(llm, [], ui, max_total_tokens=1)
    loop.usage.total_tokens = 1
    await loop.run_turn("do it")
    assert any("token budget" in stop for stop in ui.guardrail_stops)


@pytest.mark.asyncio
async def test_ask_mode_blocks_write_tool_without_prompting():
    llm = ScriptedLLMGateway([tool_call_response("mutate", {}), text_response("blocked")])
    ui = _FakeUI(decisions=[])
    loop = MarcusLoop(llm, [_sensitive_tool()], ui, mode=AgentMode.ask)

    await loop.run_turn("change it")

    assert ui.errors[0][0] == "mutate"
    assert "ask mode" in ui.errors[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [AgentMode.auto, AgentMode.yolo])
async def test_autonomous_modes_skip_approval_for_normal_writes(mode):
    llm = ScriptedLLMGateway([tool_call_response("mutate", {}), text_response("done")])
    ui = _FakeUI(decisions=[])
    loop = MarcusLoop(llm, [_sensitive_tool()], ui, mode=mode)

    await loop.run_turn("change it")

    assert ui.assistant_messages[-1] == "done"


def test_compact_history_reduces_retained_context():
    loop = MarcusLoop(
        ScriptedLLMGateway([]),
        [],
        _FakeUI(),
        system_prompt="system",
        context_window_tokens=500,
        compact_target_percent=50,
    )
    loop.state.history.extend(
        LLMMessage(role="user", content=(f"message-{index} " * 80)) for index in range(12)
    )
    before = loop.context_tokens

    reported_before, after = loop.compact_history()

    assert reported_before == before
    assert after < before
    assert loop.usage.compactions == 1
    assert loop.state.history[0].role == "system"


def test_clear_history_preserves_system_and_optionally_approvals():
    loop = MarcusLoop(ScriptedLLMGateway([]), [], _FakeUI(), system_prompt="system")
    loop.state.history.append(LLMMessage(role="user", content="hello"))
    loop.state.always_allowed.add("run_cli")

    loop.clear_history()
    assert [message.role for message in loop.state.history] == ["system"]
    assert loop.state.always_allowed == {"run_cli"}

    loop.clear_history(clear_all=True)
    assert loop.state.always_allowed == set()


@pytest.mark.asyncio
async def test_stream_failure_falls_back_to_non_streaming_request():
    class _RecoveringGateway:
        async def complete_stream(self, *args, **kwargs):
            raise LLMTransientError("stream unavailable")

        async def complete(self, *args, **kwargs):
            return text_response("recovered")

    ui = _FakeUI()
    ui.print_assistant_delta = lambda text: None
    loop = MarcusLoop(_RecoveringGateway(), [], ui)

    await loop.run_turn("hello")

    assert ui.assistant_messages == ["recovered"]


@pytest.mark.asyncio
async def test_consecutive_tool_failures_trip_circuit_breaker():
    async def fail(arguments):
        raise RuntimeError(f"failure {arguments['attempt']}")

    tool = Tool(
        name="flaky",
        description="fails",
        parameters={"type": "object"},
        handler=fail,
        risk_tier=RiskTier.read_only,
    )
    llm = ScriptedLLMGateway(
        [tool_call_response("flaky", {"attempt": index}) for index in range(5)]
    )
    ui = _FakeUI()
    loop = MarcusLoop(llm, [tool], ui, max_consecutive_tool_failures=5)

    await loop.run_turn("keep trying")

    assert len(ui.errors) == 5
    assert any("retry loop" in reason for reason in ui.guardrail_stops)


@pytest.mark.asyncio
async def test_read_only_tool_failure_is_retried_and_recovers():
    attempts = {"count": 0}

    async def flaky(arguments):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("temporary")
        return {"ok": True}

    tool = Tool(
        name="flaky",
        description="temporarily fails",
        parameters={"type": "object"},
        handler=flaky,
        risk_tier=RiskTier.read_only,
    )
    llm = ScriptedLLMGateway([tool_call_response("flaky", {}), text_response("recovered")])
    ui = _FakeUI()
    loop = MarcusLoop(llm, [tool], ui)

    await loop.run_turn("try it")

    assert attempts["count"] == 2
    assert ui.errors == []
    assert ui.assistant_messages == ["recovered"]


@pytest.mark.asyncio
async def test_wait_for_http_is_not_retried_by_outer_tool_recovery():
    attempts = {"count": 0}

    async def fail(arguments):
        attempts["count"] += 1
        raise RuntimeError("not ready")

    tool = Tool(
        name="wait_for_http",
        description="polls internally",
        parameters={"type": "object"},
        handler=fail,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )
    llm = ScriptedLLMGateway([tool_call_response("wait_for_http", {}), text_response("handled")])
    loop = MarcusLoop(llm, [tool], _FakeUI())

    await loop.run_turn("wait")

    assert attempts["count"] == 1


@pytest.mark.asyncio
async def test_empty_final_response_still_finishes_working_steps():
    ui = _FakeUI()
    llm = ScriptedLLMGateway([tool_call_response("peek", {}), text_response(None)])
    loop = MarcusLoop(llm, [_read_only_tool()], ui)

    await loop.run_turn("inspect")

    assert ui.finished_steps[-1] is True


@pytest.mark.asyncio
async def test_llm_recovery_has_an_overall_timeout():
    class _HangingGateway:
        async def complete(self, *args, **kwargs):
            await asyncio.sleep(10)

    ui = _FakeUI()
    loop = MarcusLoop(_HangingGateway(), [], ui, llm_recovery_timeout_seconds=0.01)

    await loop.run_turn("hello")

    assert any("recovery timed out" in reason for reason in ui.guardrail_stops)


@pytest.mark.asyncio
async def test_context_over_limit_stops_before_llm_call():
    llm = ScriptedLLMGateway([text_response("must not be used")])
    ui = _FakeUI()
    loop = MarcusLoop(
        llm,
        [],
        ui,
        context_window_tokens=20,
        compact_threshold_percent=100,
        history_summary_enabled=False,
    )

    await loop.run_turn("x" * 500)

    assert llm.calls == []
    assert any("context window" in reason for reason in ui.guardrail_stops)
