from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib import request
from urllib.error import HTTPError


DEFAULT_AI_MODEL = "bRadu/gemma-4-E2B-it-textonly"
AI_SYSTEM_PROMPT = (
    "You are an explanation-only assistant for a Turtle Trading bot. "
    "Use only the provided facts. Write concise Korean for the operator. "
    "Do not recommend trades, choose symbols, create signals, alter Turtle "
    "rules, change risk, enable live trading, or override blockers."
)


class AiClient(Protocol):
    def summarize_daily_report(self, report: Mapping[str, Any]) -> str:
        ...

    def summarize_runtime_events(self, events: list[Mapping[str, Any]]) -> str:
        ...

    def explain_situation(self, context: Mapping[str, Any]) -> str:
        ...

    def summarize_news(self, news_context: Mapping[str, Any]) -> str:
        ...


class ChatTransport(Protocol):
    def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: int,
    ) -> Mapping[str, Any]:
        ...


class UrllibChatTransport:
    def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: int,
    ) -> Mapping[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", **dict(headers)},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                parsed = json.loads(raw) if raw else {}
        except HTTPError as exc:
            raw = exc.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
            raise AiSummaryError(exc.code, parsed) from exc
        if not isinstance(parsed, Mapping):
            raise AiSummaryError(0, {"error": "non-object response"})
        return parsed


class AiSummaryError(RuntimeError):
    def __init__(self, status: int, payload: Mapping[str, Any]) -> None:
        self.status = status
        self.payload = dict(payload)
        super().__init__(f"AI summary request failed: status={status}")


@dataclass(frozen=True)
class AiSummaryConfig:
    base_url: str = "http://localhost:8000/v1"
    model: str = DEFAULT_AI_MODEL
    api_key: str | None = None
    timeout_seconds: int = 30
    max_tokens: int = 700
    temperature: float = 0.2

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"


class NullAiClient:
    """Disabled AI client that preserves trading behavior when AI is absent."""

    def summarize_daily_report(self, report: Mapping[str, Any]) -> str:
        return ""

    def summarize_runtime_events(self, events: list[Mapping[str, Any]]) -> str:
        return ""

    def explain_situation(self, context: Mapping[str, Any]) -> str:
        return ""

    def summarize_news(self, news_context: Mapping[str, Any]) -> str:
        return ""


class OpenAICompatibleAiClient:
    def __init__(
        self,
        *,
        config: AiSummaryConfig,
        transport: ChatTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibChatTransport()

    def summarize_daily_report(self, report: Mapping[str, Any]) -> str:
        return self._chat(daily_report_summary_prompt(report))

    def summarize_runtime_events(self, events: list[Mapping[str, Any]]) -> str:
        return self._chat(runtime_event_summary_prompt(events))

    def explain_situation(self, context: Mapping[str, Any]) -> str:
        return self._chat(situation_explanation_prompt(context))

    def summarize_news(self, news_context: Mapping[str, Any]) -> str:
        return self._chat(news_summary_prompt(news_context))

    def _chat(self, user_prompt: str) -> str:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": AI_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        headers = {}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        response = self.transport.post_json(
            self.config.chat_completions_url,
            headers=headers,
            payload=payload,
            timeout=self.config.timeout_seconds,
        )
        return extract_chat_content(response)


OpenAICompatibleSummaryClient = OpenAICompatibleAiClient


def daily_report_summary_prompt(report: Mapping[str, Any]) -> str:
    return _facts_prompt(
        title="postmarket daily report",
        instructions=[
            "오늘 상태",
            "주요 runtime event와 blocker",
            "watchlist/position 변화",
            "내일 확인할 운영 항목",
        ],
        facts=report,
    )


def runtime_event_summary_prompt(events: list[Mapping[str, Any]]) -> str:
    return _facts_prompt(
        title="runtime event summary",
        instructions=[
            "event 수준별 요약",
            "반복 blocker 또는 이상 징후",
            "운영자가 확인할 로그 포인트",
        ],
        facts={"events": events},
    )


def situation_explanation_prompt(context: Mapping[str, Any]) -> str:
    return _facts_prompt(
        title="situation explanation",
        instructions=[
            "현재 상황을 operator가 이해하기 쉽게 설명",
            "차단된 이유가 있으면 기록된 blocker 기준으로 설명",
            "다음 확인 항목을 매매 지시가 아닌 점검 항목으로 작성",
        ],
        facts=context,
    )


def news_summary_prompt(news_context: Mapping[str, Any]) -> str:
    return _facts_prompt(
        title="news summary",
        instructions=[
            "뉴스/공시 내용을 사실 중심으로 요약",
            "관련 종목은 입력에 포함된 항목만 언급",
            "매수/매도 의견이나 신규 종목 추천은 하지 않음",
        ],
        facts=news_context,
    )


def _facts_prompt(
    *,
    title: str,
    instructions: list[str],
    facts: Mapping[str, Any],
) -> str:
    compact = json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str)
    instruction_lines = "\n".join(f"- {item}" for item in instructions)
    return (
        f"아래 {title} 자료를 한국어로 요약해줘.\n"
        "반드시 지킬 것:\n"
        "- 제공된 기록과 사실만 사용한다.\n"
        "- 매수/매도 추천을 하지 않는다.\n"
        "- 종목을 새로 고르거나 제외하지 않는다.\n"
        "- Turtle rules, OrderGuard, reconciliation, market-calendar blocker를 "
        "override하지 않는다.\n"
        "- AI 응답은 설명용이며 trading state의 source of truth가 아니다.\n\n"
        "포함할 내용:\n"
        f"{instruction_lines}\n\n"
        f"FACTS_JSON:\n{compact}"
    )


def extract_chat_content(response: Mapping[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AiSummaryError(0, {"error": "missing choices"})
    first = choices[0]
    if not isinstance(first, Mapping):
        raise AiSummaryError(0, {"error": "invalid choice"})
    message = first.get("message")
    if isinstance(message, Mapping) and message.get("content") is not None:
        return str(message["content"]).strip()
    if first.get("text") is not None:
        return str(first["text"]).strip()
    raise AiSummaryError(0, {"error": "missing content"})
