from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from trad.market_data import (  # noqa: E402
    BidAsk,
    DataHealthStatus,
    DataSafetyError,
    DataValidationError,
    DeterministicReplay,
    MarketDataKind,
    MarketDataPolicy,
    MarketDataSafetyMonitor,
    OHLCV,
    OrderBook,
    OrderBookLevel,
    Ticker,
    Trade,
    TradeSide,
    event_kind,
)


UTC = timezone.utc
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def candle(offset_seconds: int, *, close: float = 101.0) -> OHLCV:
    timestamp = BASE + timedelta(seconds=offset_seconds)
    return OHLCV(
        symbol="BTC/USDT",
        timestamp=timestamp,
        received_at=timestamp,
        timeframe_seconds=60,
        open=100.0,
        high=max(101.0, close),
        low=min(99.0, close),
        close=close,
        volume=2.0,
    )


class MarketDataModelTests(unittest.TestCase):
    def test_all_normalized_models_validate_and_report_kind(self) -> None:
        timestamp = BASE
        events = (
            candle(0),
            Ticker(
                symbol="BTC/USDT",
                timestamp=timestamp,
                received_at=timestamp,
                last_price=100.5,
                bid_price=100.4,
                ask_price=100.6,
                bid_size=1.0,
                ask_size=1.5,
            ),
            Trade(
                symbol="BTC/USDT",
                timestamp=timestamp,
                received_at=timestamp,
                price=100.5,
                quantity=0.25,
                side=TradeSide.BUY,
                trade_id="trade-1",
            ),
            BidAsk(
                symbol="BTC/USDT",
                timestamp=timestamp,
                received_at=timestamp,
                bid_price=100.4,
                ask_price=100.6,
                bid_size=1.0,
                ask_size=1.5,
            ),
            OrderBook(
                symbol="BTC/USDT",
                timestamp=timestamp,
                received_at=timestamp,
                bids=(OrderBookLevel(price=100.4, quantity=1.0),),
                asks=(OrderBookLevel(price=100.6, quantity=1.5),),
                sequence=1,
            ),
        )
        expected = (
            MarketDataKind.OHLCV,
            MarketDataKind.TICKER,
            MarketDataKind.TRADE,
            MarketDataKind.BID_ASK,
            MarketDataKind.ORDER_BOOK,
        )

        self.assertEqual(tuple(event_kind(event) for event in events), expected)
        self.assertEqual(events[2].side, TradeSide.BUY)
        self.assertEqual(events[4].sequence, 1)

    def test_rejects_invalid_values_and_timestamps(self) -> None:
        with self.assertRaises(DataValidationError):
            candle(0, close=float("nan"))
        with self.assertRaises(DataValidationError):
            OHLCV(
                symbol="BTC/USDT",
                timestamp=BASE,
                received_at=BASE,
                timeframe_seconds=60,
                open=100,
                high=99,
                low=99,
                close=100,
                volume=1,
            )
        with self.assertRaises(DataValidationError):
            Ticker(
                symbol="BTC/USDT",
                timestamp=BASE,
                received_at=BASE,
                last_price=100,
                bid_price=101,
                ask_price=100,
            )
        with self.assertRaises(DataValidationError):
            Trade(
                symbol="BTC/USDT",
                timestamp=BASE,
                received_at=BASE,
                price=100,
                quantity=1,
                side="unknown",
            )
        with self.assertRaises(DataValidationError):
            BidAsk(
                symbol="BTC/USDT",
                timestamp=BASE,
                received_at=BASE,
                bid_price=100,
                ask_price=99,
                bid_size=1,
                ask_size=1,
            )
        with self.assertRaises(DataValidationError):
            OrderBook(
                symbol="BTC/USDT",
                timestamp=BASE,
                received_at=BASE,
                bids=(OrderBookLevel(price=100, quantity=1),),
                asks=(OrderBookLevel(price=99, quantity=1),),
            )
        with self.assertRaises(DataValidationError):
            OHLCV(
                symbol="BTC/USDT",
                timestamp=BASE.replace(tzinfo=None),
                received_at=BASE,
                timeframe_seconds=60,
                open=100,
                high=101,
                low=99,
                close=100,
                volume=1,
            )


