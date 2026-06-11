from __future__ import annotations

from typing import Any, Mapping

import pytest

from turtle_bot.ai_summary import (
    AI_SYSTEM_PROMPT,
    DEFAULT_AI_MODEL,
    AiSummaryConfig,
    AiSummaryError,
    NullAiClient,
    OpenAICompatibleAiClient,
    OpenAICompatibleSummaryClient,
    daily_report_summary_prompt,
    extract_chat_content,
    news_summary_prompt,
    runtime_event_summary_prompt,
    situation_explanation_prompt,
)


class FakeChatTransport:
    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: int,
    ) -> Mapping[str, Any]:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "payload": dict(payload),
                "timeout": timeout,
            }
        )
        return self.response


def test_daily_report_summary_prompt_preserves_ai_boundaries() -> None:
    prompt = daily_report_summary_prompt({"runtime_event_summary": {"blockers": ["x"]}})

    assert "매수/매도 추천을 하지 않는다" in prompt
    assert "종목을 새로 고르거나 제외하지 않는다" in prompt
    assert "source of truth" in prompt
    assert "FACTS_JSON" in prompt


def test_openai_compatible_ai_client_uses_configured_api_boundary() -> None:
    transport = FakeChatTransport(
        {"choices": [{"message": {"content": "요약입니다."}}]}
    )
    client = OpenAICompatibleAiClient(
        config=AiSummaryConfig(
            base_url="http://localhost:8000/v1",
            model=DEFAULT_AI_MODEL,
            api_key="secret",
            timeout_seconds=12,
        ),
        transport=transport,
    )

    summary = client.summarize_daily_report({"report_type": "postmarket_daily"})

    call = transport.calls[0]
    assert summary == "요약입니다."
    assert call["url"] == "http://localhost:8000/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer secret"
    assert call["payload"]["model"] == "bRadu/gemma-4-E2B-it-textonly"
    assert call["timeout"] == 12
    assert call["payload"]["messages"][0]["content"] == AI_SYSTEM_PROMPT
    assert "override blockers" in call["payload"]["messages"][0]["content"]


def test_openai_compatible_summary_client_alias_is_preserved() -> None:
    assert OpenAICompatibleSummaryClient is OpenAICompatibleAiClient


def test_ai_client_methods_are_explanation_only_prompts() -> None:
    transport = FakeChatTransport(
        {"choices": [{"message": {"content": "설명입니다."}}]}
    )
    client = OpenAICompatibleAiClient(
        config=AiSummaryConfig(base_url="http://local/v1"),
        transport=transport,
    )

    assert client.summarize_runtime_events([{"message": "blocked"}]) == "설명입니다."
    assert client.explain_situation({"blocker": "market_closed"}) == "설명입니다."
    assert client.summarize_news({"items": [{"symbol": "005930"}]}) == "설명입니다."

    prompts = [call["payload"]["messages"][1]["content"] for call in transport.calls]
    assert "runtime event summary" in prompts[0]
    assert "situation explanation" in prompts[1]
    assert "news summary" in prompts[2]
    assert all("매수/매도 추천을 하지 않는다" in prompt for prompt in prompts)


def test_null_ai_client_returns_empty_text_without_side_effects() -> None:
    client = NullAiClient()

    assert client.summarize_daily_report({"x": 1}) == ""
    assert client.summarize_runtime_events([{"x": 1}]) == ""
    assert client.explain_situation({"x": 1}) == ""
    assert client.summarize_news({"x": 1}) == ""


def test_prompt_builders_preserve_fact_payloads() -> None:
    assert '"symbol": "005930"' in news_summary_prompt({"symbol": "005930"})
    assert '"message": "paper_blocked"' in runtime_event_summary_prompt(
        [{"message": "paper_blocked"}]
    )
    assert '"blocker": "reconciliation_mismatch"' in situation_explanation_prompt(
        {"blocker": "reconciliation_mismatch"}
    )


def test_extract_chat_content_rejects_missing_choices() -> None:
    with pytest.raises(AiSummaryError):
        extract_chat_content({"choices": []})
