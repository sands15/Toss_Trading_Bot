from __future__ import annotations

from typing import Any, Mapping

import pytest

from turtle_bot.ai_summary import (
    DEFAULT_AI_MODEL,
    AiSummaryConfig,
    AiSummaryError,
    OpenAICompatibleSummaryClient,
    daily_report_summary_prompt,
    extract_chat_content,
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
    assert "REPORT_JSON" in prompt


def test_openai_compatible_summary_client_uses_configured_gemma_model() -> None:
    transport = FakeChatTransport(
        {"choices": [{"message": {"content": "요약입니다."}}]}
    )
    client = OpenAICompatibleSummaryClient(
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
    assert "override blockers" in call["payload"]["messages"][0]["content"]


def test_extract_chat_content_rejects_missing_choices() -> None:
    with pytest.raises(AiSummaryError):
        extract_chat_content({"choices": []})
