from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib import request
from urllib.error import HTTPError


DEFAULT_AI_MODEL = "bRadu/gemma-4-E2B-it-textonly"


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


class OpenAICompatibleSummaryClient:
    def __init__(
        self,
        *,
        config: AiSummaryConfig,
        transport: ChatTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibChatTransport()

    def summarize_daily_report(self, report: Mapping[str, Any]) -> str:
        payload = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You summarize trading bot reports in Korean. "
                        "Use only the provided facts. Do not recommend trades, "
                        "choose symbols, alter Turtle rules, or override blockers."
                    ),
                },
                {
                    "role": "user",
                    "content": daily_report_summary_prompt(report),
                },
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


def daily_report_summary_prompt(report: Mapping[str, Any]) -> str:
    compact = json.dumps(report, ensure_ascii=False, sort_keys=True)
    return (
        "아래 postmarket daily report를 한국어로 요약해줘.\n"
        "반드시 지킬 것:\n"
        "- 기록된 사실만 사용한다.\n"
        "- 매수/매도 추천을 하지 않는다.\n"
        "- 종목을 새로 고르거나 제외하지 않는다.\n"
        "- 터틀 원칙, OrderGuard, reconciliation, market-calendar blocker를 "
        "절대 override하지 않는다.\n"
        "- 섹션: 오늘 상태, 주요 이벤트, blocker, watchlist/position 변화, "
        "내일 확인할 점.\n\n"
        f"REPORT_JSON:\n{compact}"
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

