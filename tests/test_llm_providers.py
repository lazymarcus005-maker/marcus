from harness.llm.providers import adapt_request, provider_message_fields, resolve_provider
from harness.llm.types import LLMOptions


def _payload(model: str = "model") -> dict:
    return {"model": model, "messages": [{"role": "user", "content": "hello"}]}


def test_resolve_provider_detects_known_hosts_and_model_fallback():
    assert resolve_provider("https://ollama.com/v1", "m").name == "ollama"
    assert resolve_provider("http://localhost:11434/v1", "m").name == "ollama"
    assert resolve_provider("https://api.openai.com/v1", "m").name == "openai"
    assert resolve_provider("https://openrouter.ai/api/v1", "m").name == "openrouter"
    assert resolve_provider("https://nim.internal/v1", "nvidia/nemotron-3-super").name == "nvidia"
    assert resolve_provider("https://strict.example/v1", "m").name == "compatible"


def test_configured_provider_overrides_host_detection():
    adapter = resolve_provider("https://proxy.internal/v1", "gpt-5.2", "openai")
    assert adapter.name == "openai"


def test_compatible_adapter_omits_reasoning_and_protects_core_payload_fields():
    request = adapt_request(
        _payload(),
        base_url="https://strict.example/v1",
        configured_provider="auto",
        options=LLMOptions(
            reasoning_effort="high",
            max_completion_tokens=256,
            extra_body={"model": "hijack", "messages": [], "seed": 7},
        ),
    )

    assert request.payload["model"] == "model"
    assert request.payload["messages"]
    assert request.payload["max_tokens"] == 256
    assert request.payload["seed"] == 7
    assert request.reasoning_fields == frozenset()


def test_openai_adapter_maps_supported_efforts_and_skips_non_reasoning_models():
    disabled = adapt_request(
        _payload("gpt-5.2"),
        base_url="https://api.openai.com/v1",
        configured_provider="auto",
        options=LLMOptions(reasoning_effort="off"),
    )
    older = adapt_request(
        _payload("o3-mini"),
        base_url="https://api.openai.com/v1",
        configured_provider="auto",
        options=LLMOptions(reasoning_effort="off"),
    )
    plain = adapt_request(
        _payload("gpt-4o-mini"),
        base_url="https://api.openai.com/v1",
        configured_provider="auto",
        options=LLMOptions(reasoning_effort="high"),
    )

    assert disabled.payload["reasoning_effort"] == "none"
    assert older.payload["reasoning_effort"] == "low"
    assert "reasoning_effort" not in plain.payload


def test_ollama_adapter_uses_supported_output_and_effort_fields():
    gpt_oss = adapt_request(
        _payload("gpt-oss:120b"),
        base_url="https://ollama.com/v1",
        configured_provider="auto",
        options=LLMOptions(reasoning_effort="off", max_completion_tokens=1024),
    )
    qwen = adapt_request(
        _payload("qwen3:32b"),
        base_url="https://ollama.com/v1",
        configured_provider="auto",
        options=LLMOptions(reasoning_effort="off"),
    )

    assert gpt_oss.payload["reasoning_effort"] == "low"
    assert gpt_oss.effective_effort == "low"
    assert gpt_oss.payload["max_tokens"] == 1024
    assert "max_completion_tokens" not in gpt_oss.payload
    assert qwen.payload["reasoning_effort"] == "none"


def test_openrouter_adapter_maps_effort_and_hard_reasoning_budget():
    request = adapt_request(
        _payload("anthropic/claude-sonnet"),
        base_url="https://openrouter.ai/api/v1",
        configured_provider="auto",
        options=LLMOptions(reasoning_effort="high", reasoning_budget_tokens=4096),
    )

    assert request.payload["reasoning"] == {"max_tokens": 4096}
    assert request.fallback_payload.get("reasoning") is None


def test_nvidia_adapter_maps_effort_and_model_specific_budgets():
    super_request = adapt_request(
        _payload("nvidia/nemotron-3-super-120b-a12b"),
        base_url="https://integrate.api.nvidia.com/v1",
        configured_provider="auto",
        options=LLMOptions(reasoning_effort="low", reasoning_budget_tokens=2048),
    )
    ultra_request = adapt_request(
        _payload("nvidia/nemotron-3-ultra-550b"),
        base_url="https://integrate.api.nvidia.com/v1",
        configured_provider="auto",
        options=LLMOptions(reasoning_effort="high", reasoning_budget_tokens=4096),
    )
    nano_request = adapt_request(
        _payload("nvidia/nvidia-nemotron-nano-9b-v2"),
        base_url="https://integrate.api.nvidia.com/v1",
        configured_provider="auto",
        options=LLMOptions(reasoning_effort="medium", reasoning_budget_tokens=512),
    )

    assert super_request.payload["chat_template_kwargs"] == {
        "enable_thinking": True,
        "low_effort": True,
        "reasoning_budget": 2048,
    }
    assert ultra_request.payload["thinking_token_budget"] == 4096
    assert nano_request.payload["nvext"] == {"max_thinking_tokens": 512}


def test_nvidia_adapter_disables_thinking_without_sending_a_budget():
    request = adapt_request(
        _payload("nvidia/nemotron-3-super"),
        base_url="https://integrate.api.nvidia.com/v1",
        configured_provider="auto",
        options=LLMOptions(reasoning_effort="off", reasoning_budget_tokens=2048),
    )

    assert request.payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert "thinking_token_budget" not in request.payload


def test_provider_message_fields_are_scoped_to_the_active_protocol():
    assert provider_message_fields(
        "https://openrouter.ai/api/v1", "model", "auto"
    ) == frozenset({"reasoning", "reasoning_content", "reasoning_details"})
    assert provider_message_fields(
        "https://strict.example/v1", "model", "auto"
    ) == frozenset()
