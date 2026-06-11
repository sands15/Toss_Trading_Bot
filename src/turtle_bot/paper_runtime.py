from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Protocol, Sequence
from uuid import uuid4

from .domain import Candle, PositionState, Signal, SignalKind, StrategyState
from .health import HealthSnapshot
from .notifier import Notifier
from .position_sync import ReconcileResult
from .strategy import evaluate_signals


class PaperMarketDataProvider(Protocol):
    def get_completed_candles(self, symbol: str) -> Sequence[Candle]:
        ...

    def get_current_price(self, symbol: str) -> Decimal:
        ...


class PaperStateStore(Protocol):
    def load_position(self, symbol: str) -> PositionState | None:
        ...

    def list_positions(self, *, status=None) -> list[PositionState]:
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
    max_units_per_symbol: int = 4
    pyramid_step_n: Decimal = Decimal("0.5")


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
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


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
        now=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.config = config
        self.market_data = market_data
        self.position_sync = position_sync
        self.store = store
        self.notifier = notifier
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
                evaluated_symbols=(),
                generated_at=self._now(),
            )
            self._last_result = result
            return result

        blockers: list[str] = []
        intents: list[PaperOrderIntent] = []
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

            position = self.store.load_position(symbol)
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
                intent = self._intent_from_signal(signal, position=position)
                intents.append(intent)
                payload = intent.as_payload()
                self.store.record_runtime_event("INFO", "paper_order_intent", payload)
                self.notifier.notify(
                    "paper_order_intent",
                    level="info",
                    payload=payload,
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
            evaluated_symbols=tuple(evaluated_symbols),
            generated_at=self._now(),
        )
        self._last_result = result
        return result

    def health_snapshot(self) -> HealthSnapshot:
        result = self._last_result
        if result is None:
            positions = tuple(_position_payload(position) for position in self.store.list_positions())
            return HealthSnapshot(
                mode="paper",
                ready=True,
                positions=positions,
                open_orders=(),
                blockers=(),
                generated_at=self._now(),
            )

        positions = tuple(_position_payload(position) for position in self.store.list_positions())
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
    ) -> PaperOrderIntent:
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
