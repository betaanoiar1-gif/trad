# Perpetual Futures paper engine

## Scope and safety boundary

`FuturesPaperEngine` is a deterministic, simulation-only perpetual Futures
accounting engine. It has no exchange client, private endpoint, credential
field, live order path, cloud database, or network dependency. Every fill,
mark, and funding rate is supplied explicitly by the caller. It never invents a
price.

The engine is deliberately separate from `SpotPaperEngine`:

- a new Futures engine starts with an independent `1,000.00 USDT` collateral
  wallet;
- Futures balances, reservations, positions, ledger entries, audit events, and
  SQLite state are not read from or written to Spot state; and
- the engine supports one configured contract in one-way net-position mode.
  An opposite-side order is rejected rather than silently hedging or netting a
  different contract.

The implementation uses `Decimal` for financial values and the existing
`MarketDataSafetyMonitor` as a fail-closed gate. The monitor starts at
`no_data`. A caller must first ingest a validated event for the configured
contract, and must recover from stale, delayed, invalid, duplicate,
out-of-order, or gapped data according to
[`docs/market-data-safety.md`](market-data-safety.md).

## Offline example

Run the checked-in deterministic example without installing anything:

```bash
PYTHONPATH=src python3 examples/futures_paper_offline.py
```

It creates a safe candle, opens and fills a long position, marks it at an
explicit price, closes it, and prints the collateral and reconciliation
result. It does not contact Binance or any other service.

## Constructing an engine

```python
from decimal import Decimal
from trad.futures_paper import (
    FuturesFeeConfig,
    FuturesMarginConfig,
    FuturesPaperEngine,
    FuturesRiskLimits,
)

engine = FuturesPaperEngine(
    risk_limits=FuturesRiskLimits(
        default_leverage=Decimal("2"),
        max_leverage=Decimal("5"),
        max_order_notional=Decimal("5000"),
        max_position_notional=Decimal("10000"),
        max_open_orders=4,
    ),
    margin_config=FuturesMarginConfig(
        # None means notional divided by order leverage.
        initial_margin_rate=None,
        maintenance_margin_rate=Decimal("0.05"),
        liquidation_fee_rate=Decimal("0.01"),
    ),
    fee_config=FuturesFeeConfig(rate=Decimal("0.001")),
    database_path="futures-paper.sqlite3",  # omit for isolated in-memory state
)
```

`FuturesContractRules` makes the contract symbol, settlement asset, quantity
and price precision, contract multiplier, and quantity limits explicit. The
default is `BTC/USDT:USDT`, with `USDT` collateral, eight quantity decimals,
two price decimals, and a multiplier of one. Supplying a different valid
contract is possible by constructing a new engine; one engine does not accept
orders for a second contract.

`FuturesPaperEngine.from_run_config()` accepts the validated
`config/paper-perpetual-futures.example.toml` declaration. The existing config
only supplies starting collateral and conservative leverage. Margin, fee,
funding, exposure, and liquidation assumptions are explicit Python policy
objects so they cannot be mistaken for venue rules.

## Market-data safety

```python
from datetime import datetime, timedelta, timezone
from trad.market_data import OHLCV

now = datetime(2026, 1, 1, tzinfo=timezone.utc)
candle = OHLCV(
    symbol="BTC/USDT",
    timestamp=now - timedelta(minutes=1),
    close_time=now,
    received_at=now,
    timeframe_seconds=60,
    open=100.0,
    high=101.0,
    low=99.0,
    close=100.0,
    volume=1.0,
)
health = engine.record_market_data(candle, now=now)
assert health.allow_new_positions
```

`record_market_data()` accepts only validated `MarketData` objects for the
configured contract. `report_invalid_market_data()` is available to adapters
that cannot construct a model; it immediately keeps the gate closed. The
engine checks health before accepting an order, applying a fill, marking a
position, or applying funding. A restart deliberately creates a fresh safety
monitor, so recovered positions require fresh validated data before further
operations.

## Order and fill lifecycle

Orders are explicit-price, deterministic limit-style paper orders. The public
helpers are:

- `open_position(side, quantity, price, leverage=...)` for opening or
  increasing the current net position;
- `reduce_position(side, quantity, price)` for partial reduction or closing;
- `submit_order(...)` for the same operations using explicit action enums;
- `execute_fill(order_id, quantity, price, fill_id=...)` for a deterministic
  execution; and
