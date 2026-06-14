from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from time import sleep
from typing import Iterable


@dataclass(frozen=True)
class KrxDownloadResult:
    symbol: str
    rows: int
    raw_path: Path
    normalized_path: Path
    error: str | None = None


def download_krx_ohlcv(
    *,
    symbols: Iterable[str],
    start: str,
    end: str,
    raw_dir: str | Path = "data/raw/krx",
    normalized_dir: str | Path = "data/normalized/krx",
    sleep_seconds: float = 1.0,
    adjusted: bool = True,
    continue_on_error: bool = False,
) -> tuple[KrxDownloadResult, ...]:
    """Download KRX daily OHLCV copies and normalized backtest CSV files."""

    try:
        from pykrx import stock
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "pykrx is required. Install with: python -m pip install -e .[data]"
        ) from exc

    raw_base = Path(raw_dir)
    normalized_base = Path(normalized_dir)
    raw_base.mkdir(parents=True, exist_ok=True)
    normalized_base.mkdir(parents=True, exist_ok=True)

    results: list[KrxDownloadResult] = []
    clean_symbols = tuple(_clean_symbols(symbols))
    for index, symbol in enumerate(clean_symbols):
        if index and sleep_seconds > 0:
            sleep(sleep_seconds)

        raw_path = raw_base / f"{symbol}-{start}-{end}.csv"
        normalized_path = normalized_base / f"{symbol}-{start}-{end}.csv"
        try:
            frame = stock.get_market_ohlcv(
                start,
                end,
                symbol,
                adjusted=adjusted,
            )
            if frame.empty:
                raise RuntimeError("empty OHLCV data")
            frame.to_csv(raw_path, encoding="utf-8-sig")
            normalized = normalize_krx_ohlcv_frame(frame, symbol=symbol)
            normalized.to_csv(normalized_path, index=False, encoding="utf-8")
            results.append(
                KrxDownloadResult(
                    symbol=symbol,
                    rows=len(normalized),
                    raw_path=raw_path,
                    normalized_path=normalized_path,
                )
            )
            continue
        except Exception as exc:
            if not continue_on_error:
                raise
            results.append(
                KrxDownloadResult(
                    symbol=symbol,
                    rows=0,
                    raw_path=raw_path,
                    normalized_path=normalized_path,
                    error=str(exc),
                )
            )
            continue

    return tuple(results)


def normalize_krx_ohlcv_frame(frame, *, symbol: str):
    """Convert a pykrx OHLCV DataFrame to the backtest CSV contract."""

    normalized = frame.reset_index()
    date_column = normalized.columns[0]
    normalized = normalized.rename(
        columns={
            date_column: "timestamp",
            "시가": "open",
            "고가": "high",
            "저가": "low",
            "종가": "close",
            "거래량": "volume",
        }
    )
    missing = {"open", "high", "low", "close", "volume"} - set(normalized.columns)
    if missing:
        missing_text = ", ".join(sorted(missing))
        raise RuntimeError(f"KRX OHLCV data for {symbol} is missing: {missing_text}")
    normalized.insert(1, "symbol", symbol)
    return normalized[
        ["timestamp", "symbol", "open", "high", "low", "close", "volume"]
    ]


def _clean_symbols(symbols: Iterable[str]) -> Iterable[str]:
    for symbol in symbols:
        for part in str(symbol).split(","):
            clean = part.strip()
            if clean:
                if clean.isdigit() and len(clean) < 6:
                    clean = clean.zfill(6)
                yield clean


def fetch_naver_kospi200_symbols(
    *,
    pages: int = 20,
    sleep_seconds: float = 0.2,
) -> tuple[str, ...]:
    """Fetch current KOSPI200 component symbols from Naver Finance pages."""

    import requests

    symbols: list[str] = []
    for page in range(1, pages + 1):
        if page > 1 and sleep_seconds > 0:
            sleep(sleep_seconds)
        response = requests.get(
            "https://finance.naver.com/sise/entryJongmok.naver",
            params={"page": page},
            timeout=15,
        )
        response.raise_for_status()
        text = response.content.decode("euc-kr", errors="replace")
        page_symbols = re.findall(r"code=(\d{6})", text)
        if not page_symbols:
            break
        symbols.extend(page_symbols)
    return tuple(dict.fromkeys(symbols))


def write_symbols_file(symbols: Iterable[str], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    clean = tuple(dict.fromkeys(_clean_symbols(symbols)))
    target.write_text("\n".join(clean) + "\n", encoding="utf-8")
    return target
