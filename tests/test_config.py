from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from trad.config import (  # noqa: E402
    ConfigurationError,
    Instrument,
    MarketDataSource,
    PerpetualFuturesConfig,
    RunConfig,
    RunMode,
    SpotConfig,
    load_config,
)


class RunConfigTests(unittest.TestCase):
    def test_defaults_are_backtest_spot_and_simulation_only(self) -> None:
        config = RunConfig.from_mapping({})

        self.assertEqual(config.mode, RunMode.BACKTEST)
        self.assertEqual(config.instrument, Instrument.SPOT)
        self.assertEqual(config.market_data_source, MarketDataSource.REPLAY)
        self.assertTrue(config.safety.simulation_only)
        self.assertEqual(config.resources.max_cpu_workers, 1)
        self.assertIs(config.active_accounting_config, config.spot)

    def test_loads_paper_spot_configuration(self) -> None:
        config = RunConfig.from_mapping(
            {
                "run": {
                    "mode": "paper",
                    "instrument": "spot",
                    "symbol": "ETH/USDT",
                    "market_data_source": "public_read_only",
                },
                "safety": {"simulation_only": True},
                "spot": {
                    "starting_quote_balance": 2500,
                    "starting_base_balance": 0.25,
                },
            }
        )

        self.assertEqual(config.mode, RunMode.PAPER)
        self.assertEqual(config.market_data_source, MarketDataSource.PUBLIC_READ_ONLY)
        self.assertEqual(config.spot, SpotConfig(2500.0, 0.25))
        self.assertIs(config.active_accounting_config, config.spot)

    def test_loads_futures_without_spot_settings(self) -> None:
        config = RunConfig.from_mapping(
            {
                "run": {
                    "mode": "paper",
                    "instrument": "perpetual_futures",
                    "symbol": "BTC/USDT:USDT",
                },
                "perpetual_futures": {
                    "starting_collateral": 5000,
                    "initial_leverage": 2,
                    "max_leverage": 3,
                },
            }
        )

        self.assertEqual(config.instrument, Instrument.PERPETUAL_FUTURES)
        self.assertEqual(
            config.active_accounting_config,
            PerpetualFuturesConfig(5000.0, 2.0, 3.0),
        )
        self.assertIs(config.active_accounting_config, config.perpetual_futures)

    def test_toml_loader_reads_example(self) -> None:
        path = Path(__file__).parents[1] / "config" / "paper-spot.example.toml"
        config = load_config(path)

        self.assertEqual(config.mode, RunMode.PAPER)
        self.assertEqual(config.instrument, Instrument.SPOT)
        self.assertTrue(config.safety.simulation_only)

    def test_rejects_live_mode(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "must be one of"):
            RunConfig.from_mapping({"run": {"mode": "live"}})

    def test_rejects_simulation_disabled(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "must remain true"):
            RunConfig.from_mapping({"safety": {"simulation_only": False}})

    def test_rejects_sensitive_or_real_trading_keys(self) -> None:
        for key in (
            "api_key",
            "private_credentials",
            "client_secret",
            "real_trading",
            "submit_orders",
        ):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ConfigurationError, "does not accept"):
                    RunConfig.from_mapping({key: "not allowed"})

    def test_rejects_mixed_spot_and_futures_settings(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "cannot be combined"):
            RunConfig.from_mapping(
                {
                    "run": {"instrument": "spot"},
                    "perpetual_futures": {},
                }
            )

        with self.assertRaisesRegex(ConfigurationError, "cannot be combined"):
            RunConfig.from_mapping(
                {
                    "run": {"instrument": "perpetual_futures"},
                    "spot": {},
                }
            )

    def test_rejects_invalid_futures_leverage(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "cannot exceed"):
            RunConfig.from_mapping(
                {
                    "run": {"instrument": "perpetual_futures"},
                    "perpetual_futures": {
                        "initial_leverage": 5,
                        "max_leverage": 2,
                    },
                }
            )

    def test_rejects_unknown_keys_and_bad_toml(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "unknown key"):
            RunConfig.from_mapping({"run": {"gpu": True}})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.toml"
            path.write_text("[run\nmode = 'paper'", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "invalid TOML"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
