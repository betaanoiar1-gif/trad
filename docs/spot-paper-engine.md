# Spot paper-trading engine

## Scope and safety boundary

`SpotPaperEngine` is an in-memory, deterministic Spot wallet, order
lifecycle, execution, risk, and ledger component. It never submits an order
to Binance or any other exchange. Binance's public connector remains a
read-only market-data component; it is not an execution transport.

The engine is usable without a network connection. It requires a healthy
`MarketDataSafetyMonitor` state before accepting or filling an order. The
monitor starts in `no_data`, so a caller must first record a validated event or
explicitly report invalid upstream data.

The existing CLI validates TOML declarations only. The paper engine is a
Python API so callers can inject prices, clocks, persistence, and later strategy
or UI integrations without adding a live-trading path.

## Create a simulation

```python
from datetime import datetime, timezone
from decimal import Decimal

from trad.config import load_config
from trad.market_data import OHLCV
from trad.spot_paper import OrderSide, SpotPaperEngine

now = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
config = load_config("config/paper-spot.example.toml")
engine = SpotPaperEngine.from_run_config(config, clock=lambda: now)

# The existing OHLCV model uses validated market-data values.  The engine's
# financial calculations use Decimal values for orders and balances.
candle = OHLCV(
    symbol="BTC/USDT",
    timestamp=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
    close_time=now,
    received_at=now,
    timeframe_seconds=60,
    open=100.0,
    high=101.0,
    low=99.0,
    close=100.0,
    volume=2.0,
)
health = engine.record_market_data(candle, now=now)
assert health.allow_new_positions
```

A new engine defaults to 1,000.00 units of the quote asset and zero base
asset. `SpotPaperEngine.from_run_config` uses the explicit `[spot]` balances
from the existing configuration. A custom constructor can provide
`starting_quote_balance`, `starting_base_balance`, or `initial_balances`.
Spot and Perpetual Futures configurations are rejected or kept in separate
engine instances; balances are never shared.

## Submit, fill, inspect, and cancel

Orders are deterministic limit-style paper orders. Prices are supplied by the
caller; no market price is invented and no exchange request is made.

```python
order = engine.submit_order(
    side=OrderSide.BUY,
    quantity=Decimal("0.01000000"),
    price=Decimal("100.00"),
    client_order_id="example-buy-1",
    now=now,
)

if order.status.value == "accepted":
    fill = engine.execute_fill(
        order.order_id,
        quantity=Decimal("0.01000000"),
        price=Decimal("100.00"),
        fill_id="example-fill-1",
        now=now,
    )
    current_order = engine.order(order.order_id)
    print(fill, current_order.status)

print(engine.balances())
print(engine.fills(order.order_id))
print(engine.ledger())

# An accepted but unfilled or partially filled order releases its remaining
# reservation when cancelled.
pending = engine.submit_order(
    side=OrderSide.BUY,
    quantity=Decimal("0.00500000"),
    price=Decimal("100.00"),
    now=now,
)
engine.cancel_order(pending.order_id, now=now)
```

Risk rejections return an `Order` with `status=REJECTED`, a stable
`rejection_code`, and a human-readable `rejection_reason`. Malformed orders
raise `OrderValidationError`; impossible lifecycle operations raise
`OrderStateError` or `ExecutionError`.

The lifecycle is:

```text
accepted -> partially_filled -> filled
accepted -> cancelled
partially_filled -> cancelled
```

A rejected order is terminal. Filled and cancelled orders are terminal. The
same `client_order_id` with the same parameters returns the original order;
reusing it with different parameters raises `DuplicateOrderError`. A repeated
`fill_id` with identical parameters returns the original fill and posts no
second ledger entry.

## Accounting, fees, and precision

- Balances expose `available`, `reserved`, and `total` quantities. Total is
  always available plus reserved; reservations do not double-count portfolio
  value.
- BUY reserves quote currency and credits base currency on fill. SELL reserves
  base currency and credits quote currency on fill.
- Financial calculations use `Decimal`, not binary floating-point arithmetic.
- Default `BTC/USDT` rules accept 8 base-quantity decimals, 2 price decimals,
  and 2 quote-currency decimals. `SpotSymbolRules` makes these and minimum or
  maximum quantities/notionals explicit and configurable. Extra precision is
  rejected rather than silently truncated.
- The default fee is 0.10% in the quote asset, rounded down to quote precision.
  `FeeConfig` can select quote or base fee currency and `down`, `half_up`, or
  `up` rounding. Fee currency and rounding are recorded on orders and fills.
- BUY reservations include the configured fee when it is charged in quote.
  SELL reservations include the fee when it is charged in base. Unused
  reservations are released after partial fills or cancellation.
- Every initialization, reservation, release, fill, and rejection has an
  immutable `LedgerEntry`. `engine.reconcile()` compares ledger-derived
  available/reserved balances with live balances.

## Risk and market-data integration

`SpotRiskLimits` has no hidden caps. Callers may set:

- minimum or maximum order notional;
- maximum base-position value;
- maximum base exposure as a fraction of fully valued portfolio equity; and
- maximum simultaneous open orders.

The risk gate checks available quote funds for BUY orders, available base
funds for SELL orders, fees, symbol precision, and all configured limits. An
exposure check fails closed if another positive holding has no explicit price.
A stale, delayed, invalid, future, or otherwise unsafe market-data state
returns a `market_data_unsafe` rejection. The execution gate repeats that
safety check at fill time, preventing a later stale state from being used.

The engine never uses a future event: the monitor's `now` is supplied by the
caller or injected clock, and the existing market-data models reject source
timestamps after receipt.

## Portfolio valuation

```python
snapshot = engine.portfolio_snapshot(
    {"BTC": Decimal("100.00")},
    now=now,
)
print(snapshot.total_value, snapshot.unpriced_assets)
```

Prices are explicit asset-to-valuation-asset inputs. The valuation asset is
worth exactly one. A positive holding without a price is listed in
`unpriced_assets` and makes `total_value` equal to `None`; it is never silently
valued at zero. Non-positive, non-finite, or malformed prices raise
`ValuationError`.

## Tests and non-live operation

Run the complete deterministic suite from the repository root:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
```

The engine tests inject clocks and validated in-memory market events. They do
not contact Binance, require API keys, access accounts, or place orders. The
project still does not provide a live trading mode, private account access,
exchange order submission, a strategy, or a frontend. The separate Futures
engine and its local persistence are documented in
[futures-paper-engine.md](futures-paper-engine.md); this document describes
only the completed Spot engine.
