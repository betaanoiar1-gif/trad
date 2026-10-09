from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import sys
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from trad.binance_spot import (  # noqa: E402
    BinanceSpotAPIError,
    BinanceSpotConnectorSettings,
    BinanceSpotDataError,
    BinanceSpotHTTPError,
    BinanceSpotPublicConnector,
    BinanceSpotRequestError,
    BinanceSpotResponseError,
    BinanceSpotTransportError,
)
from trad.market_data import OHLCV  # noqa: E402


UTC = timezone.utc
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def kline(
    open_time: datetime,
    *,
    timeframe_seconds: int = 60,
    open_price: str = "100.0",
    high_price: str = "101.0",
    low_price: str = "99.0",
    close_price: str = "100.5",
    volume: str = "12.0",
    close_ms: int | None = None,
) -> list[str]:
    open_ms = int(open_time.timestamp() * 1000)
    expected_close_ms = open_ms + timeframe_seconds * 1000 - 1
    return [
        str(open_ms),
        open_price,
        high_price,
        low_price,
        close_price,
        volume,
        str(expected_close_ms if close_ms is None else close_ms),
        "1206.0",
        "10",
        "5.0",
        "603.0",
        "0",
    ]


class FakeHTTP:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout: float) -> bytes:
        self.calls.append((url, timeout))
        return json.dumps(self.payload).encode("utf-8")


