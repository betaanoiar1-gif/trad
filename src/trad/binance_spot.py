"""Public, read-only Binance Spot REST connector.

Only the public klines endpoint is used here.  This module deliberately does
not accept API credentials, access account endpoints, or submit orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import socket
from types import MappingProxyType
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .market_data import DataValidationError, OHLCV


class BinanceSpotConnectorError(RuntimeError):
    """Base class for errors raised by the public connector."""


class BinanceSpotRequestError(BinanceSpotConnectorError, ValueError):
    """Raised when a caller supplies an unsupported request parameter."""


class BinanceSpotHTTPError(BinanceSpotConnectorError):
    """Raised when Binance returns a non-success HTTP status."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"Binance Spot HTTP {status_code}: {message}")


class BinanceSpotTransportError(BinanceSpotConnectorError):
    """Raised for timeout, DNS, connection, or other transport failures."""


class BinanceSpotResponseError(BinanceSpotConnectorError):
    """Raised when a response is too large, not JSON, or has the wrong shape."""


class BinanceSpotAPIError(BinanceSpotConnectorError):
    """Raised when Binance returns a structured API error payload."""

    def __init__(self, code: int | None, message: str) -> None:
        self.code = code
        self.message = message
        label = f"code {code}: " if code is not None else ""
        super().__init__(f"Binance Spot API error ({label}{message})")


class BinanceSpotDataError(BinanceSpotConnectorError):
    """Raised when a successful response violates the kline data contract."""


@dataclass(frozen=True)
class BinanceSpotConnectorSettings:
    """Safe local settings for the public Binance Spot connector."""

    base_url: str = "https://api.binance.com"
    timeout_seconds: float = 10.0
    max_response_bytes: int = 2_000_000
    user_agent: str = "trad-public-market-data/0.1"

    def __post_init__(self) -> None:
        _validate_base_url(self.base_url)
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise BinanceSpotRequestError("timeout_seconds must be a positive number")
        if not math.isfinite(float(self.timeout_seconds)) or self.timeout_seconds <= 0:
            raise BinanceSpotRequestError("timeout_seconds must be a positive number")
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or self.max_response_bytes < 1
        ):
            raise BinanceSpotRequestError(
                "max_response_bytes must be a positive integer"
            )
        if not isinstance(self.user_agent, str) or not self.user_agent.strip():
            raise BinanceSpotRequestError("user_agent must be a non-empty string")


HttpGet = Callable[[str, float], bytes]
Clock = Callable[[], datetime]


