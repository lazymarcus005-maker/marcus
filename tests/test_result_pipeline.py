from harness.runtime.result_pipeline import truncate_result


def test_small_dict_passes_through_unchanged():
    result = truncate_result({"hits": ["a", "b"]}, max_chars=1000)
    assert result == {"hits": ["a", "b"]}


def test_non_dict_value_is_wrapped():
    result = truncate_result(42, max_chars=1000)
    assert result == {"value": 42}

    result = truncate_result("plain string", max_chars=1000)
    assert result == {"value": "plain string"}


def test_oversized_result_is_truncated_with_head_and_tail():
    big = {"content": "x" * 5000}
    result = truncate_result(big, max_chars=200)

    assert result["_truncated"] is True
    assert result["_original_length"] > 200
    assert len(result["content"]) < result["_original_length"]
    assert result["content"].startswith('{"content":"')
    assert "truncated" in result["content"]


def test_truncation_is_deterministic_and_idempotent_on_shape():
    big = {"content": "y" * 10_000}
    first = truncate_result(big, max_chars=500)
    second = truncate_result(big, max_chars=500)
    assert first == second
