#!/usr/bin/env python3
"""Run a deterministic, offline Perpetual Futures paper simulation.

Usage from the repository root:
    PYTHONPATH=src python3 examples/futures_paper_offline.py
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from trad.futures_paper import FuturesPaperEngine, FuturesPositionSide
from trad.market_data import OHLCV


START = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)


def main() -> None:
    with FuturesPaperEngine(clock=lambda: START) as engine:
        candle = OHLCV(
            symbol="BTC/USDT",
            timestamp=START - timedelta(minutes=1),
            close_time=START,
            received_at=START,
            timeframe_seconds=60,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1.0,
        )
        engine.record_market_data(candle, now=START)

        opening = engine.open_position(
            FuturesPositionSide.LONG,
            quantity=Decimal("0.01000000"),
            price=Decimal("100.00"),
            client_order_id="offline-open-1",
            now=START,
        )
        engine.execute_fill(
            opening.order_id,
            quantity=Decimal("0.01000000"),
            price=Decimal("100.00"),
            fill_id="offline-fill-1",
            now=START,
        )
        engine.mark_to_market(Decimal("105.00"), now=START + timedelta(seconds=1))

        closing = engine.reduce_position(
            FuturesPositionSide.LONG,
            quantity=Decimal("0.01000000"),
            price=Decimal("105.00"),
            client_order_id="offline-close-1",
            now=START + timedelta(seconds=1),
        )
        engine.execute_fill(
            closing.order_id,
            quantity=Decimal("0.01000000"),
            price=Decimal("105.00"),
            fill_id="offline-fill-2",
            now=START + timedelta(seconds=1),
        )

        balance = engine.balance()
        report = engine.reconcile()
        print(f"futures balance: {balance.available} available, {balance.reserved} reserved {balance.asset}")
        print(f"fills: {len(engine.fills())}; audit events: {len(engine.audit_events())}")
        print(f"reconciled: {report.is_consistent}")


if __name__ == "__main__":
    main()
