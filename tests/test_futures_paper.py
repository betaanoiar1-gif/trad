from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

import trad
from trad.config import Instrument, load_config
from trad.futures_paper import (
    FuturesContractRules,
    FuturesDuplicateError,
    FuturesExecutionError,
    FuturesFeeConfig,
    FuturesMarginConfig,
    FuturesOrderAction,
    FuturesOrderStateError,
    FuturesOrderStatus,
    FuturesPaperEngine,
    FuturesPersistenceError,
    FuturesPositionSide,
    FuturesRiskCode,
    FuturesRiskLimits,
    FuturesRiskError,
    FuturesValidationError,
)
from trad.market_data import DataHealthStatus, OHLCV


D = Decimal
NOW = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)


def candle(at: datetime = NOW) -> OHLCV:
    return OHLCV(
        symbol="BTC/USDT",
        timestamp=at - timedelta(minutes=1),
        close_time=at,
        received_at=at,
        timeframe_seconds=60,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=1.0,
    )


class FuturesPaperEngineTests(unittest.TestCase):
    def make_engine(self, **kwargs: object) -> FuturesPaperEngine:
        engine = FuturesPaperEngine(clock=lambda: NOW, **kwargs)
        self.addCleanup(engine.close)
        return engine

    def make_safe(self, engine: FuturesPaperEngine) -> None:
        health = engine.record_market_data(candle(), now=NOW)
        self.assertEqual(health.status, DataHealthStatus.SAFE)

    def test_package_exports_futures_interfaces(self) -> None:
        self.assertIs(trad.FuturesPaperEngine, FuturesPaperEngine)
        self.assertIs(trad.FuturesPositionSide, FuturesPositionSide)
        self.assertIs(trad.FuturesMarginConfig, FuturesMarginConfig)

    def test_wallet_is_independent_and_defaults_to_one_thousand_usdt(self) -> None:
        engine = self.make_engine()
        self.assertEqual(engine.balance().asset, "USDT")
        self.assertEqual(engine.balance().available, D("1000.00"))
        self.assertEqual(engine.balance().reserved, D("0"))
        self.assertEqual(engine.positions(), ())

    def test_safety_gate_is_fail_closed_until_valid_data(self) -> None:
        engine = self.make_engine()
        rejected = engine.open_position(
            FuturesPositionSide.LONG, quantity="1", price="100", now=NOW
        )
        self.assertEqual(rejected.status, FuturesOrderStatus.REJECTED)
        self.assertEqual(rejected.rejection_code, FuturesRiskCode.MARKET_DATA_UNSAFE)
        self.make_safe(engine)
        accepted = engine.open_position(
            FuturesPositionSide.LONG, quantity="1", price="100", now=NOW
        )
        self.assertEqual(accepted.status, FuturesOrderStatus.ACCEPTED)

    def test_long_lifecycle_supports_partial_fill_increase_reduce_and_close(self) -> None:
        engine = self.make_engine(
            risk_limits=FuturesRiskLimits(default_leverage="2", max_leverage="5")
        )
        self.make_safe(engine)

        opening = engine.open_position(
            FuturesPositionSide.LONG,
            quantity="1.00000000",
            price="100.00",
            leverage="2",
            client_order_id="open-1",
            now=NOW,
        )
        self.assertEqual(opening.reserved_collateral, D("50.10"))
        first = engine.execute_fill(
            opening.order_id, quantity="0.5", price="100.00", fill_id="fill-1", now=NOW
        )
        self.assertEqual(first.fee, D("0.05"))
        self.assertEqual(engine.position().quantity, D("0.5"))
        self.assertEqual(engine.order(opening.order_id).status, FuturesOrderStatus.PARTIALLY_FILLED)
        second = engine.execute_fill(
            opening.order_id, quantity="0.5", price="100.00", fill_id="fill-2", now=NOW
        )
        self.assertEqual(second.realized_pnl, D("0"))
        self.assertEqual(engine.position().margin, D("50.00"))

        increase = engine.open_position(
            FuturesPositionSide.LONG, quantity="0.5", price="100.00", now=NOW
        )
        self.assertEqual(increase.leverage, D("2"))
        engine.execute_fill(increase.order_id, quantity="0.5", price="100.00", now=NOW)
        self.assertEqual(engine.position().quantity, D("1.5"))
        self.assertEqual(engine.position().entry_price, D("100.00"))

        marked = engine.mark_to_market("110.00", now=NOW + timedelta(seconds=1))
        self.assertEqual(marked.unrealized_pnl(engine.contract_rules), D("15.000"))
        reduction = engine.reduce_position(
            FuturesPositionSide.LONG, quantity="0.5", price="110.00", now=NOW + timedelta(seconds=1)
        )
        reduced = engine.execute_fill(reduction.order_id, quantity="0.5", price="110.00", now=NOW + timedelta(seconds=1))
        self.assertEqual(reduced.realized_pnl, D("5.00"))
        self.assertEqual(engine.position().quantity, D("1.0"))
        self.assertEqual(engine.position().realized_pnl, D("5.00"))

        closing = engine.reduce_position(
            FuturesPositionSide.LONG, quantity="1", price="110.00", now=NOW + timedelta(seconds=1)
        )
        final = engine.execute_fill(closing.order_id, quantity="1", price="110.00", now=NOW + timedelta(seconds=1))
        self.assertEqual(final.realized_pnl, D("10.00"))
        self.assertIsNone(engine.position())
        self.assertEqual(engine.balance().reserved, D("0"))
        self.assertTrue(engine.reconcile().is_consistent)
        # Fifteen units of gross P&L less the four explicit trading fees.
        self.assertEqual(engine.balance().total, D("1014.68"))

    def test_configured_exposure_and_order_limits_are_enforced(self) -> None:
        engine = self.make_engine(
            risk_limits=FuturesRiskLimits(
                default_leverage="1",
                max_leverage="1",
                max_order_notional="50",
                max_position_notional="200",
                max_open_orders=1,
            )
        )
        self.make_safe(engine)
        too_large = engine.open_position(
            FuturesPositionSide.LONG, quantity="1", price="100", now=NOW
        )
        self.assertEqual(too_large.status, FuturesOrderStatus.REJECTED)
        self.assertEqual(too_large.rejection_code, FuturesRiskCode.EXPOSURE_LIMIT)
        engine = self.make_engine(
            risk_limits=FuturesRiskLimits(
                default_leverage="1",
                max_leverage="1",
                max_position_notional="200",
                max_open_orders=1,
            )
        )
        self.make_safe(engine)
        accepted = engine.open_position(
            FuturesPositionSide.LONG, quantity="1", price="100", now=NOW
        )
        self.assertEqual(accepted.status, FuturesOrderStatus.ACCEPTED)
        blocked = engine.open_position(
            FuturesPositionSide.LONG, quantity="0.5", price="100", now=NOW
        )
        self.assertEqual(blocked.rejection_code, FuturesRiskCode.OPEN_ORDER_LIMIT)

    def test_short_pnl_has_the_opposite_sign(self) -> None:
        engine = self.make_engine(
            risk_limits=FuturesRiskLimits(default_leverage="2", max_leverage="2")
        )
        self.make_safe(engine)
        order = engine.open_position(
            FuturesPositionSide.SHORT, quantity="1", price="100", leverage="2", now=NOW
        )
        engine.execute_fill(order.order_id, quantity="1", price="100", now=NOW)
        position = engine.mark_to_market("90", now=NOW + timedelta(seconds=1))
        self.assertEqual(position.unrealized_pnl(engine.contract_rules), D("10"))
        close = engine.reduce_position(
            FuturesPositionSide.SHORT, quantity="1", price="90", now=NOW + timedelta(seconds=1)
        )
        fill = engine.execute_fill(close.order_id, quantity="1", price="90", now=NOW + timedelta(seconds=1))
        self.assertEqual(fill.realized_pnl, D("10.00"))
        self.assertIsNone(engine.position())
        self.assertTrue(engine.reconcile().is_consistent)

    def test_duplicate_orders_fills_and_funding_are_idempotent(self) -> None:
        engine = self.make_engine()
        self.make_safe(engine)
        order = engine.open_position(
            FuturesPositionSide.LONG,
            quantity="1",
            price="100",
            client_order_id="same-order",
            now=NOW,
        )
        same = engine.open_position(
            FuturesPositionSide.LONG,
            quantity="1.0",
            price="100.00",
            client_order_id="same-order",
            now=NOW,
        )
        self.assertEqual(same, order)
        with self.assertRaises(FuturesDuplicateError):
            engine.open_position(
                FuturesPositionSide.LONG,
                quantity="2",
                price="100",
                client_order_id="same-order",
                now=NOW,
            )
        fill = engine.execute_fill(order.order_id, quantity="1", price="100", fill_id="same-fill", now=NOW)
        self.assertEqual(engine.execute_fill(order.order_id, quantity="1", price="100", fill_id="same-fill", now=NOW), fill)
        engine.mark_to_market("100", now=NOW + timedelta(seconds=1))
        event_count = len(engine.audit_events())
        engine.mark_to_market("100.00", now=NOW + timedelta(seconds=1))
        self.assertEqual(len(engine.audit_events()), event_count)
        payment = engine.apply_funding("0.01", payment_id="fund-1", now=NOW)
        self.assertEqual(engine.apply_funding("0.010", payment_id="fund-1", now=NOW), payment)
        with self.assertRaises(FuturesDuplicateError):
            engine.apply_funding("0.02", payment_id="fund-1", now=NOW)
        self.assertEqual(len(engine.fills()), 1)
        self.assertEqual(len(engine.funding_payments()), 1)

    def test_cancel_releases_reservation_and_terminal_state_is_enforced(self) -> None:
        engine = self.make_engine()
        self.make_safe(engine)
        order = engine.open_position(
            FuturesPositionSide.LONG, quantity="1", price="100", now=NOW
        )
        before = engine.balance()
        cancelled = engine.cancel_order(order.order_id, now=NOW)
        self.assertEqual(cancelled.status, FuturesOrderStatus.CANCELLED)
        self.assertEqual(engine.balance().available, D("1000.00"))
        self.assertEqual(engine.balance().reserved, D("0"))
        self.assertEqual(before.total, engine.balance().total)
        with self.assertRaises(FuturesOrderStateError):
            engine.cancel_order(order.order_id, now=NOW)
        self.assertTrue(engine.reconcile().is_consistent)

    def test_explicit_price_and_precision_errors_do_not_mutate_state(self) -> None:
        engine = self.make_engine()
        self.make_safe(engine)
        order = engine.open_position(
            FuturesPositionSide.LONG, quantity="1", price="100", now=NOW
        )
        balance = engine.balance()
        with self.assertRaises(FuturesExecutionError):
            engine.execute_fill(order.order_id, quantity="1", price="101", now=NOW)
        self.assertEqual(engine.balance(), balance)
        with self.assertRaises(FuturesValidationError):
            engine.open_position(
                FuturesPositionSide.LONG, quantity="1.000000001", price="100", now=NOW
            )
        self.assertEqual(engine.balance(), balance)

    def test_funding_credits_shorts_and_charges_longs(self) -> None:
        long_engine = self.make_engine()
        self.make_safe(long_engine)
        order = long_engine.open_position(FuturesPositionSide.LONG, quantity="1", price="100", now=NOW)
        long_engine.execute_fill(order.order_id, quantity="1", price="100", now=NOW)
        before = long_engine.balance().available
        payment = long_engine.apply_funding("0.01", payment_id="long-funding", now=NOW)
        self.assertEqual(payment.amount, D("-1.00"))
        self.assertEqual(long_engine.balance().available, before - D("1.00"))

        short_engine = self.make_engine()
        self.make_safe(short_engine)
        order = short_engine.open_position(FuturesPositionSide.SHORT, quantity="1", price="100", now=NOW)
        short_engine.execute_fill(order.order_id, quantity="1", price="100", now=NOW)
        before = short_engine.balance().available
        payment = short_engine.apply_funding("0.01", payment_id="short-funding", now=NOW)
        self.assertEqual(payment.amount, D("1.00"))
        self.assertEqual(short_engine.balance().available, before + D("1.00"))
        short_engine.close()

    def test_liquidation_is_explicit_conservative_and_cancels_open_orders(self) -> None:
        engine = self.make_engine(
            risk_limits=FuturesRiskLimits(default_leverage="10", max_leverage="10"),
            margin_config=FuturesMarginConfig(
                maintenance_margin_rate="0.05", liquidation_fee_rate="0.01"
            ),
        )
        self.make_safe(engine)
        opening = engine.open_position(
            FuturesPositionSide.LONG, quantity="1", price="100", leverage="10", now=NOW
        )
        engine.execute_fill(opening.order_id, quantity="1", price="100", now=NOW)
        pending = engine.open_position(
            FuturesPositionSide.LONG, quantity="0.1", price="100", now=NOW
        )
        result = engine.mark_to_market("1.00", now=NOW + timedelta(seconds=1))
        self.assertIsNone(result)
        self.assertIsNone(engine.position())
        self.assertEqual(engine.order(pending.order_id).status, FuturesOrderStatus.CANCELLED)
        self.assertEqual(engine.balance().reserved, D("0"))
        liquidation = engine.liquidations()[0]
        self.assertEqual(liquidation.shortfall, D("89.01"))
        self.assertTrue(engine.reconcile().is_consistent)

    def test_from_run_config_uses_futures_wallet_and_not_spot(self) -> None:
        config = load_config("config/paper-perpetual-futures.example.toml")
        self.assertEqual(config.instrument, Instrument.PERPETUAL_FUTURES)
        engine = FuturesPaperEngine.from_run_config(config, clock=lambda: NOW)
        self.addCleanup(engine.close)
        self.assertEqual(engine.balance().asset, "USDT")
        self.assertEqual(engine.balance().available, D("1000.00"))
        with self.assertRaises(FuturesValidationError):
            FuturesPaperEngine.from_run_config(
                load_config("config/paper-spot.example.toml"), clock=lambda: NOW
            )


