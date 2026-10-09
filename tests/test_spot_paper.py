from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from trad.config import Instrument, RunConfig, load_config  # noqa: E402
from trad.market_data import DataHealthStatus, OHLCV  # noqa: E402
from trad.spot_paper import (  # noqa: E402
    DuplicateFillError,
    DuplicateOrderError,
    ExecutionError,
    FeeConfig,
    FeeCurrency,
    FeeRounding,
    LedgerEntryType,
    OrderSide,
    OrderStateError,
    OrderStatus,
    OrderValidationError,
    RiskRejectionCode,
    SpotPaperEngine,
    SpotRiskLimits,
    SpotSymbolRules,
    ValuationError,
)


D = Decimal
UTC = timezone.utc
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def completed_candle(
    *,
    close_time: datetime = BASE,
    receive_delay_seconds: int = 0,
    close: str = "100.00",
) -> OHLCV:
    open_time = close_time - timedelta(minutes=1)
    received_at = close_time + timedelta(seconds=receive_delay_seconds)
    return OHLCV(
        symbol="BTC/USDT",
        timestamp=open_time,
        close_time=close_time,
        received_at=received_at,
        timeframe_seconds=60,
        open=float("99.00"),
        high=float("101.00"),
        low=float("98.00"),
        close=float(close),
        volume=float("2"),
    )


def safe_engine(**kwargs: object) -> SpotPaperEngine:
    engine = SpotPaperEngine(clock=lambda: BASE, **kwargs)
    health = engine.record_market_data(completed_candle(), now=BASE)
    assert health.status is DataHealthStatus.SAFE
    return engine


