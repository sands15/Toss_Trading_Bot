from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
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
    unit_qty: Decimal = Decimal("1")
    minimum_tick: Decimal = Decimal("0")
    n_method: str = "turtle"
    stop_n: Decimal = Decimal("2")
    max_units_per_symbol: int = 4
    pyramid_step_n: Decimal = Decimal("0.5")
    simulate_fills: bool = True


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
                passed=reconcile.clean,
                message="reconcile is clean" if reconcile.clean else "reconcile has blockers",
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
                    or (intent.entry_n is not None and intent.entry_n > Decimal("0"))
                ),
                message="buy has entry N" if intent.side != "BUY" or intent.entry_n else "buy missing entry N",
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
            if intent.entry_n is None or intent.stop_price is None:
                raise ValueError("paper BUY fill requires entry_n and stop_price")
            if current is None or current.status != PositionStatus.OPEN:
                position = PositionState(
                    symbol=intent.symbol,
                    system=TurtleSystem(intent.system),
                    status=PositionStatus.OPEN,
                    total_qty=intent.quantity,
                    avg_entry_price=intent.fill_price,
                    entry_n=intent.entry_n,
                    current_stop_price=intent.stop_price,
                    last_unit_entry_price=intent.fill_price,
                    units=(
                        UnitState(
                            unit_no=1,
                            qty=intent.quantity,
                            entry_price=intent.fill_price,
                            n_at_entry=intent.entry_n,
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
        self.order_guard = order_guard or PaperOrderGuard()
        self.paper_broker = paper_broker or PaperBrokerSimulator(store)
        self._now = now
        self._strategy_state = StrategyState()
        self._last_result: PaperRunResult | None = None

    def run_once(self) -> PaperRunResult:
        reconcile = self.position_sync.reconcile()
        if not reconcile.clean:
            blockers = reconcile.blockers
            payload = reconcile.as_payload()
            self.store.record_runtime_event("WARN", "paper_reconcile_blocked", payload)
            self.notifier.notify(
                "paper_reconcile_blocked",
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

        blockers: list[str] = []
        intents: list[PaperOrderIntent] = []
        guard_results: list[GuardResult] = []
        evaluated_symbols: list[str] = []

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
                continue

            position = self.store.load_paper_position(symbol)
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
                    "paper_order_guard",
                    guard_result.as_payload(),
                )
                if not guard_result.passed:
                    blockers.extend(guard_result.blockers)
                    continue

                intents.append(intent)
                payload = intent.as_payload()
                self.store.record_runtime_event("INFO", "paper_order_intent", payload)
                self.notifier.notify(
                    "paper_order_intent",
                    level="info",
                    payload=payload,
                )
                if self.config.simulate_fills:
                    filled_position = self.paper_broker.fill(intent)
                    self.store.record_runtime_event(
                        "INFO",
                        "paper_fill",
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
                "paper_runtime_blocked",
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

    def health_snapshot(self) -> HealthSnapshot:
        result = self._last_result
        if result is None:
            positions = tuple(
                _position_payload(position)
                for position in self.store.list_paper_positions()
            )
            return HealthSnapshot(
                mode="paper",
                ready=True,
                positions=positions,
                open_orders=(),
                blockers=(),
                generated_at=self._now(),
            )

        positions = tuple(
            _position_payload(position)
            for position in self.store.list_paper_positions()
        )
        return HealthSnapshot(
            mode="paper",
            ready=result.ready,
            blockers=result.blockers,
            positions=positions,
            open_orders=tuple(order.as_payload() for order in result.intents),
            watchlist=tuple({"symbol": symbol} for symbol in self.config.symbols),
            generated_at=result.generated_at,
        )

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
            intent_id=f"paper-{uuid4()}",
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
