# trad

## Phase 3 status

**Step 2 complete: local zero-cost project foundation.**

This checkout is the official starting point for Phase 3. The current step
adds a validated configuration model, explicit non-live run modes, separate
Spot and Perpetual Futures configuration namespaces, a CPU-only resource
setting, a validation CLI, example configurations, and standard-library tests.

It intentionally does **not** implement market-data ingestion, a paper
execution engine, fills, portfolio accounting, risk calculations, persistence,
or a frontend. No real-trading capability or private API credential path exists.
Step 3 must be approved before those components are added.

## Safety boundary

- Supported modes are `backtest` and `paper`; `live` is rejected.
- Every configuration is required to remain `simulation_only = true`.
- Sensitive credential fields and real-order fields are rejected.
- Spot and Perpetual Futures settings cannot be mixed.
- Defaults are historical replay, Spot, one CPU worker, and conservative
  futures leverage of 1x.
- The `paper` examples are declarations validated by Step 2 only; they do not
  connect to an exchange or submit simulated orders yet.

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

- [Zero-cost operating guide](docs/zero-cost-operating-guide.md)
- [Dependencies and licenses](docs/dependencies-and-licenses.md)

No paid API, SaaS service, commercial data subscription, cloud resource, GPU,
AI subscription, private trading credential, or real financial order is needed
for this foundation. See the dependency document before proposing any future
package or data source.
