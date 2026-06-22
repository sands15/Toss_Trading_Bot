from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from uuid import uuid4

from .domain import (
    Candle,
    PositionState,
    PositionStatus,
    Signal,
    SignalKind,
    StrategyState,
    TurtleSystem,
    UnitState,
)
from .health import HealthSnapshot
from .notifier import Notifier
from .position_sync import ReconcileResult
from .strategy import build_indicator_snapshot, evaluate_signals
from .universe import average_traded_value


class PaperMarketDataProvider(Protocol):
    def get_completed_candles(self, symbol: str) -> Sequence[Candle]:
        ...

    def get_current_price(self, symbol: str) -> Decimal:
        ...


class PaperStateStore(Protocol):
    def load_position(self, symbol: str) -> PositionState | None:
        ...

    def load_paper_position(self, symbol: str) -> PositionState | None:
        ...

    def list_positions(self, *, status=None) -> list[PositionState]:
        ...

    def list_paper_positions(self, *, status=None) -> list[PositionState]:
        ...

    def save_position(self, position: PositionState) -> None:
        ...

    def save_paper_position(self, position: PositionState) -> None:
        ...

    def record_runtime_event(
        self,
        level: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        ...


class PaperPositionSync(Protocol):
    def reconcile(self) -> ReconcileResult:
        ...


@dataclass(frozen=True)
class PaperRuntimeConfig:
    symbols: tuple[str, ...]
    mode: str = "paper"
    unit_qty: Decimal = Decimal("1")
    initial_equity: Decimal = Decimal("100000")
    strategy_kind: str = "turtle"
    minimum_tick: Decimal = Decimal("0")
    n_method: str = "turtle"
    stop_n: Decimal = Decimal("2")
    max_units_per_symbol: int = 4
    pyramid_step_n: Decimal = Decimal("0.5")
    simulate_fills: bool = True
    require_clean_reconcile: bool = True
    momentum_market_symbol: str = "SPY"
    momentum_lookback_days: int = 126
    momentum_skip_days: int = 21
    momentum_trend_ma_days: int = 200
    momentum_exit_ma_days: int = 75
    momentum_max_positions: int = 5
    momentum_max_exposure_pct: Decimal = Decimal("0.50")
    momentum_accept_top_n: int = 2
    momentum_target_position_pct: Decimal = Decimal("0.10")
    momentum_min_price: Decimal = Decimal("5")
    momentum_min_average_daily_value: Decimal = Decimal("50000000")
    momentum_average_daily_value_days: int = 20
    momentum_use_market_filter: bool = True


@dataclass(frozen=True)
class PaperOrderIntent:
    intent_id: str
    symbol: str
    side: str
    signal_kind: str
    system: str
    quantity: Decimal
    trigger_price: Decimal
    observed_price: Decimal
    fill_price: Decimal
    entry_n: Decimal | None
    stop_price: Decimal | None
    source_signal_id: str
    reason: str
    created_at: datetime
    mode: str = "paper"

    def as_payload(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "symbol": self.symbol,
            "side": self.side,
            "signal_kind": self.signal_kind,
            "system": self.system,
            "quantity": str(self.quantity),
            "trigger_price": str(self.trigger_price),
            "observed_price": str(self.observed_price),
            "fill_price": str(self.fill_price),
            "entry_n": str(self.entry_n) if self.entry_n is not None else None,
            "stop_price": str(self.stop_price) if self.stop_price is not None else None,
            "source_signal_id": self.source_signal_id,
            "reason": self.reason,
            "created_at": self.created_at.isoformat(),
            "mode": self.mode,
        }


@dataclass(frozen=True)
class PaperRunResult:
    ready: bool
    blockers: tuple[str, ...]
    reconcile: ReconcileResult
    intents: tuple[PaperOrderIntent, ...]
    evaluated_symbols: tuple[str, ...]
    guard_results: tuple["GuardResult", ...] = field(default_factory=tuple)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class GuardCheck:
    name: str
    passed: bool
    message: str


@dataclass(frozen=True)
class GuardResult:
    intent_id: str
    passed: bool
    checks: tuple[GuardCheck, ...]

    @property
    def blockers(self) -> tuple[str, ...]:
        return tuple(check.message for check in self.checks if not check.passed)

    def as_payload(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "passed": self.passed,
            "checks": [
                {
                    "name": check.name,
                    "passed": check.passed,
                    "message": check.message,
                }
                for check in self.checks
            ],
            "blockers": list(self.blockers),
        }


class PaperOrderGuard:
    def __init__(self, *, require_clean_reconcile: bool = True) -> None:
        self.require_clean_reconcile = require_clean_reconcile

    def validate(
        self,
        intent: PaperOrderIntent,
        *,
        position: PositionState | None,
        reconcile: ReconcileResult,
    ) -> GuardResult:
        checks = [
            GuardCheck(
                name="reconcile_clean",
                passed=reconcile.clean or not self.require_clean_reconcile,
                message=(
                    "reconcile is clean"
                    if reconcile.clean
                    else (
                        "reconcile issues allowed in shadow mode"
                        if not self.require_clean_reconcile
                        else "reconcile has blockers"
                    )
                ),
            ),
            GuardCheck(
                name="quantity_positive",
                passed=intent.quantity > Decimal("0"),
                message="quantity is positive" if intent.quantity > Decimal("0") else "quantity is not positive",
            ),
            GuardCheck(
                name="buy_has_n",
                passed=(
                    intent.side != "BUY"
                    or intent.system == TurtleSystem.MOMENTUM.value
                    or (intent.entry_n is not None and intent.entry_n > Decimal("0"))
                ),
                message=(
                    "buy has entry N"
                    if intent.side != "BUY"
                    or intent.system == TurtleSystem.MOMENTUM.value
                    or intent.entry_n
                    else "buy missing entry N"
                ),
            ),
            GuardCheck(
                name="sell_has_position",
                passed=intent.side != "SELL" or position is not None,
                message="sell has local position" if intent.side != "SELL" or position is not None else "sell missing local position",
            ),
        ]
        return GuardResult(
            intent_id=intent.intent_id,
            passed=all(check.passed for check in checks),
            checks=tuple(checks),
        )


class PaperBrokerSimulator:
    """Applies paper-only fills to local state. It never calls broker APIs."""

    def __init__(self, store: PaperStateStore) -> None:
        self.store = store

    def fill(self, intent: PaperOrderIntent) -> PositionState | None:
        current = self.store.load_paper_position(intent.symbol)
        if intent.side == "BUY":
            if intent.stop_price is None:
                raise ValueError("paper BUY fill requires stop_price")
            entry_n = intent.entry_n if intent.entry_n is not None else Decimal("0")
            if current is None or current.status != PositionStatus.OPEN:
                position = PositionState(
                    symbol=intent.symbol,
                    system=TurtleSystem(intent.system),
                    status=PositionStatus.OPEN,
                    total_qty=intent.quantity,
                    avg_entry_price=intent.fill_price,
                    entry_n=entry_n,
                    current_stop_price=intent.stop_price,
                    last_unit_entry_price=intent.fill_price,
                    units=(
                        UnitState(
                            unit_no=1,
                            qty=intent.quantity,
                            entry_price=intent.fill_price,
                            n_at_entry=entry_n,
                            stop_price=intent.stop_price,
                            client_order_id=intent.intent_id,
                        ),
                    ),
                )
            else:
                total_qty = current.total_qty + intent.quantity
                avg_entry = (
                    current.avg_entry_price * current.total_qty
                    + intent.fill_price * intent.quantity
                ) / total_qty
                position = PositionState(
                    symbol=current.symbol,
                    system=current.system,
                    status=PositionStatus.OPEN,
                    total_qty=total_qty,
                    avg_entry_price=avg_entry,
                    entry_n=current.entry_n,
                    current_stop_price=intent.stop_price,
                    last_unit_entry_price=intent.fill_price,
                    units=current.units
                    + (
                        UnitState(
                            unit_no=len(current.units) + 1,
                            qty=intent.quantity,
                            entry_price=intent.fill_price,
                            n_at_entry=current.entry_n,
                            stop_price=intent.stop_price,
                            client_order_id=intent.intent_id,
                        ),
                    ),
                )
            self.store.save_paper_position(position)
            return position

        if intent.side == "SELL" and current is not None:
            closed = PositionState(
                symbol=current.symbol,
                system=current.system,
                status=PositionStatus.CLOSED,
                total_qty=Decimal("0"),
                avg_entry_price=current.avg_entry_price,
                entry_n=current.entry_n,
                current_stop_price=current.current_stop_price,
                last_unit_entry_price=current.last_unit_entry_price,
                units=(),
            )
            self.store.save_paper_position(closed)
            return closed
        return current


class PaperTradingRuntime:
    """One-shot paper runtime loop.

    It evaluates Turtle signals and records would-be order intents only. It does
    not submit, modify, cancel, or simulate broker fills.
    """

    def __init__(
        self,
        *,
        config: PaperRuntimeConfig,
        market_data: PaperMarketDataProvider,
        position_sync: PaperPositionSync,
        store: PaperStateStore,
        notifier: Notifier,
        order_guard: PaperOrderGuard | None = None,
        paper_broker: PaperBrokerSimulator | None = None,
        now=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.config = config
        self.market_data = market_data
        self.position_sync = position_sync
        self.store = store
        self.notifier = notifier
        self.order_guard = order_guard or PaperOrderGuard(
            require_clean_reconcile=config.require_clean_reconcile
        )
        self.paper_broker = paper_broker or PaperBrokerSimulator(store)
        self._now = now
        self._strategy_state = StrategyState()
        self._last_result: PaperRunResult | None = None

    def run_once(self) -> PaperRunResult:
        reconcile = self.position_sync.reconcile()
        if not reconcile.clean:
            payload = reconcile.as_payload()
            if self.config.require_clean_reconcile:
                blockers = reconcile.blockers
                self.store.record_runtime_event(
                    "WARN",
                    f"{self.config.mode}_reconcile_blocked",
                    payload,
                )
                self.notifier.notify(
                    f"{self.config.mode}_reconcile_blocked",
                    level="warn",
                    payload=payload,
                )
                result = PaperRunResult(
                    ready=False,
                    blockers=blockers,
                    reconcile=reconcile,
                    intents=(),
                    guard_results=(),
                    evaluated_symbols=(),
                    generated_at=self._now(),
                )
                self._last_result = result
                return result
            self.store.record_runtime_event(
                "WARN",
                f"{self.config.mode}_reconcile_warning",
                payload,
            )
            self.notifier.notify(
                f"{self.config.mode}_reconcile_warning",
                level="warn",
                payload=payload,
            )

        blockers: list[str] = []
        intents: list[PaperOrderIntent] = []
        guard_results: list[GuardResult] = []
        evaluated_symbols: list[str] = []

        if self.config.strategy_kind == "momentum":
            result = self._run_momentum_once(
                reconcile=reconcile,
                blockers=blockers,
                intents=intents,
                guard_results=guard_results,
                evaluated_symbols=evaluated_symbols,
            )
            self._last_result = result
            return result

        for symbol in self.config.symbols:
            try:
                candles = tuple(self.market_data.get_completed_candles(symbol))
                current_price = self.market_data.get_current_price(symbol)
            except Exception as exc:
                blocker = f"{symbol} market data unavailable: {exc}"
                blockers.append(blocker)
                self.store.record_runtime_event(
                    "WARN",
                    "paper_market_data_blocked",
                    {"symbol": symbol, "error": str(exc)},
                )
                if self._is_rate_limit_pause(exc):
                    self.store.record_runtime_event(
                        "WARN",
                        "market_data_rate_limit_paused",
                        {"symbol": symbol, "error": str(exc)},
                    )
                    break
                continue

            position = self._load_strategy_position(symbol)
            signals, self._strategy_state = evaluate_signals(
                symbol=symbol,
                completed_candles=candles,
                current_price=current_price,
                state=self._strategy_state,
                position=position,
                minimum_tick=self.config.minimum_tick,
                n_method=self.config.n_method,
                max_units_per_symbol=self.config.max_units_per_symbol,
                pyramid_step_n=self.config.pyramid_step_n,
            )
            evaluated_symbols.append(symbol)
            for signal in signals:
                intent = self._intent_from_signal(
                    signal,
                    position=position,
                    candles=candles,
                )
                guard_result = self.order_guard.validate(
                    intent,
                    position=position,
                    reconcile=reconcile,
                )
                guard_results.append(guard_result)
                self.store.record_runtime_event(
                    "INFO" if guard_result.passed else "WARN",
                    f"{self.config.mode}_order_guard",
                    guard_result.as_payload(),
                )
                if not guard_result.passed:
                    blockers.extend(guard_result.blockers)
                    continue

                intents.append(intent)
                payload = intent.as_payload()
                self.store.record_runtime_event(
                    "INFO",
                    f"{self.config.mode}_order_intent",
                    payload,
                )
                self.notifier.notify(
                    f"{self.config.mode}_order_intent",
                    level="info",
                    payload=payload,
                )
                if self.config.simulate_fills:
                    filled_position = self.paper_broker.fill(intent)
                    self.store.record_runtime_event(
                        "INFO",
                        f"{self.config.mode}_fill",
                        {
                            "intent": payload,
                            "position": _position_payload(filled_position)
                            if filled_position is not None
                            else None,
                        },
                    )

        ready = not blockers
        if blockers:
            self.notifier.notify(
                f"{self.config.mode}_runtime_blocked",
                level="warn",
                payload={"blockers": blockers},
            )

        result = PaperRunResult(
            ready=ready,
            blockers=tuple(blockers),
            reconcile=reconcile,
            intents=tuple(intents),
            guard_results=tuple(guard_results),
            evaluated_symbols=tuple(evaluated_symbols),
            generated_at=self._now(),
        )
        self._last_result = result
        return result

    def _run_momentum_once(
        self,
        *,
        reconcile: ReconcileResult,
        blockers: list[str],
        intents: list[PaperOrderIntent],
        guard_results: list[GuardResult],
        evaluated_symbols: list[str],
    ) -> PaperRunResult:
        positions = {
            position.symbol: position
            for position in self._list_strategy_positions(status=PositionStatus.OPEN)
            if position.system == TurtleSystem.MOMENTUM
        }
        symbols = tuple(
            dict.fromkeys(
                (
                    self.config.momentum_market_symbol,
                    *self.config.symbols,
                    *positions.keys(),
                )
            )
        )
        candles_by_symbol: dict[str, tuple[Candle, ...]] = {}
        prices: dict[str, Decimal] = {}
        for symbol in symbols:
            try:
                candles_by_symbol[symbol] = tuple(
                    self.market_data.get_completed_candles(symbol)
                )
                prices[symbol] = self.market_data.get_current_price(symbol)
            except Exception as exc:
                blockers.append(f"{symbol} market data unavailable: {exc}")
                self.store.record_runtime_event(
                    "WARN",
                    "paper_market_data_blocked",
                    {"symbol": symbol, "error": str(exc), "strategy": "momentum"},
                )
                if self._is_rate_limit_pause(exc):
                    self.store.record_runtime_event(
                        "WARN",
                        "market_data_rate_limit_paused",
                        {"symbol": symbol, "error": str(exc), "strategy": "momentum"},
                    )
                    break
                continue
            evaluated_symbols.append(symbol)

        if blockers:
            return self._build_result(
                reconcile=reconcile,
                blockers=blockers,
                intents=intents,
                guard_results=guard_results,
                evaluated_symbols=evaluated_symbols,
            )

        exited_symbols: set[str] = set()
        for symbol, position in tuple(positions.items()):
            exit_ma = _sma(
                candles_by_symbol.get(symbol, ()),
                self.config.momentum_exit_ma_days,
            )
            current_price = prices.get(symbol)
            if exit_ma is None or current_price is None or current_price >= exit_ma:
                continue
            intent = self._momentum_intent(
                symbol=symbol,
                side="SELL",
                signal_kind=SignalKind.EXIT.value,
                quantity=position.total_qty,
                price=current_price,
                trigger_price=exit_ma,
                reason="momentum_exit_ma",
                stop_price=exit_ma,
            )
            if self._record_intent(
                intent,
                position=position,
                reconcile=reconcile,
                blockers=blockers,
                intents=intents,
                guard_results=guard_results,
            ):
                exited_symbols.add(symbol)
                positions.pop(symbol, None)

        if self.config.momentum_use_market_filter and not self._momentum_market_ok(
            candles_by_symbol.get(self.config.momentum_market_symbol, ())
        ):
            self.store.record_runtime_event(
                "INFO",
                "momentum_market_filter_blocked",
                {"market_symbol": self.config.momentum_market_symbol},
            )
            return self._build_result(
                reconcile=reconcile,
                blockers=blockers,
                intents=intents,
                guard_results=guard_results,
                evaluated_symbols=evaluated_symbols,
            )

        candidates = [
            candidate
            for symbol, candles in candles_by_symbol.items()
            if symbol != self.config.momentum_market_symbol
            for candidate in (self._momentum_candidate(symbol, candles),)
            if candidate is not None
        ]
        candidates.sort(key=lambda candidate: (-candidate["score"], candidate["symbol"]))
        accepted_symbols: list[str] = []
        for candidate in candidates:
            symbol = str(candidate["symbol"])
            if len(accepted_symbols) >= self.config.momentum_accept_top_n:
                break
            if len(positions) >= self.config.momentum_max_positions:
                break
            if symbol in positions or symbol in exited_symbols:
                continue
            current_price = prices.get(symbol)
            if current_price is None or current_price <= candidate["exit_ma"]:
                continue
            quantity = self._momentum_quantity(current_price, positions, prices)
            if quantity <= Decimal("0"):
                continue
            intent = self._momentum_intent(
                symbol=symbol,
                side="BUY",
                signal_kind=SignalKind.ENTRY.value,
                quantity=quantity,
                price=current_price,
                trigger_price=current_price,
                reason="relative_momentum",
                stop_price=candidate["exit_ma"],
            )
            if self._record_intent(
                intent,
                position=None,
                reconcile=reconcile,
                blockers=blockers,
                intents=intents,
                guard_results=guard_results,
            ):
                accepted_symbols.append(symbol)
                filled = self._load_strategy_position(symbol)
                if filled is not None and filled.status == PositionStatus.OPEN:
                    positions[symbol] = filled

        self.store.record_runtime_event(
            "INFO",
            "momentum_runtime_ranked",
            {
                "recommended": [
                    {
                        "symbol": str(candidate["symbol"]),
                        "score": str(candidate["score"]),
                        "price": str(candidate["price"]),
                        "trend_ma": str(candidate["trend_ma"]),
                        "exit_ma": str(candidate["exit_ma"]),
                    }
                    for candidate in candidates[: self.config.momentum_accept_top_n]
                ],
                "accepted": accepted_symbols,
                "open_positions": sorted(positions),
            },
        )
        return self._build_result(
            reconcile=reconcile,
            blockers=blockers,
            intents=intents,
            guard_results=guard_results,
            evaluated_symbols=evaluated_symbols,
        )

    def _build_result(
        self,
        *,
        reconcile: ReconcileResult,
        blockers: list[str],
        intents: list[PaperOrderIntent],
        guard_results: list[GuardResult],
        evaluated_symbols: list[str],
    ) -> PaperRunResult:
        ready = not blockers
        if blockers:
            self.notifier.notify(
                f"{self.config.mode}_runtime_blocked",
                level="warn",
                payload={"blockers": blockers},
            )
        return PaperRunResult(
            ready=ready,
            blockers=tuple(blockers),
            reconcile=reconcile,
            intents=tuple(intents),
            guard_results=tuple(guard_results),
            evaluated_symbols=tuple(evaluated_symbols),
            generated_at=self._now(),
        )

    def _record_intent(
        self,
        intent: PaperOrderIntent,
        *,
        position: PositionState | None,
        reconcile: ReconcileResult,
        blockers: list[str],
        intents: list[PaperOrderIntent],
        guard_results: list[GuardResult],
    ) -> bool:
        guard_result = self.order_guard.validate(
            intent,
            position=position,
            reconcile=reconcile,
        )
        guard_results.append(guard_result)
        self.store.record_runtime_event(
            "INFO" if guard_result.passed else "WARN",
            f"{self.config.mode}_order_guard",
            guard_result.as_payload(),
        )
        if not guard_result.passed:
            blockers.extend(guard_result.blockers)
            return False
        intents.append(intent)
        payload = intent.as_payload()
        self.store.record_runtime_event(
            "INFO",
            f"{self.config.mode}_order_intent",
            payload,
        )
        self.notifier.notify(
            f"{self.config.mode}_order_intent",
            level="info",
            payload=payload,
        )
        if self.config.simulate_fills:
            filled_position = self.paper_broker.fill(intent)
            self.store.record_runtime_event(
                "INFO",
                f"{self.config.mode}_fill",
                {
                    "intent": payload,
                    "position": _position_payload(filled_position)
                    if filled_position is not None
                    else None,
                },
            )
        return True

    def health_snapshot(self) -> HealthSnapshot:
        result = self._last_result
        if result is None:
            positions = tuple(
                _position_payload(position)
                for position in self._list_strategy_positions()
            )
            return HealthSnapshot(
                mode=self.config.mode,
                ready=True,
                positions=positions,
                open_orders=(),
                blockers=(),
                generated_at=self._now(),
            )

        positions = tuple(
            _position_payload(position)
            for position in self._list_strategy_positions()
        )
        return HealthSnapshot(
            mode=self.config.mode,
            ready=result.ready,
            blockers=result.blockers,
            positions=positions,
            open_orders=tuple(order.as_payload() for order in result.intents),
            watchlist=tuple({"symbol": symbol} for symbol in self.config.symbols),
            generated_at=result.generated_at,
        )

    def _load_strategy_position(self, symbol: str) -> PositionState | None:
        if self.config.mode == "live":
            return self.store.load_position(symbol)
        return self.store.load_paper_position(symbol)

    @staticmethod
    def _is_rate_limit_pause(exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            "rate-limit-paused" in text
            or "요청 제한 대기" in text
            or "요청 한도를 초과" in text
            or "too-many-requests" in text
        )

    def _list_strategy_positions(self, *, status=None) -> list[PositionState]:
        if self.config.mode == "live":
            return self.store.list_positions(status=status)
        return self.store.list_paper_positions(status=status)

    def _intent_from_signal(
        self,
        signal: Signal,
        *,
        position: PositionState | None,
        candles: Sequence[Candle],
    ) -> PaperOrderIntent:
        entry_n: Decimal | None = None
        stop_price: Decimal | None = None
        if signal.kind in {SignalKind.ENTRY, SignalKind.PYRAMID}:
            if position is not None:
                entry_n = position.entry_n
            else:
                snapshot = build_indicator_snapshot(
                    symbol=signal.symbol,
                    candles=candles,
                    n_method=self.config.n_method,
                    exclude_current=False,
                )
                entry_n = snapshot.n
            if entry_n is not None:
                stop_price = signal.observed_price - self.config.stop_n * entry_n
        quantity = (
            position.total_qty
            if signal.kind in {SignalKind.EXIT, SignalKind.STOP} and position is not None
            else self.config.unit_qty
        )
        return PaperOrderIntent(
            intent_id=f"{self.config.mode}-{uuid4()}",
            symbol=signal.symbol,
            side=signal.side.value,
            signal_kind=signal.kind.value,
            system=signal.system.value,
            quantity=quantity,
            trigger_price=signal.trigger_price,
            observed_price=signal.observed_price,
            fill_price=signal.observed_price,
            entry_n=entry_n,
            stop_price=stop_price,
            source_signal_id=signal.signal_id,
            reason=signal.reason,
            created_at=self._now(),
            mode=self.config.mode,
        )

    def _momentum_market_ok(self, candles: Sequence[Candle]) -> bool:
        market_ma = _sma(candles, self.config.momentum_trend_ma_days)
        return bool(candles and market_ma is not None and candles[-1].close > market_ma)

    def _momentum_candidate(
        self,
        symbol: str,
        candles: Sequence[Candle],
    ) -> dict[str, Decimal | str] | None:
        required = max(
            self.config.momentum_lookback_days,
            self.config.momentum_trend_ma_days,
            self.config.momentum_average_daily_value_days,
            self.config.momentum_exit_ma_days,
        ) + self.config.momentum_skip_days
        if len(candles) <= required:
            return None
        price = candles[-1].close
        if price < self.config.momentum_min_price:
            return None
        average_value = average_traded_value(
            candles,
            days=self.config.momentum_average_daily_value_days,
        )
        if (
            average_value is None
            or average_value < self.config.momentum_min_average_daily_value
        ):
            return None
        trend_ma = _sma(candles, self.config.momentum_trend_ma_days)
        exit_ma = _sma(candles, self.config.momentum_exit_ma_days)
        if trend_ma is None or exit_ma is None or price <= trend_ma or price <= exit_ma:
            return None
        recent = candles[-self.config.momentum_skip_days - 1].close
        past = candles[
            -self.config.momentum_lookback_days
            - self.config.momentum_skip_days
            - 1
        ].close
        if past <= Decimal("0"):
            return None
        score = (recent - past) / past
        if score <= Decimal("0"):
            return None
        return {
            "symbol": symbol,
            "score": score,
            "price": price,
            "trend_ma": trend_ma,
            "exit_ma": exit_ma,
        }

    def _momentum_quantity(
        self,
        price: Decimal,
        positions: Mapping[str, PositionState],
        prices: Mapping[str, Decimal],
    ) -> Decimal:
        equity = self.config.initial_equity
        for symbol, position in positions.items():
            mark = prices.get(symbol, position.avg_entry_price)
            equity += (mark - position.avg_entry_price) * position.total_qty
        current_exposure = self._momentum_exposure(positions, prices)
        max_exposure = equity * self.config.momentum_max_exposure_pct
        remaining_exposure = max_exposure - current_exposure
        if remaining_exposure <= Decimal("0"):
            return Decimal("0")
        allocation = min(
            equity * self.config.momentum_target_position_pct,
            remaining_exposure,
        )
        if allocation <= Decimal("0"):
            return Decimal("0")
        return (allocation / price).to_integral_value(rounding=ROUND_FLOOR)

    def _momentum_exposure(
        self,
        positions: Mapping[str, PositionState],
        prices: Mapping[str, Decimal],
    ) -> Decimal:
        return sum(
            position.total_qty * prices.get(symbol, position.avg_entry_price)
            for symbol, position in positions.items()
        )

    def _momentum_intent(
        self,
        *,
        symbol: str,
        side: str,
        signal_kind: str,
        quantity: Decimal,
        price: Decimal,
        trigger_price: Decimal,
        reason: str,
        stop_price: Decimal,
    ) -> PaperOrderIntent:
        return PaperOrderIntent(
            intent_id=f"{self.config.mode}-{uuid4()}",
            symbol=symbol,
            side=side,
            signal_kind=signal_kind,
            system=TurtleSystem.MOMENTUM.value,
            quantity=quantity,
            trigger_price=trigger_price,
            observed_price=price,
            fill_price=price,
            entry_n=Decimal("0"),
            stop_price=stop_price,
            source_signal_id=f"sig-{uuid4()}",
            reason=reason,
            created_at=self._now(),
            mode=self.config.mode,
        )


def _position_payload(position: PositionState) -> dict[str, Any]:
    return {
        "symbol": position.symbol,
        "system": position.system.value,
        "status": position.status.value,
        "total_qty": str(position.total_qty),
        "avg_entry_price": str(position.avg_entry_price),
        "entry_n": str(position.entry_n),
        "current_stop_price": str(position.current_stop_price),
        "last_unit_entry_price": str(position.last_unit_entry_price),
        "units": len(position.units),
    }


def _sma(history: Sequence[Candle], days: int) -> Decimal | None:
    if days <= 0 or len(history) < days:
        return None
    return sum((candle.close for candle in history[-days:]), Decimal("0")) / Decimal(days)


def export_paper_report_json(result: PaperRunResult, path: str | Path) -> None:
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(paper_run_result_to_dict(result), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def paper_run_result_to_dict(result: PaperRunResult) -> dict[str, Any]:
    return {
        "ready": result.ready,
        "blockers": list(result.blockers),
        "generated_at": result.generated_at.isoformat(),
        "evaluated_symbols": list(result.evaluated_symbols),
        "reconcile": result.reconcile.as_payload(),
        "guard_results": [guard.as_payload() for guard in result.guard_results],
        "intents": [intent.as_payload() for intent in result.intents],
    }


class PaperRuntimeScheduler:
    def __init__(
        self,
        runtime: PaperTradingRuntime,
        *,
        sleep=lambda seconds: None,
    ) -> None:
        self.runtime = runtime
        self.sleep = sleep

    def run_iterations(
        self,
        iterations: int,
        *,
        interval_seconds: float = 0,
    ) -> tuple[PaperRunResult, ...]:
        results: list[PaperRunResult] = []
        for index in range(iterations):
            results.append(self.runtime.run_once())
            if interval_seconds > 0 and index < iterations - 1:
                self.sleep(interval_seconds)
        return tuple(results)