- `cancel_order(order_id)` for accepted or partially filled orders.

The lifecycle is:

```text
accepted -> partially_filled -> filled
accepted -> cancelled
partially_filled -> cancelled
```

A fill price must equal the price supplied on the order. Quantity and price
precision are checked exactly; values are rejected rather than silently
rounded. An opening fill reserves initial margin plus the estimated fee. A
reduction releases the proportional position margin, realizes P&L, and pays
its fee. The engine does not create a fill from a market quote or send an
order to an exchange.

`client_order_id` makes order submission idempotent: repeating the same
request returns the original order, while reusing the key with different
parameters raises `FuturesDuplicateError`. `fill_id` makes executions
idempotent. Funding uses `payment_id`, and durable event identities prevent a
replayed operation from posting another ledger change.

## Position, margin, P&L, fees, and funding

`FuturesPosition` exposes:

- net `quantity` and `side` (`long` or `short`);
- `entry_price`, `mark_price`, and `leverage`;
- currently allocated isolated `margin` (also exposed as
  `initial_margin`);
- `maintenance_margin`;
- `notional(rules)`, `unrealized_pnl(rules)`, and `equity(rules)` methods; and
- accumulated `realized_pnl` for reductions.

`FuturesFill` records notional, fee, realized P&L, and released margin for each
execution. `FuturesOrder.fee_paid` accumulates fees across partial fills.
Trading fees are collateral-denominated and configurable. The default is
`0.10%`, rounded to the collateral precision with `Decimal` arithmetic.

Funding is caller-supplied and explicit:

```python
payment = engine.apply_funding(
    Decimal("0.0001"),
    payment_id="funding-2026-01-01-00:00",
    now=now,
)
```

A positive rate charges longs and credits shorts. A negative rate reverses
that direction. The payment notional is the current marked position notional;
there is no hidden funding schedule or invented rate. If a charge exceeds
available collateral, the engine uses available isolated position margin only
when that remains within the configured collateral model; otherwise it rejects
the operation.

## Liquidation assumption

Liquidation is a local risk assumption, not an exchange implementation. On an
explicit `mark_to_market(price)`, the engine computes:

```text
equity = current isolated margin + unrealized P&L
liquidate when equity <= maintenance margin
net collateral = margin + unrealized P&L - liquidation fee
```

A liquidation removes the position, records its mark, P&L, fee, collateral
released, and any unmodeled shortfall, and cancels open orders for that
contract while releasing their reservations. It does not claim to reproduce
an exchange's bankruptcy price, insurance fund, partial liquidation, ADL,
mark-price source, or liquidation queue. A caller must supply the mark and is
responsible for choosing a conservative, validated source.

## Persistence, recovery, and reconciliation

Passing `database_path` enables a local SQLite store. The database contains a
version row and explicit tables for configuration, wallet balances, orders,
positions, fills, ledger entries, audit events, funding payments, and
liquidations. It contains no credentials or arbitrary caller objects.

Every state-changing operation writes a complete validated snapshot inside a
`BEGIN IMMEDIATE` transaction. The transaction covers both accounting state
and audit history. A failed write or injected failure rolls back the database;
in-memory state is replaced only after commit. Startup validates the schema
version, configuration, JSON payloads, table identities, cross-table fill
links, wallet asset, ledger initialization, and reconciliation. Newer,
corrupt, incomplete, or inconsistent state fails closed with
`FuturesPersistenceError` rather than being overwritten.

```python
with FuturesPaperEngine(database_path="futures-paper.sqlite3") as engine:
    report = engine.reconcile()
    if not report.is_consistent:
        raise RuntimeError(report.issues)
```

`reconcile()` checks that ledger-derived available and reserved collateral
match the wallet, that reserved collateral equals position margin plus open
order reservations, and that terminal orders do not retain reservations.
`ledger()`, `audit_events()`, `fills()`, `funding_payments()`, and
`liquidations()` expose the local audit history for offline review.

## Configuration and later scope

The checked-in TOML declaration remains simulation-only and keeps the Spot and
Futures sections mutually exclusive. This Section 2 implementation ends at
the local Futures paper engine, persistence, tests, exports, and documentation.
It does not begin strategy orchestration, a frontend, private endpoints, live
trading, exchange order submission, or a new network adapter.