class FuturesPersistenceTests(unittest.TestCase):
    def test_restart_recovers_state_but_requires_fresh_market_data(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "futures.sqlite3"
            first = FuturesPaperEngine(database_path=path, clock=lambda: NOW)
            first.record_market_data(candle(), now=NOW)
            order = first.open_position(
                FuturesPositionSide.LONG,
                quantity="1",
                price="100",
                client_order_id="durable-order",
                now=NOW,
            )
            first.execute_fill(order.order_id, quantity="1", price="100", fill_id="durable-fill", now=NOW)
            expected = (first.balance(), first.position(), first.orders(), first.fills(), first.reconcile())
            first.close()

            recovered = FuturesPaperEngine(database_path=path, clock=lambda: NOW)
            self.addCleanup(recovered.close)
            self.assertEqual(recovered.balance(), expected[0])
            self.assertEqual(recovered.position(), expected[1])
            self.assertEqual(recovered.orders(), expected[2])
            self.assertEqual(recovered.fills(), expected[3])
            self.assertEqual(recovered.reconcile(), expected[4])
            self.assertEqual(recovered.market_data_health(now=NOW).status, DataHealthStatus.NO_DATA)
            self.assertFalse(recovered.market_data_health(now=NOW).allow_new_positions)

    def test_failed_transaction_rolls_back_durable_and_memory_state(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "rollback.sqlite3"
            failed = {"value": False}

            def fail(operation: str) -> None:
                if operation == "execute_fill" and not failed["value"]:
                    failed["value"] = True
                    raise RuntimeError("injected failure")

            engine = FuturesPaperEngine(database_path=path, clock=lambda: NOW, failure_hook=fail)
            engine.record_market_data(candle(), now=NOW)
            order = engine.open_position(FuturesPositionSide.LONG, quantity="1", price="100", now=NOW)
            before = (engine.balance(), engine.order(order.order_id), engine.position(), len(engine.ledger()))
            with self.assertRaises(RuntimeError):
                engine.execute_fill(order.order_id, quantity="1", price="100", now=NOW)
            self.assertEqual((engine.balance(), engine.order(order.order_id), engine.position(), len(engine.ledger())), before)
            engine.close()
            recovered = FuturesPaperEngine(database_path=path, clock=lambda: NOW)
            self.addCleanup(recovered.close)
            self.assertEqual(recovered.balance(), before[0])
            self.assertEqual(recovered.order(order.order_id), before[1])
            self.assertIsNone(recovered.position())
            self.assertEqual(len(recovered.ledger()), before[3])

    def test_schema_version_zero_is_migrated_before_initialization(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "version-zero.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE schema_meta (id INTEGER PRIMARY KEY CHECK(id = 1), version INTEGER NOT NULL)"
            )
            connection.execute("INSERT INTO schema_meta(id, version) VALUES(1, 0)")
            connection.commit()
            connection.close()
            engine = FuturesPaperEngine(database_path=path, clock=lambda: NOW)
            self.addCleanup(engine.close)
            self.assertEqual(engine.balance().available, D("1000.00"))
            connection = sqlite3.connect(path)
            self.assertEqual(connection.execute("SELECT version FROM schema_meta WHERE id=1").fetchone()[0], 1)
            connection.close()

    def test_schema_and_payload_corruption_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "corrupt.sqlite3"
            engine = FuturesPaperEngine(database_path=path, clock=lambda: NOW)
            engine.close()
            connection = sqlite3.connect(path)
            connection.execute("UPDATE schema_meta SET version=999 WHERE id=1")
            connection.commit()
            connection.close()
            with self.assertRaises(FuturesPersistenceError):
                FuturesPaperEngine(database_path=path, clock=lambda: NOW)

            valid = Path(directory) / "payload.sqlite3"
            engine = FuturesPaperEngine(database_path=valid, clock=lambda: NOW)
            engine.close()
            connection = sqlite3.connect(valid)
            connection.execute("UPDATE events SET payload='not-json'")
            connection.commit()
            connection.close()
            with self.assertRaises(FuturesPersistenceError):
                FuturesPaperEngine(database_path=valid, clock=lambda: NOW)

    def test_incomplete_store_is_not_overwritten(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "partial.sqlite3"
            engine = FuturesPaperEngine(database_path=path, clock=lambda: NOW)
            engine.close()
            connection = sqlite3.connect(path)
            connection.execute("INSERT INTO orders(order_id, payload) VALUES('orphan', '{}')")
            connection.commit()
            connection.close()
            with self.assertRaises(FuturesPersistenceError):
                FuturesPaperEngine(database_path=path, clock=lambda: NOW)


if __name__ == "__main__":
    unittest.main()
