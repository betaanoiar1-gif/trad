# Zero-cost operating guide

## Scope of this step

Phase 3, Step 2 establishes a local configuration foundation only. It
validates `backtest` and `paper` declarations and enforces simulation-only
operation. It does **not** fetch market data, run a strategy, simulate a fill,
maintain a portfolio, or place an order. Paper execution begins only after a
separate approved implementation step.

## Requirements

- Python 3.11 or newer
- A local CPU; one worker is the default
- No GPU
- No exchange account
- No trading API key or private credential
- No paid service, subscription, cloud server, or hosted database

The repository is intentionally usable without downloading any Python runtime
package. Network access is not used by the Step 2 validation command.

## Validate locally

From the repository root:

```bash
python3 --version
PYTHONPATH=src python3 -m trad config/backtest.example.toml
PYTHONPATH=src python3 -m trad config/paper-spot.example.toml --json
PYTHONPATH=src python3 -m trad config/paper-perpetual-futures.example.toml
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

Run the tests with the standard library test runner:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Modes and safety boundary

| Mode | Meaning in the foundation | Network or order behavior |
| --- | --- | --- |
| `backtest` | Historical/replay declaration | No network access and no real orders |
| `paper` | Intended simulated live declaration | The engine and public-data adapter are not implemented yet |
| `live` | Unsupported | Rejected by configuration validation |

The effective value of `safety.simulation_only` is always `true`; if the field
is supplied, it must also be explicitly `true`. Sensitive credential fields and
real-order fields are rejected instead of ignored.

## Spot and Perpetual Futures separation

Spot settings live under `[spot]` and contain only Spot starting balances.
Perpetual Futures settings live under `[perpetual_futures]` and contain only
collateral and conservative leverage declarations. The validator rejects a
configuration that supplies the inactive instrument's settings. Funding,
margin, maintenance margin, and liquidation rules are intentionally deferred
to the separate Perpetual Futures simulator step.

## Data-access limitations

Step 2 does not select or call an exchange endpoint. The
`public_read_only` value is a declaration reserved for the future public-data
adapter. That adapter must later document endpoint availability, rate limits,
terms of service, timestamp quality, stale-data behavior, and a replayable
local alternative before it is used for Paper Trading.

## Recovery at this stage

If validation fails, do not bypass the error or add credentials. Correct the
TOML file and rerun the validation command. Since this step has no network,
order, or persistent trading state, recovery is simply restoring a known-good
configuration file from version control.
