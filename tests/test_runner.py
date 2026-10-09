from __future__ import annotations

import unittest

from trad.runner import build_public_providers


class FakeConnector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def fetch_ohlcv(self, symbol: str, interval: str, *, limit: int):
        self.calls.append((symbol, interval, limit))
        return ()


class RunnerWiringTests(unittest.TestCase):
    def test_public_spot_and_futures_sources_are_wired_separately(self) -> None:
        spot = FakeConnector()
        futures = FakeConnector()
        providers = build_public_providers(
            "BTC/USDT",
            "1m",
            250,
            spot_connector=spot,
            futures_connector=futures,
        )

        self.assertEqual(set(providers), {"spot", "futures"})
        self.assertEqual(providers["spot"](), ())
        self.assertEqual(providers["futures"](), ())
        self.assertEqual(spot.calls, [("BTC/USDT", "1m", 250)])
        self.assertEqual(futures.calls, [("BTC/USDT", "1m", 250)])

    def test_provider_wiring_rejects_invalid_inputs_before_network_access(self) -> None:
        with self.assertRaises(ValueError):
            build_public_providers("", "1m", 250)
        with self.assertRaises(ValueError):
            build_public_providers("BTC/USDT", "", 250)
        with self.assertRaises(ValueError):
            build_public_providers("BTC/USDT", "1m", 0)


if __name__ == "__main__":
    unittest.main()
