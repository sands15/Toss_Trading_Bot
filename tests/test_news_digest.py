from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import parse

import pytest

from turtle_news import worker
from turtle_news.worker import (
    LlmConfig,
    NewsDigestConfig,
    NewsDigestError,
    NewsItem,
    NewsStore,
    fetch_company_news,
    load_config,
    load_context,
    run_once,
)


NOW = datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, payload: object, *, status: int = 200) -> None:
        self.body = json.dumps(payload).encode("utf-8")
        self.status = status
        self.headers: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]


def _write_context(path: Path, **changes: object) -> None:
    value = {
        "schema_version": 1,
        "generated_at": NOW.isoformat(),
        "market": "US",
        "session_date": "2026-08-28",
        "active_until": (NOW + timedelta(hours=7)).isoformat(),
        "symbol": "AAPL",
        "reason": "intraday_plan",
    }
    value.update(changes)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _config(tmp_path: Path, *, llm: bool = True) -> NewsDigestConfig:
    context = tmp_path / "news-context.json"
    _write_context(context)
    return NewsDigestConfig(
        context_path=context,
        state_db=tmp_path / "news.sqlite3",
        llm=LlmConfig(
            enabled=llm,
            base_url="http://127.0.0.1:8000/v1",
            model="local-test",
        ),
    )


def _article(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "datetime": int((NOW - timedelta(minutes=5)).timestamp()),
        "headline": "Apple releases an update",
        "related": "AAPL",
        "source": "Reuters",
        "summary": "Apple released a product update.",
        "url": "https://example.com/apple-update?id=1",
    }
    value.update(changes)
    return value


