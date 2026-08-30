from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping
import hashlib
import json
import re
import sqlite3
from uuid import uuid4

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
    _INTRADAY_ORDER_ROLES = frozenset(
        {"ENTRY", "PROTECTION", "FORCE_EXIT", "EMERGENCY_EXIT"}
    )
    _INTRADAY_CLIENT_ORDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,36}$")
    _SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
    _INTRADAY_DECIMAL_RUN_COLUMNS = frozenset(
        {"owned_qty", "protected_qty", "average_entry_price"}
    )
    _INTRADAY_TIME_RUN_COLUMNS = frozenset(
        {"unprotected_since", "last_stream_sync_at", "entry_disabled_at", "loss_fuse_at"}
    )
    _INTRADAY_TEXT_RUN_COLUMNS = frozenset(
        {"entry_disabled_reason", "triggered_exit_order_id"}
    )
    _INTRADAY_STATE_TRANSITIONS = frozenset(
        {
            ("PLANNED", "APPROVED"),
            ("APPROVED", "RECONCILING"),
            ("APPROVED", "RECOVERY_REQUIRED"),
            ("READY_TO_ENTER", "RECONCILING"),
            ("READY_TO_ENTER", "PLANNED"),
            ("READY_TO_ENTER", "RECOVERY_REQUIRED"),
            ("ENTRY_SUBMITTING", "RECONCILING"),
            ("ENTRY_UNKNOWN", "RECONCILING"),
            ("ENTRY_WORKING", "RECONCILING"),
            ("ENTRY_CANCELING", "RECONCILING"),
            ("OPEN_UNPROTECTED", "RECONCILING"),
            ("PROTECTION_SUBMITTING", "RECONCILING"),
            ("PROTECTION_UNKNOWN", "RECONCILING"),
            ("PROTECTED", "RECONCILING"),
            ("EXIT_CANCELING_PROTECTION", "RECONCILING"),
            ("EXIT_SUBMITTING", "RECONCILING"),
            ("EXIT_UNKNOWN", "RECONCILING"),
            ("EXIT_WORKING", "RECONCILING"),
            ("RECONCILING", "READY_TO_ENTER"),
            ("RECONCILING", "PLANNED"),
            ("RECONCILING", "ENTRY_CANCELING"),
            ("RECONCILING", "EXIT_CANCELING_PROTECTION"),
            ("RECONCILING", "CANCELLED"),
            ("RECONCILING", "CLOSED"),
            ("RECONCILING", "RECOVERY_REQUIRED"),
            ("RECONCILING", "ENTRY_WORKING"),
            ("RECONCILING", "ENTRY_UNKNOWN"),
            ("RECONCILING", "OPEN_UNPROTECTED"),
            ("RECONCILING", "PROTECTED"),
            ("RECONCILING", "PROTECTION_UNKNOWN"),
            ("RECONCILING", "EXIT_WORKING"),
            ("RECONCILING", "EXIT_UNKNOWN"),
            ("READY_TO_ENTER", "SKIPPED"),
            ("READY_TO_ENTER", "ENTRY_SUBMITTING"),
            ("ENTRY_SUBMITTING", "ENTRY_WORKING"),
            ("ENTRY_SUBMITTING", "ENTRY_UNKNOWN"),
            ("ENTRY_SUBMITTING", "RECOVERY_REQUIRED"),
            ("ENTRY_SUBMITTING", "CANCELLED"),
            ("ENTRY_SUBMITTING", "OPEN_UNPROTECTED"),
            ("ENTRY_UNKNOWN", "ENTRY_WORKING"),
            ("ENTRY_UNKNOWN", "ENTRY_CANCELING"),
            ("ENTRY_UNKNOWN", "OPEN_UNPROTECTED"),
            ("ENTRY_UNKNOWN", "ENTRY_UNKNOWN"),
            ("ENTRY_UNKNOWN", "RECOVERY_REQUIRED"),
            ("ENTRY_UNKNOWN", "CANCELLED"),
            ("ENTRY_WORKING", "ENTRY_WORKING"),
            ("ENTRY_WORKING", "ENTRY_CANCELING"),
            ("ENTRY_WORKING", "ENTRY_UNKNOWN"),
            ("ENTRY_WORKING", "OPEN_UNPROTECTED"),
            ("ENTRY_WORKING", "CANCELLED"),
            ("ENTRY_WORKING", "RECOVERY_REQUIRED"),
            ("ENTRY_CANCELING", "ENTRY_CANCELING"),
            ("ENTRY_CANCELING", "OPEN_UNPROTECTED"),
            ("ENTRY_CANCELING", "CANCELLED"),
            ("ENTRY_CANCELING", "RECOVERY_REQUIRED"),
            ("OPEN_UNPROTECTED", "PROTECTION_SUBMITTING"),
            ("OPEN_UNPROTECTED", "EXIT_SUBMITTING"),
            ("OPEN_UNPROTECTED", "RECOVERY_REQUIRED"),
            ("PROTECTION_SUBMITTING", "PROTECTED"),
            ("PROTECTION_SUBMITTING", "PROTECTION_SUBMITTING"),
            ("PROTECTION_SUBMITTING", "EXIT_CANCELING_PROTECTION"),
            ("PROTECTION_SUBMITTING", "PROTECTION_UNKNOWN"),
            ("PROTECTION_SUBMITTING", "EXIT_WORKING"),
            ("PROTECTION_SUBMITTING", "OPEN_UNPROTECTED"),
            ("PROTECTION_SUBMITTING", "CLOSED"),
            ("PROTECTION_SUBMITTING", "RECOVERY_REQUIRED"),
            ("PROTECTION_UNKNOWN", "PROTECTED"),
            ("PROTECTION_UNKNOWN", "EXIT_WORKING"),
            ("PROTECTION_UNKNOWN", "EXIT_CANCELING_PROTECTION"),
            ("PROTECTION_UNKNOWN", "PROTECTION_UNKNOWN"),
            ("PROTECTION_UNKNOWN", "RECOVERY_REQUIRED"),
            ("PROTECTION_UNKNOWN", "EXIT_SUBMITTING"),
            ("PROTECTION_UNKNOWN", "OPEN_UNPROTECTED"),
            ("PROTECTION_UNKNOWN", "CLOSED"),
            ("PROTECTED", "EXIT_WORKING"),
            ("PROTECTED", "PROTECTION_UNKNOWN"),
            ("PROTECTED", "EXIT_CANCELING_PROTECTION"),
            ("PROTECTED", "EXIT_SUBMITTING"),
            ("PROTECTED", "OPEN_UNPROTECTED"),
            ("PROTECTED", "RECOVERY_REQUIRED"),
            ("PROTECTED", "CLOSED"),
            ("EXIT_CANCELING_PROTECTION", "EXIT_WORKING"),
            ("EXIT_CANCELING_PROTECTION", "EXIT_CANCELING_PROTECTION"),
            ("EXIT_CANCELING_PROTECTION", "EXIT_SUBMITTING"),
            ("EXIT_CANCELING_PROTECTION", "CLOSED"),
            ("EXIT_CANCELING_PROTECTION", "RECOVERY_REQUIRED"),
            ("EXIT_SUBMITTING", "EXIT_WORKING"),
            ("EXIT_SUBMITTING", "EXIT_UNKNOWN"),
            ("EXIT_SUBMITTING", "CLOSED"),
            ("EXIT_SUBMITTING", "RECOVERY_REQUIRED"),
            ("EXIT_UNKNOWN", "EXIT_WORKING"),
            ("EXIT_UNKNOWN", "CLOSED"),
            ("EXIT_UNKNOWN", "EXIT_UNKNOWN"),
            ("EXIT_UNKNOWN", "RECOVERY_REQUIRED"),
            ("EXIT_WORKING", "EXIT_WORKING"),
            ("EXIT_WORKING", "EXIT_UNKNOWN"),
            ("EXIT_WORKING", "CLOSED"),
            ("EXIT_WORKING", "RECOVERY_REQUIRED"),
            ("RECOVERY_REQUIRED", "RECONCILING"),
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
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if not self._is_memory:
            try:
                self._conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError:
                pass
        self._conn.execute("PRAGMA synchronous = FULL")
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
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS intraday_plans (
                  plan_id TEXT PRIMARY KEY,
                  account_key TEXT NOT NULL,
                  session_date TEXT NOT NULL,
                  symbol TEXT NOT NULL,
                  mode TEXT NOT NULL CHECK (mode = 'shadow'),
                  plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64),
                  payload TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE (account_key, session_date)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_outbox (
                  notification_key TEXT PRIMARY KEY,
                  message TEXT NOT NULL,
                  level TEXT NOT NULL,
                  payload TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (status IN ('PENDING', 'SENDING', 'SENT')),
                  attempt_count INTEGER NOT NULL DEFAULT 0,
                  claim_token TEXT,
                  claimed_at TEXT,
                  last_error_code TEXT,
                  created_at TEXT NOT NULL,
                  sent_at TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_notification_outbox_pending
                ON notification_outbox (status, created_at)
                """
            )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO schema_migrations (version, applied_at)
                VALUES (2, ?)
                """,
                (self._now_iso(),),
            )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO schema_migrations (version, applied_at)
                VALUES (3, ?)
                """,
                (self._now_iso(),),
            )
        self._migrate_v4()
        self._migrate_v5()

    def _migrate_v4(self) -> None:
        if self._conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 4"
        ).fetchone():
            self._verify_database_integrity()
            return

        statements = (
            "ALTER TABLE order_intents ADD COLUMN account_key TEXT",
            "ALTER TABLE order_intents ADD COLUMN plan_id TEXT",
            "ALTER TABLE order_intents ADD COLUMN order_role TEXT",
            "ALTER TABLE order_intents ADD COLUMN request_hash TEXT",
            "ALTER TABLE order_intents ADD COLUMN request_json TEXT",
            "ALTER TABLE order_intents ADD COLUMN first_attempt_at TEXT",
            "ALTER TABLE order_intents ADD COLUMN recovery_deadline_at TEXT",
            "ALTER TABLE order_intents ADD COLUMN reserved_at TEXT",
            "ALTER TABLE order_intents ADD COLUMN send_by TEXT",
            "ALTER TABLE order_intents ADD COLUMN reserved_writer_fence INTEGER",
            "ALTER TABLE order_intents ADD COLUMN reserved_run_version INTEGER",
            "ALTER TABLE execution_orders ADD COLUMN filled_quantity TEXT NOT NULL DEFAULT '0'",
            "ALTER TABLE execution_orders ADD COLUMN remaining_quantity TEXT",
            "ALTER TABLE execution_orders ADD COLUMN average_fill_price TEXT",
            "ALTER TABLE execution_orders ADD COLUMN last_broker_observed_at TEXT",
            "ALTER TABLE execution_events ADD COLUMN plan_id TEXT",
            "ALTER TABLE execution_events ADD COLUMN run_version INTEGER",
            "ALTER TABLE execution_events ADD COLUMN writer_fence INTEGER",
            """
            CREATE UNIQUE INDEX ux_order_intents_account_client
            ON order_intents(account_key, idempotency_key)
            WHERE plan_id IS NOT NULL AND account_key IS NOT NULL
            """,
            """
            CREATE UNIQUE INDEX ux_intraday_one_entry
            ON order_intents(plan_id) WHERE order_role = 'ENTRY'
            """,
            """
            CREATE UNIQUE INDEX ux_intraday_one_protection
            ON order_intents(plan_id) WHERE order_role = 'PROTECTION'
            """,
            """
            CREATE UNIQUE INDEX ux_intraday_one_local_exit
            ON order_intents(plan_id)
            WHERE order_role IN ('FORCE_EXIT','EMERGENCY_EXIT')
            """,
            """
            CREATE UNIQUE INDEX ux_intraday_event_plan_version
            ON execution_events(plan_id, run_version)
            WHERE plan_id IS NOT NULL AND run_version IS NOT NULL
            """,
            """
            CREATE UNIQUE INDEX ux_intraday_one_shot_event
            ON execution_events(intent_id, event_type)
            WHERE event_type IN (
              'create_send_reserved',
              'identity_recovery_send_reserved',
              'entry_cancel_send_reserved',
              'conditional_cancel_send_reserved',
              'conditional_cancel_acknowledged',
              'triggered_sell_cancel_send_reserved'
            )
            """,
            """
            CREATE TABLE intraday_runs (
              plan_id TEXT PRIMARY KEY
                REFERENCES intraday_plans(plan_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
              state TEXT NOT NULL CHECK (state IN (
                'PLANNED','APPROVED','RECONCILING','READY_TO_ENTER',
                'ENTRY_SUBMITTING','ENTRY_UNKNOWN','ENTRY_WORKING','ENTRY_CANCELING',
                'OPEN_UNPROTECTED','PROTECTION_SUBMITTING','PROTECTION_UNKNOWN','PROTECTED',
                'EXIT_CANCELING_PROTECTION','EXIT_SUBMITTING','EXIT_UNKNOWN','EXIT_WORKING',
                'CLOSED','SKIPPED','CANCELLED','RECOVERY_REQUIRED'
              )),
              version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
              writer_id TEXT,
              writer_fence INTEGER NOT NULL DEFAULT 0 CHECK (writer_fence >= 0),
              writer_lease_until TEXT,
              broker_sync_fence INTEGER NOT NULL DEFAULT -1
                CHECK (broker_sync_fence >= -1 AND broker_sync_fence <= writer_fence),
              boot_id_hash TEXT CHECK (boot_id_hash IS NULL OR length(boot_id_hash) = 64),
              approval_generation INTEGER NOT NULL DEFAULT 0 CHECK (approval_generation >= 0),
              approved_envelope_sha256 TEXT CHECK (
                approved_envelope_sha256 IS NULL OR length(approved_envelope_sha256) = 64
              ),
              approval_receipt_sha256 TEXT CHECK (
                approval_receipt_sha256 IS NULL OR length(approval_receipt_sha256) = 64
              ),
              approval_interaction_id TEXT UNIQUE,
              approved_at TEXT,
              approved_writer_fence INTEGER CHECK (
                approved_writer_fence IS NULL OR approved_writer_fence >= 0
              ),
              entry_disabled_at TEXT,
              entry_disabled_reason TEXT,
              entry_submit_count INTEGER NOT NULL DEFAULT 0 CHECK (entry_submit_count IN (0,1)),
              entry_intent_id TEXT UNIQUE REFERENCES order_intents(intent_id),
              protection_intent_id TEXT UNIQUE REFERENCES order_intents(intent_id),
              active_exit_intent_id TEXT UNIQUE REFERENCES order_intents(intent_id),
              triggered_exit_order_id TEXT UNIQUE,
              owned_qty TEXT NOT NULL DEFAULT '0',
              protected_qty TEXT NOT NULL DEFAULT '0',
              average_entry_price TEXT,
              unprotected_since TEXT,
              loss_fuse_at TEXT,
              last_broker_sync_at TEXT,
              last_stream_sync_at TEXT,
              reason_code TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              CHECK (
                (writer_id IS NULL AND writer_lease_until IS NULL)
                OR
                (writer_id IS NOT NULL AND writer_lease_until IS NOT NULL)
              ),
              CHECK (
                (approved_envelope_sha256 IS NULL AND approval_receipt_sha256 IS NULL
                  AND approval_interaction_id IS NULL
                  AND approved_at IS NULL AND approved_writer_fence IS NULL)
                OR
                (approved_envelope_sha256 IS NOT NULL AND approval_receipt_sha256 IS NOT NULL
                  AND approval_interaction_id IS NOT NULL
                  AND approved_at IS NOT NULL AND approved_writer_fence IS NOT NULL
                  AND boot_id_hash IS NOT NULL)
              ),
              CHECK (
                (entry_submit_count = 0 AND entry_intent_id IS NULL)
                OR
                (entry_submit_count = 1 AND entry_intent_id IS NOT NULL)
              ),
              CHECK (
                state <> 'PROTECTED'
                OR (owned_qty <> '0' AND owned_qty = protected_qty)
              ),
              CHECK (
                state NOT IN ('PROTECTION_SUBMITTING','PROTECTION_UNKNOWN','PROTECTED')
                OR protection_intent_id IS NOT NULL
              ),
              CHECK (
                state <> 'OPEN_UNPROTECTED'
                OR (owned_qty <> '0' AND unprotected_since IS NOT NULL)
              ),
              CHECK (
                state NOT IN ('EXIT_SUBMITTING','EXIT_UNKNOWN')
                OR active_exit_intent_id IS NOT NULL
              ),
              CHECK (
                state <> 'EXIT_WORKING'
                OR active_exit_intent_id IS NOT NULL OR triggered_exit_order_id IS NOT NULL
              ),
              CHECK (
                approved_writer_fence IS NULL OR approved_writer_fence <= writer_fence
              ),
              CHECK (
                state NOT IN ('CLOSED','SKIPPED','CANCELLED')
                OR (owned_qty = '0' AND protected_qty = '0')
              )
            )
            """,
            """
            CREATE UNIQUE INDEX ux_intraday_receipt_once
            ON intraday_runs(approval_receipt_sha256)
            WHERE approval_receipt_sha256 IS NOT NULL
            """,
        )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                self._conn.execute(statement)
            self._verify_database_integrity()
            self._conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (4, ?)",
                (self._intraday_time(),),
            )
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def _verify_database_integrity(self) -> None:
        foreign_key_errors = self._conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise sqlite3.IntegrityError("foreign key check failed")
        quick_check = self._conn.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or quick_check[0] != "ok":
            raise sqlite3.DatabaseError("SQLite quick_check failed")

    def _migrate_v5(self) -> None:
        if self._conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 5"
        ).fetchone():
            self._verify_database_integrity()
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(intraday_runs)")
            }
            if "approval_expires_at" not in columns:
                self._conn.execute(
                    "ALTER TABLE intraday_runs ADD COLUMN approval_expires_at TEXT"
                )
            self._verify_database_integrity()
            self._conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (5, ?)",
                (self._intraday_time(),),
            )
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

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
    def _intraday_time(at: datetime | None = None) -> str:
        timestamp = at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("intraday timestamps must be timezone-aware")
        return timestamp.astimezone(timezone.utc).isoformat(timespec="microseconds")

    @staticmethod
    def _canonical_decimal(value: Any, *, allow_zero: bool = True) -> str:
        try:
            number = Decimal(str(value))
        except Exception as exc:
            raise ValueError("intraday quantity/price must be a decimal") from exc
        if not number.is_finite() or number < 0 or (not allow_zero and number == 0):
            raise ValueError("intraday quantity/price must be finite and nonnegative")
        rendered = format(number, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return rendered or "0"

    @classmethod
    def _normalize_intraday_json(cls, value: Any) -> Any:
        if isinstance(value, Decimal):
            return cls._canonical_decimal(value)
        if value is None or isinstance(value, (str, int, bool)):
            return value
        if isinstance(value, float):
            raise ValueError("intraday request JSON may not contain floats")
        if isinstance(value, Mapping):
            normalized: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("intraday request JSON keys must be strings")
                normalized[key] = cls._normalize_intraday_json(item)
            return normalized
        if isinstance(value, (list, tuple)):
            return [cls._normalize_intraday_json(item) for item in value]
        raise ValueError("intraday request must contain canonical JSON data")

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
        existing = self._conn.execute(
            "SELECT plan_id FROM order_intents WHERE intent_id = ?", (intent.intent_id,)
        ).fetchone()
        if existing is not None and existing["plan_id"] is not None:
            raise ValueError("intraday order intents are immutable")
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
        intraday = self._conn.execute(
            "SELECT plan_id FROM order_intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        if intraday is not None and intraday["plan_id"] is not None:
            raise ValueError("intraday execution results require a fenced action")
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
                updated_at,
                filled_quantity,
                remaining_quantity,
                average_fill_price,
                last_broker_observed_at
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
            "filled_quantity": Decimal(row["filled_quantity"]),
            "remaining_quantity": (
                Decimal(row["remaining_quantity"])
                if row["remaining_quantity"] is not None
                else None
            ),
            "average_fill_price": (
                Decimal(row["average_fill_price"])
                if row["average_fill_price"] is not None
                else None
            ),
            "last_broker_observed_at": (
                datetime.fromisoformat(row["last_broker_observed_at"])
                if row["last_broker_observed_at"] is not None
                else None
            ),
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
        protected = {
            "identity_recovery_send_reserved",
            "entry_cancel_send_reserved",
            "entry_cancel_acknowledged",
            "conditional_cancel_send_reserved",
            "conditional_cancel_acknowledged",
            "create_send_reserved",
            "approval_consumed",
        }
        if str(event_type) in protected:
            raise ValueError("intraday fenced event requires the intraday state API")
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
            SELECT id, intent_id, event_type, status, payload, created_at,
                   plan_id, run_version, writer_fence
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
                "plan_id": row["plan_id"],
                "run_version": row["run_version"],
                "writer_fence": row["writer_fence"],
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

    def save_intraday_plan_once(
        self,
        *,
        account_key: str,
        session_date: date | str,
        symbol: str,
        payload: dict[str, Any],
        created_at: datetime | None = None,
        notification: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically insert an immutable shadow plan and optional notification."""

        clean_account_key = str(account_key or "").strip()
        clean_symbol = str(symbol or "").strip().upper()
        clean_session_date = (
            session_date.isoformat() if isinstance(session_date, date) else str(session_date)
        )
        try:
            date.fromisoformat(clean_session_date)
        except ValueError as exc:
            raise ValueError("session_date must be an ISO date") from exc
        if not clean_account_key or any(char.isspace() for char in clean_account_key):
            raise ValueError("account_key is required and may not contain whitespace")
        if not clean_symbol or any(char.isspace() for char in clean_symbol):
            raise ValueError("symbol is required and may not contain whitespace")
        if not isinstance(payload, dict):
            raise TypeError("intraday plan payload must be an object")
        plan_id = payload.get("plan_id")
        if not isinstance(plan_id, str) or not plan_id:
            raise ValueError("intraday plan payload must contain plan_id")
        if payload.get("account_id") != clean_account_key:
            raise ValueError("intraday plan account key does not match payload")
        if payload.get("session_date") != clean_session_date:
            raise ValueError("intraday plan session date does not match payload")
        if str(payload.get("symbol") or "").upper() != clean_symbol:
            raise ValueError("intraday plan symbol does not match payload")
        if payload.get("mode") != "shadow":
            raise ValueError("intraday plan mode must be shadow")

        encoded = self._canonical_json(payload)
        plan_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        timestamp = self._now_iso(created_at)
        notification_values = (
            self._notification_values(notification, created_at=timestamp)
            if notification is not None
            else None
        )
        try:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO intraday_plans (
                        plan_id,
                        account_key,
                        session_date,
                        symbol,
                        mode,
                        plan_hash,
                        payload,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, 'shadow', ?, ?, ?)
                    """,
                    (
                        plan_id,
                        clean_account_key,
                        clean_session_date,
                        clean_symbol,
                        plan_hash,
                        encoded,
                        timestamp,
                    ),
                )
                if notification_values is not None:
                    self._insert_notification_once(notification_values)
        except sqlite3.IntegrityError as exc:
            existing = self.load_intraday_plan(
                account_key=clean_account_key,
                session_date=clean_session_date,
            )
            if existing is not None and existing["plan_hash"] == plan_hash:
                return existing, False
            if existing is None:
                raise ValueError(
                    "intraday plan and notification transaction failed"
                ) from exc
            raise ValueError(
                "daily intraday plan is already locked for this account and session"
            ) from exc

        inserted = self.load_intraday_plan(
            account_key=clean_account_key,
            session_date=clean_session_date,
        )
        if inserted is None:  # pragma: no cover - SQLite insert/read invariant
            raise RuntimeError("inserted intraday plan could not be read back")
        return inserted, True

    def enqueue_notification_once(
        self,
        *,
        notification_key: str,
        message: str,
        level: str,
        payload: Mapping[str, Any],
        created_at: datetime | None = None,
    ) -> bool:
        """Durably enqueue one logical notification without overwriting it."""

        values = self._notification_values(
            {
                "notification_key": notification_key,
                "message": message,
                "level": level,
                "payload": payload,
            },
            created_at=self._now_iso(created_at),
        )
        with self._conn:
            return self._insert_notification_once(values)

    def claim_pending_notification(
        self,
        *,
        now: datetime | None = None,
        lease_seconds: int = 300,
    ) -> dict[str, Any] | None:
        """Claim one pending notification; stale claims become retryable."""

        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
            raise ValueError("lease_seconds must be a positive integer")
        claimed_at = now or datetime.now(timezone.utc)
        if claimed_at.tzinfo is None or claimed_at.utcoffset() is None:
            raise ValueError("notification claim time must be timezone-aware")
        claimed_at = claimed_at.astimezone(timezone.utc)
        claimed_text = claimed_at.isoformat()
        stale_text = (claimed_at - timedelta(seconds=lease_seconds)).isoformat()
        claim_token = uuid4().hex
        with self._conn:
            # The UPDATE obtains SQLite's write lock before the SELECT, so two
            # processes cannot claim the same row concurrently.
            self._conn.execute(
                """
                UPDATE notification_outbox
                SET status = 'PENDING', claim_token = NULL, claimed_at = NULL
                WHERE status = 'SENDING' AND claimed_at <= ?
                """,
                (stale_text,),
            )
            row = self._conn.execute(
                """
                SELECT notification_key
                FROM notification_outbox
                WHERE status = 'PENDING'
                ORDER BY created_at, notification_key
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            updated = self._conn.execute(
                """
                UPDATE notification_outbox
                SET status = 'SENDING', claim_token = ?, claimed_at = ?,
                    attempt_count = attempt_count + 1, last_error_code = NULL
                WHERE notification_key = ? AND status = 'PENDING'
                """,
                (claim_token, claimed_text, row["notification_key"]),
            )
            if updated.rowcount != 1:  # pragma: no cover - protected by write lock
                return None
            claimed = self._conn.execute(
                """
                SELECT * FROM notification_outbox WHERE notification_key = ?
                """,
                (row["notification_key"],),
            ).fetchone()
        return self._notification_row(claimed)

    def mark_notification_sent(
        self,
        *,
        notification_key: str,
        claim_token: str,
        sent_at: datetime | None = None,
    ) -> None:
        with self._conn:
            updated = self._conn.execute(
                """
                UPDATE notification_outbox
                SET status = 'SENT', sent_at = ?, claim_token = NULL,
                    claimed_at = NULL, last_error_code = NULL
                WHERE notification_key = ? AND status = 'SENDING' AND claim_token = ?
                """,
                (self._now_iso(sent_at), notification_key, claim_token),
            )
        if updated.rowcount != 1:
            raise ValueError("notification claim is no longer active")

    def mark_notification_failed(
        self,
        *,
        notification_key: str,
        claim_token: str,
        error_code: str,
    ) -> None:
        clean_error = str(error_code or "").strip()
        if not clean_error or len(clean_error) > 80 or not all(
            char.isalnum() or char in "_-" for char in clean_error
        ):
            raise ValueError("notification error_code must be a short safe identifier")
        with self._conn:
            updated = self._conn.execute(
                """
                UPDATE notification_outbox
                SET status = 'PENDING', claim_token = NULL, claimed_at = NULL,
                    last_error_code = ?
                WHERE notification_key = ? AND status = 'SENDING' AND claim_token = ?
                """,
                (clean_error, notification_key, claim_token),
            )
        if updated.rowcount != 1:
            raise ValueError("notification claim is no longer active")

    def list_notification_outbox(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT * FROM notification_outbox ORDER BY created_at, notification_key
            """
        ).fetchall()
        return [self._notification_row(row) for row in rows]

    def _notification_values(
        self,
        notification: Mapping[str, Any],
        *,
        created_at: str,
    ) -> tuple[str, str, str, str, str]:
        if not isinstance(notification, Mapping):
            raise TypeError("notification must be an object")
        key = str(notification.get("notification_key") or "").strip()
        message = str(notification.get("message") or "").strip()
        level = str(notification.get("level") or "").strip().lower()
        payload = notification.get("payload")
        if not key or len(key) > 240 or any(char.isspace() for char in key):
            raise ValueError("notification_key is required and may not contain whitespace")
        if not message or len(message) > 120 or any(char.isspace() for char in message):
            raise ValueError("notification message is required and may not contain whitespace")
        if level not in {"info", "warn", "error"}:
            raise ValueError("notification level must be info, warn, or error")
        if not isinstance(payload, Mapping):
            raise TypeError("notification payload must be an object")
        encoded = self._canonical_json(dict(payload))
        return key, message, level, encoded, created_at

    def _insert_notification_once(
        self,
        values: tuple[str, str, str, str, str],
    ) -> bool:
        inserted = self._conn.execute(
            """
            INSERT OR IGNORE INTO notification_outbox (
                notification_key, message, level, payload, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            values,
        )
        return inserted.rowcount == 1

    def _notification_row(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = self._json_load(row["payload"])
        if not isinstance(payload, dict):
            raise RuntimeError("notification outbox payload is not an object")
        return {
            "notification_key": row["notification_key"],
            "message": row["message"],
            "level": row["level"],
            "payload": payload,
            "status": row["status"],
            "attempt_count": row["attempt_count"],
            "claim_token": row["claim_token"],
            "claimed_at": (
                datetime.fromisoformat(row["claimed_at"])
                if row["claimed_at"] is not None
                else None
            ),
            "last_error_code": row["last_error_code"],
            "created_at": datetime.fromisoformat(row["created_at"]),
            "sent_at": (
                datetime.fromisoformat(row["sent_at"])
                if row["sent_at"] is not None
                else None
            ),
        }

    def load_intraday_plan(
        self,
        *,
        account_key: str,
        session_date: date | str,
    ) -> dict[str, Any] | None:
        clean_session_date = (
            session_date.isoformat() if isinstance(session_date, date) else str(session_date)
        )
        row = self._conn.execute(
            """
            SELECT plan_id, account_key, session_date, symbol, mode,
                   plan_hash, payload, created_at
            FROM intraday_plans
            WHERE account_key = ? AND session_date = ?
            """,
            (str(account_key), clean_session_date),
        ).fetchone()
        return self._intraday_plan_row(row) if row is not None else None

    def list_intraday_plans(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        sql = """
            SELECT plan_id, account_key, session_date, symbol, mode,
                   plan_hash, payload, created_at
            FROM intraday_plans
            ORDER BY session_date DESC, created_at DESC
            """
        params: tuple[object, ...] = ()
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                raise ValueError("limit must be a positive integer")
            sql += " LIMIT ?"
            params = (limit,)
        return [self._intraday_plan_row(row) for row in self._conn.execute(sql, params)]

    def _intraday_plan_row(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = self._json_load(row["payload"])
        if not isinstance(payload, dict):
            raise RuntimeError("stored intraday plan payload is not an object")
        encoded = self._canonical_json(payload)
        actual_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if actual_hash != row["plan_hash"]:
            raise RuntimeError("stored intraday plan failed integrity verification")
        if (
            payload.get("plan_id") != row["plan_id"]
            or payload.get("account_id") != row["account_key"]
            or payload.get("session_date") != row["session_date"]
            or str(payload.get("symbol") or "").upper() != row["symbol"]
            or payload.get("mode") != row["mode"]
        ):
            raise RuntimeError("stored intraday plan metadata failed integrity verification")
        return {
            "plan_id": row["plan_id"],
            "account_key": row["account_key"],
            "session_date": date.fromisoformat(row["session_date"]),
            "symbol": row["symbol"],
            "mode": row["mode"],
            "plan_hash": row["plan_hash"],
            "payload": payload,
            "created_at": datetime.fromisoformat(row["created_at"]),
        }

    @staticmethod
    def _canonical_json(payload: dict[str, Any]) -> str:
        try:
            return json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("intraday plan payload must be canonical JSON data") from exc

    def create_intraday_run(
        self,
        *,
        plan_id: str,
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Create the version-zero run projection and its single audit event."""

        clean_plan_id = str(plan_id or "").strip()
        if not clean_plan_id:
            raise ValueError("plan_id is required")
        timestamp = self._intraday_time(created_at)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self._conn.execute(
                "SELECT * FROM intraday_runs WHERE plan_id = ?", (clean_plan_id,)
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO intraday_runs (plan_id, state, created_at, updated_at)
                    VALUES (?, 'PLANNED', ?, ?)
                    """,
                    (clean_plan_id, timestamp, timestamp),
                )
                self._conn.execute(
                    """
                    INSERT INTO execution_events (
                        intent_id, event_type, status, payload, created_at,
                        plan_id, run_version, writer_fence
                    )
                    VALUES (?, 'run_created', 'PLANNED', NULL, ?, ?, 0, 0)
                    """,
                    (f"run:{clean_plan_id}", timestamp, clean_plan_id),
                )
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise ValueError("intraday run requires an existing immutable plan") from exc
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()
        run = self.load_intraday_run(clean_plan_id)
        if run is None:  # pragma: no cover - insert/read invariant
            raise RuntimeError("intraday run could not be read back")
        return run

    def load_intraday_run(self, plan_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM intraday_runs WHERE plan_id = ?", (str(plan_id),)
        ).fetchone()
        return self._intraday_run_row(row) if row is not None else None

    def claim_intraday_writer(
        self,
        *,
        plan_id: str,
        writer_id: str,
        now: datetime | None = None,
        lease_seconds: int = 45,
    ) -> dict[str, Any] | None:
        """Claim an empty/expired run lease, fencing every post-expiry claimant."""

        clean_writer = self._intraday_identifier(writer_id, "writer_id")
        claimed_at = self._intraday_datetime(now)
        lease_until = self._intraday_time(
            claimed_at + timedelta(seconds=self._positive_seconds(lease_seconds))
        )
        now_text = self._intraday_time(claimed_at)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT * FROM intraday_runs WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if row is None:
                self._conn.rollback()
                return None
            active = row["writer_lease_until"] is not None and row["writer_lease_until"] > now_text
            if active:
                self._conn.rollback()
                return None
            updated = self._conn.execute(
                """
                UPDATE intraday_runs
                SET writer_id = ?, writer_fence = writer_fence + 1,
                    writer_lease_until = ?, broker_sync_fence = -1,
                    last_broker_sync_at = NULL, updated_at = ?
                WHERE plan_id = ? AND writer_fence = ?
                  AND (writer_lease_until IS NULL OR writer_lease_until <= ?)
                """,
                (
                    clean_writer,
                    lease_until,
                    now_text,
                    plan_id,
                    row["writer_fence"],
                    now_text,
                ),
            )
            if updated.rowcount != 1:
                self._conn.rollback()
                return None
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()
        return self.load_intraday_run(plan_id)

    def renew_intraday_writer(
        self,
        *,
        plan_id: str,
        writer_id: str,
        writer_fence: int,
        now: datetime | None = None,
        lease_seconds: int = 45,
    ) -> dict[str, Any] | None:
        clean_writer = self._intraday_identifier(writer_id, "writer_id")
        renewed_at = self._intraday_datetime(now)
        now_text = self._intraday_time(renewed_at)
        lease_until = self._intraday_time(
            renewed_at + timedelta(seconds=self._positive_seconds(lease_seconds))
        )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            updated = self._conn.execute(
                """
                UPDATE intraday_runs
                SET writer_lease_until = ?, updated_at = ?
                WHERE plan_id = ? AND writer_id = ? AND writer_fence = ?
                  AND writer_lease_until > ?
                """,
                (lease_until, now_text, plan_id, clean_writer, writer_fence, now_text),
            )
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()
        return self.load_intraday_run(plan_id) if updated.rowcount == 1 else None

    def mark_intraday_broker_synced(
        self,
        *,
        plan_id: str,
        writer_id: str,
        writer_fence: int,
        now: datetime | None = None,
        observed_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        clean_writer = self._intraday_identifier(writer_id, "writer_id")
        timestamp = self._intraday_time(now)
        observation_timestamp = self._intraday_time(
            observed_at if observed_at is not None else now
        )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            updated = self._conn.execute(
                """
                UPDATE intraday_runs
                SET broker_sync_fence = writer_fence,
                    last_broker_sync_at = ?, updated_at = ?
                WHERE plan_id = ? AND writer_id = ? AND writer_fence = ?
                  AND writer_lease_until > ?
                """,
                (
                    observation_timestamp,
                    timestamp,
                    plan_id,
                    clean_writer,
                    writer_fence,
                    timestamp,
                ),
            )
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()
        return self.load_intraday_run(plan_id) if updated.rowcount == 1 else None

    def consume_intraday_approval(
        self,
        *,
        plan_id: str,
        plan_hash: str,
        envelope_sha256: str,
        receipt_sha256: str,
        interaction_id: str,
        boot_id_hash: str,
        approval_generation: int,
        approved_writer_fence: int,
        writer_id: str,
        writer_fence: int,
        approved_at: datetime,
        approval_expires_at: datetime,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Consume one already-verified synthetic receipt under the active writer fence."""

        hashes = {
            "plan_hash": str(plan_hash),
            "envelope_sha256": str(envelope_sha256),
            "receipt_sha256": str(receipt_sha256),
            "boot_id_hash": str(boot_id_hash),
        }
        malformed = next(
            (name for name, value in hashes.items() if self._SHA256_HEX.fullmatch(value) is None),
            None,
        )
        if malformed is not None:
            raise ValueError(f"{malformed} must be a lowercase SHA-256 hex digest")
        if (
            isinstance(approval_generation, bool)
            or not isinstance(approval_generation, int)
            or approval_generation < 1
        ):
            raise ValueError("approval_generation must be a positive integer")
        for label, value in (
            ("writer_fence", writer_fence),
            ("approved_writer_fence", approved_writer_fence),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} must be a nonnegative integer")
        clean_writer = self._intraday_identifier(writer_id, "writer_id")
        clean_interaction = self._intraday_identifier(interaction_id, "interaction_id")
        approved_datetime = self._intraday_datetime(approved_at)
        expires_datetime = self._intraday_datetime(approval_expires_at)
        checked_datetime = self._intraday_datetime(now)
        if approved_datetime > checked_datetime:
            raise ValueError("approved_at may not be in the future")
        if expires_datetime <= checked_datetime or expires_datetime <= approved_datetime:
            raise ValueError("approval_expires_at must be after approval and current time")
        approved_text = self._intraday_time(approved_datetime)
        expires_text = self._intraday_time(expires_datetime)
        checked_text = self._intraday_time(checked_datetime)

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                """
                SELECT run.*, plan.plan_hash, plan.created_at AS plan_created_at
                FROM intraday_runs AS run
                JOIN intraday_plans AS plan ON plan.plan_id = run.plan_id
                WHERE run.plan_id = ?
                """,
                (plan_id,),
            ).fetchone()
            if (
                row is None
                or row["plan_hash"] != hashes["plan_hash"]
                or row["state"] != "PLANNED"
                or row["writer_id"] != clean_writer
                or row["writer_fence"] != writer_fence
                or approved_writer_fence != writer_fence
                or row["writer_lease_until"] is None
                or row["writer_lease_until"] <= checked_text
                or row["entry_disabled_at"] is not None
                or row["loss_fuse_at"] is not None
                or approval_generation != row["approval_generation"] + 1
                or approved_datetime < datetime.fromisoformat(row["plan_created_at"])
            ):
                self._conn.rollback()
                return None
            duplicate = self._conn.execute(
                """
                SELECT 1 FROM intraday_runs
                WHERE approval_receipt_sha256 = ? OR approval_interaction_id = ?
                """,
                (hashes["receipt_sha256"], clean_interaction),
            ).fetchone()
            if duplicate is not None:
                self._conn.rollback()
                return None
            updated = self._conn.execute(
                """
                UPDATE intraday_runs
                SET state = 'APPROVED', version = version + 1,
                    approval_generation = ?, approved_envelope_sha256 = ?,
                    approval_receipt_sha256 = ?, approval_interaction_id = ?,
                    boot_id_hash = ?, approved_at = ?, approval_expires_at = ?,
                    approved_writer_fence = ?,
                    updated_at = ?
                WHERE plan_id = ? AND state = 'PLANNED' AND version = ?
                  AND writer_id = ? AND writer_fence = ?
                  AND writer_lease_until > ?
                  AND approval_generation = ?
                  AND entry_disabled_at IS NULL
                  AND loss_fuse_at IS NULL
                """,
                (
                    approval_generation,
                    hashes["envelope_sha256"],
                    hashes["receipt_sha256"],
                    clean_interaction,
                    hashes["boot_id_hash"],
                    approved_text,
                    expires_text,
                    approved_writer_fence,
                    checked_text,
                    plan_id,
                    row["version"],
                    clean_writer,
                    writer_fence,
                    checked_text,
                    approval_generation - 1,
                ),
            )
            if updated.rowcount != 1:
                self._conn.rollback()
                return None
            self._insert_intraday_event(
                plan_id=plan_id,
                run_version=row["version"] + 1,
                writer_fence=writer_fence,
                intent_id=f"run:{plan_id}",
                event_type="approval_consumed",
                status="APPROVED",
                payload={
                    "approval_generation": approval_generation,
                    "approval_expires_at": expires_text,
                },
                timestamp=checked_text,
            )
        except sqlite3.IntegrityError:
            self._conn.rollback()
            return None
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()
        return self.load_intraday_run(plan_id)

    def cas_intraday_run(
        self,
        *,
        plan_id: str,
        expected_state: str,
        expected_version: int,
        next_state: str,
        writer_id: str,
        writer_fence: int,
        event_type: str,
        event_status: str | None = None,
        intent_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        reason_code: str | None = None,
        updates: Mapping[str, object] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Apply one fenced run transition and exactly one versioned event."""

        if expected_state == "PLANNED" and next_state == "APPROVED":
            raise ValueError("PLANNED->APPROVED requires consume_intraday_approval")
        timestamp = self._intraday_time(now)
        clean_writer = self._intraday_identifier(writer_id, "writer_id")
        clean_event = self._intraday_identifier(event_type, "event_type")
        prepared_updates = self._prepare_intraday_run_updates(updates)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            updated = self._update_intraday_run_cas(
                plan_id=plan_id,
                expected_state=expected_state,
                expected_version=expected_version,
                next_state=next_state,
                writer_id=clean_writer,
                writer_fence=writer_fence,
                timestamp=timestamp,
                reason_code=reason_code,
                updates=prepared_updates,
            )
            if not updated:
                self._conn.rollback()
                return None
            self._insert_intraday_event(
                plan_id=plan_id,
                run_version=expected_version + 1,
                writer_fence=writer_fence,
                intent_id=intent_id or f"run:{plan_id}",
                event_type=clean_event,
                status=event_status or next_state,
                payload=payload,
                timestamp=timestamp,
            )
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()
        return self.load_intraday_run(plan_id)

    def reserve_intraday_order_intent(
        self,
        *,
        plan_id: str,
        account_key: str,
        intent_id: str,
        idempotency_key: str,
        order_role: str,
        method: str,
        path: str,
        body: Mapping[str, Any],
        symbol: str,
        side: str,
        quantity: Any,
        order_type: str,
        expected_state: str,
        expected_version: int,
        next_state: str,
        writer_id: str,
        writer_fence: int,
        send_by: datetime,
        limit_price: Any | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Reserve one immutable create request and its run transition."""

        clean_role = str(order_role).upper()
        if clean_role not in self._INTRADAY_ORDER_ROLES:
            raise ValueError("unsupported intraday order_role")
        clean_client_id = str(idempotency_key)
        if self._INTRADAY_CLIENT_ORDER_ID.fullmatch(clean_client_id) is None:
            raise ValueError("idempotency_key must be the final 1-36 character clientOrderId")
        clean_intent = self._intraday_identifier(intent_id, "intent_id")
        clean_account = self._intraday_identifier(account_key, "account_key")
        clean_writer = self._intraday_identifier(writer_id, "writer_id")
        clean_method = str(method).upper()
        clean_path = str(path)
        if clean_method != "POST" or not clean_path.startswith("/") or "?" in clean_path:
            raise ValueError("intraday create request requires a query-free POST path")
        clean_symbol = self._intraday_identifier(str(symbol).upper(), "symbol")
        clean_side = str(side).upper()
        if clean_side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if (clean_role == "ENTRY") != (clean_side == "BUY"):
            raise ValueError("ENTRY must BUY and protection/exit roles must SELL")
        clean_order_type = self._intraday_identifier(
            str(order_type).upper(), "order_type"
        )
        clean_quantity = self._canonical_decimal(quantity, allow_zero=False)
        clean_limit = (
            self._canonical_decimal(limit_price, allow_zero=False)
            if limit_price is not None
            else None
        )
        normalized_body = self._normalize_intraday_json(body)
        if not isinstance(normalized_body, dict):  # Mapping normalizes to dict
            raise TypeError("intraday request body must be an object")
        expected_route = (
            ("READY_TO_ENTER", "ENTRY_SUBMITTING")
            if clean_role == "ENTRY"
            else ("OPEN_UNPROTECTED", "PROTECTION_SUBMITTING")
            if clean_role == "PROTECTION"
            else None
        )
        if expected_route is not None and (expected_state, next_state) != expected_route:
            raise ValueError("intraday order role does not match its state transition")
        if clean_role in {"FORCE_EXIT", "EMERGENCY_EXIT"} and (
            expected_state
            not in {
                "OPEN_UNPROTECTED",
                "PROTECTION_UNKNOWN",
                "PROTECTED",
                "EXIT_CANCELING_PROTECTION",
            }
            or next_state != "EXIT_SUBMITTING"
        ):
            raise ValueError("local exit role does not match its state transition")
        expected_path = (
            "/api/v1/conditional-orders"
            if clean_role == "PROTECTION"
            else "/api/v1/orders"
        )
        if clean_path != expected_path:
            raise ValueError("intraday order role does not match request path")
        expected_order_type = (
            "MARKET" if clean_role in {"FORCE_EXIT", "EMERGENCY_EXIT"} else "LIMIT"
        )
        if clean_order_type != expected_order_type:
            raise ValueError("intraday order role does not match order_type")
        if (clean_role == "ENTRY") != (clean_limit is not None):
            raise ValueError("only ENTRY requires the ledger limit_price")
        if (
            normalized_body.get("clientOrderId") != clean_client_id
            or str(normalized_body.get("symbol") or "").upper() != clean_symbol
            or self._canonical_decimal(
                normalized_body.get("quantity"), allow_zero=False
            )
            != clean_quantity
            or str(normalized_body.get("orderType") or "").upper() != clean_order_type
        ):
            raise ValueError("intraday request body does not match intent projection")
        if clean_role == "PROTECTION":
            if (
                normalized_body.get("type") != "OCO"
                or not isinstance(normalized_body.get("first"), dict)
                or not isinstance(normalized_body.get("second"), dict)
                or normalized_body["first"].get("orderSide") != "SELL"
                or normalized_body["second"].get("orderSide") != "SELL"
            ):
                raise ValueError("PROTECTION request must be a two-leg SELL OCO")
        elif str(normalized_body.get("side") or "").upper() != clean_side:
            raise ValueError("intraday request body side does not match intent projection")
        if clean_role == "ENTRY" and self._canonical_decimal(
            normalized_body.get("price"), allow_zero=False
        ) != clean_limit:
            raise ValueError("ENTRY request price does not match limit_price")
        request_json = self._canonical_json(normalized_body)
        envelope = {
            "account_key": clean_account,
            "plan_id": plan_id,
            "order_role": clean_role,
            "method": clean_method,
            "path": clean_path,
            "body": normalized_body,
        }
        envelope_json = self._canonical_json(envelope)
        request_hash = hashlib.sha256(envelope_json.encode("utf-8")).hexdigest()
        reserved_at = self._intraday_datetime(now)
        reserved_text = self._intraday_time(reserved_at)
        send_by_text = self._intraday_time(send_by)
        if send_by_text < reserved_text:
            raise ValueError("send_by may not precede reservation time")
        recovery_deadline = (
            None
            if clean_role == "PROTECTION"
            else self._intraday_time(reserved_at + timedelta(minutes=8))
        )
        new_version = expected_version + 1
        self._validate_intraday_transition(expected_state, next_state)

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            matches = self._conn.execute(
                """
                SELECT * FROM order_intents
                WHERE intent_id = ? OR (account_key = ? AND idempotency_key = ?)
                """,
                (clean_intent, clean_account, clean_client_id),
            ).fetchall()
            if matches:
                existing = matches[0]
                if any(
                    row["request_hash"] != request_hash
                    or row["plan_id"] != plan_id
                    or row["order_role"] != clean_role
                    for row in matches
                ):
                    raise ValueError("intraday order identity conflicts with immutable request")
                self._conn.commit()
                return {
                    "inserted": False,
                    "intent": self._intraday_order_intent_row(existing),
                    "run": self.load_intraday_run(plan_id),
                }

            plan = self._conn.execute(
                "SELECT account_key, symbol FROM intraday_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            if (
                plan is None
                or plan["account_key"] != clean_account
                or plan["symbol"] != clean_symbol
            ):
                raise ValueError("intent account/symbol does not match immutable plan")
            run = self._conn.execute(
                "SELECT * FROM intraday_runs WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            min_lease = self._intraday_time(reserved_at + timedelta(seconds=30))
            fresh_sync = self._intraday_time(reserved_at - timedelta(seconds=5))
            if (
                run is None
                or run["state"] != expected_state
                or run["version"] != expected_version
                or run["writer_id"] != clean_writer
                or run["writer_fence"] != writer_fence
                or run["writer_lease_until"] is None
                or run["writer_lease_until"] < min_lease
                or run["broker_sync_fence"] != writer_fence
                or run["last_broker_sync_at"] is None
                or run["last_broker_sync_at"] < fresh_sync
                or (
                    clean_role == "ENTRY"
                    and (
                        run["approved_writer_fence"] != writer_fence
                        or run["approved_envelope_sha256"] is None
                        or run["approval_receipt_sha256"] is None
                        or run["approval_interaction_id"] is None
                        or run["boot_id_hash"] is None
                        or run["approved_at"] is None
                        or run["approval_expires_at"] is None
                        or run["approval_expires_at"] <= reserved_text
                        or run["entry_disabled_at"] is not None
                        or run["loss_fuse_at"] is not None
                    )
                )
            ):
                self._conn.rollback()
                return {"inserted": False, "intent": None, "run": None}

            self._conn.execute(
                """
                INSERT INTO order_intents (
                    intent_id, idempotency_key, symbol, side, quantity,
                    order_type, limit_price, payload, created_at,
                    account_key, plan_id, order_role, request_hash, request_json,
                    first_attempt_at, recovery_deadline_at, reserved_at, send_by,
                    reserved_writer_fence, reserved_run_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_intent,
                    clean_client_id,
                    clean_symbol,
                    clean_side,
                    clean_quantity,
                    clean_order_type,
                    clean_limit,
                    envelope_json,
                    reserved_text,
                    clean_account,
                    plan_id,
                    clean_role,
                    request_hash,
                    request_json,
                    reserved_text,
                    recovery_deadline,
                    reserved_text,
                    send_by_text,
                    writer_fence,
                    new_version,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO execution_orders (
                    intent_id, idempotency_key, symbol, side, status,
                    broker_order_id, raw, updated_at, filled_quantity,
                    remaining_quantity, average_fill_price, last_broker_observed_at
                )
                VALUES (?, ?, ?, ?, 'PENDING', NULL, NULL, ?, '0', ?, NULL, NULL)
                """,
                (
                    clean_intent,
                    clean_client_id,
                    clean_symbol,
                    clean_side,
                    reserved_text,
                    clean_quantity,
                ),
            )
            pointer = {
                "ENTRY": "entry_intent_id",
                "PROTECTION": "protection_intent_id",
                "FORCE_EXIT": "active_exit_intent_id",
                "EMERGENCY_EXIT": "active_exit_intent_id",
            }[clean_role]
            latch = ", entry_submit_count = 1" if clean_role == "ENTRY" else ""
            updated = self._conn.execute(
                f"""
                UPDATE intraday_runs
                SET state = ?, version = version + 1, updated_at = ?,
                    {pointer} = ?{latch}
                WHERE plan_id = ? AND state = ? AND version = ?
                  AND writer_id = ? AND writer_fence = ?
                  AND writer_lease_until >= ?
                  AND broker_sync_fence = writer_fence
                """,
                (
                    next_state,
                    reserved_text,
                    clean_intent,
                    plan_id,
                    expected_state,
                    expected_version,
                    clean_writer,
                    writer_fence,
                    min_lease,
                ),
            )
            if updated.rowcount != 1:
                self._conn.rollback()
                return {"inserted": False, "intent": None, "run": None}
            self._insert_intraday_event(
                plan_id=plan_id,
                run_version=new_version,
                writer_fence=writer_fence,
                intent_id=clean_intent,
                event_type="create_send_reserved",
                status=next_state,
                payload={"request_hash": request_hash, "order_role": clean_role},
                timestamp=reserved_text,
            )
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise ValueError("intraday order reservation violates ownership constraints") from exc
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()
        return {
            "inserted": True,
            "intent": self.load_intraday_order_intent(clean_intent),
            "run": self.load_intraday_run(plan_id),
        }

    def load_intraday_order_intent(self, intent_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM order_intents WHERE intent_id = ? AND plan_id IS NOT NULL",
            (str(intent_id),),
        ).fetchone()
        return self._intraday_order_intent_row(row) if row is not None else None

    def intraday_reservation_is_sendable(
        self,
        *,
        intent_id: str,
        plan_id: str,
        expected_state: str,
        expected_run_version: int,
        writer_id: str,
        writer_fence: int,
        request_hash: str,
        now: datetime | None = None,
        min_lease_seconds: int = 30,
    ) -> bool:
        checked_at = self._intraday_datetime(now)
        now_text = self._intraday_time(checked_at)
        minimum_lease = self._intraday_time(
            checked_at + timedelta(seconds=self._positive_seconds(min_lease_seconds))
        )
        fresh_sync = self._intraday_time(checked_at - timedelta(seconds=5))
        return (
            self._conn.execute(
                """
                SELECT 1
                FROM order_intents AS intent
                JOIN intraday_runs AS run ON run.plan_id = intent.plan_id
                WHERE intent.intent_id = ? AND intent.plan_id = ?
                  AND intent.request_hash = ?
                  AND intent.reserved_writer_fence = ?
                  AND intent.reserved_run_version = ?
                  AND intent.send_by >= ?
                  AND run.state = ? AND run.version = ?
                  AND run.writer_id = ? AND run.writer_fence = ?
                  AND run.writer_lease_until >= ?
                  AND run.broker_sync_fence = run.writer_fence
                  AND run.last_broker_sync_at >= ?
                  AND (
                    intent.order_role <> 'ENTRY' OR (
                      run.approved_writer_fence = run.writer_fence
                      AND run.approved_envelope_sha256 IS NOT NULL
                      AND run.approval_receipt_sha256 IS NOT NULL
                      AND run.approval_interaction_id IS NOT NULL
                      AND run.boot_id_hash IS NOT NULL
                      AND run.approved_at IS NOT NULL
                      AND run.approval_expires_at > ?
                      AND run.entry_disabled_at IS NULL
                      AND run.loss_fuse_at IS NULL
                    )
                  )
                """,
                (
                    intent_id,
                    plan_id,
                    request_hash,
                    writer_fence,
                    expected_run_version,
                    now_text,
                    expected_state,
                    expected_run_version,
                    writer_id,
                    writer_fence,
                    minimum_lease,
                    fresh_sync,
                    now_text,
                ),
            ).fetchone()
            is not None
        )

    def append_intraday_observation_event(
        self,
        *,
        plan_id: str,
        intent_id: str,
        event_type: str,
        status: str,
        writer_id: str,
        writer_fence: int,
        payload: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Append a non-projecting observation only for the active fenced writer."""

        timestamp = self._intraday_time(now)
        clean_writer = self._intraday_identifier(writer_id, "writer_id")
        clean_event = self._intraday_identifier(event_type, "event_type")
        clean_status = self._intraday_identifier(status, "status")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            owned = self._conn.execute(
                """
                SELECT intent.order_role, execution.broker_order_id,
                       run.state, run.entry_intent_id, run.protection_intent_id,
                       EXISTS (
                         SELECT 1 FROM execution_events AS reserved
                         WHERE reserved.intent_id = intent.intent_id
                           AND reserved.writer_fence = run.writer_fence
                           AND reserved.event_type = CASE
                             WHEN intent.order_role = 'ENTRY'
                               THEN 'entry_cancel_send_reserved'
                             ELSE 'conditional_cancel_send_reserved'
                           END
                       ) AS cancel_reserved
                FROM order_intents AS intent
                JOIN execution_orders AS execution
                  ON execution.intent_id = intent.intent_id
                JOIN intraday_runs AS run ON run.plan_id = intent.plan_id
                WHERE intent.intent_id = ? AND intent.plan_id = ?
                  AND run.writer_id = ? AND run.writer_fence = ?
                  AND run.writer_lease_until > ?
                """,
                (intent_id, plan_id, clean_writer, writer_fence, timestamp),
            ).fetchone()
            if owned is None:
                self._conn.rollback()
                return False
            if clean_event in {
                "entry_cancel_acknowledged",
                "conditional_cancel_acknowledged",
            }:
                expected_role = (
                    "ENTRY" if clean_event == "entry_cancel_acknowledged" else "PROTECTION"
                )
                expected_pointer = (
                    owned["entry_intent_id"]
                    if expected_role == "ENTRY"
                    else owned["protection_intent_id"]
                )
                expected_state = (
                    "ENTRY_CANCELING"
                    if expected_role == "ENTRY"
                    else "EXIT_CANCELING_PROTECTION"
                )
                root_order_id = (
                    payload.get("root_order_id") if isinstance(payload, Mapping) else None
                )
                if (
                    owned["order_role"] != expected_role
                    or expected_pointer != intent_id
                    or owned["state"] != expected_state
                    or owned["cancel_reserved"] != 1
                    or not isinstance(root_order_id, str)
                    or root_order_id != owned["broker_order_id"]
                ):
                    self._conn.rollback()
                    return False
            self._insert_intraday_event(
                plan_id=plan_id,
                run_version=None,
                writer_fence=writer_fence,
                intent_id=intent_id,
                event_type=clean_event,
                status=clean_status,
                payload=payload,
                timestamp=timestamp,
            )
        except sqlite3.IntegrityError:
            self._conn.rollback()
            return False
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()
        return True

    def reserve_intraday_action_event(
        self,
        *,
        plan_id: str,
        intent_id: str,
        event_type: str,
        expected_state: str,
        expected_version: int,
        writer_id: str,
        writer_fence: int,
        next_state: str | None = None,
        payload: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Reserve one recovery/cancel mutation; the unique event is its latch."""

        allowed_events = {
            "identity_recovery_send_reserved",
            "entry_cancel_send_reserved",
            "conditional_cancel_send_reserved",
        }
        clean_event = str(event_type)
        if clean_event not in allowed_events:
            raise ValueError("unsupported intraday action event")
        timestamp_at = self._intraday_datetime(now)
        timestamp = self._intraday_time(timestamp_at)
        minimum_lease = self._intraday_time(timestamp_at + timedelta(seconds=30))
        fresh_sync = self._intraday_time(timestamp_at - timedelta(seconds=5))
        clean_writer = self._intraday_identifier(writer_id, "writer_id")
        target_state = next_state or expected_state
        self._validate_intraday_transition(expected_state, target_state)
        if clean_event == "identity_recovery_send_reserved" and (
            expected_state not in {"ENTRY_UNKNOWN", "EXIT_UNKNOWN"}
            or target_state != expected_state
        ):
            raise ValueError("identity recovery must remain in an UNKNOWN state")

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            intent = self._conn.execute(
                """
                SELECT plan_id, order_role, side, recovery_deadline_at
                FROM order_intents WHERE intent_id = ?
                """,
                (intent_id,),
            ).fetchone()
            if intent is None or intent["plan_id"] != plan_id:
                raise ValueError("intraday action intent does not belong to run")
            if clean_event == "identity_recovery_send_reserved" and (
                intent["order_role"] == "PROTECTION"
                or intent["recovery_deadline_at"] is None
                or intent["recovery_deadline_at"] < timestamp
            ):
                raise ValueError("identity recovery is outside its bounded window")
            already_reserved = self._conn.execute(
                """
                SELECT 1 FROM execution_events
                WHERE intent_id = ? AND event_type = ?
                """,
                (intent_id, clean_event),
            ).fetchone()
            if already_reserved is not None:
                self._conn.rollback()
                return None
            run = self._conn.execute(
                "SELECT * FROM intraday_runs WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if (
                run is None
                or run["state"] != expected_state
                or run["version"] != expected_version
                or run["writer_id"] != clean_writer
                or run["writer_fence"] != writer_fence
                or run["writer_lease_until"] is None
                or run["writer_lease_until"] < minimum_lease
                or run["broker_sync_fence"] != writer_fence
                or run["last_broker_sync_at"] is None
                or run["last_broker_sync_at"] < fresh_sync
            ):
                self._conn.rollback()
                return None
            if not self._intraday_action_matches(
                event_type=clean_event,
                intent_id=intent_id,
                intent=intent,
                run=run,
                expected_state=expected_state,
                target_state=target_state,
            ):
                raise ValueError("intraday action does not match the run pointer and role")
            if not self._update_intraday_run_cas(
                plan_id=plan_id,
                expected_state=expected_state,
                expected_version=expected_version,
                next_state=target_state,
                writer_id=clean_writer,
                writer_fence=writer_fence,
                timestamp=timestamp,
                reason_code=None,
                updates={},
            ):
                self._conn.rollback()
                return None
            self._insert_intraday_event(
                plan_id=plan_id,
                run_version=expected_version + 1,
                writer_fence=writer_fence,
                intent_id=intent_id,
                event_type=clean_event,
                status=target_state,
                payload=payload,
                timestamp=timestamp,
            )
        except sqlite3.IntegrityError:
            self._conn.rollback()
            return None
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()
        return self.load_intraday_run(plan_id)

    def intraday_action_is_sendable(
        self,
        *,
        plan_id: str,
        intent_id: str,
        event_type: str,
        expected_state: str,
        expected_run_version: int,
        writer_id: str,
        writer_fence: int,
        request_hash: str,
        now: datetime | None = None,
        min_lease_seconds: int = 30,
    ) -> bool:
        checked_at = self._intraday_datetime(now)
        timestamp = self._intraday_time(checked_at)
        minimum_lease = self._intraday_time(
            checked_at + timedelta(seconds=self._positive_seconds(min_lease_seconds))
        )
        fresh_sync = self._intraday_time(checked_at - timedelta(seconds=5))
        if self._SHA256_HEX.fullmatch(str(request_hash)) is None:
            return False
        row = self._conn.execute(
            """
            SELECT intent.order_role, intent.side, intent.recovery_deadline_at,
                   intent.request_hash AS intent_request_hash,
                   event.payload AS event_payload,
                   run.entry_intent_id, run.protection_intent_id,
                   run.active_exit_intent_id
            FROM execution_events AS event
            JOIN order_intents AS intent ON intent.intent_id = event.intent_id
            JOIN intraday_runs AS run ON run.plan_id = event.plan_id
            WHERE event.plan_id = ? AND event.intent_id = ? AND event.event_type = ?
              AND event.run_version = ? AND event.writer_fence = ?
              AND run.state = ? AND run.version = ?
              AND run.writer_id = ? AND run.writer_fence = ?
              AND run.writer_lease_until >= ?
              AND run.broker_sync_fence = run.writer_fence
              AND run.last_broker_sync_at >= ?
              AND (
                event.event_type <> 'identity_recovery_send_reserved'
                OR intent.order_role <> 'ENTRY'
                OR (
                  run.approved_writer_fence = run.writer_fence
                  AND run.approved_envelope_sha256 IS NOT NULL
                  AND run.approval_receipt_sha256 IS NOT NULL
                  AND run.approval_interaction_id IS NOT NULL
                  AND run.boot_id_hash IS NOT NULL
                  AND run.approved_at IS NOT NULL
                  AND run.approval_expires_at > ?
                  AND run.entry_disabled_at IS NULL
                  AND run.loss_fuse_at IS NULL
                )
              )
            """,
            (
                plan_id,
                intent_id,
                event_type,
                expected_run_version,
                writer_fence,
                expected_state,
                expected_run_version,
                writer_id,
                writer_fence,
                minimum_lease,
                fresh_sync,
                timestamp,
            ),
        ).fetchone()
        if row is None:
            return False
        try:
            payload = json.loads(row["event_payload"] or "{}")
        except (TypeError, ValueError):
            return False
        if not isinstance(payload, dict):
            return False
        if not self._intraday_action_matches(
            event_type=event_type,
            intent_id=intent_id,
            intent=row,
            run=row,
            expected_state=expected_state,
            target_state=expected_state,
        ):
            return False
        if event_type == "identity_recovery_send_reserved":
            return (
                row["intent_request_hash"] == request_hash
                and payload.get("request_hash", request_hash) == request_hash
                and row["order_role"] != "PROTECTION"
                and row["recovery_deadline_at"] is not None
                and row["recovery_deadline_at"] >= timestamp
            )
        return payload.get("request_hash") == request_hash

    @staticmethod
    def _intraday_action_matches(
        *,
        event_type: str,
        intent_id: str,
        intent: Mapping[str, Any],
        run: Mapping[str, Any],
        expected_state: str,
        target_state: str,
    ) -> bool:
        role = intent["order_role"]
        side = intent["side"]
        if event_type == "identity_recovery_send_reserved":
            if expected_state == "ENTRY_UNKNOWN":
                return (
                    target_state == expected_state
                    and role == "ENTRY"
                    and side == "BUY"
                    and run["entry_intent_id"] == intent_id
                )
            return (
                expected_state == "EXIT_UNKNOWN"
                and target_state == expected_state
                and role in {"FORCE_EXIT", "EMERGENCY_EXIT"}
                and side == "SELL"
                and run["active_exit_intent_id"] == intent_id
            )
        if event_type == "entry_cancel_send_reserved":
            return (
                (
                    (expected_state == "ENTRY_WORKING" and target_state == "ENTRY_CANCELING")
                    or expected_state == target_state == "ENTRY_CANCELING"
                )
                and role == "ENTRY"
                and side == "BUY"
                and run["entry_intent_id"] == intent_id
            )
        if event_type == "conditional_cancel_send_reserved":
            return (
                expected_state in {"PROTECTED", "EXIT_CANCELING_PROTECTION"}
                and target_state == "EXIT_CANCELING_PROTECTION"
                and role == "PROTECTION"
                and side == "SELL"
                and run["protection_intent_id"] == intent_id
            )
        return False

    def complete_intraday_order_action(
        self,
        *,
        plan_id: str,
        intent_id: str,
        expected_state: str,
        expected_version: int,
        next_state: str,
        writer_id: str,
        writer_fence: int,
        event_type: str,
        status: str,
        broker_order_id: str | None = None,
        filled_quantity: Any = "0",
        remaining_quantity: Any | None = None,
        average_fill_price: Any | None = None,
        run_updates: Mapping[str, object] | None = None,
        payload: Mapping[str, Any] | None = None,
        now: datetime | None = None,
        reason_code: str | None = None,
    ) -> dict[str, Any] | None:
        """Record a broker projection and its fenced run transition atomically."""

        timestamp = self._intraday_time(now)
        clean_writer = self._intraday_identifier(writer_id, "writer_id")
        clean_status = self._intraday_identifier(status, "status")
        clean_event = self._intraday_identifier(event_type, "event_type")
        clean_filled = self._canonical_decimal(filled_quantity)
        clean_remaining = (
            self._canonical_decimal(remaining_quantity)
            if remaining_quantity is not None
            else None
        )
        clean_average = (
            self._canonical_decimal(average_fill_price)
            if average_fill_price is not None
            else None
        )
        clean_broker_id = (
            self._intraday_identifier(broker_order_id, "broker_order_id")
            if broker_order_id is not None
            else None
        )
        prepared_updates = self._prepare_intraday_run_updates(run_updates)
        raw_json = self._json_dump(dict(payload)) if payload is not None else None

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            run = self._conn.execute(
                "SELECT * FROM intraday_runs WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if (
                run is None
                or run["state"] != expected_state
                or run["version"] != expected_version
                or run["writer_id"] != clean_writer
                or run["writer_fence"] != writer_fence
                or run["writer_lease_until"] is None
                or run["writer_lease_until"] <= timestamp
            ):
                self._conn.rollback()
                return None
            order = self._conn.execute(
                """
                SELECT execution_orders.*, order_intents.plan_id,
                       order_intents.order_role, order_intents.quantity
                FROM execution_orders
                JOIN order_intents USING (intent_id)
                WHERE execution_orders.intent_id = ?
                """,
                (intent_id,),
            ).fetchone()
            if order is None or order["plan_id"] != plan_id:
                raise ValueError("intraday execution order does not belong to run")
            pointer = {
                "ENTRY": run["entry_intent_id"],
                "PROTECTION": run["protection_intent_id"],
                "FORCE_EXIT": run["active_exit_intent_id"],
                "EMERGENCY_EXIT": run["active_exit_intent_id"],
            }.get(order["order_role"])
            if pointer != intent_id:
                raise ValueError("intraday execution order is not the active run pointer")
            if (
                order["broker_order_id"] is not None
                and clean_broker_id is not None
                and order["broker_order_id"] != clean_broker_id
            ):
                raise ValueError("broker order identity may not be overwritten")
            prior_filled = Decimal(order["filled_quantity"])
            if Decimal(clean_filled) < prior_filled:
                raise ValueError("cumulative filled quantity may not decrease")
            if (
                order["order_role"] == "ENTRY"
                and Decimal(clean_filled) != prior_filled
                and prepared_updates.get("owned_qty") != clean_filled
            ):
                raise ValueError("ENTRY fill and owned_qty must advance atomically")
            if clean_remaining is not None and (
                Decimal(clean_filled) + Decimal(clean_remaining)
                != Decimal(order["quantity"])
            ):
                raise ValueError("filled and remaining quantity must equal intent quantity")

            run_updated = self._update_intraday_run_cas(
                plan_id=plan_id,
                expected_state=expected_state,
                expected_version=expected_version,
                next_state=next_state,
                writer_id=clean_writer,
                writer_fence=writer_fence,
                timestamp=timestamp,
                reason_code=reason_code,
                updates=prepared_updates,
            )
            if not run_updated:
                self._conn.rollback()
                return None
            self._conn.execute(
                """
                UPDATE execution_orders
                SET status = ?, broker_order_id = COALESCE(broker_order_id, ?),
                    raw = ?, updated_at = ?, filled_quantity = ?,
                    remaining_quantity = ?, average_fill_price = ?,
                    last_broker_observed_at = ?
                WHERE intent_id = ?
                """,
                (
                    clean_status,
                    clean_broker_id,
                    raw_json,
                    timestamp,
                    clean_filled,
                    clean_remaining,
                    clean_average,
                    timestamp,
                    intent_id,
                ),
            )
            self._insert_intraday_event(
                plan_id=plan_id,
                run_version=expected_version + 1,
                writer_fence=writer_fence,
                intent_id=intent_id,
                event_type=clean_event,
                status=clean_status,
                payload=payload,
                timestamp=timestamp,
            )
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()
        return {
            "run": self.load_intraday_run(plan_id),
            "order": self.load_execution_order(intent_id),
        }

    @staticmethod
    def _intraday_datetime(value: datetime | None) -> datetime:
        timestamp = value or datetime.now(timezone.utc)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("intraday timestamps must be timezone-aware")
        return timestamp.astimezone(timezone.utc)

    @staticmethod
    def _positive_seconds(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("lease seconds must be a positive integer")
        return value

    @staticmethod
    def _intraday_identifier(value: Any, label: str) -> str:
        clean = str(value or "").strip()
        if not clean or len(clean) > 240 or any(char.isspace() for char in clean):
            raise ValueError(f"{label} must be a non-whitespace identifier")
        return clean

    def _prepare_intraday_run_updates(
        self, updates: Mapping[str, object] | None
    ) -> dict[str, object]:
        if updates is None:
            return {}
        if not isinstance(updates, Mapping):
            raise TypeError("intraday run updates must be a mapping")
        allowed = (
            self._INTRADAY_DECIMAL_RUN_COLUMNS
            | self._INTRADAY_TIME_RUN_COLUMNS
            | self._INTRADAY_TEXT_RUN_COLUMNS
        )
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"unsupported intraday run update: {sorted(unknown)[0]}")
        prepared: dict[str, object] = {}
        for column in sorted(updates):
            value = updates[column]
            if column in self._INTRADAY_DECIMAL_RUN_COLUMNS:
                if value is None and column == "average_entry_price":
                    prepared[column] = None
                elif value is None:
                    raise ValueError(f"{column} may not be null")
                else:
                    prepared[column] = self._canonical_decimal(value)
            elif column in self._INTRADAY_TIME_RUN_COLUMNS:
                if value is None:
                    prepared[column] = None
                else:
                    timestamp = (
                        value
                        if isinstance(value, datetime)
                        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                    )
                    prepared[column] = self._intraday_time(timestamp)
            else:
                if value is None:
                    prepared[column] = None
                else:
                    prepared[column] = self._intraday_identifier(value, column)
        return prepared

    def _update_intraday_run_cas(
        self,
        *,
        plan_id: str,
        expected_state: str,
        expected_version: int,
        next_state: str,
        writer_id: str,
        writer_fence: int,
        timestamp: str,
        reason_code: str | None,
        updates: Mapping[str, object],
    ) -> bool:
        self._validate_intraday_transition(expected_state, next_state)
        assignments = ["state = ?", "version = version + 1", "updated_at = ?"]
        values: list[object] = [next_state, timestamp]
        if reason_code is not None:
            assignments.append("reason_code = ?")
            values.append(self._intraday_identifier(reason_code, "reason_code"))
        for column, value in updates.items():
            assignments.append(f"{column} = ?")
            values.append(value)
        values.extend(
            [
                plan_id,
                expected_state,
                expected_version,
                writer_id,
                writer_fence,
                timestamp,
            ]
        )
        approval_guard = ""
        if next_state == "READY_TO_ENTER":
            approval_guard = """
              AND approved_writer_fence = writer_fence
              AND approved_envelope_sha256 IS NOT NULL
              AND approval_receipt_sha256 IS NOT NULL
              AND approval_interaction_id IS NOT NULL
              AND boot_id_hash IS NOT NULL
              AND approved_at IS NOT NULL
              AND approval_expires_at > ?
              AND entry_disabled_at IS NULL
              AND loss_fuse_at IS NULL
            """
            values.append(timestamp)
        updated = self._conn.execute(
            f"""
            UPDATE intraday_runs
            SET {', '.join(assignments)}
            WHERE plan_id = ? AND state = ? AND version = ?
              AND writer_id = ? AND writer_fence = ?
              AND writer_lease_until > ?
              {approval_guard}
            """,
            values,
        )
        return updated.rowcount == 1

    @classmethod
    def _validate_intraday_transition(cls, current: str, next_state: str) -> None:
        if (str(current), str(next_state)) not in cls._INTRADAY_STATE_TRANSITIONS:
            raise ValueError(f"intraday state transition is not allowed: {current}->{next_state}")

    def _insert_intraday_event(
        self,
        *,
        plan_id: str,
        run_version: int | None,
        writer_fence: int,
        intent_id: str,
        event_type: str,
        status: str,
        payload: Mapping[str, Any] | None,
        timestamp: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO execution_events (
                intent_id, event_type, status, payload, created_at,
                plan_id, run_version, writer_fence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intent_id,
                event_type,
                status,
                self._json_dump(dict(payload)) if payload is not None else None,
                timestamp,
                plan_id,
                run_version,
                writer_fence,
            ),
        )

    def _intraday_run_row(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for column in (
            "writer_lease_until",
            "approved_at",
            "approval_expires_at",
            "entry_disabled_at",
            "unprotected_since",
            "loss_fuse_at",
            "last_broker_sync_at",
            "last_stream_sync_at",
            "created_at",
            "updated_at",
        ):
            result[column] = (
                datetime.fromisoformat(result[column]) if result[column] is not None else None
            )
        for column in ("owned_qty", "protected_qty", "average_entry_price"):
            result[column] = (
                Decimal(result[column]) if result[column] is not None else None
            )
        return result

    def _intraday_order_intent_row(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            envelope = json.loads(row["payload"])
            body = json.loads(row["request_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("stored intraday request is not canonical JSON") from exc
        if not isinstance(envelope, dict) or not isinstance(body, dict):
            raise RuntimeError("stored intraday request must be a JSON object")
        expected_envelope = {
            "account_key": row["account_key"],
            "plan_id": row["plan_id"],
            "order_role": row["order_role"],
            "method": envelope.get("method"),
            "path": envelope.get("path"),
            "body": body,
        }
        canonical_envelope = self._canonical_json(expected_envelope)
        actual_hash = hashlib.sha256(canonical_envelope.encode("utf-8")).hexdigest()
        if (
            self._canonical_json(envelope) != canonical_envelope
            or self._canonical_json(body) != row["request_json"]
            or actual_hash != row["request_hash"]
        ):
            raise RuntimeError("stored intraday request failed integrity verification")
        return {
            "intent_id": row["intent_id"],
            "idempotency_key": row["idempotency_key"],
            "account_key": row["account_key"],
            "plan_id": row["plan_id"],
            "order_role": row["order_role"],
            "symbol": row["symbol"],
            "side": row["side"],
            "quantity": Decimal(row["quantity"]),
            "order_type": row["order_type"],
            "limit_price": Decimal(row["limit_price"]) if row["limit_price"] else None,
            "method": envelope.get("method"),
            "path": envelope.get("path"),
            "body": body,
            "request_json": row["request_json"],
            "request_hash": row["request_hash"],
            "first_attempt_at": datetime.fromisoformat(row["first_attempt_at"]),
            "recovery_deadline_at": (
                datetime.fromisoformat(row["recovery_deadline_at"])
                if row["recovery_deadline_at"] is not None
                else None
            ),
            "reserved_at": datetime.fromisoformat(row["reserved_at"]),
            "send_by": datetime.fromisoformat(row["send_by"]),
            "reserved_writer_fence": row["reserved_writer_fence"],
            "reserved_run_version": row["reserved_run_version"],
            "created_at": datetime.fromisoformat(row["created_at"]),
        }
