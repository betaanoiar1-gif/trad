# trad

## Phase 3 status

**Step 3 complete: market-data safety layer.**

Step 2 established the local zero-cost foundation. Step 3 adds validated,
normalized market-data models, deterministic replay support, timestamp and
ordering checks, gap and sequence-gap detection, stale/delayed-data checks,
and a fail-closed guard for opening new simulated positions. Spot and
Perpetual Futures configuration remains separate, with 1,000 USDT in each
separate default wallet.

## Phase 4 status

**Prompt 1 complete: public Binance Spot OHLCV connector.**

The connector uses only the unauthenticated Binance Spot `/api/v3/klines`
endpoint, converts completed candles to the existing `OHLCV` model, validates
UTC timing, close boundaries, ordering, gaps, values, response size, HTTP
errors, transport failures, and structured API errors. It omits the current
in-progress candle and never accepts private credentials.

This checkout still does **not** implement ticker/trade/order-book adapters,
paper execution, fills, portfolio accounting, risk calculations, persistence,
or a frontend. No real-trading capability or private API credential path
exists. Prompt 2 and later steps must be handled separately.

## Safety boundary

- Supported modes are `backtest` and `paper`; `live` is rejected.
- Every configuration is required to remain `simulation_only = true`.
- Sensitive credential fields and real-order fields are rejected.
- Spot and Perpetual Futures settings cannot be mixed.
- Defaults are historical replay, Spot, one CPU worker, and 1,000 USDT for each
  separate Spot/Futures wallet with conservative futures leverage of 1x.
- Market-data safety starts at `no_data` and blocks new simulated positions
  until validated data is received.
- Stale, delayed, invalid, duplicated, out-of-order, or gapped data keeps the
  position-opening guard closed.
- The Binance connector is opt-in and does not run automatically from the CLI
  or Paper configuration.
- No endpoint for accounts, API keys, or order submission exists.

## Quick start without installing dependencies

Python 3.11 or newer is required. The application and tests use the Python
standard library only:

```bash
PYTHONPATH=src python3 -m trad config/backtest.example.toml
PYTHONPATH=src python3 -m trad config/paper-spot.example.toml --json
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

An optional editable installation is documented in
[`docs/zero-cost-operating-guide.md`](docs/zero-cost-operating-guide.md).

## Configuration examples

- [`config/backtest.example.toml`](config/backtest.example.toml) — historical
  replay declaration with Spot accounting settings.
- [`config/paper-spot.example.toml`](config/paper-spot.example.toml) —
  simulation-only Paper Trading declaration for Spot.
- [`config/paper-perpetual-futures.example.toml`](config/paper-perpetual-futures.example.toml)
  — separate 1x Perpetual Futures declaration.

## Documentation

- [Binance Spot connector](docs/binance-spot-connector.md)
- [Market-data safety](docs/market-data-safety.md)
- [Zero-cost operating guide](docs/zero-cost-operating-guide.md)
- [Dependencies and licenses](docs/dependencies-and-licenses.md)

No paid API, SaaS service, commercial data subscription, cloud resource, GPU,
AI subscription, private trading credential, or real financial order is needed
for this foundation or its local tests. See the dependency document before
proposing any future package or data source.
