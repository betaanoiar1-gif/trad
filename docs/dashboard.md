# Local paper-trading dashboard

## Safety and architecture

The dashboard is a small standard-library application: Python's
`http.server`, a JSON API, and static HTML/CSS/JavaScript. It adds no runtime
package dependency and does not replace either paper engine. The browser is a
view and request form; all balances, reservations, fees, P&L, margin, funding,
liquidation, validation, idempotency, and reconciliation remain authoritative
in Python.

The service owns two separate engine instances:

- Spot uses the existing in-memory `SpotPaperEngine` and its own safety monitor
  and wallet.
- Futures uses the existing `FuturesPaperEngine`, with a local SQLite file for
  durable configuration, accounting snapshots, audit history, recovery, and
  reconciliation.

No route fetches market data, accesses credentials, calls a private endpoint,
or submits an exchange order. The browser never receives a filesystem path or
an exception traceback.

## Install and start

The project supports Python 3.11+ and has no runtime dependencies. From the
repository root, either run directly:

```bash
PYTHONPATH=src python3 -m trad.dashboard
```

or install the local package using the existing standard packaging workflow:

```bash
python3 -m pip install --editable .
trad-dashboard
```

The default URL is:

```text
http://127.0.0.1:8765/
```

The default server bind is localhost and the default durable Futures database
is `var/futures-dashboard.sqlite3`. The `var/` directory and SQLite files are
ignored by Git. To choose another local file or port:

```bash
PYTHONPATH=src python3 -m trad.dashboard \
  --host 127.0.0.1 \
  --port 8765 \
  --futures-db var/my-futures-dashboard.sqlite3
```

A controlled preview may start the process with `--host 0.0.0.0`; that is an
explicit deployment choice, not a claim that the service has authentication
or public-network security. Do not expose a financial-control interface to an
untrusted network.

## Using the dashboard safely

1. Open the local URL and confirm the `PAPER ONLY` warning.
2. Both health badges begin at `NO DATA`. Enter a completed candle in **Record
   explicit market data**. All candle times, prices, and volume are submitted
   explicitly; the page does not query or invent a live price.
3. Confirm that the Spot and Futures badges show `SAFE` before submitting or
   filling an order.
4. Supply a unique client order id for every Spot or Futures submission. A
   retry with the same parameters is idempotent; a retry with changed
   parameters is rejected by the engine.
5. Supply a unique fill id when applying a deterministic explicit-price fill.
6. For Futures, use the mark form only with a deliberate explicit mark. The
   backend may apply its documented conservative liquidation rule. The page
   asks for confirmation before this action.
7. Apply funding only with a caller-supplied rate and unique payment id.
8. Inspect reconciliation and history after consequential operations.

The reset button requires confirmation and returns both safety monitors to
`NO DATA`. Fresh validated market data is required again; reset does not make
an unsafe source safe.

The dashboard supports the existing safe engine actions only:

- Spot: submit, explicitly fill, and cancel accepted/partially filled orders;
- Futures: open/increase, reduce/close, explicitly fill, and cancel orders;
  apply an explicit mark; and apply an explicit funding payment; and
- both domains: record a validated candle and inspect health, balances,
  reconciliation, and history.

The page disables a repeated submission while a request is in flight and the
API requires idempotency keys for order, fill, and funding requests. Backend
confirmation is required before the page reports success. Rejected orders are
shown as rejected operations rather than successes.

## API surface

The JSON API is local and relative to the dashboard origin:

| Method and route | Purpose |
| --- | --- |
| `GET /api/state` | Complete browser-safe Spot/Futures snapshot |
| `GET /api/health` | Simulation warning and both market-data health states |
| `POST /api/market-data` | Validate and record one explicit completed OHLCV candle |
| `POST /api/market-data/reset` | Reset both safety monitors with `{"confirm":true}` |
| `POST /api/spot/orders` | Submit a Spot order with a client id |
| `POST /api/spot/orders/{id}/fill` | Apply a Spot explicit-price fill with a fill id |
| `POST /api/spot/orders/{id}/cancel` | Cancel a Spot accepted or partial order |
| `POST /api/futures/orders` | Open/increase or reduce/close a Futures order |
| `POST /api/futures/orders/{id}/fill` | Apply a Futures explicit-price fill with a fill id |
| `POST /api/futures/orders/{id}/cancel` | Cancel a Futures accepted or partial order |
| `POST /api/futures/mark` | Apply an explicit Futures mark and report liquidation |
| `POST /api/futures/funding` | Apply a caller-supplied funding rate and payment id |

Invalid JSON, missing fields, unsupported operations, unsafe data, risk
rejections, and engine failures receive JSON error responses. Tracebacks,
local paths, and persistence implementation details are not returned to the
browser. A rejected order is returned as HTTP `422` with its stable engine
reason and current order record.

## Persistence and limitations

Futures state is loaded from the configured SQLite path on process startup.
The existing Futures engine validates the schema, configuration, JSON payloads,
relationships, ledger, and reconciliation before making the recovered state
available. A fresh process deliberately has no trusted market-data monitor
state, so a recovered position is visible but new operations remain blocked
until a fresh validated event is recorded.

Spot persistence was not added to Section 3: the existing Spot engine is
in-memory and the dashboard labels it that way. Spot orders, fills, and
balances therefore reset with the dashboard process. This is intentional and
avoids inventing a second accounting or storage implementation.

The browser is not a strategy runner, live trading terminal, performance
promise, or exchange simulator. It does not model network latency, order-book
matching, slippage, real venue funding schedules, exchange liquidation queues,
private account state, or real order execution.

## Troubleshooting and verification

- `NO DATA` or `STALE`: record a fresh completed candle with explicit times;
  do not work around the monitor.
- `DELAYED`, `DUPLICATE`, `OUT OF ORDER`, or `GAP`: follow the market-data
  monitor recovery rule, reset after verifying the source, then record fresh
  data.
- `422 order_rejected`: read the stable engine reason in the order table and
  correct the explicit input or safety state.
- `503 persistence_unavailable`: stop the process and inspect the local SQLite
  store using the Futures persistence/recovery documentation; do not delete it
  to hide an inconsistency.
- Port already in use: choose another local port with `--port` and use the
  displayed URL.

Run all deterministic tests and checks from the repository root:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
PYTHONPATH=src python3 examples/futures_paper_offline.py
```
