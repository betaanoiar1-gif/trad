from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from trad.autonomous import AutonomousPaperRunner, RunnerConfig, RunnerPersistenceError, RunnerState
from trad.backtest import BacktestConfig
from trad.market_data import OHLCV


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def history(values: list[float], *, flat: bool = False) -> tuple[OHLCV, ...]:
    events = []
    for index, value in enumerate(values):
        opened = BASE + timedelta(minutes=index)
        completed = opened + timedelta(minutes=1)
        close = 100.0 if flat else value
        events.append(
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
    return tuple(events)


class AutonomousRunnerTests(unittest.TestCase):
    def test_selection_execution_idempotency_and_restart_recovery(self) -> None:
        candles = history([100 + index * 0.5 for index in range(180)])
        clock = lambda: BASE + timedelta(minutes=180, seconds=5)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runner = AutonomousPaperRunner(
                journal_path=root / "runner.sqlite3",
                futures_database_path=root / "futures.sqlite3",
                config=RunnerConfig(history_limit=180),
                clock=clock,
            )
            result = runner.run_once({"spot": lambda: candles, "futures": lambda: candles})
            self.assertEqual(result["status"], "running")
            self.assertEqual(runner.state, RunnerState.RUNNING)
            self.assertTrue(runner.snapshot()["selected"]["spot"])
            self.assertTrue(runner.snapshot()["selected"]["futures"])
            self.assertEqual(len(runner.spot.fills()), 1)
            self.assertEqual(len(runner.futures.fills()), 1)
            duplicate = runner.process_event("spot", candles[-1], ingest=False)
            self.assertEqual(duplicate["status"], "duplicate_decision")
            self.assertEqual(len(runner.spot.fills()), 1)
            self.assertGreaterEqual(runner.snapshot()["counts"]["strategy_results"], 16)
            self.assertGreaterEqual(runner.snapshot()["counts"]["operations"], 2)
            self.assertEqual(runner.pause()["state"], RunnerState.PAUSED.value)
            runner.close()

            recovered = AutonomousPaperRunner(
                journal_path=root / "runner.sqlite3",
                futures_database_path=root / "futures.sqlite3",
                config=RunnerConfig(history_limit=180),
                clock=clock,
            )
            try:
                self.assertEqual(recovered.state, RunnerState.PAUSED)
                self.assertTrue(recovered.snapshot()["selected"]["spot"])
                self.assertEqual(len(recovered.spot.fills()), 1)
                self.assertEqual(len(recovered.futures.fills()), 1)
                self.assertEqual(recovered.spot.balance("USDT").total, Decimal("899.91"))
                self.assertEqual(
                    recovered.run_once({"spot": lambda: candles, "futures": lambda: candles})["status"],
                    "paused",
                )
                self.assertEqual(recovered.resume()["state"], RunnerState.RUNNING.value)
                recovered.stop()
            finally:
                recovered.close()

    def test_journal_storage_failure_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            journal_directory = Path(directory) / "journal-directory"
            journal_directory.mkdir()
            with self.assertRaises(RunnerPersistenceError):
                AutonomousPaperRunner(journal_path=journal_directory)

    def test_no_accepted_strategy_blocks_new_positions(self) -> None:
        candles = history([100.0 for _ in range(180)], flat=True)
        with TemporaryDirectory() as directory:
            runner = AutonomousPaperRunner(
                journal_path=Path(directory) / "runner.sqlite3",
                futures_database_path=Path(directory) / "futures.sqlite3",
                config=RunnerConfig(history_limit=180),
                backtest_config=BacktestConfig(
                    min_validation_trades=1,
                    min_validation_return=Decimal("0.01"),
                    require_train_non_negative=False,
                ),
                clock=lambda: BASE + timedelta(minutes=180, seconds=5),
            )
            selections = runner.evaluate_all({"spot": candles, "futures": candles})
            self.assertIsNone(selections["spot"].selected_strategy)
            self.assertEqual(runner.start()["state"], RunnerState.BLOCKED.value)
            event = runner.process_event("spot", candles[-1])
            self.assertEqual(event["status"], "not_running")
            self.assertEqual(runner.spot.orders(), ())
            runner.close()


if __name__ == "__main__":
    unittest.main()
