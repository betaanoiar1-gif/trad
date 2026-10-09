"""Local browser dashboard and API integration for the paper engines.

The dashboard is deliberately small and dependency-free. It owns one Spot
engine and one Futures engine, delegates all accounting to those engines, and
exposes paper operations over a local HTTP server. The CLI can opt into the
public Binance OHLCV providers for historical evaluation and a supervised
background paper loop; it never submits exchange orders or persists credentials.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import argparse
from http import HTTPStatus
import importlib.resources
import json
import math
from pathlib import Path
import threading
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .autonomous import AutonomousPaperRunner, RunnerConfig
from .backtest import validate_historical_candles
from .futures_paper import (
    FuturesError,
    FuturesOrderStatus,
    FuturesPaperEngine,
    FuturesPersistenceError,
    FuturesReconciliationError,
)
from .market_data import DataHealthStatus, DataSafetyError, OHLCV
from .spot_paper import (
    OrderStatus,
    SpotPaperEngine,
    SpotPaperError,
)
from .runner import build_public_providers


DEFAULT_DASHBOARD_HOST = "127.0.0.1"
DEFAULT_DASHBOARD_PORT = 8765
DEFAULT_FUTURES_DATABASE = Path("var/futures-dashboard.sqlite3")
_MAX_REQUEST_BYTES = 64 * 1024


class DashboardError(RuntimeError):
    """Base error for safe dashboard request handling."""


class DashboardRequestError(DashboardError, ValueError):
    """Raised when an API request is malformed or cannot be accepted."""


class DashboardMarketDataError(DashboardRequestError):
    """Raised when a configured public market-data source cannot be used."""


class DashboardPersistenceError(DashboardError):
    """Raised when the durable Futures state cannot be used."""


class DashboardService:
    """Authoritative local integration facade for Spot and Futures engines.

    Spot remains the existing in-memory engine.  Futures receives the optional
    SQLite path and therefore retains the durable persistence/recovery contract
    from :mod:`trad.futures_paper`.  The two engines always have independent
    safety monitors, wallets, ledgers, and accounting state.
    """

    def __init__(
        self,
        *,
        spot: SpotPaperEngine | None = None,
        futures: FuturesPaperEngine | None = None,
        futures_database_path: str | Path | None = None,
        runner: AutonomousPaperRunner | None = None,
        runner_database_path: str | Path | None = None,
        runner_config: RunnerConfig | None = None,
        public_providers: Mapping[str, Callable[[], Sequence[OHLCV]]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._public_providers = dict(public_providers or {})
        self._background_stop = threading.Event()
        self._background_thread: threading.Thread | None = None
        self._background_error: str | None = None
        self.spot = spot or SpotPaperEngine(clock=self._clock)
        if futures is not None:
            self.futures = futures
            self._futures_durable = futures.database_path != ":memory:"
        else:
            if futures_database_path is not None:
                path_text = str(futures_database_path)
                if path_text != ":memory:":
                    Path(path_text).parent.mkdir(parents=True, exist_ok=True)
            self.futures = FuturesPaperEngine(
                database_path=futures_database_path,
                clock=self._clock,
            )
            self._futures_durable = self.futures.database_path != ":memory:"
        if runner is not None:
            self.runner = runner
        else:
            if runner_database_path is not None:
                journal_path = runner_database_path
            elif self._futures_durable:
                journal_path = Path(self.futures.database_path).with_name("trad-runner.sqlite3")
            else:
                journal_path = ":memory:"
            self.runner = AutonomousPaperRunner(
                journal_path=journal_path,
                spot=self.spot,
                futures=self.futures,
                config=runner_config or RunnerConfig(symbol=self.spot.symbol_rules.symbol),
                clock=self._clock,
            )
        self._last_prices: dict[str, Decimal] = {}
        self._last_market_events: dict[str, dict[str, Any]] = {}
        self._closed = False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.runner.pause()
            self._stop_background_loop()
            self.runner.close()
            self.futures.close()
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise DashboardPersistenceError("dashboard service is closed")

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise DashboardRequestError("dashboard clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    def _begin_manual_operation(self, domain: str, key: str, operation_type: str, payload: Mapping[str, Any]) -> None:
        try:
            self.runner.journal.record_operation(domain, key, operation_type, "started", payload)
        except Exception as exc:
            raise DashboardPersistenceError("could not journal manual paper operation") from exc

    def _finish_manual_operation(
        self,
        domain: str,
        key: str,
        status: str,
        payload: Any,
        error: str | None = None,
    ) -> None:
        try:
            self.runner.journal.update_operation(domain, key, status, payload, error)
        except Exception as exc:
            raise DashboardPersistenceError("could not finalize manual paper operation") from exc

    def _required_domains(self) -> tuple[str, ...]:
        return ("spot", "futures") if self.runner.config.run_both_domains else ("spot",)

    def _persisted_histories(self, domains: Sequence[str] | None = None) -> dict[str, tuple[OHLCV, ...]]:
        histories: dict[str, tuple[OHLCV, ...]] = {}
        for domain in tuple(domains or self._required_domains()):
            history = self.runner.journal.market_events(domain)
            if len(history) < 40:
                raise DashboardRequestError(
                    f"at least 40 persisted {domain} candles are required before evaluation"
                )
            histories[domain] = history
        return histories

    def _fetch_public_histories(self, domains: Sequence[str] | None = None) -> dict[str, Sequence[OHLCV]]:
        if not self._public_providers:
            raise DashboardRequestError(
                "no public market-data provider is configured; record completed candles first"
            )
        histories: dict[str, Sequence[OHLCV]] = {}
        for domain in tuple(domains or self._required_domains()):
            provider = self._public_providers.get(domain)
            if provider is None:
                raise DashboardRequestError(f"public {domain} market-data provider is not configured")
            try:
                fetched = validate_historical_candles(
                    tuple(provider()),
                    expected_symbol=self.runner.config.symbol,
                    minimum=40,
                )
            except Exception as exc:
                message = f"public {domain} market data is unavailable or incomplete: {type(exc).__name__}: {exc}"
                self._background_error = message
                try:
                    self.runner.journal.record_error(domain, "public_data", message, {})
                except Exception:
                    pass
                raise DashboardMarketDataError(message) from exc
            if not fetched:
                message = f"public {domain} market data returned no completed candles"
                self._background_error = message
                try:
                    self.runner.journal.record_error(domain, "public_data", message, {})
                except Exception:
                    pass
                raise DashboardMarketDataError(message)
            histories[domain] = fetched
        return histories

    def _ensure_selection_for_start(self) -> None:
        snapshot = self.runner.snapshot()
        selections = snapshot.get("selections", {})
        if all(domain in selections for domain in self._required_domains()):
            return
        # A configured public source makes the Dashboard's Start button useful:
        # it performs a bounded historical evaluation first. Without one, keep
        # the existing fail-closed behavior and let runner.start explain why.
        if self._public_providers:
            self.runner.evaluate_all(self._fetch_public_histories())

    def _run_background_loop(self, stop_event: threading.Event) -> None:
        try:
            self.runner.run_forever(self._public_providers, stop_event=stop_event)
        except Exception as exc:
            self._background_error = f"{type(exc).__name__}: {exc}"
            try:
                self.runner.journal.record_error(None, "dashboard_background", self._background_error, {})
            except Exception:
                pass

    def _start_background_loop(self) -> None:
        if not self._public_providers:
            return
        if self._background_thread is not None and self._background_thread.is_alive():
            return
        self._background_stop = threading.Event()
        self._background_error = None
        self._background_thread = threading.Thread(
            target=self._run_background_loop,
            args=(self._background_stop,),
            name="trad-public-paper-loop",
            daemon=True,
        )
        self._background_thread.start()

    def _stop_background_loop(self) -> None:
        self._background_stop.set()
        thread = self._background_thread
        self._background_thread = None
        if thread is not None and thread is not threading.current_thread():
            # The public connector has a bounded ten-second request timeout;
            # wait long enough that closing engines cannot race an in-flight
            # provider cycle.
            thread.join(timeout=12)
        if thread is not None and thread.is_alive():
            self._background_thread = thread

    def _automation_snapshot(self) -> dict[str, Any]:
        automation = self.runner.snapshot()
        automation["data_source"] = "public_binance" if self._public_providers else "dashboard_market_data_api"
        automation["background_loop"] = {
            "configured": bool(self._public_providers),
            "running": self._background_thread is not None and self._background_thread.is_alive(),
            "last_error": self._background_error,
            "description": (
                "Dashboard-owned public Spot and Futures polling loop"
                if self._public_providers
                else "No remote provider; use the market-data API to feed completed candles"
            ),
        }
        return automation

    def snapshot(self) -> dict[str, Any]:
        """Return a browser-safe snapshot composed from authoritative engines."""

        with self._lock:
            self._ensure_open()
            now = self._now()
            spot_health = self.spot.market_data_health(now=now)
            futures_health = self.futures.market_data_health(now=now)
            spot_prices = {}
            if spot_health.status is DataHealthStatus.SAFE and "spot" in self._last_prices:
                spot_prices[self.spot.symbol_rules.base_asset] = self._last_prices["spot"]
            try:
                spot_valuation = self.spot.portfolio_snapshot(
                    spot_prices,
                    now=now,
                    valuation_asset=self.spot.symbol_rules.quote_asset,
                )
            except Exception:
                # State reporting must not make the browser unusable.  The
                # actual health/reconciliation information remains available.
                spot_valuation = None
            spot_reconciliation = self.spot.reconcile()
            futures_reconciliation = self.futures.reconcile()
            spot_wallet = [_with_total(balance) for balance in self.spot.balances()]
            futures_wallet = [_with_total(balance) for balance in self.futures.balances()]
            futures_positions = []
            futures_unrealized = Decimal("0")
            for position in self.futures.positions():
                unrealized = position.unrealized_pnl(self.futures.contract_rules)
                futures_unrealized += unrealized
                item = _to_jsonable(position)
                item.update(
                    {
                        "notional": position.notional(self.futures.contract_rules),
                        "unrealized_pnl": unrealized,
                        "equity": position.equity(self.futures.contract_rules),
                    }
                )
                futures_positions.append(item)
            futures_wallet_total = sum((balance.total for balance in self.futures.balances()), Decimal("0"))
            futures_valuation = {
                "valuation_asset": self.futures.contract_rules.collateral_asset,
                "wallet_total": futures_wallet_total,
                "unrealized_pnl": futures_unrealized,
                "account_equity": futures_wallet_total + futures_unrealized,
                "mark_is_current": futures_health.status is DataHealthStatus.SAFE,
                "basis": "last explicit mark; unrealized P&L is not a settled wallet balance",
            }
            snapshot = {
                "mode": "paper",
                "simulation_only": True,
                "generated_at": now,
                "warning": "Paper trading only: no real exchange orders are submitted.",
                "automation": self._automation_snapshot(),
                "spot": {
                    "symbol": self.spot.symbol_rules.symbol,
                    "wallet": spot_wallet,
                    "valuation": spot_valuation,
                    "market_data": spot_health,
                    "orders": self.spot.orders(),
                    "fills": self.spot.fills(),
                    "ledger": self.spot.ledger(),
                    "reconciliation": spot_reconciliation,
                    "persistence": {
                        "kind": "in_memory",
                        "durable": False,
                        "status": "available",
                    },
                    "last_recorded_price": self._last_prices.get("spot"),
                    "last_market_event": self._last_market_events.get("spot"),
                },
                "futures": {
                    "symbol": self.futures.contract_rules.symbol,
                    "wallet": futures_wallet,
                    "valuation": futures_valuation,
                    "positions": futures_positions,
                    "market_data": futures_health,
                    "orders": self.futures.orders(),
                    "fills": self.futures.fills(),
                    "funding": self.futures.funding_payments(),
                    "liquidations": self.futures.liquidations(),
                    "ledger": self.futures.ledger(),
                    "audit_events": self.futures.audit_events(),
                    "reconciliation": futures_reconciliation,
                    "persistence": {
                        "kind": "sqlite" if self._futures_durable else "in_memory",
                        "durable": self._futures_durable,
                        "status": "available",
                    },
                    "last_recorded_price": self._last_prices.get("futures"),
                    "last_market_event": self._last_market_events.get("futures"),
                },
            }
            return _to_jsonable(snapshot)

    def record_market_data(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Validate one explicit completed candle and feed selected monitors."""

        with self._lock:
            self._ensure_open()
            data = _object(payload, "market-data request")
            domain = _choice(data.get("domain", "both"), {"spot", "futures", "both"}, "domain")
            event = _ohlcv_from_payload(data)
            checked_at = _datetime(data.get("checked_at"), "checked_at")
            results: dict[str, Any] = {}
            automation: dict[str, Any] = {}
            targets = ("spot", "futures") if domain == "both" else (domain,)
            for target in targets:
                if target == "spot":
                    health = self.spot.record_market_data(event, now=checked_at)
                else:
                    health = self.futures.record_market_data(event, now=checked_at)
                results[target] = health
                try:
                    automation[target] = self.runner.process_event(target, event, ingest=False)
                except Exception as exc:
                    raise DashboardPersistenceError("automation journal could not store the market event") from exc
                if health.status is DataHealthStatus.SAFE:
                    self._last_prices[target] = Decimal(str(event.close))
                    self._last_market_events[target] = {
                        "symbol": event.symbol,
                        "close": Decimal(str(event.close)),
                        "close_time": event.close_time,
                        "received_at": event.received_at,
                    }
            return {"domain": domain, "results": results, "automation": automation, "event": event}

    def reset_market_data(self) -> dict[str, Any]:
        """Reset both safety monitors; fresh validated data is then required."""

        with self._lock:
            self._ensure_open()
            self.spot.safety_monitor.reset()
            self.futures.safety_monitor.reset()
            return {"status": "reset", "health": self._health_snapshot()}

    def submit_spot_order(self, payload: Mapping[str, Any]) -> Any:
        with self._lock:
            self._ensure_open()
            data = _object(payload, "Spot order request")
            client_order_id = _required_text(data.get("client_order_id"), "client_order_id", max_length=128)
            side = _required_text(data.get("side"), "side")
            quantity = _required_decimal(data.get("quantity"), "quantity")
            price = _required_decimal(data.get("price"), "price")
            key = f"manual:spot:order:{client_order_id}"
            self._begin_manual_operation("spot", key, "manual_order", {"side": side, "quantity": str(quantity), "price": str(price)})
            try:
                result = self.spot.submit_order(
                    side=side,
                    quantity=quantity,
                    price=price,
                    client_order_id=client_order_id,
                    now=self._now(),
                )
            except Exception as exc:
                self._finish_manual_operation("spot", key, "failed", {"error": str(exc)}, str(exc))
                raise
            status = getattr(result, "status", "accepted")
            self._finish_manual_operation("spot", key, "rejected" if status is OrderStatus.REJECTED else "accepted", _to_jsonable(result), getattr(result, "rejection_reason", None))
            self.runner.journal.set_meta("spot_engine_snapshot", _to_jsonable(self.spot.state_snapshot()))
            return result

    def fill_spot_order(self, order_id: str, payload: Mapping[str, Any]) -> Any:
        with self._lock:
            self._ensure_open()
            data = _object(payload, "Spot fill request")
            fill_id = _required_text(data.get("fill_id"), "fill_id", max_length=128)
            quantity = _required_decimal(data.get("quantity"), "quantity")
            price = _required_decimal(data.get("price"), "price")
            key = f"manual:spot:fill:{fill_id}"
            self._begin_manual_operation("spot", key, "manual_fill", {"order_id": order_id, "quantity": str(quantity), "price": str(price)})
            try:
                result = self.spot.execute_fill(
                    order_id,
                    quantity=quantity,
                    price=price,
                    fill_id=fill_id,
                    now=self._now(),
                )
            except Exception as exc:
                self._finish_manual_operation("spot", key, "failed", {"error": str(exc)}, str(exc))
                raise
            status = getattr(result, "status", "filled")
            self._finish_manual_operation("spot", key, "rejected" if status is OrderStatus.REJECTED else "filled", _to_jsonable(result), getattr(result, "rejection_reason", None))
            self.runner.journal.set_meta("spot_engine_snapshot", _to_jsonable(self.spot.state_snapshot()))
            return result

    def cancel_spot_order(self, order_id: str) -> Any:
        with self._lock:
            self._ensure_open()
            key = f"manual:spot:cancel:{order_id}"
            self._begin_manual_operation("spot", key, "manual_cancel", {"order_id": order_id})
            try:
                result = self.spot.cancel_order(order_id, now=self._now())
            except Exception as exc:
                self._finish_manual_operation("spot", key, "failed", {"error": str(exc)}, str(exc))
                raise
            self._finish_manual_operation("spot", key, "cancelled", _to_jsonable(result))
            self.runner.journal.set_meta("spot_engine_snapshot", _to_jsonable(self.spot.state_snapshot()))
            return result

    def submit_futures_order(self, payload: Mapping[str, Any]) -> Any:
        with self._lock:
            self._ensure_open()
            data = _object(payload, "Futures order request")
            client_order_id = _required_text(data.get("client_order_id"), "client_order_id", max_length=128)
            action = _choice(data.get("action"), {"open", "reduce"}, "action")
            kwargs: dict[str, Any] = {
                "position_side": _required_text(data.get("side"), "side"),
                "quantity": _required_decimal(data.get("quantity"), "quantity"),
                "price": _required_decimal(data.get("price"), "price"),
                "client_order_id": client_order_id,
                "now": self._now(),
            }
            side = kwargs.pop("position_side")
            if action == "open":
                leverage = data.get("leverage")
                if leverage not in (None, ""):
                    kwargs["leverage"] = _required_decimal(leverage, "leverage")
                return self.futures.open_position(side=side, **kwargs)
            return self.futures.reduce_position(side=side, **kwargs)

    def fill_futures_order(self, order_id: str, payload: Mapping[str, Any]) -> Any:
        with self._lock:
            self._ensure_open()
            data = _object(payload, "Futures fill request")
            fill_id = _required_text(data.get("fill_id"), "fill_id", max_length=128)
            return self.futures.execute_fill(
                order_id,
                quantity=_required_decimal(data.get("quantity"), "quantity"),
                price=_required_decimal(data.get("price"), "price"),
                fill_id=fill_id,
                now=self._now(),
            )

    def cancel_futures_order(self, order_id: str) -> Any:
        with self._lock:
            self._ensure_open()
            return self.futures.cancel_order(order_id, now=self._now())

    def mark_futures(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            data = _object(payload, "Futures mark request")
            before = len(self.futures.liquidations())
            position = self.futures.mark_to_market(
                _required_decimal(data.get("price"), "price"),
                now=self._now(),
            )
            liquidations = self.futures.liquidations()
            liquidation = liquidations[-1] if len(liquidations) > before else None
            return {"position": position, "liquidation": liquidation}

    def apply_funding(self, payload: Mapping[str, Any]) -> Any:
        with self._lock:
            self._ensure_open()
            data = _object(payload, "funding request")
            return self.futures.apply_funding(
                _required_decimal(data.get("rate"), "rate"),
                payment_id=_required_text(data.get("payment_id"), "payment_id", max_length=128),
                now=self._now(),
            )

    def evaluate_automation(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Evaluate a fresh public window, or persisted candles in offline mode."""

        with self._lock:
            self._ensure_open()
            if self.runner.state.value == "running":
                raise DashboardRequestError("pause automation before replacing its strategy evaluation")
            data = _object(payload or {}, "automation evaluation request")
            domain = _choice(data.get("domain", "both"), {"spot", "futures", "both"}, "domain")
            domains = ("spot", "futures") if domain == "both" else (domain,)
            if self._public_providers:
                histories = self._fetch_public_histories(domains)
            else:
                histories = self._persisted_histories(domains)
            selections: dict[str, Any] = {}
            for target in domains:
                if target not in histories:
                    raise DashboardRequestError(f"no {target} market-data history is available")
                selections[target] = self.runner.evaluate(target, histories[target]).as_dict()
            return {
                "status": "evaluated",
                "data_source": "public_binance" if self._public_providers else "dashboard_market_data_api",
                "candle_counts": {target: len(histories[target]) for target in domains},
                "selections": selections,
                "runner": self._automation_snapshot(),
            }

    def start_automation(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            self._ensure_selection_for_start()
            result = self.runner.start()
            if result["state"] == "blocked":
                raise DashboardRequestError(result.get("blocked_reason") or "automation is blocked")
            self._start_background_loop()
            return self._automation_snapshot()

    def pause_automation(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            result = self.runner.pause()
            self._stop_background_loop()
            return self._automation_snapshot()

    def resume_automation(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            result = self.runner.resume()
            if result["state"] == "blocked":
                raise DashboardRequestError(result.get("blocked_reason") or "automation is blocked")
            self._start_background_loop()
            return self._automation_snapshot()

    def stop_automation(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            self.runner.pause()
            self._stop_background_loop()
            self.runner.stop()
            return self._automation_snapshot()

    def _health_snapshot(self) -> dict[str, Any]:
        now = self._now()
        return {"spot": self.spot.market_data_health(now=now), "futures": self.futures.market_data_health(now=now)}


def _object(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DashboardRequestError(f"{field_name} must be a JSON object")
    return dict(value)


def _required_text(value: Any, field_name: str, *, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DashboardRequestError(f"{field_name} is required")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise DashboardRequestError(f"{field_name} is too long")
    return normalized


def _choice(value: Any, choices: set[str], field_name: str) -> str:
    normalized = _required_text(value, field_name).lower()
    if normalized not in choices:
        raise DashboardRequestError(f"{field_name} must be one of: {', '.join(sorted(choices))}")
    return normalized


def _required_decimal(value: Any, field_name: str) -> Decimal:
    if value is None or value == "":
        raise DashboardRequestError(f"{field_name} is required")
    try:
        converted = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DashboardRequestError(f"{field_name} must be a finite decimal") from exc
    if not converted.is_finite():
        raise DashboardRequestError(f"{field_name} must be a finite decimal")
    return converted


def _datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise DashboardRequestError(f"{field_name} must be an ISO-8601 datetime")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise DashboardRequestError(f"{field_name} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DashboardRequestError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _ohlcv_from_payload(data: Mapping[str, Any]) -> OHLCV:
    symbol = _required_text(data.get("symbol"), "symbol", max_length=64)
    timeframe_value = data.get("timeframe_seconds")
    try:
        timeframe = int(timeframe_value)
    except (TypeError, ValueError) as exc:
        raise DashboardRequestError("timeframe_seconds must be a positive integer") from exc
    if isinstance(timeframe_value, bool) or timeframe < 1 or str(timeframe) != str(timeframe_value).strip():
        raise DashboardRequestError("timeframe_seconds must be a positive integer")
    values: dict[str, float] = {}
    for field_name in ("open", "high", "low", "close", "volume"):
        number = float(_required_decimal(data.get(field_name), field_name))
        if not math.isfinite(number):
            raise DashboardRequestError(f"{field_name} must be finite")
        values[field_name] = number
    return OHLCV(
        symbol=symbol,
        timestamp=_datetime(data.get("timestamp"), "timestamp"),
        close_time=_datetime(data.get("close_time"), "close_time"),
        received_at=_datetime(data.get("received_at"), "received_at"),
        timeframe_seconds=timeframe,
        **values,
    )


def _with_total(balance: Any) -> dict[str, Any]:
    return {
        "asset": balance.asset,
        "available": balance.available,
        "reserved": balance.reserved,
        "total": balance.total,
    }


def _health_to_dict(value: Any) -> Any:
    return _to_jsonable(value)


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"cannot serialize {type(value).__name__}")


def json_bytes(value: Any) -> bytes:
    return json.dumps(_to_jsonable(value), sort_keys=True, separators=(",", ":")).encode("utf-8")


class DashboardHTTPServer(ThreadingHTTPServer):
    """Threaded local server carrying one dashboard service."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], service: DashboardService):
        self.service = service
        super().__init__(server_address, DashboardRequestHandler)

    def server_close(self) -> None:
        try:
            self.service.close()
        finally:
            super().server_close()


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Small JSON API and static asset handler."""

    server: DashboardHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Keep normal operation quiet; startup and errors are handled by the
        # CLI. This also avoids echoing request bodies into local logs.
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        try:
            if path == "/":
                self._asset("index.html", "text/html; charset=utf-8")
            elif path == "/static/app.js":
                self._asset("app.js", "text/javascript; charset=utf-8")
            elif path == "/static/styles.css":
                self._asset("styles.css", "text/css; charset=utf-8")
            elif path in {"/api/state", "/api/health", "/api/automation/state"}:
                snapshot = self.server.service.snapshot()
                if path == "/api/automation/state":
                    self._json(HTTPStatus.OK, {"ok": True, "data": snapshot["automation"]})
                    return
                body = snapshot if path == "/api/state" else {
                    "mode": snapshot["mode"],
                    "simulation_only": snapshot["simulation_only"],
                    "warning": snapshot["warning"],
                    "spot": snapshot["spot"]["market_data"],
                    "futures": snapshot["futures"]["market_data"],
                }
                self._json(HTTPStatus.OK, {"ok": True, "data": body})
            else:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
        except Exception as exc:
            self._handle_exception(exc)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        try:
            payload = self._read_json()
            data: Any
            if path == "/api/market-data":
                data = self.server.service.record_market_data(payload)
            elif path == "/api/market-data/reset":
                if payload.get("confirm") is not True:
                    raise DashboardRequestError("reset requires confirm=true")
                data = self.server.service.reset_market_data()
            elif path == "/api/spot/orders":
                data = self.server.service.submit_spot_order(payload)
            elif path.startswith("/api/spot/orders/") and path.endswith("/fill"):
                data = self.server.service.fill_spot_order(_path_id(path, "/api/spot/orders/", "/fill"), payload)
            elif path.startswith("/api/spot/orders/") and path.endswith("/cancel"):
                data = self.server.service.cancel_spot_order(_path_id(path, "/api/spot/orders/", "/cancel"))
            elif path == "/api/futures/orders":
                data = self.server.service.submit_futures_order(payload)
            elif path.startswith("/api/futures/orders/") and path.endswith("/fill"):
                data = self.server.service.fill_futures_order(_path_id(path, "/api/futures/orders/", "/fill"), payload)
            elif path.startswith("/api/futures/orders/") and path.endswith("/cancel"):
                data = self.server.service.cancel_futures_order(_path_id(path, "/api/futures/orders/", "/cancel"))
            elif path == "/api/futures/mark":
                data = self.server.service.mark_futures(payload)
            elif path == "/api/futures/funding":
                data = self.server.service.apply_funding(payload)
            elif path == "/api/automation/evaluate":
                data = self.server.service.evaluate_automation(payload)
            elif path == "/api/automation/start":
                if payload.get("confirm") is not True:
                    raise DashboardRequestError("automation start requires confirm=true")
                data = self.server.service.start_automation()
            elif path == "/api/automation/pause":
                data = self.server.service.pause_automation()
            elif path == "/api/automation/resume":
                if payload.get("confirm") is not True:
                    raise DashboardRequestError("automation resume requires confirm=true")
                data = self.server.service.resume_automation()
            elif path == "/api/automation/stop":
                if payload.get("confirm") is not True:
                    raise DashboardRequestError("automation stop requires confirm=true")
                data = self.server.service.stop_automation()
            else:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
                return
            rejected = getattr(data, "status", None) in {OrderStatus.REJECTED, FuturesOrderStatus.REJECTED}
            if rejected:
                self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {
                    "ok": False,
                    "error": {"code": "order_rejected", "message": getattr(data, "rejection_reason", "order was rejected")},
                    "data": data,
                })
            else:
                self._json(HTTPStatus.OK, {"ok": True, "data": data})
        except Exception as exc:
            self._handle_exception(exc)

    def _read_json(self) -> dict[str, Any]:
        length_text = self.headers.get("Content-Length")
        try:
            length = int(length_text or "0")
        except ValueError as exc:
            raise DashboardRequestError("Content-Length must be an integer") from exc
        if length <= 0 or length > _MAX_REQUEST_BYTES:
            raise DashboardRequestError("request body must be a non-empty JSON object under 64 KiB")
        try:
            raw = self.rfile.read(length)
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DashboardRequestError("request body must be valid UTF-8 JSON") from exc
        return _object(parsed, "request body")

    def _asset(self, name: str, content_type: str) -> None:
        try:
            resource = importlib.resources.files("trad").joinpath("web", name)
            content = resource.read_bytes()
        except (FileNotFoundError, ModuleNotFoundError) as exc:
            self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "asset_unavailable", "dashboard asset unavailable")
            return
        self._send(HTTPStatus.OK, content, content_type)

    def _json(self, status: HTTPStatus, value: Any) -> None:
        self._send(status, json_bytes(value), "application/json; charset=utf-8")

    def _json_error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._json(status, {"ok": False, "error": {"code": code, "message": message}})

    def _send(self, status: HTTPStatus, content: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def _handle_exception(self, exc: Exception) -> None:
        if isinstance(exc, DashboardMarketDataError):
            self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, "market_data_unavailable", str(exc))
        elif isinstance(exc, DashboardRequestError):
            self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
        elif isinstance(exc, (DashboardPersistenceError, FuturesPersistenceError, FuturesReconciliationError)):
            self._json_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "persistence_unavailable",
                "durable paper state is unavailable",
            )
        elif isinstance(exc, (DataSafetyError, FuturesError, SpotPaperError, ValueError, KeyError)):
            # Engine validation/risk errors are safe to show as actionable
            # messages; no traceback or filesystem detail crosses the API.
            self._json_error(HTTPStatus.UNPROCESSABLE_ENTITY, "operation_rejected", str(exc))
        else:
            self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "server_error", "the dashboard could not complete the request")


def _path_id(path: str, prefix: str, suffix: str) -> str:
    value = path[len(prefix) : -len(suffix)]
    if not value or "/" in value:
        raise DashboardRequestError("order id is invalid")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trad-dashboard",
        description="Run the local simulation-only trad paper-trading dashboard.",
    )
    parser.add_argument("--host", default=DEFAULT_DASHBOARD_HOST, help="bind host (default: localhost)")
    parser.add_argument("--port", type=int, default=DEFAULT_DASHBOARD_PORT, help="bind port (default: 8765)")
    parser.add_argument("--symbol", default="BTC/USDT", help="public Spot/Futures symbol (default: BTC/USDT)")
    parser.add_argument("--interval", default="1m", help="public fixed-duration interval (default: 1m)")
    parser.add_argument("--history-limit", type=int, default=250, help="completed candles fetched for evaluation and cycles")
    parser.add_argument(
        "--futures-db",
        type=Path,
        default=DEFAULT_FUTURES_DATABASE,
        help="local Futures SQLite file (default: var/futures-dashboard.sqlite3)",
    )
    parser.add_argument(
        "--runner-db",
        type=Path,
        default=Path("var/trad-runner.sqlite3"),
        help="persistent strategy/run journal (default: var/trad-runner.sqlite3)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("port must be between 1 and 65535")
    try:
        if args.history_limit < 40:
            raise SystemExit("history-limit must be at least 40")
        public_providers = build_public_providers(args.symbol, args.interval, args.history_limit)
        service = DashboardService(
            futures_database_path=args.futures_db,
            runner_database_path=args.runner_db,
            runner_config=RunnerConfig(
                symbol=args.symbol,
                interval=args.interval,
                history_limit=args.history_limit,
            ),
            public_providers=public_providers,
        )
        server = DashboardHTTPServer((args.host, args.port), service)
    except Exception as exc:
        raise SystemExit(f"dashboard startup failed: {exc}") from exc
    print(f"trad dashboard listening on http://{args.host}:{args.port}")
    print("paper trading only; no real exchange orders are submitted")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
