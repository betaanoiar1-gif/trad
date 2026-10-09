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

    def test_package_exports_connector_interface_without_network(self) -> None:
        import trad

        self.assertIs(trad.BinanceSpotPublicConnector, BinanceSpotPublicConnector)
        self.assertEqual(
            BinanceSpotConnectorSettings().base_url,
            "https://data-api.binance.vision",
        )
        fake_http = FakeHTTP([kline(BASE)])
        connector = trad.BinanceSpotPublicConnector(
            http_get=fake_http,
            clock=lambda: BASE + timedelta(minutes=1),
        )

        [event] = connector.fetch_ohlcv("BTCUSDT", "1m")

        self.assertIsInstance(event, OHLCV)
        self.assertEqual(event.symbol, "BTCUSDT")
        self.assertEqual(len(fake_http.calls), 1)
        self.assertTrue(
            fake_http.calls[0][0].startswith(
                "https://data-api.binance.vision/api/v3/klines?"
            )
        )

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

    def test_exact_completion_boundary_uses_exclusive_project_close_time(self) -> None:
        just_before_close = BASE + timedelta(minutes=1) - timedelta(milliseconds=1)
        before_connector, _ = self.connector(
            [kline(BASE)],
            received_at=just_before_close,
        )
        at_close_connector, _ = self.connector(
            [kline(BASE)],
            received_at=BASE + timedelta(minutes=1),
        )

        self.assertEqual(
            before_connector.fetch_ohlcv("BTC/USDT", "1m"),
            (),
        )
        completed = at_close_connector.fetch_ohlcv("BTC/USDT", "1m")
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].close_time, BASE + timedelta(minutes=1))

    def test_converts_non_utc_clock_and_candle_boundaries_to_utc(self) -> None:
        local_open = datetime(
            2024,
            12,
            31,
            19,
            0,
            tzinfo=timezone(timedelta(hours=-5)),
        )
        local_receipt = datetime(
            2025,
            1,
            1,
            0,
            1,
            tzinfo=timezone(timedelta(hours=-5)),
        )
        connector, _ = self.connector(
            [kline(local_open)],
            received_at=local_receipt,
        )

        [event] = connector.fetch_ohlcv("BTC/USDT", "1m")

        self.assertEqual(event.open_time, datetime(2025, 1, 1, tzinfo=UTC))
        self.assertEqual(event.close_time, datetime(2025, 1, 1, 0, 1, tzinfo=UTC))
        self.assertEqual(event.received_at, datetime(2025, 1, 1, 5, 1, tzinfo=UTC))

    def test_rejects_future_and_non_utc_clock_timestamps(self) -> None:
        future_connector, _ = self.connector(
            [kline(BASE + timedelta(days=1))],
            received_at=BASE + timedelta(minutes=2),
        )
        with self.assertRaisesRegex(BinanceSpotDataError, "future"):
            future_connector.fetch_ohlcv("BTC/USDT", "1m")

        negative_timestamp = kline(BASE)
        negative_timestamp[0] = "-1"
        invalid_connector, _ = self.connector(
            [negative_timestamp],
            received_at=BASE + timedelta(minutes=2),
        )
        with self.assertRaises(BinanceSpotDataError):
            invalid_connector.fetch_ohlcv("BTC/USDT", "1m")

        overflowing_timestamp = kline(BASE)
        overflowing_timestamp[0] = "1" + "0" * 30
        overflowing_timestamp[6] = str(int(overflowing_timestamp[0]) + 59_999)
        overflowing_connector, _ = self.connector(
            [overflowing_timestamp],
            received_at=BASE + timedelta(minutes=2),
        )
        with self.assertRaisesRegex(BinanceSpotDataError, "supported datetime"):
            overflowing_connector.fetch_ohlcv("BTC/USDT", "1m")

        naive_clock_connector = BinanceSpotPublicConnector(
            http_get=FakeHTTP([kline(BASE)]),
            clock=lambda: BASE.replace(tzinfo=None),
        )
        with self.assertRaisesRegex(BinanceSpotDataError, "timezone-aware"):
            naive_clock_connector.fetch_ohlcv("BTC/USDT", "1m")

    def test_preserves_millisecond_precision_for_large_valid_timestamps(self) -> None:
        open_ms = 253402297200123  # 9999-12-31 23:00:00.123 UTC
        row = kline(BASE)
        row[0] = str(open_ms)
        row[6] = str(open_ms + 59_999)
        received_at = datetime(9999, 12, 31, 23, 1, 0, 123_000, tzinfo=UTC)
        connector, _ = self.connector([row], received_at=received_at)

        [event] = connector.fetch_ohlcv("BTC/USDT", "1m")

        self.assertEqual(
            event.open_time,
            datetime(9999, 12, 31, 23, 0, 0, 123_000, tzinfo=UTC),
        )
        self.assertEqual(event.close_time, received_at)

    def test_handles_adjacent_hour_day_month_and_year_boundaries(self) -> None:
        cases = (
            (datetime(2024, 12, 31, 23, 0, tzinfo=UTC), 3600),
            (datetime(2024, 1, 31, tzinfo=UTC), 86_400),
            (datetime(2024, 12, 31, tzinfo=UTC), 86_400),
        )
        for open_time, timeframe_seconds in cases:
            with self.subTest(open_time=open_time, timeframe_seconds=timeframe_seconds):
                next_open = open_time + timedelta(seconds=timeframe_seconds)
                payload = [
                    kline(open_time, timeframe_seconds=timeframe_seconds),
                    kline(next_open, timeframe_seconds=timeframe_seconds),
                ]
                received_at = next_open + timedelta(seconds=timeframe_seconds)
                connector, _ = self.connector(payload, received_at=received_at)

                events = connector.fetch_ohlcv(
                    "BTC/USDT",
                    "1h" if timeframe_seconds == 3600 else "1d",
                    limit=2,
                )

                self.assertEqual(len(events), 2)
                self.assertEqual(events[1].open_time, next_open)
                self.assertEqual(
                    events[0].close_time,
                    open_time + timedelta(seconds=timeframe_seconds),
                )

    def test_rejects_invalid_request_parameters(self) -> None:
        connector, _ = self.connector([], received_at=BASE)

        with self.assertRaises(BinanceSpotRequestError):
            connector.fetch_ohlcv("BTC/USDT", "1M")
        with self.assertRaises(BinanceSpotRequestError):
            connector.fetch_ohlcv("BTC/USDT", "1m", limit=1001)
        with self.assertRaises(BinanceSpotRequestError):
            connector.fetch_ohlcv("BTC USDT", "1m")

    def test_rejects_malformed_kline_shapes_and_numeric_contract_violations(self) -> None:
        invalid_rows = (
            (kline(BASE)[:6], "too few fields"),
            (kline(BASE) + ["unexpected"], "too many fields"),
            (kline(BASE, open_price="-1"), "negative open"),
            (kline(BASE, high_price="99"), "high below open"),
            (kline(BASE, low_price="101"), "low above close"),
            (kline(BASE, close_price="0"), "non-positive close"),
            (kline(BASE, volume="-1"), "negative volume"),
            (kline(BASE, high_price="NaN"), "not finite high"),
            (kline(BASE, low_price="inf"), "not finite low"),
            (kline(BASE, close_price="1e309"), "overflow close"),
            (kline(BASE, volume="not-a-number"), "non-numeric volume"),
            (kline(BASE, close_ms=123), "bad close time"),
        )
        for row, label in invalid_rows:
            with self.subTest(label=label):
                connector, _ = self.connector(
                    [row],
                    received_at=BASE + timedelta(minutes=2),
                )
                with self.assertRaises(BinanceSpotDataError):
                    connector.fetch_ohlcv("BTC/USDT", "1m")

        for field, value in ((0, "1.5"), (0, True), (6, "-1"), (6, "not-an-int")):
            with self.subTest(field=field, value=value):
                row = kline(BASE)
                row[field] = value  # type: ignore[assignment]
                connector, _ = self.connector(
                    [row],
                    received_at=BASE + timedelta(minutes=2),
                )
                with self.assertRaises(BinanceSpotDataError):
                    connector.fetch_ohlcv("BTC/USDT", "1m")

    def test_rejects_wrong_top_level_shape_empty_body_and_oversized_body(self) -> None:
        wrong_shape, _ = self.connector({}, received_at=BASE)
        with self.assertRaises(BinanceSpotResponseError):
            wrong_shape.fetch_ohlcv("BTC/USDT", "1m")

        empty_body = BinanceSpotPublicConnector(
            http_get=lambda _url, _timeout: b"",
            clock=lambda: BASE,
        )
        with self.assertRaises(BinanceSpotResponseError):
            empty_body.fetch_ohlcv("BTC/USDT", "1m")

        invalid_utf8 = BinanceSpotPublicConnector(
            http_get=lambda _url, _timeout: b"\xff",
            clock=lambda: BASE,
        )
        with self.assertRaises(BinanceSpotResponseError):
            invalid_utf8.fetch_ohlcv("BTC/USDT", "1m")

        oversized = BinanceSpotPublicConnector(
            settings=BinanceSpotConnectorSettings(max_response_bytes=3),
            http_get=lambda _url, _timeout: b"1234",
            clock=lambda: BASE,
        )
        with self.assertRaises(BinanceSpotResponseError):
            oversized.fetch_ohlcv("BTC/USDT", "1m")

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

    def test_rejects_timestamp_duplicates_gaps_and_out_of_order_rows(self) -> None:
        duplicate_connector, _ = self.connector(
            [kline(BASE), kline(BASE)],
            received_at=BASE + timedelta(minutes=2),
        )
        with self.assertRaisesRegex(BinanceSpotDataError, "strictly ordered"):
            duplicate_connector.fetch_ohlcv("BTC/USDT", "1m", limit=2)

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

        timeout_calls = 0

        def timeout(_url: str, _timeout: float) -> bytes:
            nonlocal timeout_calls
            timeout_calls += 1
            raise TimeoutError()

        timeout_connector = BinanceSpotPublicConnector(
            http_get=timeout,
            clock=lambda: BASE,
        )
        with self.assertRaises(BinanceSpotTransportError):
            timeout_connector.fetch_ohlcv("BTC/USDT", "1m")
        self.assertEqual(timeout_calls, 1)

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
