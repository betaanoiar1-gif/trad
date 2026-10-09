# trad

## Phase 3 status

**Step 3 complete: market-data safety layer.**

Step 2 established the local zero-cost foundation. Step 3 adds validated,
normalized market-data models, deterministic replay support, timestamp and
ordering checks, gap and sequence-gap detection, stale/delayed-data checks,
and a fail-closed guard for opening new simulated positions. Spot and
Perpetual Futures configuration remains separate, with 1,000 USDT in each
separate default wallet.

This checkout still does **not** implement a public exchange adapter, paper
execution engine, fills, portfolio accounting, risk calculations, persistence,
or a frontend. No real-trading capability or private API credential path
exists. Step 4 must be approved before those components are added.

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
- The `paper` examples do not connect to an exchange or submit orders. The
  `public_read_only` value is only a future adapter declaration.

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

- [Market-data safety](docs/market-data-safety.md)
- [Zero-cost operating guide](docs/zero-cost-operating-guide.md)
- [Dependencies and licenses](docs/dependencies-and-licenses.md)

No paid API, SaaS service, commercial data subscription, cloud resource, GPU,
AI subscription, private trading credential, or real financial order is needed
for this foundation. See the dependency document before proposing any future
package or data source.