def test_package_import_never_imports_trading_package() -> None:
    script = (
        "import sys, turtle_news; "
        "assert not any(x == 'turtle_bot' or x.startswith('turtle_bot.') "
        "for x in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        env={**dict(__import__("os").environ), "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_context_is_exact_fresh_current_session_and_active(tmp_path: Path) -> None:
    path = tmp_path / "context.json"
    _write_context(path)

    selected = load_context(path, now=NOW, max_age_seconds=300)

    assert selected.symbol == "AAPL"
    _write_context(path, generated_at=(NOW - timedelta(minutes=6)).isoformat())
    with pytest.raises(NewsDigestError, match="context_stale"):
        load_context(path, now=NOW, max_age_seconds=300)
    _write_context(path, extra="not allowed")
    with pytest.raises(NewsDigestError, match="context_schema_invalid"):
        load_context(path, now=NOW, max_age_seconds=300)

    _write_context(
        path,
        active_until=datetime(2026, 8, 29, 13, 0, tzinfo=timezone.utc).isoformat(),
    )
    with pytest.raises(NewsDigestError, match="context_session_invalid"):
        load_context(path, now=NOW, max_age_seconds=300)

    weekend = datetime(2026, 8, 29, 13, 0, tzinfo=timezone.utc)
    _write_context(
        path,
        generated_at=weekend.isoformat(),
        session_date="2026-08-29",
        active_until=(weekend + timedelta(hours=3)).isoformat(),
    )
    with pytest.raises(NewsDigestError, match="context_session_inactive"):
        load_context(path, now=weekend, max_age_seconds=300)


def test_config_rejects_unknown_keys_and_non_loopback_llm(tmp_path: Path) -> None:
    path = tmp_path / "news.json"
    path.write_text(
        json.dumps(
            {
                "context_path": "context.json",
                "state_db": "news.sqlite3",
                "surprise": True,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(NewsDigestError, match="config_unknown_key"):
        load_config(path)

    path.write_text(
        json.dumps(
            {
                "context_path": "context.json",
                "state_db": "news.sqlite3",
                "llm": {
                    "enabled": True,
                    "base_url": "https://llm.example.com/v1",
                    "model": "x",
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(NewsDigestError, match="config_llm_not_loopback"):
        load_config(path)


@pytest.mark.parametrize(
    "dangerous",
    ["TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "DISCORD_TRADE_ALERT_WEBHOOK_URL"],
)
def test_news_process_refuses_trading_environment(
    tmp_path: Path, dangerous: str
) -> None:
    with pytest.raises(
        NewsDigestError, match="trading_credentials_in_news_environment"
    ):
        run_once(_config(tmp_path), env={dangerous: "even-empty-presence-matters"}, now=NOW)


def test_news_process_rejects_webhook_for_other_channel(
    tmp_path: Path, monkeypatch
) -> None:
    calls = []

    def fake_open(req, timeout):
        calls.append(req)
        if req.full_url.startswith("https://discord.com") and req.get_method() == "GET":
            return FakeResponse({"channel_id": "223456789012345678"})
        raise AssertionError("channel mismatch must stop before other network calls")

    monkeypatch.setattr(worker, "_open_url", fake_open)

    with pytest.raises(NewsDigestError, match="discord_channel_mismatch"):
        run_once(
            _config(tmp_path),
            env={
                "FINNHUB_API_KEY": "feed-secret",
                "DISCORD_NEWS_WEBHOOK_URL": "https://discord.com/api/webhooks/123/token",
                "DISCORD_ALLOWED_CHANNEL_ID": "123456789012345678",
            },
            now=NOW,
        )

    assert len(calls) == 1
    assert calls[0].get_method() == "GET"


def test_finnhub_filters_exact_symbol_age_and_https(monkeypatch) -> None:
    calls = []

    def fake_open(req, timeout):
        calls.append(req)
        return FakeResponse(
            [
                _article(),
                _article(related="AA", url="https://example.com/wrong"),
                _article(
                    datetime=int((NOW - timedelta(hours=25)).timestamp()),
                    url="https://example.com/old",
                ),
                _article(url="http://example.com/insecure"),
            ]
        )

    monkeypatch.setattr(worker, "_open_url", fake_open)

    items = fetch_company_news(
        symbol="AAPL",
        api_key="feed-secret",
        now=NOW,
        max_age=timedelta(hours=24),
        timeout=4,
    )

    assert [item.url for item in items] == [
        "https://example.com/apple-update?id=1"
    ]
    query = parse.parse_qs(parse.urlsplit(calls[0].full_url).query)
    assert query["symbol"] == ["AAPL"]
    assert calls[0].get_header("X-finnhub-token") == "feed-secret"
    assert "feed-secret" not in calls[0].full_url


def test_news_store_refuses_trading_db_and_deduplicates_with_lease(
    tmp_path: Path,
) -> None:
    trading = tmp_path / "trading.sqlite3"
    connection = sqlite3.connect(trading)
    connection.execute("CREATE TABLE intraday_plans (id TEXT)")
    connection.commit()
    connection.close()
    original_trading_bytes = trading.read_bytes()
    with pytest.raises(NewsDigestError, match="trading_db_forbidden"):
        NewsStore(trading)
    assert trading.read_bytes() == original_trading_bytes

    item = NewsItem(
        "AAPL",
        "Headline",
        "Excerpt",
        "Reuters",
        "https://example.com/article",
        NOW - timedelta(minutes=1),
    )
    with NewsStore(tmp_path / "news.sqlite3") as store:
        assert store.insert_items(
            [item, item], session_date="2026-08-28", now=NOW
        ) == 1
        first = store.claim(
            symbol="AAPL",
            session_date="2026-08-28",
            now=NOW,
            oldest_allowed=NOW - timedelta(hours=24),
        )
        assert first is not None
        assert (
            store.claim(
                symbol="AAPL",
                session_date="2026-08-28",
                now=NOW + timedelta(seconds=1),
                oldest_allowed=NOW - timedelta(hours=24),
            )
            is None
        )
        recovered = store.claim(
            symbol="AAPL",
            session_date="2026-08-28",
            now=NOW + timedelta(seconds=301),
            oldest_allowed=NOW - timedelta(hours=24),
        )
        assert recovered is not None
        assert recovered.claim_token != first.claim_token


def test_news_store_refuses_recovery_sidecars_without_touching_them(
    tmp_path: Path,
) -> None:
    database = tmp_path / "unknown.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE harmless (id INTEGER)")
    connection.commit()
    connection.close()
    journal = Path(f"{database}-journal")
    journal.write_bytes(b"do-not-recover")
    before = database.read_bytes()

    with pytest.raises(NewsDigestError, match="news_db_recovery_required"):
        NewsStore(database)

    assert database.read_bytes() == before
    assert journal.read_bytes() == b"do-not-recover"


def test_news_store_never_carries_pending_item_into_new_session(tmp_path: Path) -> None:
    item = NewsItem(
        "AAPL",
        "Headline",
        "Excerpt",
        "Reuters",
        "https://example.com/previous-session",
        NOW - timedelta(minutes=1),
    )
    with NewsStore(tmp_path / "news.sqlite3") as store:
        store.insert_items([item], session_date="2026-08-27", now=NOW)

        claimed = store.claim(
            symbol="AAPL",
            session_date="2026-08-28",
            now=NOW,
            oldest_allowed=NOW - timedelta(hours=24),
        )

        assert claimed is None
        status = store.connection.execute(
            "SELECT status FROM news_articles"
        ).fetchone()[0]
        assert status == "EXPIRED"


def test_news_store_expires_poison_item_after_three_failed_attempts(
    tmp_path: Path,
) -> None:
    item = NewsItem(
        "AAPL",
        "Headline",
        "Excerpt",
        "Reuters",
        "https://example.com/poison",
        NOW - timedelta(minutes=1),
    )
    with NewsStore(tmp_path / "news.sqlite3") as store:
        store.insert_items([item], session_date="2026-08-28", now=NOW)
        for attempt in range(3):
            tick = NOW + timedelta(seconds=attempt)
            claim = store.claim(
                symbol="AAPL",
                session_date="2026-08-28",
                now=tick,
                oldest_allowed=tick - timedelta(hours=24),
            )
            assert claim is not None
            store.release(claim, error_code="discord_send_failed", now=tick)

        assert (
            store.claim(
                symbol="AAPL",
                session_date="2026-08-28",
                now=NOW + timedelta(seconds=4),
                oldest_allowed=NOW - timedelta(hours=24),
            )
            is None
        )
        assert store.connection.execute(
            "SELECT status FROM news_articles"
        ).fetchone()[0] == "EXPIRED"


def test_invalid_llm_falls_back_and_discord_marks_sent_once(
    tmp_path: Path, monkeypatch
) -> None:
    requests = []

    def fake_open(req, timeout):
        requests.append(req)
        if req.full_url.startswith(worker.FINNHUB_COMPANY_NEWS_URL):
            return FakeResponse([_article()])
        if req.full_url.startswith("http://127.0.0.1:8000"):
            return FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": "TSLA를 999에 매수 추천 https://bad.test @everyone"
                            }
                        }
                    ]
                }
            )
        if req.full_url.startswith("https://discord.com") and req.get_method() == "GET":
            return FakeResponse({"channel_id": "123456789012345678"})
        if req.full_url.startswith("https://discord.com"):
            return FakeResponse({"id": "message-1"})
        raise AssertionError(req.full_url)

    monkeypatch.setattr(worker, "_open_url", fake_open)
    config = _config(tmp_path)
    env = {
        "FINNHUB_API_KEY": "feed-secret",
        "DISCORD_NEWS_WEBHOOK_URL": "https://discord.com/api/webhooks/123/token",
        "DISCORD_ALLOWED_CHANNEL_ID": "123456789012345678",
    }

    first = run_once(config, env=env, now=NOW)
    second = run_once(config, env=env, now=NOW + timedelta(seconds=1))

    assert first.sent == 1
    assert first.source_fallbacks == 1
    assert first.error_codes == ("llm_output_rejected",)
    assert second.sent == 0
    discord_calls = [
        req for req in requests if req.full_url.startswith("https://discord.com")
    ]
    assert len(discord_calls) == 4
    assert len([req for req in discord_calls if req.get_method() == "GET"]) == 3
    discord_posts = [req for req in discord_calls if req.get_method() == "POST"]
    assert len(discord_posts) == 1
    assert parse.parse_qs(parse.urlsplit(discord_posts[0].full_url).query) == {
        "wait": ["true"]
    }
    body = json.loads(discord_posts[0].data)
    assert body["allowed_mentions"] == {"parse": []}
    assert "TSLA" not in body["content"]
    with sqlite3.connect(config.state_db) as connection:
        row = connection.execute(
            "SELECT status, summary_kind, cached_summary, last_error_code "
            "FROM news_articles"
        ).fetchone()
    assert row == (
        "SENT",
        "source",
        "요약을 생략했습니다. 원문을 확인하세요.",
        "llm_output_rejected",
    )


def test_discord_failure_retries_cached_llm_without_second_llm_call(
    tmp_path: Path, monkeypatch
) -> None:
    counts = {"llm": 0, "discord_get": 0, "discord_post": 0}

    def fake_open(req, timeout):
        if req.full_url.startswith(worker.FINNHUB_COMPANY_NEWS_URL):
            return FakeResponse([_article()])
        if req.full_url.startswith("http://127.0.0.1:8000"):
            counts["llm"] += 1
            return FakeResponse(
                {"choices": [{"message": {"content": "Apple이 업데이트를 공개했습니다."}}]}
            )
        if req.full_url.startswith("https://discord.com") and req.get_method() == "GET":
            counts["discord_get"] += 1
            return FakeResponse({"channel_id": "123456789012345678"})
        if req.full_url.startswith("https://discord.com"):
            counts["discord_post"] += 1
            if counts["discord_post"] == 1:
                raise OSError("secret canary must never be persisted")
            return FakeResponse({"id": "message-1"})
        raise AssertionError(req.full_url)

    monkeypatch.setattr(worker, "_open_url", fake_open)
    config = _config(tmp_path)
    env = {
        "FINNHUB_API_KEY": "feed-secret",
        "DISCORD_NEWS_WEBHOOK_URL": "https://discord.com/api/webhooks/123/token",
        "DISCORD_ALLOWED_CHANNEL_ID": "123456789012345678",
    }

    first = run_once(config, env=env, now=NOW)
    second = run_once(config, env=env, now=NOW + timedelta(seconds=1))

    assert first.sent == 0
    assert first.error_codes == ("discord_send_failed",)
    assert second.sent == 1
    assert counts == {"llm": 1, "discord_get": 4, "discord_post": 2}
    with sqlite3.connect(config.state_db) as connection:
        row = connection.execute(
            "SELECT status, attempt_count, cached_summary, last_error_code "
            "FROM news_articles"
        ).fetchone()
    assert row == (
        "SENT",
        2,
        "Apple이 업데이트를 공개했습니다.",
        "discord_send_failed",
    )
    assert "canary" not in str(row)


def test_context_is_rechecked_after_slow_summary_before_discord(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path, llm=False)
    _write_context(
        config.context_path,
        active_until=(NOW + timedelta(seconds=2)).isoformat(),
    )
    calls = {"discord_get": 0, "discord_post": 0}

    def fake_open(req, timeout):
        if req.full_url.startswith(worker.FINNHUB_COMPANY_NEWS_URL):
            return FakeResponse([_article()])
        if req.full_url.startswith("https://discord.com") and req.get_method() == "GET":
            calls["discord_get"] += 1
            return FakeResponse({"channel_id": "123456789012345678"})
        if req.full_url.startswith("https://discord.com"):
            calls["discord_post"] += 1
            return FakeResponse({"id": "must-not-send"})
        raise AssertionError(req.full_url)

    ticks = iter(
        [
            NOW,
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=3),
            NOW + timedelta(seconds=3),
        ]
    )
    monkeypatch.setattr(worker, "_open_url", fake_open)

    result = run_once(
        config,
        env={
            "FINNHUB_API_KEY": "feed-secret",
            "DISCORD_NEWS_WEBHOOK_URL": "https://discord.com/api/webhooks/123/token",
            "DISCORD_ALLOWED_CHANNEL_ID": "123456789012345678",
        },
        clock=lambda: next(ticks),
    )

    assert result.sent == 0
    assert result.error_codes == ("context_inactive",)
    assert calls == {"discord_get": 1, "discord_post": 0}
    with sqlite3.connect(config.state_db) as connection:
        assert connection.execute(
            "SELECT status FROM news_articles"
        ).fetchone()[0] == "EXPIRED"


def test_discord_channel_metadata_is_rechecked_before_every_article(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path, llm=False)
    counts = {"metadata": 0, "posts": 0}

    def fake_open(req, timeout):
        if req.full_url.startswith(worker.FINNHUB_COMPANY_NEWS_URL):
            return FakeResponse(
                [
                    _article(url="https://example.com/first"),
                    _article(url="https://example.com/second", headline="Second item"),
                ]
            )
        if req.full_url.startswith("https://discord.com") and req.get_method() == "GET":
            counts["metadata"] += 1
            channel = (
                "123456789012345678"
                if counts["metadata"] <= 2
                else "223456789012345678"
            )
            return FakeResponse({"channel_id": channel})
        if req.full_url.startswith("https://discord.com"):
            counts["posts"] += 1
            return FakeResponse({"id": f"message-{counts['posts']}"})
        raise AssertionError(req.full_url)

    monkeypatch.setattr(worker, "_open_url", fake_open)
    result = run_once(
        config,
        env={
            "FINNHUB_API_KEY": "feed-secret",
            "DISCORD_NEWS_WEBHOOK_URL": "https://discord.com/api/webhooks/123/token",
            "DISCORD_ALLOWED_CHANNEL_ID": "123456789012345678",
        },
        now=NOW,
    )

    assert result.sent == 1
    assert counts == {"metadata": 3, "posts": 1}
