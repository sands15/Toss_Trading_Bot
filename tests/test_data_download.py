from __future__ import annotations

import json
from pathlib import Path

from turtle_bot.cli import run
from turtle_bot.data_download import (
    KrxDownloadResult,
    fetch_naver_kospi200_symbols,
    write_symbols_file,
)


def test_cli_download_krx_ohlcv_uses_symbols_and_outputs_paths(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    symbols_path = tmp_path / "symbols.txt"
    symbols_path.write_text("# comment\n005930\n000660\n", encoding="utf-8")
    calls = {}

    def fake_download(**kwargs):
        calls.update(kwargs)
        return (
            KrxDownloadResult(
                symbol="005930",
                rows=10,
                raw_path=Path("data/raw/krx/005930.csv"),
                normalized_path=Path("data/normalized/krx/005930.csv"),
            ),
        )

    monkeypatch.setattr("turtle_bot.cli.download_krx_ohlcv", fake_download)

    result = run(
        [
            "--download-krx-ohlcv",
            "--krx-symbol",
            "005930,035420",
            "--krx-symbols-file",
            str(symbols_path),
            "--krx-start",
            "20150101",
            "--krx-end",
            "20260612",
            "--krx-sleep-seconds",
            "0",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert calls["symbols"] == ("005930", "035420", "000660")
    assert calls["start"] == "20150101"
    assert calls["end"] == "20260612"
    assert payload["items"][0]["rows"] == 10


def test_fetch_naver_kospi200_symbols_parses_codes(monkeypatch) -> None:
    class FakeResponse:
        content = (
            b'<a href="/item/main.naver?code=005930">x</a>'
            b'<a href="/item/main.naver?code=000660">y</a>'
        )

        def raise_for_status(self) -> None:
            return None

    def fake_get(*_args, **_kwargs):
        return FakeResponse()

    monkeypatch.setattr("requests.get", fake_get)

    symbols = fetch_naver_kospi200_symbols(pages=1, sleep_seconds=0)

    assert symbols == ("005930", "000660")


def test_write_symbols_file_zero_pads_numeric_symbols(tmp_path) -> None:
    path = write_symbols_file(("5930", "000660"), tmp_path / "symbols.txt")

    assert path.read_text(encoding="utf-8").splitlines() == ["005930", "000660"]
