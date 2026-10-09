from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from trad.backtest import (
    BacktestConfig,
    BacktestError,
    InstrumentMode,
    evaluate_strategy,
    select_strategy,
    validate_historical_candles,
)
from trad.market_data import OHLCV
from trad.strategies import (
    SignalAction,
    StrategyDefinition,
    StrategyRegistry,
    StrategySignal,
    bollinger_bands,
    ema,
    macd,
    rsi,
)


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def candles(values: list[float]) -> tuple[OHLCV, ...]:
    output = []
    for index, close in enumerate(values):
        opened = BASE + timedelta(minutes=index)
        completed = opened + timedelta(minutes=1)
        output.append(
            OHLCV(
                symbol="BTC/USDT",
                timestamp=opened,
                close_time=completed,
                received_at=completed,
                timeframe_seconds=60,
                open=close,
                high=close + 1,
                low=max(0.01, close - 1),
                close=close,
                volume=1,
            )
        )
    return tuple(output)


class StrategyIndicatorTests(unittest.TestCase):
    def test_registry_contains_documented_strategy_catalogue(self) -> None:
        registry = StrategyRegistry.default()
        self.assertEqual(
            registry.names(),
            (
                "ema_crossover",
                "rsi_reversion",
                "macd_crossover",
                "bollinger_bands",
                "breakout",
                "momentum",
                "trend_following",
                "mean_reversion",
            ),
        )
        self.assertEqual(len(registry.all()), 8)

    def test_indicators_warm_up_without_looking_ahead(self) -> None:
        values = tuple(float(index + 1) for index in range(40))
        ema_values = ema(values, 10)
        rsi_values = rsi(values, 14)
        middle, upper, lower = bollinger_bands(values, 20)
        line, signal, histogram = macd(values)
        self.assertIsNone(ema_values[8])
        self.assertIsNotNone(ema_values[9])
        self.assertIsNone(rsi_values[13])
        self.assertIsNotNone(rsi_values[14])
        self.assertIsNone(middle[18])
        self.assertGreater(upper[-1], middle[-1])
        self.assertLess(lower[-1], middle[-1])
        self.assertIsNotNone(line[-1])
        self.assertIsNotNone(signal[-1])
        self.assertIsNotNone(histogram[-1])

    def test_causal_strategy_context_never_contains_future_candles(self) -> None:
        history = candles([100 + index * 0.2 for index in range(100)])
        observed: list[tuple[int, datetime]] = []

        def causal(context):
            observed.append((len(context.candles), context.current.timestamp))
            self.assertEqual(len(context.candles), context.index + 1)
            self.assertLessEqual(max(item.timestamp for item in context.candles), context.current.timestamp)
            return StrategySignal("causal_test", SignalAction.FLAT, context.current.close_time, "flat")

        definition = StrategyDefinition("causal_test", "Causal test", "test", 2, causal)
        result = evaluate_strategy(
            definition,
            history,
            instrument=InstrumentMode.SPOT,
            config=BacktestConfig(min_validation_trades=1, require_train_non_negative=False),
        )
        self.assertTrue(result.causal)
        self.assertGreater(len(observed), 0)
        self.assertTrue(all(size >= 2 for size, _ in observed))

    def test_strategy_exception_is_recorded_as_failed_candidate(self) -> None:
        history = candles([100 + index * 0.1 for index in range(100)])

        def broken(_context):
            raise RuntimeError("deliberate strategy failure")

        definition = StrategyDefinition("broken", "Broken", "test failure", 2, broken)
        result = evaluate_strategy(
            definition,
            history,
            instrument=InstrumentMode.FUTURES,
            config=BacktestConfig(min_validation_trades=1, require_train_non_negative=False),
        )
        self.assertEqual(result.status, "error")
        self.assertFalse(result.accepted)
        self.assertIn("deliberate strategy failure", result.error)
        self.assertEqual(result.failure_reasons, ("strategy evaluation raised an exception",))


class BacktestSelectionTests(unittest.TestCase):
    def test_selection_records_every_strategy_and_can_select_winner(self) -> None:
        history = candles([100 + index * 0.5 for index in range(180)])
        selection = select_strategy(
            history,
            instrument=InstrumentMode.FUTURES,
            config=BacktestConfig(min_validation_trades=1, require_train_non_negative=False),
            now=BASE,
        )
        self.assertEqual(len(selection.results), 8)
        self.assertIsNotNone(selection.selected_strategy)
        self.assertIn(selection.selected_strategy, {result.strategy_name for result in selection.results})
        self.assertTrue(any(result.accepted for result in selection.results))
        self.assertTrue(all(result.validation is not None for result in selection.results if result.status != "error"))
        winner = next(result for result in selection.results if result.strategy_name == selection.selected_strategy)
        self.assertGreaterEqual(len(winner.trades), 1)
        self.assertEqual(selection.created_at, BASE)

    def test_no_candidate_meeting_acceptance_blocks_selection(self) -> None:
        history = candles([100.0] * 180)
        selection = select_strategy(
            history,
            instrument=InstrumentMode.FUTURES,
            config=BacktestConfig(
                min_validation_trades=1,
                min_validation_return=Decimal("0.01"),
                require_train_non_negative=False,
            ),
            now=BASE,
        )
        self.assertIsNone(selection.selected_strategy)
        self.assertFalse(selection.accepted)
        self.assertIn("no strategy passed", selection.reason)
        self.assertTrue(all(not result.accepted for result in selection.results))

    def test_fees_are_included_in_performance_and_gaps_fail_closed(self) -> None:
        history = candles([100 + index * 0.5 for index in range(100)])
        result = evaluate_strategy(
            StrategyRegistry.default().get("momentum"),
            history,
            instrument=InstrumentMode.FUTURES,
            config=BacktestConfig(min_validation_trades=1, require_train_non_negative=False),
        )
        self.assertGreater(result.validation.total_fees, Decimal("0"))
        broken = list(history)
        broken[50] = OHLCV(
            symbol="BTC/USDT",
            timestamp=broken[49].timestamp + timedelta(minutes=2),
            close_time=broken[49].timestamp + timedelta(minutes=3),
            received_at=broken[49].timestamp + timedelta(minutes=3),
            timeframe_seconds=60,
            open=125,
            high=126,
            low=124,
            close=125,
            volume=1,
        )
        with self.assertRaises(BacktestError):
            validate_historical_candles(broken)


if __name__ == "__main__":
    unittest.main()
