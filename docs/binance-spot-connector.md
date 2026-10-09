# Binance Spot public connector

## Scope

Phase 4, Step 4 provides one CPU-only, read-only connector for completed
Spot OHLCV candles. It uses Python's standard library and the public Binance
Spot REST endpoint:

```text
GET https://api.binance.com/api/v3/klines
```

No API key, account endpoint, private credential, order endpoint, or trading
operation is present. The ordinary configuration CLI and Dashboard do not
silently fetch remote data; the explicit `trad-runner` command is the opt-in
integration that starts this public connector for each bounded or supervised
cycle.

## Usage

```python
from trad.binance_spot import BinanceSpotPublicConnector

connector = BinanceSpotPublicConnector()
candles = connector.fetch_ohlcv("BTC/USDT", "1m", limit=100)

for candle in candles:
    print(candle.open_time, candle.close_time, candle.close)
```

The connector accepts compact Binance symbols such as `BTCUSDT` and the
project display form `BTC/USDT`. Supported fixed-duration intervals are:

```text
1s, 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d, 3d, 1w
```

The variable-length `1M` interval is intentionally not supported because the
project's `OHLCV` model requires a fixed number of seconds.

## Time and completeness rules

Binance klines contain an opening timestamp and an inclusive API close-time
field in milliseconds. The connector verifies that the API close time matches
the requested interval, then converts it to the project's exclusive
`close_time` boundary:

```text
project close_time = open_time + timeframe_seconds
```

`received_at` is the local UTC time captured after the response is decoded.
The final in-progress Binance candle is omitted. An incomplete candle in any
other position is rejected as an inconsistent response. Returned candles are
therefore compatible with the existing `OHLCV` model, including its
`received_at >= close_time` requirement.

The connector requires Binance's twelve-field kline row shape and also
rejects duplicate, out-of-order, or gapped candle rows. The returned tuple
still needs to be passed through the existing
`MarketDataSafetyMonitor`; this connector does not weaken or replace the
project's stale-data and position-opening guard.

## Error behavior

The connector fails closed and raises typed errors for:

- invalid symbol, interval, limit, or connector settings;
- HTTP errors such as rate limiting or server errors;
- timeout, DNS, connection, and other transport failures;
- oversized, invalid-UTF-8, invalid-JSON, or wrong-shaped responses;
- Binance structured API error payloads;
- missing or extra kline fields, non-finite values, invalid prices/volume,
  invalid, overflowing, or future timestamps, bad close times, incomplete
  non-final candles, duplicates, ordering errors, and gaps.

It performs no automatic retry or silent fallback. Callers must apply their own
reconnect policy while keeping the safety monitor closed until fresh validated
data has been received. The response size and request timeout have bounded
local defaults.

## Free-access limitations

This is a public unauthenticated REST adapter. Availability, rate limits,
listing rules, data retention, and terms of service are controlled by Binance
and can change. The project does not claim guaranteed availability or fills.
Use a local deterministic replay for tests and backtests. A synchronized local
UTC clock is recommended; if the local clock is behind Binance, a recently
closed candle may be conservatively omitted rather than accepted early.

## Runner integration and testing

`trad-runner` wires this connector to the Spot paper engine and wires the
separate `BinanceFuturesPublicConnector` to the Futures paper engine. Each
cycle fetches a bounded completed-candle window, validates it before strategy
selection, and keeps the position-opening guard closed on any transport,
completeness, ordering, or freshness failure. The runner never treats Spot
candles as Futures candles.

Connector tests inject fixed HTTP responses and a deterministic clock, so they
do not access Binance or require the internet:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
