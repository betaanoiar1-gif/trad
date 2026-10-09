"""CLI for bounded or supervisor-managed public-data paper automation."""

from __future__ import annotations

import argparse
from pathlib import Path
import signal
import threading
from typing import Sequence

from .autonomous import AutonomousPaperRunner, RunnerConfig, RunnerState
from .binance_futures import BinanceFuturesPublicConnector
from .binance_spot import BinanceSpotPublicConnector


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trad-runner",
        description=(
            "Run the local Spot and Futures paper loop from public OHLCV data. "
            "No private credentials or real orders are supported."
        ),
    )
    parser.add_argument("--symbol", default="BTC/USDT", help="public display symbol (default: BTC/USDT)")
    parser.add_argument("--interval", default="1m", help="fixed-duration public interval (default: 1m)")
    parser.add_argument("--history-limit", type=int, default=250, help="completed candles used for selection and each cycle")
    parser.add_argument("--refresh-seconds", type=float, default=60.0, help="delay between bounded cycles")
    parser.add_argument("--journal-db", type=Path, default=Path("var/trad-runner.sqlite3"), help="persistent run journal")
    parser.add_argument("--futures-db", type=Path, default=Path("var/futures-runner.sqlite3"), help="persistent Futures engine database")
    parser.add_argument("--max-cycles", type=int, default=None, help="stop after this many provider cycles")
    parser.add_argument("--once", action="store_true", help="fetch and process exactly one cycle")
    parser.add_argument("--resume", action="store_true", help="explicitly resume a previously paused recovered runner")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.once:
        args.max_cycles = 1
    if args.max_cycles is not None and (isinstance(args.max_cycles, bool) or args.max_cycles < 1):
        raise SystemExit("--max-cycles must be positive")
    spot_connector = BinanceSpotPublicConnector()
    futures_connector = BinanceFuturesPublicConnector()
    runner = AutonomousPaperRunner(
        journal_path=args.journal_db,
        futures_database_path=args.futures_db,
        config=RunnerConfig(
            symbol=args.symbol,
            interval=args.interval,
            history_limit=args.history_limit,
            refresh_seconds=args.refresh_seconds,
        ),
    )
    stop_event = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stop_event.set()
        runner.pause()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    providers = {
        "spot": lambda: spot_connector.fetch_ohlcv(args.symbol, args.interval, limit=args.history_limit),
        "futures": lambda: futures_connector.fetch_ohlcv(args.symbol, args.interval, limit=args.history_limit),
    }
    try:
        runner.run_forever(providers, stop_event=stop_event, max_cycles=args.max_cycles, resume=args.resume)
        print(runner.snapshot())
        return 0 if runner.state is not RunnerState.BLOCKED else 2
    finally:
        runner.close()


if __name__ == "__main__":
    raise SystemExit(main())
