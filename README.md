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

**Step 4 complete: public Binance Spot OHLCV connector.**

The connector uses only the unauthenticated Binance Spot `/api/v3/klines`
endpoint, converts completed candles to the existing `OHLCV` model, validates
UTC timing, exclusive close boundaries, exact response shape, ordering, gaps,
values, bounded responses, HTTP and transport errors, and structured API
errors. It omits the current in-progress candle, rejects future or malformed
data, has deterministic offline coverage, and never accepts private
credentials.

This checkout still does **not** implement ticker/trade/order-book exchange
adapters or strategy execution. No real-trading capability or private API
credential path exists. The public Binance connector remains read-only and
both paper engines never submit exchange orders.

## Section 1 status

**Complete: Spot paper-trading engine.**

`SpotPaperEngine` provides a default 1,000 USDT simulation wallet, Decimal-based
Spot balances, reservations, BUY/SELL accounting, configurable fees, explicit
order and fill lifecycles, an auditable ledger, deterministic execution,
portfolio valuation, and fail-closed risk checks integrated with the existing
market-data safety monitor. See
[`docs/spot-paper-engine.md`](docs/spot-paper-engine.md) for the actual Python
interfaces and policies.

## Section 2 status

**Complete: Perpetual Futures paper engine and local persistence.**

`FuturesPaperEngine` is an isolated, one-contract net-position simulator with a
new 1,000 USDT collateral wallet. It supports deterministic explicit-price
opening, increasing, partial reduction, and closing fills for long and short
positions; Decimal margin, leverage, notional, P&L, fees, funding, exposure,
maintenance-margin liquidation assumptions, audit events, and reconciliation.
A SQLite path enables versioned transactional snapshots, restart recovery,
corruption checks, and rollback-safe durable history. See
[`docs/futures-paper-engine.md`](docs/futures-paper-engine.md) and the
[offline example](examples/futures_paper_offline.py).

The engine is simulation-only. It integrates the existing fail-closed
`MarketDataSafetyMonitor`, never invents a mark or fill price, never calls a
private endpoint, and never submits an exchange order. Its liquidation model
is deliberately explicit and conservative rather than venue-specific.

## Section 3 status

**Complete: local browser dashboard and full engine integration.**

`trad.dashboard` provides a dependency-free Python HTTP server and static
browser dashboard at `http://127.0.0.1:8765/`. The dashboard displays the
separate Spot and Futures wallets, backend-authoritative valuation, positions,
orders, fills, fees, funding, liquidation and audit history, safety state, and
reconciliation. Explicit candle, order, fill, mark, funding, cancel, and safety
reset actions are validated by the existing engines; duplicate financial
requests require the engines' idempotency keys. The Futures SQLite store is
used across dashboard restarts, while the existing Spot engine remains
in-memory as documented.

Start it locally with:

```bash
PYTHONPATH=src python3 -m trad.dashboard
```

The server binds to localhost by default. Use `--host 0.0.0.0` only when a
controlled local preview requires it; this is not a public deployment or an
authenticated financial-control service.

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
PYTHONPATH=src python3 -m trad config/paper-perpetual-futures.example.toml
PYTHONPATH=src python3 examples/futures_paper_offline.py
PYTHONPATH=src python3 -m trad.dashboard --host 127.0.0.1 --port 8765
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
- [Spot paper-trading engine](docs/spot-paper-engine.md)
- [Perpetual Futures paper engine](docs/futures-paper-engine.md)
- [Local paper dashboard](docs/dashboard.md)
- [Market-data safety](docs/market-data-safety.md)
- [Zero-cost operating guide](docs/zero-cost-operating-guide.md)
- [Dependencies and licenses](docs/dependencies-and-licenses.md)

No paid API, SaaS service, commercial data subscription, cloud resource, GPU,
AI subscription, private trading credential, or real financial order is needed
for this foundation or its local tests. See the dependency document before
proposing any future package or data source.
