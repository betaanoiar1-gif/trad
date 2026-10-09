"""Public, read-only Binance USDⓈ-M Futures OHLCV connector.

Only the public ``/fapi/v1/klines`` endpoint is used.  It deliberately shares
Binance's validated kline parsing with the Spot connector but has its own
base URL and symbol identity so a Futures stream can never silently reuse a
Spot price source.
"""

from __future__ import annotations

from dataclasses import dataclass

from .binance_spot import (
    BinanceSpotConnectorSettings,
    BinanceSpotPublicConnector,
)


@dataclass(frozen=True)
class BinanceFuturesConnectorSettings:
    """Safe settings for the public Binance Futures market-data endpoint."""

    base_url: str = "https://fapi.binance.com"
    timeout_seconds: float = 10.0
    max_response_bytes: int = 2_000_000
    user_agent: str = "trad-public-futures-market-data/0.1"

    def as_spot_settings(self) -> BinanceSpotConnectorSettings:
        """Adapt validated transport settings to the shared parser."""

        return BinanceSpotConnectorSettings(
            base_url=self.base_url,
            timeout_seconds=self.timeout_seconds,
            max_response_bytes=self.max_response_bytes,
            user_agent=self.user_agent,
        )


class BinanceFuturesPublicConnector(BinanceSpotPublicConnector):
    """Fetch completed public Binance Futures candles without credentials."""

    KLINES_PATH = "/fapi/v1/klines"

    def __init__(
        self,
        *,
        settings: BinanceFuturesConnectorSettings | None = None,
        http_get=None,
        clock=None,
    ) -> None:
        configured = settings or BinanceFuturesConnectorSettings()
        super().__init__(
            settings=configured.as_spot_settings(),
            http_get=http_get,
            clock=clock,
        )
