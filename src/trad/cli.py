"""Command-line validation entry point for Step 2."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .config import ConfigurationError, RunConfig, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trad-config",
        description=(
            "Validate a local trad configuration. This command does not fetch "
            "data or place orders."
        ),
    )
    parser.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path("config/backtest.example.toml"),
        help="TOML file to validate (default: config/backtest.example.toml)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="print the validated non-secret summary as JSON",
    )
    return parser


def _summary(config: RunConfig) -> dict[str, object]:
    return {
        "status": "valid",
        "mode": config.mode.value,
        "instrument": config.instrument.value,
        "symbol": config.symbol,
        "market_data_source": config.market_data_source.value,
        "simulation_only": config.safety.simulation_only,
        "max_cpu_workers": config.resources.max_cpu_workers,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigurationError as exc:
        print(f"configuration error: {exc}")
        return 2

    summary = _summary(config)
    if args.as_json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print("configuration is valid")
        for key, value in summary.items():
            if key != "status":
                print(f"{key}={value}")
    return 0