class BinanceSpotConnectorTests(unittest.TestCase):
    def connector(
        self,
        payload: object,
        *,
        received_at: datetime,
    ) -> tuple[BinanceSpotPublicConnector, FakeHTTP]:
        fake_http = FakeHTTP(payload)
        connector = BinanceSpotPublicConnector(
            http_get=fake_http,
            clock=lambda: received_at,
        )
        return connector, fake_http

    def test_fetches_completed_klines_and_omits_in_progress_tail(self) -> None:
        payload = [
            kline(BASE),
            kline(BASE + timedelta(minutes=1)),
            kline(BASE + timedelta(minutes=2)),
        ]
        received_at = BASE + timedelta(minutes=2, seconds=10)
        connector, fake_http = self.connector(payload, received_at=received_at)

        events = connector.fetch_ohlcv("BTC/USDT", "1m", limit=3)

        self.assertEqual(len(events), 2)
        self.assertTrue(all(isinstance(event, OHLCV) for event in events))
        self.assertEqual(events[0].symbol, "BTC/USDT")
        self.assertEqual(events[0].open_time, BASE)
        self.assertEqual(events[0].close_time, BASE + timedelta(minutes=1))
        self.assertEqual(events[0].received_at, received_at)
        self.assertEqual(events[1].open_time, BASE + timedelta(minutes=1))
        self.assertEqual(len(fake_http.calls), 1)

        parsed = parse_qs(urlsplit(fake_http.calls[0][0]).query)
        self.assertEqual(parsed["symbol"], ["BTCUSDT"])
        self.assertEqual(parsed["interval"], ["1m"])
        self.assertEqual(parsed["limit"], ["3"])
        self.assertEqual(fake_http.calls[0][1], 10.0)

    def test_fetches_completed_five_minute_klines(self) -> None:
        payload = [
            kline(BASE, timeframe_seconds=300),
            kline(BASE + timedelta(minutes=5), timeframe_seconds=300),
        ]
        received_at = BASE + timedelta(minutes=10)
        connector, _ = self.connector(payload, received_at=received_at)

        events = connector.fetch_ohlcv("BTCUSDT", "5m", limit=2)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].timeframe_seconds, 300)
        self.assertEqual(events[0].close_time, BASE + timedelta(minutes=5))
        self.assertEqual(events[1].open_time, BASE + timedelta(minutes=5))

    def test_only_in_progress_candle_returns_no_completed_data(self) -> None:
        connector, _ = self.connector(
            [kline(BASE)],
            received_at=BASE + timedelta(seconds=30),
        )

        self.assertEqual(connector.fetch_ohlcv("BTC/USDT", "1m"), ())

    def test_rejects_invalid_request_parameters(self) -> None:
        connector, _ = self.connector([], received_at=BASE)

        with self.assertRaises(BinanceSpotRequestError):
            connector.fetch_ohlcv("BTC/USDT", "1M")
        with self.assertRaises(BinanceSpotRequestError):
            connector.fetch_ohlcv("BTC/USDT", "1m", limit=1001)
        with self.assertRaises(BinanceSpotRequestError):
            connector.fetch_ohlcv("BTC USDT", "1m")

    def test_rejects_missing_fields_invalid_values_and_bad_close_time(self) -> None:
        cases = (
            ([kline(BASE)[:6]], "missing fields"),
            ([kline(BASE, high_price="not-a-number")], "invalid number"),
            ([kline(BASE, close_ms=123)], "bad close time"),
        )
        for payload, label in cases:
            with self.subTest(label=label):
                connector, _ = self.connector(
                    payload,
                    received_at=BASE + timedelta(minutes=2),
                )
                with self.assertRaises(BinanceSpotDataError):
                    connector.fetch_ohlcv("BTC/USDT", "1m")

    def test_rejects_nonfinal_incomplete_candle(self) -> None:
        payload = [
            kline(BASE),
            kline(BASE + timedelta(minutes=1)),
        ]
        connector, _ = self.connector(
            payload,
            received_at=BASE + timedelta(seconds=30),
        )

        with self.assertRaisesRegex(BinanceSpotDataError, "incomplete"):
            connector.fetch_ohlcv("BTC/USDT", "1m", limit=2)

    def test_rejects_timestamp_gaps_and_out_of_order_rows(self) -> None:
        gap_connector, _ = self.connector(
            [kline(BASE), kline(BASE + timedelta(minutes=2))],
            received_at=BASE + timedelta(minutes=3),
        )
        with self.assertRaisesRegex(BinanceSpotDataError, "gap"):
            gap_connector.fetch_ohlcv("BTC/USDT", "1m", limit=2)

        out_of_order_connector, _ = self.connector(
            [kline(BASE + timedelta(minutes=1)), kline(BASE)],
            received_at=BASE + timedelta(minutes=3),
        )
        with self.assertRaisesRegex(BinanceSpotDataError, "strictly ordered"):
            out_of_order_connector.fetch_ohlcv("BTC/USDT", "1m", limit=2)

    def test_rejects_invalid_json_and_binance_api_error_payload(self) -> None:
        invalid_json = BinanceSpotPublicConnector(
            http_get=lambda _url, _timeout: b"not-json",
            clock=lambda: BASE,
        )
        with self.assertRaises(BinanceSpotResponseError):
            invalid_json.fetch_ohlcv("BTC/USDT", "1m")

        api_error, _ = self.connector(
            {"code": -1121, "msg": "Invalid symbol."},
            received_at=BASE,
        )
        with self.assertRaisesRegex(BinanceSpotAPIError, "-1121"):
            api_error.fetch_ohlcv("BTC/USDT", "1m")

    def test_maps_http_timeout_and_connection_failures(self) -> None:
        def http_error(_url: str, _timeout: float) -> bytes:
            raise HTTPError(
                "https://api.binance.com/api/v3/klines",
                429,
                "Too Many Requests",
                hdrs=None,
                fp=BytesIO(b"rate limited"),
            )

        connector = BinanceSpotPublicConnector(http_get=http_error, clock=lambda: BASE)
        with self.assertRaises(BinanceSpotHTTPError) as http_context:
            connector.fetch_ohlcv("BTC/USDT", "1m")
        self.assertEqual(http_context.exception.status_code, 429)

        timeout_connector = BinanceSpotPublicConnector(
            http_get=lambda _url, _timeout: (_ for _ in ()).throw(TimeoutError()),
            clock=lambda: BASE,
        )
        with self.assertRaises(BinanceSpotTransportError):
            timeout_connector.fetch_ohlcv("BTC/USDT", "1m")

        connection_connector = BinanceSpotPublicConnector(
            http_get=lambda _url, _timeout: (_ for _ in ()).throw(
                ConnectionError("offline")
            ),
            clock=lambda: BASE,
        )
        with self.assertRaises(BinanceSpotTransportError):
            connection_connector.fetch_ohlcv("BTC/USDT", "1m")

    def test_rejects_unsafe_connector_settings(self) -> None:
        with self.assertRaises(BinanceSpotRequestError):
            BinanceSpotConnectorSettings(base_url="http://example.invalid")


if __name__ == "__main__":
    unittest.main()
