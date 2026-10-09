from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest

from trad.binance_futures import (
    BinanceFuturesConnectorSettings,
    BinanceFuturesPublicConnector,
)


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def row(open_time: datetime, value: str) -> list[object]:
    open_ms = int(open_time.timestamp() * 1000)
    close_ms = open_ms + 60_000 - 1
    return [open_ms, value, str(float(value) + 1), str(float(value) - 1), value, "2", close_ms, "0", 1, "0", "0", "0"]


class BinanceFuturesConnectorTests(unittest.TestCase):
    def test_uses_public_futures_endpoint_and_shared_completed_candle_validation(self) -> None:
        requested: list[str] = []
        payload = [row(BASE, "100"), row(BASE + timedelta(minutes=1), "101")]

        def fake_get(url: str, _timeout: float) -> bytes:
            requested.append(url)
            return json.dumps(payload).encode()

        connector = BinanceFuturesPublicConnector(
            settings=BinanceFuturesConnectorSettings(timeout_seconds=2),
            http_get=fake_get,
            clock=lambda: BASE + timedelta(minutes=2),
        )
        result = connector.fetch_ohlcv("BTC/USDT", "1m", limit=2)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].symbol, "BTC/USDT")
        self.assertIn("https://fapi.binance.com/fapi/v1/klines", requested[0])
        self.assertIn("symbol=BTCUSDT", requested[0])

    def test_futures_settings_reject_credentials_through_shared_validation(self) -> None:
        with self.assertRaises(ValueError):
            BinanceFuturesConnectorSettings(base_url="https://user:secret@fapi.binance.com").as_spot_settings()


if __name__ == "__main__":
    unittest.main()
