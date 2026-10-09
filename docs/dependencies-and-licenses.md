# Dependencies and licenses

## Section 2 inventory

The completed Spot and Perpetual Futures paper engines have **no third-party
runtime dependencies**. They use Python 3.11 or newer and the standard library
only:

- `tomllib` for TOML parsing
- `dataclasses`, `decimal`, `enum`, `pathlib`, and `typing` for validated
  configuration and Spot/Futures accounting models
- `sqlite3` and `json` for versioned local Futures persistence and recovery
- `http.server`, `threading`, and `importlib.resources` for the local dashboard
- `argparse` for the validation command and dashboard startup
- `unittest` for the test suite

Python and its standard library are distributed under the Python Software
Foundation License; the applicable text is documented by Python at
<https://docs.python.org/3/license.html>.

The project declares `dependencies = []` in `pyproject.toml`. Both paper
engines use `decimal.Decimal` from the standard library for financial
arithmetic, and Futures durability uses the local SQLite library rather than a
hosted database. No AI service, exchange SDK, paid API, hosted database,
SaaS service, or cloud resource is required.

## Optional packaging tool

The `pyproject.toml` build backend names `setuptools>=61` only to support an
optional editable/package installation. It is not imported by the application
at runtime, and direct operation and tests can be run without installing it:

```bash
PYTHONPATH=src python3 -m trad config/backtest.example.toml
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Setuptools is free and open source under the MIT license. Its license text was
reviewed from the upstream repository:
<https://github.com/pypa/setuptools/blob/main/LICENSE>.

## Future dependency policy

Before adding any dependency, the project must record:

1. the exact functionality that cannot reasonably be provided by the standard
   library or a small local module;
2. the package version and license;
3. CPU and offline behavior;
4. whether it is optional or mandatory; and
5. rate-limit, data-license, and terms-of-service implications if it accesses
   public market data.

A dependency that requires payment, a subscription, GPU hardware, private
credentials, or a hosted service will not be made mandatory.

The repository does not yet declare a project-level redistribution license.
That is an owner decision and is separate from the licenses of the tools used
by this foundation.