class BinanceSpotPublicConnector:
    """Fetch completed public Spot klines from Binance.

    The connector uses ``GET /api/v3/klines`` only.  It returns immutable
    project ``OHLCV`` models and omits Binance's final in-progress candle.  No
    retries or silent fallbacks are performed: callers receive a typed error
    and can decide when a safe reconnect is appropriate.

    ``http_get`` and ``clock`` are injectable for deterministic local tests;
    production use should leave both unset.
    """

    KLINES_PATH = "/api/v3/klines"
    MAX_KLINE_LIMIT = 1000
    KLINE_FIELD_COUNT = 12

    _INTERVAL_SECONDS: Mapping[str, int] = MappingProxyType(
        {
            "1s": 1,
            "1m": 60,
            "3m": 180,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "2h": 7200,
            "4h": 14_400,
            "6h": 21_600,
            "8h": 28_800,
            "12h": 43_200,
            "1d": 86_400,
            "3d": 259_200,
            "1w": 604_800,
        }
    )

    def __init__(
        self,
        *,
        settings: BinanceSpotConnectorSettings | None = None,
        http_get: HttpGet | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.settings = settings or BinanceSpotConnectorSettings()
        self._http_get = http_get or self._urllib_get
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @classmethod
    def supported_intervals(cls) -> tuple[str, ...]:
        """Return fixed-duration Binance intervals supported by this model."""

        return tuple(cls._INTERVAL_SECONDS)

    def fetch_ohlcv(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int = 500,
    ) -> tuple[OHLCV, ...]:
        """Fetch and normalize completed klines for one Binance Spot symbol.

        ``symbol`` accepts Binance's compact form such as ``BTCUSDT`` and the
        project's display form such as ``BTC/USDT``.  ``interval`` must be one
        of :meth:`supported_intervals`; the variable-length ``1M`` interval is
        intentionally excluded because the project model requires seconds.
        """

        api_symbol, display_symbol = _normalize_symbol(symbol)
        timeframe_seconds = self._interval_seconds(interval)
        normalized_limit = _validate_limit(limit, self.MAX_KLINE_LIMIT)
        payload = self._request_json(
            self.KLINES_PATH,
            {
                "symbol": api_symbol,
                "interval": interval,
                "limit": str(normalized_limit),
            },
        )
        if not isinstance(payload, list):
            raise BinanceSpotResponseError("klines response must be a JSON array")

        received_at = self._receipt_time()
        return self._parse_klines(
            payload,
            display_symbol=display_symbol,
            timeframe_seconds=timeframe_seconds,
            received_at=received_at,
        )

    def _interval_seconds(self, interval: str) -> int:
        if not isinstance(interval, str) or interval not in self._INTERVAL_SECONDS:
            supported = ", ".join(self.supported_intervals())
            raise BinanceSpotRequestError(
                f"unsupported fixed-duration interval {interval!r}; "
                f"choose one of: {supported}"
            )
        return self._INTERVAL_SECONDS[interval]

    def _request_json(self, path: str, params: Mapping[str, str]) -> Any:
        query = urlencode(params)
        url = f"{self.settings.base_url.rstrip('/')}{path}?{query}"
        try:
            body = self._http_get(url, float(self.settings.timeout_seconds))
        except BinanceSpotConnectorError:
            raise
        except HTTPError as exc:
            raise BinanceSpotHTTPError(exc.code, exc.reason or "HTTP error") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise BinanceSpotTransportError("Binance request timed out") from exc
        except (URLError, ConnectionError, OSError) as exc:
            raise BinanceSpotTransportError(
                f"Binance request failed: {exc}"
            ) from exc

        if not isinstance(body, (bytes, bytearray)):
            raise BinanceSpotResponseError("HTTP transport must return bytes")
        if len(body) > self.settings.max_response_bytes:
            raise BinanceSpotResponseError("Binance response exceeded configured size limit")
        try:
            decoded = bytes(body).decode("utf-8")
            payload = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BinanceSpotResponseError("Binance response was not valid UTF-8 JSON") from exc

        if isinstance(payload, dict) and ("code" in payload or "msg" in payload):
            code = payload.get("code")
            if code is not None and not isinstance(code, int):
                code = None
            message = payload.get("msg")
            if not isinstance(message, str) or not message.strip():
                message = "unknown Binance API error"
            raise BinanceSpotAPIError(code, message)
        return payload

    def _urllib_get(self, url: str, timeout: float) -> bytes:
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": self.settings.user_agent,
            },
            method="GET",
        )
        with urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if not isinstance(status, int) or not 200 <= status < 300:
                raise BinanceSpotHTTPError(
                    int(status) if isinstance(status, int) else 0,
                    "unexpected HTTP status",
                )
            body = response.read(self.settings.max_response_bytes + 1)
            if len(body) > self.settings.max_response_bytes:
                raise BinanceSpotResponseError(
                    "Binance response exceeded configured size limit"
                )
            return body

    def _receipt_time(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise BinanceSpotDataError("clock must return a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise BinanceSpotDataError("clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _parse_klines(
        rows: list[Any],
        *,
        display_symbol: str,
        timeframe_seconds: int,
        received_at: datetime,
    ) -> tuple[OHLCV, ...]:
        interval_ms = timeframe_seconds * 1000
        previous_open_ms: int | None = None
        completed: list[OHLCV] = []

        for index, row in enumerate(rows):
            if (
                not isinstance(row, list)
                or len(row) != BinanceSpotPublicConnector.KLINE_FIELD_COUNT
            ):
                raise BinanceSpotDataError(
                    f"kline row {index} must contain exactly "
                    f"{BinanceSpotPublicConnector.KLINE_FIELD_COUNT} fields"
                )
            open_ms = _integer_field(row[0], f"kline row {index} open time", minimum=0)
            close_ms = _integer_field(row[6], f"kline row {index} close time", minimum=0)
            if previous_open_ms is not None:
                if open_ms <= previous_open_ms:
                    raise BinanceSpotDataError(
                        "klines must be strictly ordered with no duplicates"
                    )
                if open_ms - previous_open_ms != interval_ms:
                    raise BinanceSpotDataError(
                        "klines contain a timestamp gap for the requested interval"
                    )
            previous_open_ms = open_ms

            expected_close_ms = open_ms + interval_ms - 1
            if close_ms != expected_close_ms:
                raise BinanceSpotDataError(
                    f"kline row {index} close time does not match its interval"
                )

            open_price = _number_field(row[1], f"kline row {index} open")
            high_price = _number_field(row[2], f"kline row {index} high")
            low_price = _number_field(row[3], f"kline row {index} low")
            close_price = _number_field(row[4], f"kline row {index} close")
            volume = _number_field(row[5], f"kline row {index} volume")
            open_time = _milliseconds_to_utc(open_ms, f"kline row {index} open time")
            close_time = open_time + timedelta(seconds=timeframe_seconds)
            if open_time > received_at:
                raise BinanceSpotDataError(
                    f"kline row {index} open time is in the future"
                )

            # Binance commonly includes the currently forming candle.  It is
            # safe to omit only the final valid row; an incomplete row in the
            # middle indicates an inconsistent response and is rejected.
            if close_time > received_at:
                if index == len(rows) - 1:
                    continue
                raise BinanceSpotDataError(
                    "an incomplete kline appeared before the final response row"
                )

            try:
                completed.append(
                    OHLCV(
                        symbol=display_symbol,
                        timestamp=open_time,
                        close_time=close_time,
                        received_at=received_at,
                        timeframe_seconds=timeframe_seconds,
                        open=open_price,
                        high=high_price,
                        low=low_price,
                        close=close_price,
                        volume=volume,
                    )
                )
            except DataValidationError as exc:
                raise BinanceSpotDataError(
                    f"kline row {index} failed market-data validation: {exc}"
                ) from exc
        return tuple(completed)


def _validate_base_url(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise BinanceSpotRequestError("base_url must be a non-empty HTTPS URL")
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.netloc:
        raise BinanceSpotRequestError("base_url must be an HTTPS URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise BinanceSpotRequestError(
            "base_url cannot contain credentials, query parameters, or fragments"
        )


def _normalize_symbol(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or not value.strip():
        raise BinanceSpotRequestError("symbol must be a non-empty string")
    raw = value.strip().upper()
    if any(character.isspace() for character in raw):
        raise BinanceSpotRequestError("symbol cannot contain whitespace")
    if "/" in raw:
        parts = raw.split("/")
        if len(parts) != 2 or not all(part.isalnum() for part in parts):
            raise BinanceSpotRequestError(
                "slash-form symbol must look like BASE/QUOTE, for example BTC/USDT"
            )
        return "".join(parts), f"{parts[0]}/{parts[1]}"
    if not raw.isalnum():
        raise BinanceSpotRequestError(
            "compact Binance symbol must contain only letters and digits"
        )
    return raw, raw


def _validate_limit(value: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BinanceSpotRequestError("limit must be an integer")
    if value < 1 or value > maximum:
        raise BinanceSpotRequestError(f"limit must be between 1 and {maximum}")
    return value


def _integer_field(value: Any, field_name: str, *, minimum: int) -> int:
    if isinstance(value, bool):
        raise BinanceSpotDataError(f"{field_name} must be an integer")
    if isinstance(value, int):
        converted = value
    elif isinstance(value, str) and value.strip().isdigit():
        converted = int(value.strip())
    else:
        raise BinanceSpotDataError(f"{field_name} must be an integer")
    if converted < minimum:
        raise BinanceSpotDataError(f"{field_name} is below the allowed minimum")
    return converted


def _number_field(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise BinanceSpotDataError(f"{field_name} must be numeric")
    try:
        converted = float(value)
    except (TypeError, ValueError) as exc:
        raise BinanceSpotDataError(f"{field_name} must be numeric") from exc
    if not math.isfinite(converted):
        raise BinanceSpotDataError(f"{field_name} must be finite")
    return converted


def _milliseconds_to_utc(value: int, field_name: str) -> datetime:
    # Avoid converting through float: large, otherwise valid millisecond
    # timestamps lose sub-second precision when represented as seconds.
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    seconds, milliseconds = divmod(value, 1000)
    try:
        return epoch + timedelta(seconds=seconds, milliseconds=milliseconds)
    except (OverflowError, OSError, ValueError) as exc:
        raise BinanceSpotDataError(
            f"{field_name} is outside the supported datetime range"
        ) from exc
