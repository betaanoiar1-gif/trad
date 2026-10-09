"""Persistent, pauseable paper-trading orchestration.

`AutonomousPaperRunner` is the safety boundary between strategy selection and
the existing Spot/Futures paper engines.  It stores every selection, signal,
market event, operation, recovery, and error in a local SQLite run journal.
It never submits real orders.  A runner starts in ``paused`` after recovery;
operators must explicitly resume it after checking fresh public market data.

The runner accepts provider callables instead of hiding network access.  The
CLI supplies public read-only Binance providers, while tests and the dashboard
can supply deterministic offline providers.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from enum import Enum
import json
from pathlib import Path
import sqlite3
import threading
import uuid
from typing import Any, Callable, Mapping, Sequence

from .backtest import BacktestConfig, InstrumentMode, SelectionResult, select_strategy, validate_historical_candles
from .futures_paper import FuturesOrderStatus, FuturesPaperEngine, FuturesPositionSide
from .market_data import OHLCV
from .spot_paper import OrderStatus, SpotPaperEngine
from .strategies import StrategyRegistry


class RunnerError(RuntimeError):
    """Base class for orchestration and persistence errors."""


class RunnerPersistenceError(RunnerError):
    """Raised when the run journal cannot be read or written safely."""


class RunnerState(str, Enum):
    """Durable lifecycle states of one autonomous paper process."""

    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class RunnerConfig:
    """Safe operating parameters shared by both isolated domains."""

    symbol: str = "BTC/USDT"
    interval: str = "1m"
    refresh_seconds: float = 60.0
    history_limit: int = 250
    position_fraction: Decimal = Decimal("0.10")
    run_both_domains: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise RunnerError("runner symbol must be non-empty")
        if not isinstance(self.interval, str) or not self.interval.strip():
            raise RunnerError("runner interval must be non-empty")
        refresh = float(self.refresh_seconds)
        if refresh <= 0 or refresh != refresh or refresh == float("inf"):
            raise RunnerError("refresh_seconds must be positive and finite")
        if isinstance(self.history_limit, bool) or not isinstance(self.history_limit, int) or self.history_limit < 40:
            raise RunnerError("history_limit must be at least 40")
        fraction = _decimal(self.position_fraction, "position_fraction")
        if fraction <= 0 or fraction > 1:
            raise RunnerError("position_fraction must be between 0 and 1")
        if not isinstance(self.run_both_domains, bool):
            raise RunnerError("run_both_domains must be boolean")
        object.__setattr__(self, "symbol", self.symbol.strip())
        object.__setattr__(self, "interval", self.interval.strip())
        object.__setattr__(self, "refresh_seconds", refresh)
        object.__setattr__(self, "position_fraction", fraction)


class RunJournal:
    """Small transactional SQLite journal for progress and audit history."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._create_schema()
        except sqlite3.Error as exc:
            raise RunnerPersistenceError(f"could not open run journal {self.path!r}") from exc
        self._lock = threading.RLock()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runner_meta (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS selection_runs (
                run_id TEXT PRIMARY KEY,
                domain TEXT NOT NULL,
                created_at TEXT NOT NULL,
                selected_strategy TEXT,
                accepted INTEGER NOT NULL,
                reason TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS strategy_results (
                run_id TEXT NOT NULL,
                domain TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                status TEXT NOT NULL,
                accepted INTEGER NOT NULL,
                score REAL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (run_id, domain, strategy_name),
                FOREIGN KEY (run_id) REFERENCES selection_runs(run_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS market_events (
                domain TEXT NOT NULL,
                open_time TEXT NOT NULL,
                close_time TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (domain, open_time)
            );
            CREATE TABLE IF NOT EXISTS decisions (
                domain TEXT NOT NULL,
                decision_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                strategy_name TEXT,
                action TEXT NOT NULL,
                reason TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (domain, decision_key)
            );
            CREATE TABLE IF NOT EXISTS operations (
                domain TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                operation_type TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                error_text TEXT,
                PRIMARY KEY (domain, idempotency_key)
            );
            CREATE TABLE IF NOT EXISTS run_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT,
                created_at TEXT NOT NULL,
                category TEXT NOT NULL,
                message TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recovery_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            """
        )

    @contextmanager
    def _transaction(self):
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                yield self._connection
                self._connection.execute("COMMIT")
            except Exception as exc:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                if isinstance(exc, RunnerPersistenceError):
                    raise
                raise RunnerPersistenceError("run journal transaction failed") from exc

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def set_meta(self, key: str, value: Any) -> None:
        try:
            encoded = _json(value)
            with self._transaction() as connection:
                connection.execute(
                    "INSERT INTO runner_meta(key,value_json) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                    (key, encoded),
                )
        except RunnerPersistenceError:
            raise
        except Exception as exc:
            raise RunnerPersistenceError(f"could not persist runner metadata {key!r}") from exc

    def get_meta(self, key: str, default: Any = None) -> Any:
        try:
            row = self._connection.execute("SELECT value_json FROM runner_meta WHERE key=?", (key,)).fetchone()
            return default if row is None else json.loads(row["value_json"])
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            raise RunnerPersistenceError("could not read runner metadata") from exc

    def record_selection(self, domain: str, selection: SelectionResult, run_id: str) -> None:
        payload = selection.as_dict()
        created = selection.created_at.isoformat()
        try:
            with self._transaction() as connection:
                connection.execute(
                    "INSERT INTO selection_runs(run_id,domain,created_at,selected_strategy,accepted,reason,payload_json) VALUES(?,?,?,?,?,?,?)",
                    (run_id, domain, created, selection.selected_strategy, int(selection.accepted), selection.reason, _json(payload)),
                )
                for result in selection.results:
                    connection.execute(
                        "INSERT INTO strategy_results(run_id,domain,strategy_name,status,accepted,score,payload_json) VALUES(?,?,?,?,?,?,?)",
                        (run_id, domain, result.strategy_name, result.status, int(result.accepted), result.objective_score, _json(result.as_dict())),
                    )
        except sqlite3.IntegrityError as exc:
            raise RunnerPersistenceError(f"selection run {run_id!r} already exists") from exc

    def record_market_event(self, domain: str, event: OHLCV) -> bool:
        payload = _json(_jsonable(event))
        try:
            with self._transaction() as connection:
                existing = connection.execute(
                    "SELECT payload_json FROM market_events WHERE domain=? AND open_time=?",
                    (domain, event.timestamp.isoformat()),
                ).fetchone()
                if existing is not None:
                    previous_payload = json.loads(existing["payload_json"])
                    current_payload = json.loads(payload)
                    comparable = tuple(
                        key for key in current_payload
                        if key not in {"received_at"}
                    )
                    if any(previous_payload.get(key) != current_payload.get(key) for key in comparable):
                        raise RunnerPersistenceError("conflicting market event reused the same timestamp")
                    if previous_payload.get("received_at") != current_payload.get("received_at"):
                        connection.execute(
                            "UPDATE market_events SET payload_json=? WHERE domain=? AND open_time=?",
                            (payload, domain, event.timestamp.isoformat()),
                        )
                    return False
                connection.execute(
                    "INSERT INTO market_events(domain,open_time,close_time,payload_json) VALUES(?,?,?,?)",
                    (domain, event.timestamp.isoformat(), event.close_time.isoformat(), payload),
                )
                return True
        except RunnerPersistenceError:
            raise
        except sqlite3.Error as exc:
            raise RunnerPersistenceError("could not persist market event") from exc

    def market_events(self, domain: str) -> tuple[OHLCV, ...]:
        try:
            rows = self._connection.execute(
                "SELECT payload_json FROM market_events WHERE domain=? ORDER BY open_time",
                (domain,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise RunnerPersistenceError("could not read market events") from exc
        return tuple(_ohlcv_from_json(json.loads(row["payload_json"])) for row in rows)

    def record_decision(self, domain: str, key: str, payload: Mapping[str, Any]) -> bool:
        try:
            with self._transaction() as connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO decisions(domain,decision_key,created_at,strategy_name,action,reason,payload_json) VALUES(?,?,?,?,?,?,?)",
                    (domain, key, payload.get("created_at", _now().isoformat()), payload.get("strategy_name"), payload.get("action", "hold"), payload.get("reason", ""), _json(payload)),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise RunnerPersistenceError("could not persist strategy decision") from exc

    def record_operation(self, domain: str, key: str, operation_type: str, status: str, payload: Mapping[str, Any], error: str | None = None) -> bool:
        try:
            with self._transaction() as connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO operations(domain,idempotency_key,created_at,operation_type,status,payload_json,error_text) VALUES(?,?,?,?,?,?,?)",
                    (domain, key, _now().isoformat(), operation_type, status, _json(payload), error),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise RunnerPersistenceError("could not persist paper operation") from exc

    def update_operation(self, domain: str, key: str, status: str, payload: Mapping[str, Any], error: str | None = None) -> None:
        try:
            with self._transaction() as connection:
                connection.execute(
                    "UPDATE operations SET status=?, payload_json=?, error_text=? WHERE domain=? AND idempotency_key=?",
                    (status, _json(payload), error, domain, key),
                )
        except sqlite3.Error as exc:
            raise RunnerPersistenceError("could not update paper operation") from exc

    def record_error(self, domain: str | None, category: str, message: str, payload: Mapping[str, Any] | None = None) -> None:
        try:
            with self._transaction() as connection:
                connection.execute(
                    "INSERT INTO run_errors(domain,created_at,category,message,payload_json) VALUES(?,?,?,?,?)",
                    (domain, _now().isoformat(), category, message, _json(payload or {})),
                )
        except sqlite3.Error as exc:
            raise RunnerPersistenceError("could not persist runner error") from exc

    def record_recovery(self, event_type: str, payload: Mapping[str, Any]) -> None:
        try:
            with self._transaction() as connection:
                connection.execute(
                    "INSERT INTO recovery_events(created_at,event_type,payload_json) VALUES(?,?,?)",
                    (_now().isoformat(), event_type, _json(payload)),
                )
        except sqlite3.Error as exc:
            raise RunnerPersistenceError("could not persist recovery event") from exc

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for table in ("strategy_results", "market_events", "decisions", "operations", "run_errors", "recovery_events"):
            try:
                row = self._connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
                result[table] = int(row["count"])
            except sqlite3.Error as exc:
                raise RunnerPersistenceError("could not count runner journal records") from exc
        return result

    def recent(self, table: str, limit: int = 50) -> tuple[dict[str, Any], ...]:
        allowed = {"decisions", "operations", "run_errors", "recovery_events"}
        if table not in allowed:
            raise RunnerPersistenceError(f"unsupported journal table {table!r}")
        try:
            order_column = "id" if table in {"run_errors", "recovery_events"} else "created_at"
            rows = self._connection.execute(f"SELECT * FROM {table} ORDER BY {order_column} DESC LIMIT ?", (limit,)).fetchall()
        except sqlite3.Error as exc:
            raise RunnerPersistenceError("could not read runner journal records") from exc
        output = []
        for row in rows:
            item = dict(row)
            if "payload_json" in item:
                item["payload"] = json.loads(item.pop("payload_json"))
            output.append(item)
        return tuple(output)


class AutonomousPaperRunner:
    """A durable, pauseable orchestrator for two isolated paper engines."""

    def __init__(
        self,
        *,
        journal_path: str | Path,
        futures_database_path: str | Path | None = None,
        spot: SpotPaperEngine | None = None,
        futures: FuturesPaperEngine | None = None,
        config: RunnerConfig | None = None,
        registry: StrategyRegistry | None = None,
        backtest_config: BacktestConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config or RunnerConfig()
        self.registry = registry or StrategyRegistry.default()
        self.backtest_config = backtest_config or BacktestConfig(
            position_fraction=self.config.position_fraction,
            allow_short=True,
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.journal = RunJournal(journal_path)
        self._owns_futures = futures is None
        self.spot = spot or SpotPaperEngine(symbol=self.config.symbol, clock=self._clock)
        saved_spot_state = self.journal.get_meta("spot_engine_snapshot")
        if isinstance(saved_spot_state, dict):
            try:
                self.spot.restore_state(saved_spot_state)
            except Exception as exc:
                self.journal.record_error("spot", "recovery", f"{type(exc).__name__}: {exc}", {})
                raise RunnerPersistenceError("persisted Spot paper state failed reconciliation") from exc
        self.futures = futures or FuturesPaperEngine(database_path=futures_database_path, clock=self._clock)
        self._selections: dict[str, SelectionResult] = {}
        self._selection_payloads: dict[str, dict[str, Any]] = {}
        self._selected_names: dict[str, str | None] = {}
        for domain in ("spot", "futures"):
            saved = self.journal.get_meta(f"latest_selection:{domain}")
            if isinstance(saved, dict):
                self._selection_payloads[domain] = saved
                selected = saved.get("selected_strategy")
                self._selected_names[domain] = selected if isinstance(selected, str) else None
        previous = self.journal.get_meta("state", RunnerState.STOPPED.value)
        try:
            previous_state = RunnerState(previous)
        except ValueError as exc:
            self.journal.record_error(None, "recovery", "invalid persisted runner state", {"state": previous})
            raise RunnerPersistenceError("persisted runner state is invalid") from exc
        self._state = RunnerState.PAUSED if previous_state is RunnerState.RUNNING else previous_state
        self.journal.set_meta("spot_engine_snapshot", _jsonable(self.spot.state_snapshot()))
        self._last_cycle_at = self.journal.get_meta("last_cycle_at")
        self._blocked_reason = self.journal.get_meta("blocked_reason")
        if previous == RunnerState.RUNNING.value:
            self.journal.record_recovery("startup_paused_after_previous_run", {"previous_state": previous})
            self._state = RunnerState.PAUSED
            self.journal.set_meta("state", self._state.value)
        self.journal.set_meta("runner_config", _jsonable(self.config))

    def close(self) -> None:
        with self._lock:
            self.journal.set_meta("state", self._state.value)
            if self._owns_futures:
                self.futures.close()
            self.journal.close()

    @property
    def state(self) -> RunnerState:
        return self._state

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            selections = dict(self._selection_payloads)
            selections.update({
                domain: selection.as_dict()
                for domain, selection in self._selections.items()
            })
            return {
                "state": self._state.value,
                "blocked_reason": self._blocked_reason,
                "last_cycle_at": self._last_cycle_at,
                "selected": dict(self._selected_names),
                "selections": selections,
                "counts": self.journal.counts(),
                "recent_decisions": list(self.journal.recent("decisions", 20)),
                "recent_operations": list(self.journal.recent("operations", 20)),
                "recent_errors": list(self.journal.recent("run_errors", 20)),
                "recent_recoveries": list(self.journal.recent("recovery_events", 20)),
                "persistent_journal": self.journal.path != ":memory:",
                "warning": "Paper automation only; no real exchange orders are submitted.",
            }

    def evaluate(self, domain: str, candles: Sequence[OHLCV]) -> SelectionResult:
        domain = _domain(domain)
        instrument = InstrumentMode.SPOT if domain == "spot" else InstrumentMode.FUTURES
        settings = self.backtest_config
        if instrument is InstrumentMode.SPOT and settings.allow_short:
            settings = BacktestConfig(
                initial_equity=settings.initial_equity,
                fee_rate=settings.fee_rate,
                position_fraction=settings.position_fraction,
                train_fraction=settings.train_fraction,
                max_drawdown=settings.max_drawdown,
                min_validation_trades=settings.min_validation_trades,
                min_validation_return=settings.min_validation_return,
                max_validation_drawdown=settings.max_validation_drawdown,
                require_train_non_negative=settings.require_train_non_negative,
                allow_short=False,
            )
        history = validate_historical_candles(candles, expected_symbol=self.config.symbol, minimum=40)
        for event in history:
            self.journal.record_market_event(domain, event)
        selection = select_strategy(
            history,
            instrument=instrument,
            registry=self.registry,
            config=settings,
            symbol=self.config.symbol,
            now=self._now(),
        )
        run_id = f"{domain}:{selection.created_at.isoformat()}:{uuid.uuid4().hex}"
        self.journal.record_selection(domain, selection, run_id)
        self._selections[domain] = selection
        self._selection_payloads[domain] = selection.as_dict()
        self._selected_names[domain] = selection.selected_strategy
        self.journal.set_meta(f"latest_selection:{domain}", selection.as_dict())
        if selection.selected_strategy is None:
            self._blocked_reason = selection.reason
            self._set_state(RunnerState.BLOCKED, selection.reason)
        return selection

    def evaluate_all(self, candles_by_domain: Mapping[str, Sequence[OHLCV]]) -> dict[str, SelectionResult]:
        output: dict[str, SelectionResult] = {}
        for domain in ("spot", "futures") if self.config.run_both_domains else ("spot",):
            if domain not in candles_by_domain:
                self._block(f"missing {domain} historical candles")
                continue
            output[domain] = self.evaluate(domain, candles_by_domain[domain])
        return output

    def start(self) -> dict[str, Any]:
        with self._lock:
            if not self._ready_to_run():
                self._block("no accepted strategy is available; evaluate historical data before starting")
            else:
                self._blocked_reason = None
                self._set_state(RunnerState.RUNNING, "operator started paper automation")
            return self.snapshot()

    def pause(self) -> dict[str, Any]:
        with self._lock:
            if self._state is RunnerState.RUNNING:
                self._set_state(RunnerState.PAUSED, "operator paused paper automation")
            return self.snapshot()

    def resume(self) -> dict[str, Any]:
        with self._lock:
            if not self._ready_to_run():
                self._block("resume denied: no accepted strategy is available")
            else:
                self._blocked_reason = None
                self._set_state(RunnerState.RUNNING, "operator resumed paper automation")
            return self.snapshot()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._set_state(RunnerState.STOPPED, "operator stopped paper automation")
            return self.snapshot()

    def process_event(self, domain: str, event: OHLCV, *, ingest: bool = True) -> dict[str, Any]:
        """Persist and optionally ingest one completed event; trade only while RUNNING."""

        domain = _domain(domain)
        with self._lock:
            engine = self.spot if domain == "spot" else self.futures
            try:
                self.journal.record_market_event(domain, event)
                health = engine.record_market_data(event, now=self._now()) if ingest else engine.market_data_health(now=self._now())
            except Exception as exc:
                self._record_failure(domain, "market_data", exc, {"event": _jsonable(event)})
                self._block(f"market data failed for {domain}: {type(exc).__name__}: {exc}")
                raise
            payload: dict[str, Any] = {"domain": domain, "health": _jsonable(health), "event": _jsonable(event)}
            if self._state is not RunnerState.RUNNING:
                payload["status"] = "not_running"
                return payload
            selected_name = self._selected_names.get(domain)
            if not selected_name:
                self._block("market event received without an accepted strategy")
                payload["status"] = "blocked_no_strategy"
                return payload
            try:
                definition = self.registry.get(selected_name)
                history = self.journal.market_events(domain)
                if len(history) < definition.required_bars:
                    payload["status"] = "warming_up"
                    return payload
                signal = definition.evaluate(history)
            except Exception as exc:
                self._record_failure(domain, "strategy", exc, {"strategy_name": selected_name, "event": _jsonable(event)})
                self._block(f"strategy evaluation failed for {domain}: {type(exc).__name__}: {exc}")
                raise
            decision_key = f"{event.timestamp.isoformat()}:{selected_name}"
            decision_payload = {
                "created_at": self._now().isoformat(),
                "strategy_name": signal.strategy_name,
                "action": signal.action.value,
                "reason": signal.reason,
                "confidence": signal.confidence,
                "indicators": dict(signal.indicators),
                "event_open_time": event.timestamp.isoformat(),
            }
            inserted = self.journal.record_decision(domain, decision_key, decision_payload)
            payload.update({"status": "decision_recorded" if inserted else "duplicate_decision", "signal": _jsonable(signal)})
            if not inserted:
                return payload
            target = signal.action.target
            if target == -1 and domain == "spot":
                target = 0
                payload["short_blocked"] = True
            if target is None:
                return payload
            try:
                operations = self._reconcile_target(domain, target, event, signal)
                payload["operations"] = operations
                self._persist_spot_state()
            except Exception as exc:
                self._persist_spot_state()
                self._record_failure(domain, "execution", exc, decision_payload)
                self._block(f"paper operation failed for {domain}: {type(exc).__name__}: {exc}")
                payload["status"] = "blocked_error"
            self._last_cycle_at = self._now().isoformat()
            self.journal.set_meta("last_cycle_at", self._last_cycle_at)
            return payload

    def run_once(self, providers: Mapping[str, Callable[[], Sequence[OHLCV]]], *, resume: bool = False) -> dict[str, Any]:
        """Fetch one history window per domain and process its newest candle."""

        with self._lock:
            if not self._selections:
                histories = {domain: provider() for domain, provider in providers.items()}
                self.evaluate_all(histories)
                if self._state is RunnerState.BLOCKED:
                    return {"status": "blocked", "runner": self.snapshot()}
                if self._state is RunnerState.STOPPED:
                    self.start()
                elif self._state is RunnerState.PAUSED:
                    if not resume:
                        return {"status": "paused", "runner": self.snapshot()}
                    self.resume()
            elif self._state is RunnerState.PAUSED:
                if not resume:
                    return {"status": "paused", "runner": self.snapshot()}
                self.resume()
            output: dict[str, Any] = {"status": "running", "domains": {}}
            for domain, provider in providers.items():
                history = validate_historical_candles(provider(), expected_symbol=self.config.symbol, minimum=40)
                for event in history:
                    self.journal.record_market_event(domain, event)
                output["domains"][domain] = self.process_event(domain, history[-1])
            self._last_cycle_at = self._now().isoformat()
            self.journal.set_meta("last_cycle_at", self._last_cycle_at)
            return output

    def run_forever(
        self,
        providers: Mapping[str, Callable[[], Sequence[OHLCV]]],
        *,
        stop_event: threading.Event | None = None,
        max_cycles: int | None = None,
        resume: bool = False,
    ) -> None:
        """Run a bounded or externally stoppable public-data loop.

        This method intentionally has no daemon promise.  The caller owns the
        process supervisor and can provide a stop event or a finite cycle count.
        """

        stopper = stop_event or threading.Event()
        cycles = 0
        while not stopper.is_set() and (max_cycles is None or cycles < max_cycles):
            try:
                result = self.run_once(providers, resume=resume)
                cycles += 1
                if result.get("status") in {"blocked", "paused"} or self._state in {RunnerState.BLOCKED, RunnerState.PAUSED}:
                    return
            except Exception as exc:
                self._record_failure(None, "provider", exc, {})
                self._block(f"provider cycle failed: {type(exc).__name__}: {exc}")
                return
            stopper.wait(self.config.refresh_seconds)

    def _reconcile_target(self, domain: str, target: int, event: OHLCV, signal: Any) -> list[dict[str, Any]]:
        if domain == "spot":
            return self._spot_target(target, event, signal)
        return self._futures_target(target, event, signal)

    def _spot_target(self, target: int, event: OHLCV, signal: Any) -> list[dict[str, Any]]:
        base = self.spot.balance(self.spot.symbol_rules.base_asset)
        quote = self.spot.balance(self.spot.symbol_rules.quote_asset)
        price = _execution_price(event.close, self.spot.symbol_rules.price_precision)
        operations: list[dict[str, Any]] = []
        if target > 0 and base.total <= 0:
            quantity = _quantize(quote.available * self.config.position_fraction / price, Decimal("0.00000001"))
            if quantity > 0:
                operations.append(self._spot_order("buy", quantity, price, event, signal))
        elif target == 0 and base.available > 0:
            operations.append(self._spot_order("sell", base.available, price, event, signal))
        return operations

    def _spot_order(self, side: str, quantity: Decimal, price: Decimal, event: OHLCV, signal: Any) -> dict[str, Any]:
        key = f"auto:spot:{event.timestamp.isoformat()}:{side}"
        if not self.journal.record_operation("spot", key, "submit_and_fill", "started", {"side": side, "quantity": str(quantity), "price": str(price), "strategy": signal.strategy_name}):
            return {"idempotency_key": key, "status": "duplicate_operation"}
        try:
            order = self.spot.submit_order(side=side, quantity=quantity, price=price, client_order_id=key, now=self._now())
            if order.status is OrderStatus.REJECTED:
                self.journal.update_operation("spot", key, "rejected", _jsonable(order), order.rejection_reason)
                return {"idempotency_key": key, "status": "rejected", "reason": order.rejection_reason}
            fill = self.spot.execute_fill(order.order_id, quantity=quantity, price=price, fill_id=key + ":fill", now=self._now())
        except Exception as exc:
            self.journal.update_operation("spot", key, "failed", {"error": f"{type(exc).__name__}: {exc}"}, str(exc))
            raise
        self.journal.update_operation("spot", key, "filled", {"order": _jsonable(order), "fill": _jsonable(fill)})
        return {"idempotency_key": key, "status": "filled", "order_id": order.order_id, "fill_id": fill.fill_id}

    def _futures_target(self, target: int, event: OHLCV, signal: Any) -> list[dict[str, Any]]:
        operations: list[dict[str, Any]] = []
        price = _execution_price(event.close, self.futures.contract_rules.price_precision)
        positions = self.futures.positions()
        current = positions[0] if positions else None
        desired_side = FuturesPositionSide.LONG if target > 0 else FuturesPositionSide.SHORT if target < 0 else None
        if current is not None and (desired_side is None or current.side is not desired_side):
            key = f"auto:futures:{event.timestamp.isoformat()}:reduce"
            quantity = current.quantity
            reduced = False
            if self.journal.record_operation(
                "futures", key, "reduce_and_fill", "started",
                {"quantity": str(quantity), "price": str(price), "strategy": signal.strategy_name},
            ):
                try:
                    order = self.futures.reduce_position(
                        side=current.side, quantity=quantity, price=price,
                        client_order_id=key, now=self._now(),
                    )
                    if order.status is FuturesOrderStatus.REJECTED:
                        self.journal.update_operation("futures", key, "rejected", _jsonable(order), order.rejection_reason)
                        operations.append({"idempotency_key": key, "status": "rejected", "reason": order.rejection_reason})
                    else:
                        fill = self.futures.execute_fill(
                            order.order_id, quantity=quantity, price=price,
                            fill_id=key + ":fill", now=self._now(),
                        )
                        self.journal.update_operation("futures", key, "filled", {"order": _jsonable(order), "fill": _jsonable(fill)})
                        operations.append({"idempotency_key": key, "status": "filled", "order_id": order.order_id, "fill_id": fill.fill_id})
                        reduced = True
                except Exception as exc:
                    self.journal.update_operation("futures", key, "failed", {"error": f"{type(exc).__name__}: {exc}"}, str(exc))
                    raise
            else:
                operations.append({"idempotency_key": key, "status": "duplicate_operation"})
            if not reduced:
                return operations
            current = None
        if desired_side is not None and current is None:
            key = f"auto:futures:{event.timestamp.isoformat()}:open:{desired_side.value}"
            if self.journal.record_operation(
                "futures", key, "open_and_fill", "started",
                {"quantity_fraction": str(self.config.position_fraction), "price": str(price), "strategy": signal.strategy_name},
            ):
                try:
                    balance = self.futures.balance()
                    quantity = _quantize(balance.available * self.config.position_fraction / price, Decimal("0.00000001"))
                    if quantity <= 0:
                        self.journal.update_operation("futures", key, "rejected", {"reason": "calculated quantity is zero"}, "calculated quantity is zero")
                        operations.append({"idempotency_key": key, "status": "rejected", "reason": "calculated quantity is zero"})
                    else:
                        order = self.futures.open_position(
                            side=desired_side, quantity=quantity, price=price,
                            client_order_id=key, now=self._now(),
                        )
                        if order.status is FuturesOrderStatus.REJECTED:
                            self.journal.update_operation("futures", key, "rejected", _jsonable(order), order.rejection_reason)
                            operations.append({"idempotency_key": key, "status": "rejected", "reason": order.rejection_reason})
                        else:
                            fill = self.futures.execute_fill(
                                order.order_id, quantity=quantity, price=price,
                                fill_id=key + ":fill", now=self._now(),
                            )
                            self.journal.update_operation("futures", key, "filled", {"order": _jsonable(order), "fill": _jsonable(fill)})
                            operations.append({"idempotency_key": key, "status": "filled", "order_id": order.order_id, "fill_id": fill.fill_id})
                except Exception as exc:
                    self.journal.update_operation("futures", key, "failed", {"error": f"{type(exc).__name__}: {exc}"}, str(exc))
                    raise
            else:
                operations.append({"idempotency_key": key, "status": "duplicate_operation"})
        return operations

    def _persist_spot_state(self) -> None:
        self.journal.set_meta("spot_engine_snapshot", _jsonable(self.spot.state_snapshot()))

    def _ready_to_run(self) -> bool:
        domains = ("spot", "futures") if self.config.run_both_domains else ("spot",)
        return all(self._selected_names.get(domain) for domain in domains)

    def _set_state(self, state: RunnerState, reason: str) -> None:
        self._state = state
        self.journal.set_meta("state", state.value)
        self.journal.set_meta("state_reason", reason)
        self.journal.record_recovery(f"state_{state.value}", {"reason": reason})

    def _block(self, reason: str) -> None:
        self._blocked_reason = reason
        self.journal.set_meta("blocked_reason", reason)
        self._set_state(RunnerState.BLOCKED, reason)

    def _record_failure(self, domain: str | None, category: str, exc: Exception, payload: Mapping[str, Any]) -> None:
        self.journal.record_error(domain, category, f"{type(exc).__name__}: {exc}", payload)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RunnerError("runner clock must return an aware datetime")
        return value.astimezone(timezone.utc)


def _domain(value: str) -> str:
    if value not in {"spot", "futures"}:
        raise RunnerError("domain must be spot or futures")
    return value


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:
        raise RunnerError(f"{field_name} must be decimal") from exc
    if not result.is_finite():
        raise RunnerError(f"{field_name} must be finite")
    return result


def _quantize(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_DOWN)


def _execution_price(value: Any, precision: int) -> Decimal:
    decimal_value = _decimal(value, "execution price")
    if decimal_value <= 0:
        raise RunnerError("execution price must be positive")
    return decimal_value.quantize(Decimal("1").scaleb(-precision), rounding=ROUND_HALF_UP)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _json(value: Any) -> str:
    return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"))


def _ohlcv_from_json(payload: Mapping[str, Any]) -> OHLCV:
    return OHLCV(
        symbol=str(payload["symbol"]),
        timestamp=datetime.fromisoformat(str(payload["timestamp"])),
        close_time=datetime.fromisoformat(str(payload["close_time"])),
        received_at=datetime.fromisoformat(str(payload["received_at"])),
        timeframe_seconds=int(payload["timeframe_seconds"]),
        open=float(payload["open"]),
        high=float(payload["high"]),
        low=float(payload["low"]),
        close=float(payload["close"]),
        volume=float(payload["volume"]),
    )
