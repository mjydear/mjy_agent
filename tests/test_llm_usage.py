from __future__ import annotations

from athena.infra.llm import _parse_usage


def test_parse_usage_mapping_flattens_details_and_exposes_cache_aliases() -> None:
    usage = _parse_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 25, "audio_tokens": 2},
            "completion_tokens_details": {"reasoning_tokens": 8},
        }
    )

    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 20
    assert usage["total_tokens"] == 120
    assert usage["cached_tokens"] == 25
    assert usage["cache_read_input_tokens"] == 25
    assert usage["audio_tokens"] == 2
    assert usage["reasoning_tokens"] == 8


def test_parse_usage_supports_model_dump_and_preserves_top_level_precedence() -> None:
    class DumpedUsage:
        def model_dump(self) -> dict[str, object]:
            return {
                "input_tokens": 50,
                "cache_read_input_tokens": 7,
                "input_tokens_details": {"cached_tokens": 3},
                "completion_tokens_details": {"reasoning_tokens": 11},
            }

    usage = _parse_usage(DumpedUsage())

    assert usage["input_tokens"] == 50
    assert usage["cache_read_input_tokens"] == 7
    assert usage["cached_tokens"] == 3
    assert usage["reasoning_tokens"] == 11


def test_parse_usage_supports_plain_object_and_filters_non_numeric_fields() -> None:
    class Details:
        cached_tokens = 4
        reasoning_tokens = 6
        secret = "do-not-export"

    class Usage:
        prompt_tokens = 80
        total_tokens = 80
        prompt_tokens_details = Details()
        api_key = "sk-test-secret"
        request_id = "request-123"
        invalid = None
        flag = True

    usage = _parse_usage(Usage())

    assert usage["prompt_tokens"] == 80
    assert usage["total_tokens"] == 80
    assert usage["cached_tokens"] == 4
    assert usage["cache_read_input_tokens"] == 4
    assert usage["reasoning_tokens"] == 6
    assert "api_key" not in usage
    assert "request_id" not in usage
    assert "secret" not in usage
    assert "flag" not in usage


def test_parse_usage_supports_slot_backed_objects() -> None:
    class Usage:
        __slots__ = ("prompt_tokens", "completion_tokens", "output_tokens_details")

        def __init__(self) -> None:
            self.prompt_tokens = 12
            self.completion_tokens = 5
            self.output_tokens_details = {"reasoning_tokens": 2}

    usage = _parse_usage(Usage())

    assert usage == {
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "reasoning_tokens": 2,
    }