class MarketDataSafetyMonitorTests(unittest.TestCase):
    def policy(self, **kwargs: object) -> MarketDataPolicy:
        defaults: dict[str, object] = {
            "max_age_seconds": 30.0,
            "max_source_lag_seconds": 10.0,
        }
        defaults.update(kwargs)
        return MarketDataPolicy(**defaults)

    def test_deterministic_replay_accepts_contiguous_ohlcv(self) -> None:
        events = (candle(0), candle(60, close=102), candle(120, close=103))
        first = DeterministicReplay(events).replay(
            MarketDataSafetyMonitor(self.policy(max_age_seconds=120))
        )
        second = DeterministicReplay(events).replay(
            MarketDataSafetyMonitor(self.policy(max_age_seconds=120))
        )

        self.assertEqual(
            tuple(result.status for result in first),
            (DataHealthStatus.SAFE,) * 3,
        )
        self.assertEqual(
            tuple(result.status for result in first),
            tuple(result.status for result in second),
        )
        self.assertTrue(first[-1].allow_new_positions)

    def test_stale_data_blocks_new_positions_and_hard_guard(self) -> None:
        monitor = MarketDataSafetyMonitor(self.policy())
        monitor.ingest(candle(0), now=BASE)

        health = monitor.health(BASE + timedelta(seconds=31))

        self.assertEqual(health.status, DataHealthStatus.STALE)
        self.assertFalse(health.allow_new_positions)
        self.assertFalse(monitor.can_open_new_positions(BASE + timedelta(seconds=31)))
        with self.assertRaises(DataSafetyError):
            monitor.require_safe_for_new_position(BASE + timedelta(seconds=31))

    def test_reported_invalid_payload_blocks_new_positions(self) -> None:
        monitor = MarketDataSafetyMonitor(self.policy())
        monitor.ingest(candle(0), now=BASE)

        health = monitor.report_invalid_data(
            "payload omitted the exchange timestamp",
            now=BASE,
            kind=MarketDataKind.TICKER,
            symbol="BTC/USDT",
        )

        self.assertEqual(health.status, DataHealthStatus.INVALID)
        self.assertFalse(health.allow_new_positions)
        self.assertFalse(monitor.can_open_new_positions(BASE))

    def test_delayed_data_blocks_new_positions(self) -> None:
        monitor = MarketDataSafetyMonitor(self.policy())
        delayed = OHLCV(
            symbol="BTC/USDT",
            timestamp=BASE,
            received_at=BASE + timedelta(seconds=11),
            timeframe_seconds=60,
            open=100,
            high=101,
            low=99,
            close=100,
            volume=1,
        )

        health = monitor.ingest(delayed, now=delayed.received_at)

        self.assertEqual(health.status, DataHealthStatus.DELAYED)
        self.assertFalse(health.allow_new_positions)

    def test_duplicate_out_of_order_and_gap_are_blocking(self) -> None:
        duplicate_monitor = MarketDataSafetyMonitor(self.policy(max_age_seconds=120))
        duplicate_monitor.ingest(candle(0), now=BASE)
        duplicate_health = duplicate_monitor.ingest(candle(0), now=BASE)
        self.assertEqual(duplicate_health.status, DataHealthStatus.DUPLICATE)
        self.assertFalse(duplicate_health.allow_new_positions)

        order_monitor = MarketDataSafetyMonitor(self.policy(max_age_seconds=120))
        order_monitor.ingest(candle(60), now=BASE + timedelta(seconds=60))
        order_health = order_monitor.ingest(candle(0), now=BASE + timedelta(seconds=60))
        self.assertEqual(order_health.status, DataHealthStatus.OUT_OF_ORDER)

        gap_monitor = MarketDataSafetyMonitor(self.policy(max_age_seconds=180))
        gap_monitor.ingest(candle(0), now=BASE)
        gap_health = gap_monitor.ingest(
            candle(120), now=BASE + timedelta(seconds=120)
        )
        self.assertEqual(gap_health.status, DataHealthStatus.GAP)
        self.assertFalse(gap_health.allow_new_positions)

    def test_order_book_sequence_gap_is_blocking(self) -> None:
        def book(sequence: int) -> OrderBook:
            timestamp = BASE + timedelta(seconds=sequence)
            return OrderBook(
                symbol="BTC/USDT",
                timestamp=timestamp,
                received_at=timestamp,
                bids=(OrderBookLevel(price=100, quantity=1),),
                asks=(OrderBookLevel(price=101, quantity=1),),
                sequence=sequence,
            )

        monitor = MarketDataSafetyMonitor(self.policy(max_age_seconds=120))
        monitor.ingest(book(1), now=BASE + timedelta(seconds=1))
        health = monitor.ingest(book(3), now=BASE + timedelta(seconds=3))

        self.assertEqual(health.status, DataHealthStatus.SEQUENCE_GAP)
        self.assertFalse(health.allow_new_positions)

    def test_trade_events_with_same_timestamp_are_distinct_by_id(self) -> None:
        monitor = MarketDataSafetyMonitor(self.policy())
        first = Trade(
            symbol="BTC/USDT",
            timestamp=BASE,
            received_at=BASE,
            price=100,
            quantity=1,
            side="buy",
            trade_id="a",
        )
        second = Trade(
            symbol="BTC/USDT",
            timestamp=BASE,
            received_at=BASE,
            price=100.1,
            quantity=1,
            side="sell",
            trade_id="b",
        )
        duplicate = Trade(
            symbol="BTC/USDT",
            timestamp=BASE,
            received_at=BASE,
            price=100,
            quantity=1,
            side="buy",
            trade_id="a",
        )

        self.assertEqual(monitor.ingest(first, now=BASE).status, DataHealthStatus.SAFE)
        self.assertEqual(monitor.ingest(second, now=BASE).status, DataHealthStatus.SAFE)
        self.assertEqual(
            monitor.ingest(duplicate, now=BASE).status,
            DataHealthStatus.DUPLICATE,
        )

    def test_required_streams_and_reset_recovery(self) -> None:
        policy = self.policy(
            required_kinds=frozenset({MarketDataKind.BID_ASK, MarketDataKind.ORDER_BOOK}),
            max_age_seconds=120,
        )
        monitor = MarketDataSafetyMonitor(policy)
        quote = BidAsk(
            symbol="BTC/USDT",
            timestamp=BASE,
            received_at=BASE,
            bid_price=100,
            ask_price=101,
            bid_size=1,
            ask_size=1,
        )
        missing = monitor.ingest(quote, now=BASE)
        self.assertEqual(missing.status, DataHealthStatus.NO_DATA)
        self.assertFalse(missing.allow_new_positions)

        monitor.reset()
        self.assertEqual(monitor.health(BASE).status, DataHealthStatus.NO_DATA)
        order_book = OrderBook(
            symbol="BTC/USDT",
            timestamp=BASE,
            received_at=BASE,
            bids=(OrderBookLevel(price=100, quantity=1),),
            asks=(OrderBookLevel(price=101, quantity=1),),
            sequence=1,
        )
        monitor.ingest(quote, now=BASE)
        healthy = monitor.ingest(order_book, now=BASE)
        self.assertEqual(healthy.status, DataHealthStatus.SAFE)
        self.assertTrue(healthy.allow_new_positions)

        monitor.reset()
        monitor.ingest(candle(0), now=BASE)
        self.assertFalse(monitor.can_open_new_positions(BASE))


if __name__ == "__main__":
    unittest.main()
