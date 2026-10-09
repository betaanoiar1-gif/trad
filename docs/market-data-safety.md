# Market-data safety layer

## Scope

Phase 3, Step 3 adds a local safety layer only. It normalizes and validates
market-data objects and gives future Paper Trading code a fail-closed decision
for opening new simulated positions. It does not fetch from an exchange, use
private credentials, submit orders, simulate fills, or maintain an account.

## Normalized models

All supported events contain:

- `symbol`
- a timezone-aware source/exchange time
- a timezone-aware local `received_at` timestamp

All timestamps are normalized to UTC. A source time after local receipt is
rejected. The models are immutable after validation.

### OHLCV time semantics

For `OHLCV`:

- `timestamp` and its explicit read-only alias `open_time` are the candle
  opening time.
- `close_time` is the exclusive completion boundary and must equal
  `open_time + timeframe_seconds`.
- `received_at` is the local time at which the completed candle arrived.
- `received_at < close_time` is rejected as an incomplete candle.
- source lag is `received_at - close_time`, not `received_at - open_time`.
- staleness is `now - close_time`, not `now - open_time`.
- ordering and OHLCV gap detection still use `open_time`.

This prevents the one-minute or five-minute candle construction interval from
being incorrectly counted as delivery lag or staleness.

The supported models are:

- `OHLCV`: timeframe, open, high, low, close, and non-negative volume.
- `Ticker`: last price and optional paired best bid/ask values and sizes.
- `Trade`: price, positive quantity, normalized buy/sell side, and optional
  trade identifier.
- `BidAsk`: positive bid/ask prices and quote sizes.
- `OrderBook`: sorted, non-crossed bid and ask levels with an optional
  sequence number.

Invalid, non-finite, non-positive, crossed, unsorted, or inconsistent values
raise `DataValidationError` before they reach the safety monitor.

## Safety policy and assumptions

`MarketDataPolicy` defaults are intentionally conservative:

| Check | Default behavior |
| --- | --- |
| Maximum freshness age after completion | 30 seconds |
| Maximum source-to-receipt/completion lag | 10 seconds |
| OHLCV gap interval | Each candle's `timeframe_seconds` using opening time |
| Other stream gaps | Only when an expected interval is explicitly configured |
| Order-book sequence | If supplied, sequence numbers are assumed contiguous |
| Required streams | None by default; callers may require specific kinds |

The OHLCV timestamp is treated as the candle opening timestamp. Ticker and
trade streams are naturally irregular; the monitor does not invent a cadence
for them. An order-book sequence jump is unsafe because the layer models a
contiguous update stream; a snapshot/reconnect boundary must be explicitly
reset before new data is accepted.

## Fail-closed behavior

The monitor begins in `no_data`, so the position-opening decision is false.
The following states block new simulated positions:

- `stale`
- `delayed`
- `invalid`
- `duplicate`
- `out_of_order`
- `gap`
- `sequence_gap`
- `no_data`

The monitor stores `last_ordering_time` separately from
`latest_freshness_time`. For OHLCV, the former is `open_time` and the latter is
`close_time`; for other event types both are based on the event timestamp.
`DataHealth.latest_freshness_time` exposes the freshness time (with
`latest_timestamp` retained as a compatibility alias).

`MarketDataSafetyMonitor.can_open_new_positions(now)` is the boolean guard.
Future Paper Trading code must call it before opening a new simulated position;
`require_safe_for_new_position(now)` is the hard-guard variant that raises
`DataSafetyError` when the decision is unsafe. A future adapter that cannot
construct a model must call `report_invalid_data(...)`, which blocks the
monitor immediately instead of allowing the previous quote to remain trusted.
Existing positions are not closed or modified by this layer; it only prevents
new openings.

After a data-integrity failure, the monitor remains blocked. A caller must
confirm the source/replay recovery boundary, call `reset()`, and ingest fresh
validated events. `reset()` itself returns the monitor to `no_data`; it never
makes data safe without new events.

## Deterministic replay

`DeterministicReplay` accepts an in-memory sequence of normalized events and
feeds them to a monitor in the supplied order. It uses no randomness, network,
exchange SDK, or clock unless a deterministic clock callback is supplied. The
unit tests use it to exercise contiguous candles and to reproduce identical
health outcomes.

## Not implemented in Step 3

- HTTP/WebSocket exchange adapters
- reconnect/backoff workers
- private endpoints or API secrets
- order submission or order simulation
- Spot or Perpetual Futures accounting
- persistence, UI, or live Paper Trading orchestration

Those items remain separate approval-gated steps.
