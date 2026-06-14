from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from functools import cached_property
from pathlib import Path
from typing import Any, Iterable, Mapping


class PitUniverseCoverageError(ValueError):
    """Raised when a required point-in-time universe snapshot is missing."""


@dataclass(frozen=True)
class PitUniverseRow:
    as_of: date
    symbol: str
    included: bool
    reasons: tuple[str, ...] = ()
    market: str | None = None
    instrument_type: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "symbol": self.symbol,
            "included": self.included,
            "reasons": list(self.reasons),
            "market": self.market,
            "instrument_type": self.instrument_type,
        }


@dataclass(frozen=True)
class PitUniverse:
    rows: tuple[PitUniverseRow, ...]

    @cached_property
    def _rows_by_date(self) -> dict[date, tuple[PitUniverseRow, ...]]:
        grouped: dict[date, list[PitUniverseRow]] = {}
        for row in self.rows:
            grouped.setdefault(row.as_of, []).append(row)
        return {key: tuple(value) for key, value in grouped.items()}

    @cached_property
    def _eligible_by_date(self) -> dict[date, frozenset[str]]:
        return {
            key: frozenset(row.symbol for row in rows if row.included)
            for key, rows in self._rows_by_date.items()
        }

    def snapshot_dates(self) -> tuple[date, ...]:
        return tuple(sorted(self._rows_by_date))

    def rows_for(self, as_of: date | datetime) -> tuple[PitUniverseRow, ...]:
        key = _as_date(as_of)
        return self._rows_by_date.get(key, ())

    def require_rows_for(self, as_of: date | datetime) -> tuple[PitUniverseRow, ...]:
        key = _as_date(as_of)
        rows = self.rows_for(key)
        if not rows:
            raise PitUniverseCoverageError(
                f"missing PIT universe coverage for {key.isoformat()}"
            )
        return rows

    def eligible_symbols(self, as_of: date | datetime) -> frozenset[str]:
        key = _as_date(as_of)
        self.require_rows_for(key)
        return self._eligible_by_date[key]

    def is_eligible(self, as_of: date | datetime, symbol: str) -> bool:
        return symbol in self.eligible_symbols(as_of)


def load_pit_universe_csv(path: str | Path) -> PitUniverse:
    """Load point-in-time universe snapshots.

    Required CSV columns:
    - `as_of` or `date`: snapshot date
    - `symbol`: symbol as known on that date
    - `included` or `eligible`: boolean inclusion flag

    Optional columns:
    - `reasons`: semicolon/pipe/comma-separated reasons
    - `market`
    - `instrument_type` or `type`
    """

    rows: list[PitUniverseRow] = []
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            rows.append(_row_from_csv(raw))
    return PitUniverse(rows=tuple(sorted(rows, key=lambda row: (row.as_of, row.symbol))))


def write_pit_universe_csv(rows: Iterable[PitUniverseRow], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "as_of",
                "symbol",
                "included",
                "reasons",
                "market",
                "instrument_type",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "as_of": row.as_of.isoformat(),
                    "symbol": row.symbol,
                    "included": "true" if row.included else "false",
                    "reasons": ";".join(row.reasons),
                    "market": row.market or "",
                    "instrument_type": row.instrument_type or "",
                }
            )
    return target


def pit_rows_from_universe(
    universe: Any,
    *,
    as_of: date | datetime | None = None,
) -> tuple[PitUniverseRow, ...]:
    snapshot_date = _as_date(as_of or universe.generated_at)
    rows: list[PitUniverseRow] = []
    for decision in universe.decisions:
        stock = decision.stock or {}
        rows.append(
            PitUniverseRow(
                as_of=snapshot_date,
                symbol=decision.symbol,
                included=decision.included,
                reasons=tuple(decision.reasons),
                market=_optional(
                    stock.get("market")
                    or stock.get("marketCode")
                    or stock.get("exchange")
                ),
                instrument_type=_optional(
                    stock.get("instrument_type")
                    or stock.get("instrumentType")
                    or stock.get("type")
                    or stock.get("stockType")
                ),
            )
        )
    return tuple(rows)


def _row_from_csv(raw: Mapping[str, str]) -> PitUniverseRow:
    as_of = _parse_date(_first(raw, "as_of", "date"))
    symbol = _first(raw, "symbol", "ticker").strip()
    if not symbol:
        raise ValueError("PIT universe row has empty symbol")
    return PitUniverseRow(
        as_of=as_of,
        symbol=symbol,
        included=_parse_bool(_first(raw, "included", "eligible", default="true")),
        reasons=_parse_reasons(raw.get("reasons") or raw.get("reason") or ""),
        market=_optional(raw.get("market")),
        instrument_type=_optional(raw.get("instrument_type") or raw.get("type")),
    )


def _first(raw: Mapping[str, str], *names: str, default: str | None = None) -> str:
    for name in names:
        value = raw.get(name)
        if value not in (None, ""):
            return str(value)
    if default is not None:
        return default
    raise ValueError(f"missing PIT universe CSV column; expected one of {', '.join(names)}")


def _parse_date(value: str) -> date:
    return datetime.fromisoformat(value).date()


def _parse_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "t", "yes", "y", "included"}


def _parse_reasons(value: str) -> tuple[str, ...]:
    parts = value.replace("|", ";").replace(",", ";").split(";")
    return tuple(part.strip() for part in parts if part.strip())


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    clean = str(value).strip()
    return clean or None


def _as_date(value: date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value