class SpotPaperAccountingTests(unittest.TestCase):
    def test_new_wallet_defaults_to_one_thousand_usdt(self) -> None:
        engine = SpotPaperEngine(clock=lambda: BASE)

        self.assertEqual(engine.balance("USDT").available, D("1000.00"))
        self.assertEqual(engine.balance("USDT").reserved, D("0.00"))
        self.assertEqual(engine.balance("USDT").total, D("1000.00"))
        self.assertEqual(engine.balance("BTC").total, D("0E-8"))
        self.assertTrue(engine.reconcile().is_consistent)
        self.assertEqual(
            [entry.entry_type for entry in engine.ledger()],
            [LedgerEntryType.INITIALIZATION, LedgerEntryType.INITIALIZATION],
        )

    def test_successful_buy_reserves_then_debits_quote_and_credits_base(self) -> None:
        engine = safe_engine()

        order = engine.submit_order(
            side=OrderSide.BUY,
            quantity="1.00000000",
            price="100.00",
            client_order_id="buy-1",
            now=BASE,
        )
        self.assertEqual(order.status, OrderStatus.ACCEPTED)
        self.assertEqual(engine.balance("USDT").available, D("899.90"))
        self.assertEqual(engine.balance("USDT").reserved, D("100.10"))
        self.assertEqual(engine.balance("BTC").total, D("0E-8"))

        fill = engine.execute_fill(
            order.order_id,
            quantity="1.00000000",
            price="100.00",
            fill_id="fill-buy-1",
            now=BASE,
        )

        self.assertEqual(fill.gross_quote, D("100.00"))
        self.assertEqual(fill.fee_amount, D("0.10"))
        self.assertEqual(fill.fee_currency, FeeCurrency.QUOTE)
        self.assertEqual(engine.order(order.order_id).status, OrderStatus.FILLED)
        self.assertEqual(engine.balance("USDT").available, D("899.90"))
        self.assertEqual(engine.balance("USDT").reserved, D("0.00"))
        self.assertEqual(engine.balance("BTC").available, D("1.00000000"))
        self.assertEqual(engine.balance("BTC").total, D("1.00000000"))
        self.assertEqual(engine.acquired_assets()[0].asset, "BTC")
        self.assertTrue(engine.reconcile().is_consistent)

    def test_successful_sell_debits_base_and_credits_quote_after_fee(self) -> None:
        engine = safe_engine(
            starting_quote_balance="0.00",
            starting_base_balance="2.00000000",
        )
        order = engine.submit_order(
            side="sell",
            quantity="1.00000000",
            price="100.00",
            now=BASE,
        )
        self.assertEqual(engine.balance("BTC").available, D("1.00000000"))
        self.assertEqual(engine.balance("BTC").reserved, D("1.00000000"))

        fill = engine.execute_fill(
            order.order_id,
            quantity="1.00000000",
            price="100.00",
            fill_id="sell-fill-1",
            now=BASE,
        )

        self.assertEqual(fill.quote_delta, D("99.90"))
        self.assertEqual(engine.balance("BTC").total, D("1.00000000"))
        self.assertEqual(engine.balance("USDT").available, D("99.90"))
        self.assertEqual(engine.balance("USDT").total, D("99.90"))
        self.assertTrue(engine.reconcile().is_consistent)

    def test_partial_fills_keep_only_remaining_reservation(self) -> None:
        engine = safe_engine()
        order = engine.submit_order(
            side="buy",
            quantity="2.00000000",
            price="100.00",
            now=BASE,
        )
        self.assertEqual(engine.balance("USDT").reserved, D("200.20"))

        first = engine.execute_fill(
            order.order_id,
            quantity="0.50000000",
            price="100.00",
            fill_id="partial-1",
            now=BASE,
        )
        self.assertEqual(first.fee_amount, D("0.05"))
        self.assertEqual(engine.order(order.order_id).status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(engine.balance("USDT").available, D("799.80"))
        self.assertEqual(engine.balance("USDT").reserved, D("150.15"))
        self.assertEqual(engine.balance("BTC").available, D("0.50000000"))

        engine.execute_fill(
            order.order_id,
            quantity="1.50000000",
            price="100.00",
            fill_id="partial-2",
            now=BASE,
        )
        final = engine.order(order.order_id)
        self.assertEqual(final.status, OrderStatus.FILLED)
        self.assertEqual(final.filled_quantity, D("2.00000000"))
        self.assertEqual(engine.balance("USDT").total, D("799.80"))
        self.assertEqual(engine.balance("BTC").total, D("2.00000000"))
        self.assertEqual(engine.balance("USDT").reserved, D("0.00"))
        self.assertTrue(engine.reconcile().is_consistent)

    def test_base_currency_fee_is_applied_to_asset_quantity(self) -> None:
        engine = safe_engine(
            fee_config=FeeConfig(
                rate=D("0.001"),
                currency=FeeCurrency.BASE,
                rounding=FeeRounding.DOWN,
            )
        )
        order = engine.submit_order(
            side="buy",
            quantity="1.00000000",
            price="100.00",
            now=BASE,
        )
        fill = engine.execute_fill(
            order.order_id,
            quantity="1.00000000",
            price="100.00",
            fill_id="base-fee-buy",
            now=BASE,
        )
        self.assertEqual(fill.fee_amount, D("0.00100000"))
        self.assertEqual(engine.balance("BTC").available, D("0.99900000"))
        self.assertEqual(engine.balance("USDT").total, D("900.00"))

        sell_engine = safe_engine(
            starting_quote_balance="0.00",
            starting_base_balance="1.00100000",
            fee_config=FeeConfig(currency=FeeCurrency.BASE),
        )
        sell = sell_engine.submit_order(
            side="sell",
            quantity="1.00000000",
            price="100.00",
            now=BASE,
        )
        sell_engine.execute_fill(
            sell.order_id,
            quantity="1.00000000",
            price="100.00",
            fill_id="base-fee-sell",
            now=BASE,
        )
        self.assertEqual(sell_engine.balance("BTC").total, D("0E-8"))
        self.assertEqual(sell_engine.balance("USDT").total, D("100.00"))

    def test_cancel_releases_reservation_and_terminal_states_are_enforced(self) -> None:
        engine = safe_engine()
        order = engine.submit_order(
            side="buy",
            quantity="1.00000000",
            price="100.00",
            now=BASE,
        )
        cancelled = engine.cancel_order(order.order_id, now=BASE)
        self.assertEqual(cancelled.status, OrderStatus.CANCELLED)
        self.assertEqual(engine.balance("USDT").available, D("1000.00"))
        self.assertEqual(engine.balance("USDT").reserved, D("0.00"))
        with self.assertRaises(OrderStateError):
            engine.cancel_order(order.order_id, now=BASE)

        filled_order = engine.submit_order(
            side="buy",
            quantity="1.00000000",
            price="100.00",
            now=BASE,
        )
        engine.execute_fill(
            filled_order.order_id,
            quantity="1.00000000",
            price="100.00",
            now=BASE,
        )
        with self.assertRaises(OrderStateError):
            engine.cancel_order(filled_order.order_id, now=BASE)

    def test_duplicate_orders_and_fills_are_idempotent(self) -> None:
        engine = safe_engine()
        order = engine.submit_order(
            side="buy",
            quantity="1.00000000",
            price="100.00",
            client_order_id="same-order",
            now=BASE,
        )
        ledger_count = len(engine.ledger())
        same = engine.submit_order(
            side="buy",
            quantity="1.00000000",
            price="100.00",
            client_order_id="same-order",
            now=BASE,
        )
        self.assertEqual(same, order)
        self.assertEqual(len(engine.ledger()), ledger_count)
        with self.assertRaises(DuplicateOrderError):
            engine.submit_order(
                side="buy",
                quantity="2.00000000",
                price="100.00",
                client_order_id="same-order",
                now=BASE,
            )

        fill = engine.execute_fill(
            order.order_id,
            quantity="1.00000000",
            price="100.00",
            fill_id="same-fill",
            now=BASE,
        )
        ledger_count = len(engine.ledger())
        same_fill = engine.execute_fill(
            order.order_id,
            quantity="1.00000000",
            price="100.00",
            fill_id="same-fill",
            now=BASE,
        )
        self.assertEqual(same_fill, fill)
        self.assertEqual(len(engine.ledger()), ledger_count)
        self.assertEqual(len(engine.fills()), 1)
        with self.assertRaises(DuplicateFillError):
            engine.execute_fill(
                order.order_id,
                quantity="0.50000000",
                price="100.00",
                fill_id="same-fill",
                now=BASE,
            )

    def test_invalid_precision_and_order_values_are_rejected(self) -> None:
        engine = safe_engine()
        invalid_requests = (
            {"side": "buy", "quantity": "0", "price": "100.00"},
            {"side": "buy", "quantity": "-1.00000000", "price": "100.00"},
            {"side": "buy", "quantity": "1.000000001", "price": "100.00"},
            {"side": "buy", "quantity": "1.00000000", "price": "100.001"},
            {"side": "buy", "quantity": "not-a-number", "price": "100.00"},
            {"side": "hold", "quantity": "1.00000000", "price": "100.00"},
        )
        for request in invalid_requests:
            with self.subTest(request=request):
                with self.assertRaises(OrderValidationError):
                    engine.submit_order(**request, now=BASE)
        self.assertEqual(engine.balance("USDT").total, D("1000.00"))
        self.assertTrue(engine.reconcile().is_consistent)
        with self.assertRaises(OrderValidationError):
            engine.submit_order(
                symbol="",
                side="buy",
                quantity="1.00000000",
                price="100.00",
                now=BASE,
            )
        with self.assertRaises(OrderValidationError):
            engine.submit_order(
                symbol="ETH/USDT",
                side="buy",
                quantity="1.00000000",
                price="100.00",
                now=BASE,
            )

        with self.assertRaises(OrderValidationError):
            SpotSymbolRules.for_symbol("BTC/USDT", min_notional="0.001")

    def test_insufficient_quote_and_base_orders_are_rejected_without_mutation(self) -> None:
        buy_engine = safe_engine()
        rejected_buy = buy_engine.submit_order(
            side="buy",
            quantity="20.00000000",
            price="100.00",
            now=BASE,
        )
        self.assertEqual(rejected_buy.status, OrderStatus.REJECTED)
        self.assertEqual(rejected_buy.rejection_code, RiskRejectionCode.INSUFFICIENT_FUNDS)
        self.assertEqual(buy_engine.balance("USDT").available, D("1000.00"))
        self.assertEqual(buy_engine.balance("USDT").reserved, D("0.00"))

        sell_engine = safe_engine(starting_base_balance="0.50000000")
        rejected_sell = sell_engine.submit_order(
            side="sell",
            quantity="1.00000000",
            price="100.00",
            now=BASE,
        )
        self.assertEqual(rejected_sell.status, OrderStatus.REJECTED)
        self.assertEqual(rejected_sell.rejection_code, RiskRejectionCode.INSUFFICIENT_FUNDS)
        self.assertEqual(sell_engine.balance("BTC").total, D("0.50000000"))
        self.assertTrue(sell_engine.reconcile().is_consistent)

    def test_fee_rounding_is_configurable_and_deterministic(self) -> None:
        engine = safe_engine(
            fee_config=FeeConfig(
                rate=D("0.005"),
                currency=FeeCurrency.QUOTE,
                rounding=FeeRounding.HALF_UP,
            )
        )
        order = engine.submit_order(
            side="buy",
            quantity="1.00000000",
            price="1.00",
            now=BASE,
        )
        fill = engine.execute_fill(
            order.order_id,
            quantity="1.00000000",
            price="1.00",
            now=BASE,
        )
        self.assertEqual(fill.fee_amount, D("0.01"))
        self.assertEqual(engine.balance("USDT").total, D("998.99"))


class SpotPaperRiskAndSafetyTests(unittest.TestCase):
    def test_market_data_safety_is_required_and_fail_closed(self) -> None:
        engine = SpotPaperEngine(clock=lambda: BASE)
        rejected = engine.submit_order(
            side="buy",
            quantity="1.00000000",
            price="100.00",
            now=BASE,
        )
        self.assertEqual(rejected.status, OrderStatus.REJECTED)
        self.assertEqual(rejected.rejection_code, RiskRejectionCode.MARKET_DATA_UNSAFE)

        engine.record_market_data(completed_candle(), now=BASE)
        accepted = engine.submit_order(
            side="buy",
            quantity="1.00000000",
            price="100.00",
            now=BASE,
        )
        self.assertEqual(accepted.status, OrderStatus.ACCEPTED)

        stale = engine.submit_order(
            side="buy",
            quantity="1.00000000",
            price="100.00",
            now=BASE + timedelta(seconds=31),
        )
        self.assertEqual(stale.status, OrderStatus.REJECTED)
        self.assertEqual(stale.rejection_code, RiskRejectionCode.MARKET_DATA_UNSAFE)

    def test_delayed_and_invalid_market_data_keep_the_gate_closed(self) -> None:
        delayed_engine = SpotPaperEngine(clock=lambda: BASE + timedelta(seconds=11))
        delayed = delayed_engine.record_market_data(
            completed_candle(receive_delay_seconds=11),
            now=BASE + timedelta(seconds=11),
        )
        self.assertEqual(delayed.status, DataHealthStatus.DELAYED)
        rejected = delayed_engine.submit_order(
            side="buy",
            quantity="1.00000000",
            price="100.00",
            now=BASE + timedelta(seconds=11),
        )
        self.assertEqual(rejected.rejection_code, RiskRejectionCode.MARKET_DATA_UNSAFE)

        invalid_engine = SpotPaperEngine(clock=lambda: BASE)
        health = invalid_engine.report_invalid_market_data(
            "invalid upstream price", now=BASE
        )
        self.assertEqual(health.status, DataHealthStatus.INVALID)
        rejected = invalid_engine.submit_order(
            side="buy",
            quantity="1.00000000",
            price="100.00",
            now=BASE,
        )
        self.assertEqual(rejected.rejection_code, RiskRejectionCode.MARKET_DATA_UNSAFE)

        future_engine = SpotPaperEngine(clock=lambda: BASE)
        future_health = future_engine.record_market_data(
            completed_candle(close_time=BASE + timedelta(minutes=1)),
            now=BASE,
        )
        self.assertEqual(future_health.status, DataHealthStatus.INVALID)
        future_rejected = future_engine.submit_order(
            side="buy",
            quantity="1.00000000",
            price="100.00",
            now=BASE,
        )
        self.assertEqual(future_rejected.rejection_code, RiskRejectionCode.MARKET_DATA_UNSAFE)

    def test_risk_limits_cap_notional_position_exposure_and_open_orders(self) -> None:
        engine = safe_engine(
            risk_limits=SpotRiskLimits(
                max_order_notional=D("500"),
                max_position_value=D("250"),
                max_open_orders=1,
            )
        )
        too_large = engine.submit_order(
            side="buy",
            quantity="6.00000000",
            price="100.00",
            now=BASE,
        )
        self.assertEqual(too_large.rejection_code, RiskRejectionCode.ORDER_NOTIONAL_TOO_LARGE)

        exposure_engine = safe_engine(
            risk_limits=SpotRiskLimits(max_portfolio_exposure=D("0.50"))
        )
        exposure = exposure_engine.submit_order(
            side="buy",
            quantity="6.00000000",
            price="100.00",
            now=BASE,
        )
        self.assertEqual(exposure.rejection_code, RiskRejectionCode.EXPOSURE_LIMIT)

        accepted = engine.submit_order(
            side="buy",
            quantity="1.00000000",
            price="100.00",
            client_order_id="open-1",
            now=BASE,
        )
        self.assertEqual(accepted.status, OrderStatus.ACCEPTED)
        second = engine.submit_order(
            side="buy",
            quantity="1.00000000",
            price="100.00",
            client_order_id="open-2",
            now=BASE,
        )
        self.assertEqual(second.rejection_code, RiskRejectionCode.OPEN_ORDER_LIMIT)

        engine.cancel_order(accepted.order_id, now=BASE)
        position_limited = engine.submit_order(
            side="buy",
            quantity="3.00000000",
            price="100.00",
            now=BASE,
        )
        self.assertEqual(position_limited.rejection_code, RiskRejectionCode.POSITION_LIMIT)

    def test_risk_manager_rejects_unpriced_assets_for_exposure_checks(self) -> None:
        engine = safe_engine(
            initial_balances={"ETH": "1"},
            risk_limits=SpotRiskLimits(max_portfolio_exposure=D("0.90")),
        )
        rejected = engine.submit_order(
            side="buy",
            quantity="1.00000000",
            price="100.00",
            now=BASE,
        )
        self.assertEqual(rejected.rejection_code, RiskRejectionCode.UNVALUED_ASSET)

    def test_execution_price_and_fill_errors_are_atomic(self) -> None:
        engine = safe_engine()
        order = engine.submit_order(
            side="buy",
            quantity="1.00000000",
            price="100.00",
            now=BASE,
        )
        before_balances = engine.balances()
        before_ledger = engine.ledger()
        with self.assertRaises(ExecutionError):
            engine.execute_fill(
                order.order_id,
                quantity="1.00000000",
                price="101.00",
                fill_id="bad-price",
                now=BASE,
            )
        self.assertEqual(engine.balances(), before_balances)
        self.assertEqual(engine.ledger(), before_ledger)
        self.assertEqual(engine.order(order.order_id).status, OrderStatus.ACCEPTED)
        with self.assertRaises(ExecutionError):
            engine.execute_fill(
                order.order_id,
                quantity="1.00000000",
                price="100.00",
                fill_id="stale-fill",
                now=BASE + timedelta(seconds=31),
            )
        self.assertEqual(engine.balances(), before_balances)
        self.assertEqual(engine.ledger(), before_ledger)

        with self.assertRaises(OrderValidationError):
            engine.execute_fill(
                order.order_id,
                quantity="1.000000001",
                price="100.00",
                fill_id="bad-quantity",
                now=BASE,
            )
        self.assertEqual(engine.balances(), before_balances)
        self.assertEqual(engine.ledger(), before_ledger)


class SpotPaperPortfolioTests(unittest.TestCase):
    def test_valuation_is_explicit_and_reports_unpriced_assets(self) -> None:
        engine = safe_engine(
            initial_balances={"ETH": "1.25"},
        )
        partial = engine.portfolio_snapshot({"BTC": "100.00"}, now=BASE)
        self.assertFalse(partial.fully_valued)
        self.assertIsNone(partial.total_value)
        self.assertEqual(partial.unpriced_assets, ("ETH",))
        self.assertEqual(partial.valued_value, D("1000.00"))

        complete = engine.portfolio_snapshot(
            {"BTC": "100.00", "ETH": "2000.00"},
            now=BASE,
        )
        self.assertTrue(complete.fully_valued)
        self.assertEqual(complete.total_value, D("3500.00"))

        with self.assertRaises(ValuationError):
            engine.portfolio_snapshot({"BTC": "0"}, now=BASE)
        with self.assertRaises(ValuationError):
            engine.portfolio_snapshot({"BTC": "NaN"}, now=BASE)

    def test_reconciliation_covers_initial_reservation_fill_release_and_rejection(self) -> None:
        engine = safe_engine()
        rejected = engine.submit_order(
            side="buy",
            quantity="20.00000000",
            price="100.00",
            now=BASE,
        )
        self.assertEqual(rejected.status, OrderStatus.REJECTED)
        order = engine.submit_order(
            side="buy",
            quantity="1.00000000",
            price="100.00",
            now=BASE,
        )
        engine.execute_fill(
            order.order_id,
            quantity="0.50000000",
            price="100.00",
            fill_id="reconcile-fill",
            now=BASE,
        )
        engine.cancel_order(order.order_id, now=BASE)
        report = engine.reconcile()
        self.assertTrue(report.is_consistent)
        self.assertEqual(report.differences, ())
        self.assertEqual(engine.balance("BTC").total, D("0.50000000"))
        self.assertEqual(engine.balance("USDT").total, D("949.95"))


class SpotPaperIntegrationTests(unittest.TestCase):
    def test_package_exports_paper_engine_interfaces(self) -> None:
        import trad

        self.assertIs(trad.SpotPaperEngine, SpotPaperEngine)
        self.assertIs(trad.OrderStatus, OrderStatus)
        self.assertIs(trad.SpotRiskLimits, SpotRiskLimits)

    def test_from_run_config_uses_spot_wallet_and_rejects_futures(self) -> None:
        spot_config = load_config("config/paper-spot.example.toml")
        engine = SpotPaperEngine.from_run_config(
            spot_config,
            clock=lambda: BASE,
        )
        self.assertEqual(spot_config.instrument, Instrument.SPOT)
        self.assertEqual(engine.balance("USDT").total, D("1000.00"))

        futures_config = load_config("config/paper-perpetual-futures.example.toml")
        self.assertEqual(futures_config.instrument, Instrument.PERPETUAL_FUTURES)
        with self.assertRaises(OrderValidationError):
            SpotPaperEngine.from_run_config(futures_config, clock=lambda: BASE)

    def test_separate_engines_do_not_share_spot_or_futures_balances(self) -> None:
        first = SpotPaperEngine(clock=lambda: BASE)
        second = SpotPaperEngine(
            starting_quote_balance="25.00",
            starting_base_balance="2.00000000",
            clock=lambda: BASE,
        )
        self.assertEqual(first.balance("USDT").total, D("1000.00"))
        self.assertEqual(second.balance("USDT").total, D("25.00"))
        self.assertEqual(second.balance("BTC").total, D("2.00000000"))
        self.assertNotEqual(first.balances(), second.balances())


if __name__ == "__main__":
    unittest.main()
