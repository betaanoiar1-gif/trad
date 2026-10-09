# Zero-cost operating guide

## Scope of the completed steps

Phase 3, Step 2 establishes a local configuration foundation and Step 3 adds
the market-data safety layer. Phase 4, Step 4 adds an opt-in public Binance
Spot connector for completed OHLCV candles.

The current code validates `backtest` and `paper` declarations, normalizes
local market-data events, validates public Binance klines, and provides
CPU-only Spot and Perpetual Futures paper engines, a causal strategy
catalogue with offline backtests, a persistent pauseable paper runner, and a
dependency-free local browser dashboard. Both engines use
deterministic fills, Decimal accounting, fees, risk checks, and auditable
ledgers. The Futures engine additionally provides explicit margin, funding,
liquidation, and transactional local SQLite recovery; see
[futures-paper-engine.md](futures-paper-engine.md).

It does **not** run a strategy, submit an exchange order, access an account, or
provide live trading. The configuration validator remains offline; callers
explicitly opt into public market data through `trad-runner` or the Dashboard's
Evaluate/Start controls. The paper engines never submit exchange orders. See
[spot-paper-engine.md](spot-paper-engine.md) and
[futures-paper-engine.md](futures-paper-engine.md).

## Requirements

- Python 3.11 or newer
- A local CPU; one worker is the default
- No GPU
- No exchange account
- No trading API key or private credential
- No paid service, subscription, cloud server, or hosted database

The runtime and tests use the Python standard library only. The connector's
optional live use requires outbound access to Binance's public HTTPS endpoint;
all tests remain local and deterministic.

## Validate locally

From the repository root:

```bash
python3 --version
PYTHONPATH=src python3 -m trad config/backtest.example.toml
PYTHONPATH=src python3 -m trad config/paper-spot.example.toml --json
PYTHONPATH=src python3 -m trad config/paper-perpetual-futures.example.toml
PYTHONPATH=src python3 examples/futures_paper_offline.py
PYTHONPATH=src python3 -m trad.dashboard --host 127.0.0.1 --port 8765
PYTHONPATH=src python3 -m trad.runner --once
```

The optional packaging workflow is:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --editable .
trad-config config/backtest.example.toml
```

The editable-install command uses the free/open-source build tool documented
in [dependencies and licenses](dependencies-and-licenses.md). It is not needed
for direct local execution.

Run the complete local test suite with the standard library test runner:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The connector tests inject fixed HTTP responses and a fixed UTC clock; they do
not access Binance or require the internet.

## Optional public Binance OHLCV access

```python
from trad.binance_spot import BinanceSpotPublicConnector

connector = BinanceSpotPublicConnector()
candles = connector.fetch_ohlcv("BTC/USDT", "1m", limit=100)
```

The connector uses only:

```text
GET https://data-api.binance.vision/api/v3/klines
```

It supports fixed-duration intervals and returns existing project `OHLCV`
objects. The final in-progress candle is omitted, and malformed or unsafe
responses raise typed errors. It does not retry silently. See
[Binance Spot connector](binance-spot-connector.md) for the exact timestamp,
rate-limit, error, and recovery assumptions.

## Optional Spot paper execution

The offline paper engine is documented in
[Spot paper-trading engine](spot-paper-engine.md). It consumes explicitly
supplied prices and validated market-data health, starts a new Spot simulation
with 1,000 USDT by default, and never calls Binance for order execution. It
keeps Spot balances separate from the declared Perpetual Futures configuration.

## Optional Perpetual Futures paper execution

The local Futures engine is documented in
[Perpetual Futures paper engine](futures-paper-engine.md). It starts a separate
1,000 USDT collateral wallet, uses explicit prices and caller-supplied funding,
and supports local SQLite snapshots and restart recovery. Its liquidation model
is conservative and explicitly documented; it is not an exchange matching or
liquidation implementation. The checked-in
`examples/futures_paper_offline.py` is fully deterministic and requires no
network.

## Local browser dashboard

The dependency-free dashboard is documented in
[dashboard.md](dashboard.md). Start it with
`PYTHONPATH=src python3 -m trad.dashboard`; it serves
`http://127.0.0.1:8765/` by default, binds only to localhost, and stores
Futures accounting plus the separate automation journal in ignored local
`var/` SQLite files. Its default automation controls explicitly use only the
public Spot and Futures kline endpoints when Evaluate or Start is pressed; the
manual market-data form remains available for offline replay. No private
exchange route or order endpoint exists. The dashboard tests use a local
ephemeral HTTP server and do not require a browser driver or network access.
Strategy selection and runner recovery are also deterministic offline tests;
the bounded `trad-runner --once` command uses only public read-only kline
endpoints when explicitly run.

## Modes and safety boundary

| Mode | Meaning in the foundation | Network or order behavior |
| --- | --- | --- |
| `backtest` | Historical/replay declaration | No network access and no real orders |
| `paper` | Intended simulated live declaration | No automatic connector or paper engine |
| `live` | Unsupported | Rejected by configuration validation |

The effective value of `safety.simulation_only` is always `true`; if the field
is supplied, it must also be explicitly `true`. Sensitive credential fields and
real-order fields are rejected instead of ignored.

## Spot and Perpetual Futures separation

Spot settings live under `[spot]` and contain only Spot starting balances.
Perpetual Futures settings live under `[perpetual_futures]` and contain only
collateral and conservative leverage declarations. The validator rejects a
configuration that supplies the inactive instrument's settings. The Futures
paper engine adds explicit Python-side funding, margin, maintenance-margin, and
liquidation assumptions without sharing any Spot wallet or ledger.

## Data-access and recovery limitations

The connector is an unauthenticated public REST adapter. Binance controls
availability, rate limits, listing rules, data retention, and terms of service.
No availability or fill guarantee is made. A synchronized local UTC clock is
recommended; if it is behind Binance, a recently closed candle may be omitted
conservatively.

HTTP, timeout, connection, response, API, and data-validation errors are
raised instead of hidden. Do not bypass them or add credentials. If a feed
fails, keep the safety monitor closed, apply a caller-controlled reconnect
policy, and ingest fresh validated data before considering new simulated
positions. The monitor's existing `reset()` recovery rule still applies.
