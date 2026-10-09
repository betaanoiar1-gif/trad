# Dependencies and licenses

## Step 2 inventory

The Step 2 foundation has **no third-party runtime dependencies**. It uses
Python 3.11 or newer and the standard library only:

- `tomllib` for TOML parsing
- `dataclasses`, `enum`, `pathlib`, and `typing` for the configuration model
- `argparse` and `json` for the validation command
- `unittest` for the test suite

Python and its standard library are distributed under the Python Software
Foundation License; the applicable text is documented by Python at
<https://docs.python.org/3/license.html>.

The project declares `dependencies = []` in `pyproject.toml`. No AI service,
exchange SDK, paid API, hosted database, SaaS service, or cloud resource is
required.

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
