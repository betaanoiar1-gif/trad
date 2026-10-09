# Strategies, backtests, and autonomous paper runner

## Implemented strategy catalogue

The standard-library registry currently contains eight transparent candle
strategies:

| Slug | Method | Short behavior |
| --- | --- | --- |
| `ema_crossover` | 12/26 EMA crossover | Long or short |
| `rsi_reversion` | 14-period RSI, 30/70 bands | Long or short |
| `macd_crossover` | 12/26 MACD with 9-period signal | Long or short |
| `bollinger_bands` | 20-period, 2-deviation bands | Long or short |
| `breakout` | Prior 20-candle Donchian range | Long or short |
| `momentum` | 20-candle return with a 2% neutral band | Long or short |
| `trend_following` | EMA direction, slope, and price confirmation | Long or short |
| `mean_reversion` | 20-candle close z-score | Long or short |

These are intentionally conventional implementations, not claims that every
variant or every strategy in the world is present. The registry is extensible
through `StrategyRegistry.register`. The code does not currently implement
machine learning, portfolio optimization, multi-timeframe signals, order-book
strategies, or a claim of profitability.

## Backtest contract

`trad.backtest` requires a chronological sequence of completed `OHLCV` candles
with one symbol, one timeframe, no duplicate/out-of-order rows, and no gaps by
default. It splits the data chronologically into training and validation
periods. For every signal, the strategy receives a prefix ending at the
completed signal candle; execution is at the next candle's explicit open.
Therefore the current candle's close and any later candle cannot be used as an
execution price.

The research accounting model includes:

- Decimal initial equity and fees;
- explicit fractional position sizing;
- long/short support for Futures and short blocking for Spot;
- entry and exit fees in net P&L;
- max drawdown halting;
- validation return, fees, drawdown, trades, win rate, and Sharpe-like score;
- recorded risk breaches and strategy exceptions.

Every registered strategy produces a result, including failed or rejected
candidates. The default acceptance gate requires at least one validation
round-trip, a non-negative validation return, validation drawdown within the
configured limit, non-negative training return, and no risk halt. These are
safety gates, not investment advice. The selector ranks only accepted results
by a documented objective score. If none pass, `selected_strategy` is `None`
and the runner is blocked from opening new positions.

Use the API directly in offline research:

```python
from trad.backtest import InstrumentMode, select_strategy
from trad.strategies import StrategyRegistry

selection = select_strategy(
    completed_candles,
    instrument=InstrumentMode.FUTURES,
    registry=StrategyRegistry.default(),
)
```

## Persistent paper runner

`AutonomousPaperRunner` owns a separate SQLite journal. It records market
candles, strategy selection results, decisions and indicators, idempotency
operations, errors, recovery transitions, and a durable Spot accounting
snapshot. The existing Futures engine keeps its own isolated SQLite accounting
database. On startup:

- a previously running runner becomes `paused` and records a recovery event;
- a corrupt state or journal fails closed instead of being overwritten;
- accepted strategy metadata is recovered, but fresh validated market data is
  still required before resuming decisions;
- repeated candles, decisions, order keys, and fill keys do not repeat side effects.

The runner has explicit `start`, `pause`, `resume`, and `stop` transitions.
`run_forever` accepts provider callables and a stop event or finite cycle cap;
it does not claim that an ordinary process is a guaranteed 24/7 service.

For public, read-only Binance Spot and USDⓈ-M Futures OHLCV providers:

```bash
PYTHONPATH=src python3 -m trad.runner \
  --symbol BTC/USDT \
  --interval 1m \
  --journal-db var/trad-runner.sqlite3 \
  --futures-db var/futures-runner.sqlite3
```

Use `--once` or `--max-cycles` for a bounded local check. A recovered
previously-running process remains paused until `--resume` is supplied (or the
Dashboard Resume button is used). The process needs a real supervisor and
persistent host if it is expected to run continuously;
this repository does not provide cloud hosting, a service manager, or a
24/7 availability guarantee. The only network routes used by the CLI are the
public Binance kline endpoints. No credentials, private endpoints, account
queries, or real order endpoints exist.

Futures candles come from the separate public Futures endpoint and are not
relabelled Spot candles. The two connectors share strict completed-candle
parsing but retain separate endpoint identities and engine safety monitors.
