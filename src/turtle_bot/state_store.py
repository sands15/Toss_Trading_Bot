from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
import json
import sqlite3

from .domain import Candle, PositionDirection, PositionStatus, TurtleSystem, UnitState, PositionState, as_decimal
from .watchlist import Watchlist, WatchlistRow


class SQLiteStateStore:
    """SQLite-backed state store for watchlists, positions, and runtime metadata."""

    unresolved_order_statuses = frozenset(
        {
            "OPEN",
            "UNKNOWN",
            "PENDING",
            "PARTIAL_FILLED",
            "PENDING_CANCEL",
            "PENDING_REPLACE",
        }
    )
    unresolved_execution_statuses = frozenset(
        {
            "PENDING",
            "PENDING_CANCEL",
            "PENDING_REPLACE",
            "SENT",
            "ACKNOWLEDGED",
            "PARTIAL_FILLED",
            "UNKNOWN",
        }
    )

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = self._normalize_path(path)
        self._is_memory = self.path == ":memory:"
        if not self._is_memory:
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if not self._is_memory:
            try:
                self._conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError:
                pass
        self.initialize_schema()

    @staticmethod
    def _normalize_path(path: str | Path) -> str:
        if isinstance(path, Path):
            return str(path)
        return path

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SQLiteStateStore":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def initialize_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                  version INTEGER PRIMARY KEY,
                  applied_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO schema_migrations (version, applied_at)
                VALUES (1, ?)
                """,
                (self._now_iso(),),
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watchlists (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT NOT NULL,
                  generated_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watchlist_items (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  watchlist_id INTEGER NOT NULL,
                  rank INTEGER NOT NULL,
                  symbol TEXT NOT NULL,
                  current_price TEXT NOT NULL,
                  entry_high_20 TEXT,
                  entry_high_55 TEXT,
                  distance_to_20 TEXT,
                  distance_to_55 TEXT,
                  nearest_distance TEXT NOT NULL,
                  reason TEXT NOT NULL DEFAULT '',
                  is_new INTEGER NOT NULL,
                  FOREIGN KEY (watchlist_id) REFERENCES watchlists(id) ON DELETE CASCADE,
                  UNIQUE (watchlist_id, symbol)
                )
                """
            )
            self._ensure_column("watchlist_items", "reason", "TEXT NOT NULL DEFAULT ''")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS positions (
                  symbol TEXT PRIMARY KEY,
                  system TEXT NOT NULL,
                  status TEXT NOT NULL,
                  total_qty TEXT NOT NULL,
                  avg_entry_price TEXT NOT NULL,
                  entry_n TEXT NOT NULL,
                  current_stop_price TEXT NOT NULL,
                  last_unit_entry_price TEXT NOT NULL,
                  direction TEXT NOT NULL DEFAULT 'LONG'
                )
                """
            )
            self._ensure_column("positions", "direction", "TEXT NOT NULL DEFAULT 'LONG'")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS position_units (
                  position_symbol TEXT NOT NULL,
                  unit_no INTEGER NOT NULL,
                  qty TEXT NOT NULL,
                  entry_price TEXT NOT NULL,
                  n_at_entry TEXT NOT NULL,
                  stop_price TEXT NOT NULL,
                  broker_order_id TEXT,
                  client_order_id TEXT,
                  PRIMARY KEY (position_symbol, unit_no),
                  FOREIGN KEY (position_symbol) REFERENCES positions(symbol) ON DELETE CASCADE
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_positions (
                  symbol TEXT PRIMARY KEY,
                  system TEXT NOT NULL,
                  status TEXT NOT NULL,
                  total_qty TEXT NOT NULL,
                  avg_entry_price TEXT NOT NULL,
                  entry_n TEXT NOT NULL,
                  current_stop_price TEXT NOT NULL,
                  last_unit_entry_price TEXT NOT NULL,
                  direction TEXT NOT NULL DEFAULT 'LONG'
                )
                """
            )
            self._ensure_column("paper_positions", "direction", "TEXT NOT NULL DEFAULT 'LONG'")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_position_units (
                  position_symbol TEXT NOT NULL,
                  unit_no INTEGER NOT NULL,
                  qty TEXT NOT NULL,
                  entry_price TEXT NOT NULL,
                  n_at_entry TEXT NOT NULL,
                  stop_price TEXT NOT NULL,
                  broker_order_id TEXT,
                  client_order_id TEXT,
                  PRIMARY KEY (position_symbol, unit_no),
                  FOREIGN KEY (position_symbol) REFERENCES paper_positions(symbol) ON DELETE CASCADE
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS broker_orders (
                  client_order_id TEXT PRIMARY KEY,
                  symbol TEXT NOT NULL,
                  side TEXT NOT NULL,
                  status TEXT NOT NULL,
                  broker_order_id TEXT,
                  raw TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS order_intents (
                  intent_id TEXT PRIMARY KEY,
                  idempotency_key TEXT NOT NULL,
                  symbol TEXT NOT NULL,
                  side TEXT NOT NULL,
                  quantity TEXT NOT NULL,
                  order_type TEXT NOT NULL,
                  limit_price TEXT,
                  payload TEXT NOT NULL,
                  created_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_orders (
                  intent_id TEXT PRIMARY KEY,
                  idempotency_key TEXT NOT NULL,
                  symbol TEXT NOT NULL,
                  side TEXT NOT NULL,
                  status TEXT NOT NULL,
                  broker_order_id TEXT,
                  raw TEXT,
                  updated_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  intent_id TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  status TEXT NOT NULL,
                  payload TEXT,
                  created_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS market_data_snapshots (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  kind TEXT NOT NULL,
                  symbol TEXT NOT NULL,
                  captured_at TEXT NOT NULL,
                  payload TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS broker_snapshots (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  kind TEXT NOT NULL,
                  captured_at TEXT NOT NULL,
                  payload TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  level TEXT NOT NULL,
                  message TEXT NOT NULL,
                  payload TEXT,
                  created_at TEXT NOT NULL
                )
                """
            )

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        if any(row["name"] == column for row in rows):
            return
        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _now_iso(at: datetime | None = None) -> str:
        timestamp = at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        else:
            timestamp = timestamp.astimezone(timezone.utc)
        return timestamp.isoformat()

    @staticmethod
    def _to_str(value: Decimal | None) -> str | None:
        if value is None:
            return None
        return str(value)

    @staticmethod
    def _from_str(value: str | None) -> Decimal | None:
        if value is None:
            return None
        return as_decimal(value)

    @staticmethod
    def _bool_to_db(value: bool) -> int:
        return 1 if value else 0

    @staticmethod
    def _bool_from_db(value: int | bool) -> bool:
        return bool(value)

    @staticmethod
    def _json_dump(payload: Any) -> str | None:
        if payload is None:
            return None
        return json.dumps(payload, default=lambda value: str(value))

    @staticmethod
    def _json_load(raw: str | None) -> dict[str, Any] | None:
        if raw is None:
            return None
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            return loaded
        return {"value": loaded}

    def save_watchlist(self, watchlist: Watchlist, *, name: str = "premarket") -> int:
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO watchlists (name, generated_at)
                VALUES (?, ?)
                """,
                (name, self._now_iso(watchlist.generated_at)),
            )
            watchlist_id = cursor.lastrowid

            for rank, row in enumerate(watchlist.rows):
                self._conn.execute(
                    """
                    INSERT INTO watchlist_items (
                        watchlist_id,
                        rank,
                        symbol,
                        current_price,
                        entry_high_20,
                        entry_high_55,
                        distance_to_20,
                        distance_to_55,
                        nearest_distance,
                        reason,
                        is_new
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        watchlist_id,
                        rank,
                        row.symbol,
                        self._to_str(row.current_price),
                        self._to_str(row.entry_high_20),
                        self._to_str(row.entry_high_55),
                        self._to_str(row.distance_to_20),
                        self._to_str(row.distance_to_55),
                        self._to_str(row.nearest_distance),
                        row.reason,
                        self._bool_to_db(row.is_new),
                    ),
                )

        return watchlist_id

    def load_latest_watchlist(self, *, name: str = "premarket") -> Watchlist | None:
        watchlist_row = self._conn.execute(
            """
            SELECT id, generated_at
            FROM watchlists
            WHERE name = ?
            ORDER BY generated_at DESC, id DESC
            LIMIT 1
            """,
            (name,),
        ).fetchone()
        if watchlist_row is None:
            return None

        item_rows = self._conn.execute(
            """
            SELECT
                symbol,
                current_price,
                entry_high_20,
                entry_high_55,
                distance_to_20,
                distance_to_55,
                nearest_distance,
                reason,
                is_new
            FROM watchlist_items
            WHERE watchlist_id = ?
            ORDER BY rank ASC
            """,
            (watchlist_row["id"],),
        ).fetchall()

        rows = tuple(
            WatchlistRow(
                symbol=item["symbol"],
                current_price=self._from_str(item["current_price"]),
                entry_high_20=self._from_str(item["entry_high_20"]),
                entry_high_55=self._from_str(item["entry_high_55"]),
                distance_to_20=self._from_str(item["distance_to_20"]),
                distance_to_55=self._from_str(item["distance_to_55"]),
                nearest_distance=self._from_str(item["nearest_distance"]),
                reason=str(item["reason"] or ""),
                is_new=self._bool_from_db(item["is_new"]),
            )
            for item in item_rows
        )

        generated_at = datetime.fromisoformat(watchlist_row["generated_at"])
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=timezone.utc)
        return Watchlist(generated_at=generated_at, rows=rows)

    def save_position(self, position: PositionState) -> None:
        self._save_position_to_tables(
            position,
            positions_table="positions",
            units_table="position_units",
        )

    def load_position(self, symbol: str) -> PositionState | None:
        return self._load_position_from_tables(
            symbol,
            positions_table="positions",
            units_table="position_units",
        )

    def save_paper_position(self, position: PositionState) -> None:
        self._save_position_to_tables(
            position,
            positions_table="paper_positions",
            units_table="paper_position_units",
        )

    def load_paper_position(self, symbol: str) -> PositionState | None:
        return self._load_position_from_tables(
            symbol,
            positions_table="paper_positions",
            units_table="paper_position_units",
        )

    def _save_position_to_tables(
        self,
        position: PositionState,
        *,
        positions_table: str,
        units_table: str,
    ) -> None:
        with self._conn:
            self._conn.execute(
                f"""
                INSERT INTO {positions_table} (
                    symbol,
                    system,
                    status,
                    total_qty,
                    avg_entry_price,
                    entry_n,
                    current_stop_price,
                    last_unit_entry_price,
                    direction
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                  system = excluded.system,
                  status = excluded.status,
                  total_qty = excluded.total_qty,
                  avg_entry_price = excluded.avg_entry_price,
                  entry_n = excluded.entry_n,
                  current_stop_price = excluded.current_stop_price,
                  last_unit_entry_price = excluded.last_unit_entry_price,
                  direction = excluded.direction
                """,
                (
                    position.symbol,
                    position.system.value,
                    position.status.value,
                    self._to_str(position.total_qty),
                    self._to_str(position.avg_entry_price),
                    self._to_str(position.entry_n),
                    self._to_str(position.current_stop_price),
                    self._to_str(position.last_unit_entry_price),
                    position.direction.value,
                ),
            )
            self._conn.execute(
                f"DELETE FROM {units_table} WHERE position_symbol = ?",
                (position.symbol,),
            )
            for unit in position.units:
                self._conn.execute(
                    f"""
                    INSERT INTO {units_table} (
                        position_symbol,
                        unit_no,
                        qty,
                        entry_price,
                        n_at_entry,
                        stop_price,
                        broker_order_id,
                        client_order_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        position.symbol,
                        unit.unit_no,
                        self._to_str(unit.qty),
                        self._to_str(unit.entry_price),
                        self._to_str(unit.n_at_entry),
                        self._to_str(unit.stop_price),
                        unit.broker_order_id,
                        unit.client_order_id,
                    ),
                )

    def _load_position_from_tables(
        self,
        symbol: str,
        *,
        positions_table: str,
        units_table: str,
    ) -> PositionState | None:
        position_row = self._conn.execute(
            """
            SELECT
                system,
                status,
                total_qty,
                avg_entry_price,
                entry_n,
                current_stop_price,
                last_unit_entry_price,
                direction
            FROM {positions_table}
            WHERE symbol = ?
            """.format(positions_table=positions_table),
            (symbol,),
        ).fetchone()

        if position_row is None:
            return None

        unit_rows = self._conn.execute(
            """
            SELECT
                unit_no,
                qty,
                entry_price,
                n_at_entry,
                stop_price,
                broker_order_id,
                client_order_id
            FROM {units_table}
            WHERE position_symbol = ?
            ORDER BY unit_no ASC
            """.format(units_table=units_table),
            (symbol,),
        ).fetchall()

        units = tuple(
            UnitState(
                unit_no=item["unit_no"],
                qty=self._from_str(item["qty"]),
                entry_price=self._from_str(item["entry_price"]),
                n_at_entry=self._from_str(item["n_at_entry"]),
                stop_price=self._from_str(item["stop_price"]),
                broker_order_id=item["broker_order_id"],
                client_order_id=item["client_order_id"],
            )
            for item in unit_rows
        )

        return PositionState(
            symbol=symbol,
            system=TurtleSystem(position_row["system"]),
            status=PositionStatus(position_row["status"]),
            total_qty=self._from_str(position_row["total_qty"]),
            avg_entry_price=self._from_str(position_row["avg_entry_price"]),
            entry_n=self._from_str(position_row["entry_n"]),
            current_stop_price=self._from_str(position_row["current_stop_price"]),
            last_unit_entry_price=self._from_str(position_row["last_unit_entry_price"]),
            direction=PositionDirection(position_row["direction"]),
            units=units,
        )

    def list_positions(
        self,
        *,
        status: PositionStatus | str | None = None,
    ) -> list[PositionState]:
        return self._list_positions_from_tables(
            positions_table="positions",
            units_table="position_units",
            status=status,
        )

    def list_paper_positions(
        self,
        *,
        status: PositionStatus | str | None = None,
    ) -> list[PositionState]:
        return self._list_positions_from_tables(
            positions_table="paper_positions",
            units_table="paper_position_units",
            status=status,
        )

    def _list_positions_from_tables(
        self,
        *,
        positions_table: str,
        units_table: str,
        status: PositionStatus | str | None = None,
    ) -> list[PositionState]:
        params: tuple[object, ...] = ()
        sql = f"SELECT symbol FROM {positions_table}"
        if status is not None:
            status_value = status.value if isinstance(status, PositionStatus) else str(status)
            sql += " WHERE status = ?"
            params = (status_value,)
        sql += " ORDER BY symbol ASC"
        rows = self._conn.execute(sql, params).fetchall()
        positions: list[PositionState] = []
        for row in rows:
            position = self._load_position_from_tables(
                row["symbol"],
                positions_table=positions_table,
                units_table=units_table,
            )
            if position is not None:
                positions.append(position)
        return positions

    def record_broker_order(
        self,
        client_order_id: str,
        symbol: str,
        side: str,
        status: str,
        *,
        broker_order_id: str | None = None,
        raw: Any = None,
    ) -> None:
        payload = self._json_dump(raw)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO broker_orders (
                    client_order_id,
                    symbol,
                    side,
                    status,
                    broker_order_id,
                    raw
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                  symbol = excluded.symbol,
                  side = excluded.side,
                  status = excluded.status,
                  broker_order_id = excluded.broker_order_id,
                  raw = excluded.raw
                """,
                (
                    client_order_id,
                    symbol,
                    side,
                    status,
                    broker_order_id,
                    payload,
                ),
            )

    def has_unresolved_client_order_id(self, client_order_id: str) -> bool:
        row = self._conn.execute(
            "SELECT status FROM broker_orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        if row is None:
            return False
        return row["status"].upper() in self.unresolved_order_statuses

    def record_order_intent(self, intent: Any) -> None:
        payload = intent.as_payload()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO order_intents (
                    intent_id,
                    idempotency_key,
                    symbol,
                    side,
                    quantity,
                    order_type,
                    limit_price,
                    payload,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(intent_id) DO UPDATE SET
                  idempotency_key = excluded.idempotency_key,
                  symbol = excluded.symbol,
                  side = excluded.side,
                  quantity = excluded.quantity,
                  order_type = excluded.order_type,
                  limit_price = excluded.limit_price,
                  payload = excluded.payload,
                  created_at = excluded.created_at
                """,
                (
                    intent.intent_id,
                    intent.idempotency_key,
                    intent.symbol,
                    intent.side.value,
                    self._to_str(intent.quantity),
                    intent.order_type.value,
                    self._to_str(intent.limit_price),
                    self._json_dump(payload),
                    self._now_iso(intent.created_at),
                ),
            )

    def record_execution_order(
        self,
        *,
        intent_id: str,
        idempotency_key: str,
        symbol: str,
        side: str,
        status: str,
        broker_order_id: str | None = None,
        raw: Any = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO execution_orders (
                    intent_id,
                    idempotency_key,
                    symbol,
                    side,
                    status,
                    broker_order_id,
                    raw,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(intent_id) DO UPDATE SET
                  idempotency_key = excluded.idempotency_key,
                  symbol = excluded.symbol,
                  side = excluded.side,
                  status = excluded.status,
                  broker_order_id = excluded.broker_order_id,
                  raw = excluded.raw,
                  updated_at = excluded.updated_at
                """,
                (
                    intent_id,
                    idempotency_key,
                    symbol,
                    side,
                    status,
                    broker_order_id,
                    self._json_dump(raw),
                    self._now_iso(),
                ),
            )

    def has_unresolved_execution_key(self, idempotency_key: str) -> bool:
        row = self._conn.execute(
            "SELECT status FROM execution_orders WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return False
        return row["status"].upper() in self.unresolved_execution_statuses

    def load_execution_order(self, intent_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT
                intent_id,
                idempotency_key,
                symbol,
                side,
                status,
                broker_order_id,
                raw,
                updated_at
            FROM execution_orders
            WHERE intent_id = ?
            """,
            (intent_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "intent_id": row["intent_id"],
            "idempotency_key": row["idempotency_key"],
            "symbol": row["symbol"],
            "side": row["side"],
            "status": row["status"],
            "broker_order_id": row["broker_order_id"],
            "raw": self._json_load(row["raw"]),
            "updated_at": datetime.fromisoformat(row["updated_at"]),
        }

    def list_unresolved_execution_orders(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in self.unresolved_execution_statuses)
        sql = f"""
            SELECT
                intent_id,
                idempotency_key,
                symbol,
                side,
                status,
                broker_order_id,
                raw,
                updated_at
            FROM execution_orders
            WHERE UPPER(status) IN ({placeholders})
            ORDER BY updated_at ASC
            """
        params: tuple[object, ...] = tuple(self.unresolved_execution_statuses)
        if limit is not None:
            sql += " LIMIT ?"
            params = (*params, limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "intent_id": row["intent_id"],
                "idempotency_key": row["idempotency_key"],
                "symbol": row["symbol"],
                "side": row["side"],
                "status": row["status"],
                "broker_order_id": row["broker_order_id"],
                "raw": self._json_load(row["raw"]),
                "updated_at": datetime.fromisoformat(row["updated_at"]),
            }
            for row in rows
        ]

    def execution_summary_since(self, since: datetime) -> dict[str, Any]:
        rows = self._conn.execute(
            """
            SELECT status, raw
            FROM execution_orders
            WHERE updated_at >= ?
            """,
            (self._now_iso(since),),
        ).fetchall()
        count = 0
        notional = Decimal("0")
        for row in rows:
            status = str(row["status"]).upper()
            if status in {"REJECTED", "FAILED"}:
                continue
            count += 1
            raw = self._json_load(row["raw"]) or {}
            request = raw.get("request") if isinstance(raw.get("request"), dict) else raw
            value = None
            if isinstance(request, dict):
                value = request.get("notional") or request.get("orderAmount")
            if value is not None:
                try:
                    notional += Decimal(str(value))
                except Exception:
                    pass
        return {"count": count, "notional": notional}

    def record_execution_event(
        self,
        *,
        intent_id: str,
        event_type: str,
        status: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO execution_events (
                    intent_id,
                    event_type,
                    status,
                    payload,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    intent_id,
                    event_type,
                    status,
                    self._json_dump(payload),
                    self._now_iso(),
                ),
            )

    def list_execution_events(
        self,
        *,
        intent_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT id, intent_id, event_type, status, payload, created_at
            FROM execution_events
            """
        params: tuple[object, ...] = ()
        if intent_id is not None:
            sql += " WHERE intent_id = ?"
            params = (intent_id,)
        sql += " ORDER BY created_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params = (*params, limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "id": row["id"],
                "intent_id": row["intent_id"],
                "event_type": row["event_type"],
                "status": row["status"],
                "payload": self._json_load(row["payload"]),
                "created_at": datetime.fromisoformat(row["created_at"]),
            }
            for row in rows
        ]

    def record_market_data_snapshot(
        self,
        kind: str,
        symbol: str,
        payload: dict[str, Any],
        *,
        captured_at: datetime | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO market_data_snapshots (
                    kind,
                    symbol,
                    captured_at,
                    payload
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    kind,
                    symbol,
                    self._now_iso(captured_at),
                    self._json_dump(payload),
                ),
            )

    def latest_market_data_snapshot(self, kind: str, symbol: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT payload
            FROM market_data_snapshots
            WHERE kind = ? AND symbol = ?
            ORDER BY captured_at DESC, id DESC
            LIMIT 1
            """,
            (kind, symbol),
        ).fetchone()
        if row is None:
            return None
        return self._json_load(row["payload"])

    def latest_candles_snapshot(
        self,
        symbol: str,
        *,
        interval: str = "1d",
    ) -> tuple[tuple[Candle, ...], datetime | None] | None:
        row = self._conn.execute(
            """
            SELECT payload, captured_at
            FROM market_data_snapshots
            WHERE kind = ? AND symbol = ?
            ORDER BY captured_at DESC, id DESC
            LIMIT 1
            """,
            ("candles", symbol),
        ).fetchone()
        if row is None:
            return None
        payload = self._json_load(row["payload"])
        if not payload or str(payload.get("interval") or "1d") != interval:
            return None
        raw_candles = payload.get("candles")
        if not isinstance(raw_candles, list):
            return None
        candles = tuple(
            Candle.from_api(candle)
            for candle in raw_candles
            if isinstance(candle, dict)
        )
        if not candles:
            return None
        return candles, datetime.fromisoformat(row["captured_at"])

    def record_broker_snapshot(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        captured_at: datetime | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO broker_snapshots (
                    kind,
                    captured_at,
                    payload
                )
                VALUES (?, ?, ?)
                """,
                (
                    kind,
                    self._now_iso(captured_at),
                    self._json_dump(payload),
                ),
            )

    def latest_broker_snapshot(self, kind: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT payload
            FROM broker_snapshots
            WHERE kind = ?
            ORDER BY captured_at DESC, id DESC
            LIMIT 1
            """,
            (kind,),
        ).fetchone()
        if row is None:
            return None
        return self._json_load(row["payload"])

    def latest_broker_snapshot_record(self, kind: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT captured_at, payload
            FROM broker_snapshots
            WHERE kind = ?
            ORDER BY captured_at DESC, id DESC
            LIMIT 1
            """,
            (kind,),
        ).fetchone()
        if row is None:
            return None
        return {
            "kind": kind,
            "captured_at": datetime.fromisoformat(row["captured_at"]),
            "payload": self._json_load(row["payload"]),
        }

    def record_runtime_event(
        self,
        level: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO runtime_events (
                    level,
                    message,
                    payload,
                    created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (level, message, self._json_dump(payload), self._now_iso()),
            )

    def list_runtime_events(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        sql = """
            SELECT id, level, message, payload, created_at
            FROM runtime_events
            ORDER BY created_at DESC, id DESC
            """
        params: tuple[object, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "id": row["id"],
                "level": row["level"],
                "message": row["message"],
                "payload": self._json_load(row["payload"]),
                "created_at": datetime.fromisoformat(row["created_at"]),
            }
            for row in rows
        ]
