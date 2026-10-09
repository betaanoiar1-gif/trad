"""Leak-resistant chronological backtesting and objective strategy selection.

The backtester consumes already validated completed candles.  It never asks a
strategy for a signal with candles after the signal candle and executes the
signal at the next candle's open.  Fees, position sizing, drawdown controls,
short support, and result metrics are all calculated in this backend module.

Backtests are research evidence only.  A paper runner may trade only a
strategy that passed the validation acceptance criteria; failed strategies and
exceptions remain part of the persisted result set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
import math
from statistics import mean, pstdev
from typing import Any, Iterable, Sequence

from .market_data import OHLCV
from .strategies import StrategyDefinition, StrategyRegistry


class BacktestError(ValueError):
    """Raised when historical data or backtest settings are invalid."""


class InstrumentMode(str, Enum):
    """Accounting behavior used by the research simulator."""

    SPOT = "spot"
    FUTURES = "futures"


@dataclass(frozen=True)
class BacktestConfig:
    """Conservative, explicit simulation settings."""

    initial_equity: Decimal = Decimal("1000.00")
    fee_rate: Decimal = Decimal("0.001")
    position_fraction: Decimal = Decimal("0.25")
    train_fraction: float = 0.70
    max_drawdown: Decimal = Decimal("0.30")
    min_validation_trades: int = 1
    min_validation_return: Decimal = Decimal("0")
    max_validation_drawdown: Decimal = Decimal("0.30")
    require_train_non_negative: bool = True
    allow_short: bool = True

    def __post_init__(self) -> None:
        initial = _positive_decimal(self.initial_equity, "initial_equity")
        fee = _non_negative_decimal(self.fee_rate, "fee_rate")
        fraction = _positive_decimal(self.position_fraction, "position_fraction")
        train = float(self.train_fraction)
        drawdown = _non_negative_decimal(self.max_drawdown, "max_drawdown")
        validation_drawdown = _non_negative_decimal(self.max_validation_drawdown, "max_validation_drawdown")
        if fee >= Decimal("1"):
            raise BacktestError("fee_rate must be below 1")
        if fraction > Decimal("1"):
            raise BacktestError("position_fraction cannot exceed 1")
        if not math.isfinite(train) or not 0.5 <= train < 1.0:
            raise BacktestError("train_fraction must be at least 0.5 and below 1")
        if drawdown >= Decimal("1") or validation_drawdown >= Decimal("1"):
            raise BacktestError("drawdown limits must be below 1")
        if isinstance(self.min_validation_trades, bool) or self.min_validation_trades < 1:
            raise BacktestError("min_validation_trades must be positive")
        if not isinstance(self.require_train_non_negative, bool) or not isinstance(self.allow_short, bool):
            raise BacktestError("boolean backtest settings must be booleans")
        object.__setattr__(self, "initial_equity", initial)
        object.__setattr__(self, "fee_rate", fee)
        object.__setattr__(self, "position_fraction", fraction)
        object.__setattr__(self, "train_fraction", train)
        object.__setattr__(self, "max_drawdown", drawdown)
        object.__setattr__(self, "min_validation_return", _decimal(self.min_validation_return, "min_validation_return"))
        object.__setattr__(self, "max_validation_drawdown", validation_drawdown)


@dataclass(frozen=True)
class BacktestTrade:
    """One completed round-trip in the research simulator."""

    side: int
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    gross_pnl: Decimal
    entry_fee: Decimal
    exit_fee: Decimal
    net_pnl: Decimal
    bars_held: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "entry_time": self.entry_time.isoformat(),
            "exit_time": self.exit_time.isoformat(),
            "entry_price": str(self.entry_price),
            "exit_price": str(self.exit_price),
            "quantity": str(self.quantity),
            "gross_pnl": str(self.gross_pnl),
            "entry_fee": str(self.entry_fee),
            "exit_fee": str(self.exit_fee),
            "net_pnl": str(self.net_pnl),
            "bars_held": self.bars_held,
        }


@dataclass(frozen=True)
class BacktestMetrics:
    """Comparable performance metrics for one chronological period."""

    start_index: int
    end_index: int
    candles: int
    starting_equity: Decimal
    ending_equity: Decimal
    net_pnl: Decimal
    return_pct: Decimal
    total_fees: Decimal
    max_drawdown: Decimal
    sharpe: float
    trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    signal_count: int
    risk_breaches: int
    halted: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_index": self.start_index,
            "end_index": self.end_index,
            "candles": self.candles,
            "starting_equity": str(self.starting_equity),
            "ending_equity": str(self.ending_equity),
            "net_pnl": str(self.net_pnl),
            "return_pct": str(self.return_pct),
            "total_fees": str(self.total_fees),
            "max_drawdown": str(self.max_drawdown),
            "sharpe": self.sharpe,
            "trades": self.trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": self.win_rate,
            "signal_count": self.signal_count,
            "risk_breaches": self.risk_breaches,
            "halted": self.halted,
        }


@dataclass(frozen=True)
class BacktestResult:
    """Full auditable result, including failures and train/validation metrics."""

    strategy_name: str
    strategy_display_name: str
    instrument: InstrumentMode
    symbol: str
    total_candles: int
    split_index: int
    status: str
    accepted: bool
    train: BacktestMetrics | None
    validation: BacktestMetrics | None
    trades: tuple[BacktestTrade, ...] = ()
    failure_reasons: tuple[str, ...] = ()
    error: str | None = None
    objective_score: float | None = None
    causal: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "strategy_display_name": self.strategy_display_name,
            "instrument": self.instrument.value,
            "symbol": self.symbol,
            "total_candles": self.total_candles,
            "split_index": self.split_index,
            "status": self.status,
            "accepted": self.accepted,
            "train": None if self.train is None else self.train.as_dict(),
            "validation": None if self.validation is None else self.validation.as_dict(),
            "trades": [trade.as_dict() for trade in self.trades],
            "failure_reasons": list(self.failure_reasons),
            "error": self.error,
            "objective_score": self.objective_score,
            "causal": self.causal,
        }


@dataclass(frozen=True)
class SelectionResult:
    """All strategy outcomes and the one strategy allowed to run, if any."""

    instrument: InstrumentMode
    symbol: str
    results: tuple[BacktestResult, ...]
    selected_strategy: str | None
    reason: str
    created_at: datetime

    @property
    def accepted(self) -> bool:
        return self.selected_strategy is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument.value,
            "symbol": self.symbol,
            "results": [result.as_dict() for result in self.results],
            "selected_strategy": self.selected_strategy,
            "reason": self.reason,
            "created_at": self.created_at.isoformat(),
            "accepted": self.accepted,
        }


def validate_historical_candles(
    candles: Iterable[OHLCV],
    *,
    expected_symbol: str | None = None,
    require_contiguous: bool = True,
    minimum: int = 40,
) -> tuple[OHLCV, ...]:
    """Validate an oldest-to-newest completed candle sequence."""

    sequence = tuple(candles)
    if len(sequence) < minimum:
        raise BacktestError(f"at least {minimum} completed candles are required")
    if not all(isinstance(candle, OHLCV) for candle in sequence):
        raise BacktestError("historical data must contain OHLCV objects")
    symbol = sequence[0].symbol
    timeframe = sequence[0].timeframe_seconds
    if expected_symbol is not None and symbol != expected_symbol:
        raise BacktestError(f"historical symbol {symbol} does not match {expected_symbol}")
    for index, candle in enumerate(sequence):
        if candle.symbol != symbol or candle.timeframe_seconds != timeframe:
            raise BacktestError("historical candles must use one symbol and timeframe")
        if index == 0:
            continue
        previous = sequence[index - 1]
        if candle.timestamp <= previous.timestamp:
            raise BacktestError("historical candles must be strictly time ordered")
        if require_contiguous and candle.timestamp != previous.timestamp.replace() + _seconds(candle.timeframe_seconds):
            raise BacktestError("historical candles contain a timestamp gap")
    return sequence


def select_strategy(
    candles: Sequence[OHLCV],
    *,
    instrument: InstrumentMode = InstrumentMode.FUTURES,
    registry: StrategyRegistry | None = None,
    config: BacktestConfig | None = None,
    symbol: str | None = None,
    now: datetime | None = None,
) -> SelectionResult:
    """Evaluate every registered strategy and objectively choose one.

    A strategy is selected only when its validation period meets all acceptance
    rules.  If every candidate fails, ``selected_strategy`` is ``None`` and a
    runner must remain blocked rather than opening a new position.
    """

    instrument = _instrument(instrument)
    settings = config or BacktestConfig(allow_short=instrument is InstrumentMode.FUTURES)
    history = validate_historical_candles(candles, expected_symbol=symbol, minimum=max(40, settings.min_validation_trades + 2))
    registry = registry or StrategyRegistry.default()
    results: list[BacktestResult] = []
    for definition in registry.all():
        results.append(_evaluate_definition(definition, history, instrument, settings))
    accepted = [result for result in results if result.accepted and result.objective_score is not None]
    accepted.sort(key=lambda result: (-float(result.objective_score), result.strategy_name))
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not accepted:
        return SelectionResult(
            instrument=instrument,
            symbol=history[0].symbol,
            results=tuple(results),
            selected_strategy=None,
            reason="no strategy passed the independent validation acceptance criteria; new paper positions are blocked",
            created_at=timestamp,
        )
    winner = accepted[0]
    return SelectionResult(
        instrument=instrument,
        symbol=history[0].symbol,
        results=tuple(results),
        selected_strategy=winner.strategy_name,
        reason=(
            f"selected {winner.strategy_name} by validation objective score "
            f"{winner.objective_score:.6f}; all candidates remain recorded"
        ),
        created_at=timestamp,
    )


def evaluate_strategy(
    definition: StrategyDefinition,
    candles: Sequence[OHLCV],
    *,
    instrument: InstrumentMode = InstrumentMode.FUTURES,
    config: BacktestConfig | None = None,
    symbol: str | None = None,
) -> BacktestResult:
    """Evaluate one strategy and return a failed result instead of hiding errors."""

    instrument = _instrument(instrument)
    settings = config or BacktestConfig(allow_short=instrument is InstrumentMode.FUTURES)
    history = validate_historical_candles(candles, expected_symbol=symbol, minimum=40)
    return _evaluate_definition(definition, history, instrument, settings)


def _evaluate_definition(
    definition: StrategyDefinition,
    history: tuple[OHLCV, ...],
    instrument: InstrumentMode,
    settings: BacktestConfig,
) -> BacktestResult:
    split = _split_index(len(history), settings.train_fraction)
    if split <= definition.required_bars or len(history) - split < 3:
        return BacktestResult(
            definition.name,
            definition.display_name,
            instrument,
            history[0].symbol,
            len(history),
            split,
            "failed",
            False,
            None,
            None,
            failure_reasons=("insufficient candles for the required train/validation split",),
        )
    try:
        train_run = _run_period(definition, history, 0, split, instrument, settings)
        validation_start = max(definition.required_bars - 1, split - 1)
        validation_run = _run_period(definition, history, validation_start, len(history), instrument, settings)
        train = train_run.metrics
        validation = validation_run.metrics
    except Exception as exc:  # Every candidate failure is a recorded result.
        return BacktestResult(
            definition.name,
            definition.display_name,
            instrument,
            history[0].symbol,
            len(history),
            split,
            "error",
            False,
            None,
            None,
            failure_reasons=("strategy evaluation raised an exception",),
            error=f"{type(exc).__name__}: {exc}",
        )
    failures: list[str] = []
    if validation.trades < settings.min_validation_trades:
        failures.append(f"validation trades {validation.trades} < required {settings.min_validation_trades}")
    if validation.return_pct < settings.min_validation_return:
        failures.append("validation return is below the acceptance floor")
    if validation.max_drawdown > settings.max_validation_drawdown:
        failures.append("validation drawdown exceeds the acceptance limit")
    if settings.require_train_non_negative and train.return_pct < 0:
        failures.append("training return is negative")
    if validation.halted:
        failures.append("validation was halted by a risk limit")
    score = None if failures else _objective_score(train, validation)
    return BacktestResult(
        definition.name,
        definition.display_name,
        instrument,
        history[0].symbol,
        len(history),
        split,
        "accepted" if not failures else "rejected",
        not failures,
        train,
        validation,
        trades=validation_run.trades,
        failure_reasons=tuple(failures),
        objective_score=score,
    )


@dataclass
class _Position:
    side: int
    entry_time: datetime
    entry_price: Decimal
    quantity: Decimal
    entry_fee: Decimal
    entry_index: int


@dataclass
class _PeriodState:
    cash: Decimal
    peak: Decimal
    equity: Decimal
    fees: Decimal = Decimal("0")
    signals: int = 0
    risk_breaches: int = 0
    halted: bool = False
    side: int = 0
    position: _Position | None = None
    trades: list[BacktestTrade] = field(default_factory=list)
    curve: list[Decimal] = field(default_factory=list)
    returns: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class _PeriodRun:
    metrics: BacktestMetrics
    trades: tuple[BacktestTrade, ...]


def _run_period(
    definition: StrategyDefinition,
    history: tuple[OHLCV, ...],
    start: int,
    end: int,
    instrument: InstrumentMode,
    settings: BacktestConfig,
) -> _PeriodRun:
    state = _PeriodState(settings.initial_equity, settings.initial_equity, settings.initial_equity)
    first_execution = max(definition.required_bars, start + 1)
    for execution_index in range(first_execution, end):
        signal_index = execution_index - 1
        prefix = history[: signal_index + 1]
        signal = definition.evaluate(prefix)
        state.signals += 1
        target = signal.action.target
        if target is None:
            target = state.side
        if instrument is InstrumentMode.SPOT and target < 0:
            target = 0
        if state.halted:
            target = 0
        price = _price(history[execution_index].open, "next candle open")
        timestamp = history[execution_index].timestamp
        if target != state.side:
            _close_position(state, timestamp, price, execution_index, settings)
            state.equity = state.cash
            if target != 0 and not state.halted:
                _open_position(state, target, timestamp, price, execution_index, settings)
        mark = _mark_equity(state, price)
        state.equity = mark
        state.peak = max(state.peak, mark)
        drawdown = _drawdown(state.peak, mark)
        if drawdown > settings.max_drawdown:
            state.risk_breaches += 1
            state.halted = True
            _close_position(state, timestamp, price, execution_index, settings)
            state.equity = state.cash
            mark = state.equity
        state.curve.append(mark)
        if len(state.curve) > 1 and state.curve[-2] > 0:
            state.returns.append(float(mark / state.curve[-2] - Decimal("1")))
    final_index = max(first_execution, end - 1)
    if state.position is not None and final_index < len(history):
        final_candle = history[final_index]
        _close_position(state, final_candle.close_time or final_candle.timestamp, _price(final_candle.close, "final close"), final_index, settings)
        state.equity = state.cash
        if not state.curve or state.curve[-1] != state.equity:
            state.curve.append(state.equity)
    starting = settings.initial_equity
    ending = state.equity
    net = ending - starting
    return_pct = _quantized_ratio(net, starting)
    max_dd = _max_drawdown(state.curve or [starting])
    winning = sum(1 for trade in state.trades if trade.net_pnl > 0)
    losing = sum(1 for trade in state.trades if trade.net_pnl < 0)
    trades = len(state.trades)
    metrics = BacktestMetrics(
        start_index=start,
        end_index=end,
        candles=max(0, end - start),
        starting_equity=starting,
        ending_equity=ending,
        net_pnl=net,
        return_pct=return_pct,
        total_fees=state.fees,
        max_drawdown=max_dd,
        sharpe=_sharpe(state.returns),
        trades=trades,
        winning_trades=winning,
        losing_trades=losing,
        win_rate=winning / trades if trades else 0.0,
        signal_count=state.signals,
        risk_breaches=state.risk_breaches,
        halted=state.halted,
    )
    return _PeriodRun(metrics=metrics, trades=tuple(state.trades))


def _open_position(state: _PeriodState, side: int, timestamp: datetime, price: Decimal, index: int, settings: BacktestConfig) -> None:
    notional = _quantize(state.equity * settings.position_fraction, Decimal("0.00000001"))
    if notional <= 0:
        state.risk_breaches += 1
        state.halted = True
        return
    quantity = _quantize(notional / price, Decimal("0.00000001"))
    fee = _quantize(notional * settings.fee_rate, Decimal("0.00000001"))
    if quantity <= 0 or state.cash <= fee:
        state.risk_breaches += 1
        state.halted = True
        return
    state.cash -= fee
    state.fees += fee
    state.position = _Position(side, timestamp, price, quantity, fee, index)
    state.side = side


def _close_position(state: _PeriodState, timestamp: datetime, price: Decimal, index: int, settings: BacktestConfig) -> None:
    position = state.position
    if position is None:
        state.side = 0
        return
    gross = (price - position.entry_price) * position.quantity * position.side
    exit_fee = _quantize(abs(price * position.quantity) * settings.fee_rate, Decimal("0.00000001"))
    state.cash += gross - exit_fee
    state.fees += exit_fee
    state.trades.append(
        BacktestTrade(
            side=position.side,
            entry_time=position.entry_time,
            exit_time=timestamp,
            entry_price=position.entry_price,
            exit_price=price,
            quantity=position.quantity,
            gross_pnl=_quantize(gross, Decimal("0.00000001")),
            entry_fee=position.entry_fee,
            exit_fee=exit_fee,
            net_pnl=_quantize(gross - position.entry_fee - exit_fee, Decimal("0.00000001")),
            bars_held=max(1, index - position.entry_index),
        )
    )
    state.position = None
    state.side = 0


def _mark_equity(state: _PeriodState, price: Decimal) -> Decimal:
    if state.position is None:
        return state.cash
    return state.cash + (price - state.position.entry_price) * state.position.quantity * state.position.side


def _objective_score(train: BacktestMetrics, validation: BacktestMetrics) -> float:
    return (
        float(validation.return_pct) * 100.0
        - float(validation.max_drawdown) * 60.0
        + validation.sharpe * 0.5
        + min(validation.win_rate, 1.0) * 0.25
        + min(float(train.return_pct), 1.0) * 10.0
    )


def _instrument(value: InstrumentMode | str) -> InstrumentMode:
    try:
        return value if isinstance(value, InstrumentMode) else InstrumentMode(value)
    except (TypeError, ValueError) as exc:
        raise BacktestError("instrument must be spot or futures") from exc


def _split_index(length: int, fraction: float) -> int:
    split = int(length * fraction)
    return min(max(split, 1), length - 1)


def _seconds(value: int):
    from datetime import timedelta

    return timedelta(seconds=value)


def _positive_decimal(value: Any, field_name: str) -> Decimal:
    converted = _decimal(value, field_name)
    if converted <= 0:
        raise BacktestError(f"{field_name} must be positive")
    return converted


def _non_negative_decimal(value: Any, field_name: str) -> Decimal:
    converted = _decimal(value, field_name)
    if converted < 0:
        raise BacktestError(f"{field_name} must be non-negative")
    return converted


def _decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise BacktestError(f"{field_name} must be a finite decimal")
    try:
        converted = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BacktestError(f"{field_name} must be a finite decimal") from exc
    if not converted.is_finite():
        raise BacktestError(f"{field_name} must be a finite decimal")
    return converted


def _price(value: Any, field_name: str) -> Decimal:
    converted = _positive_decimal(value, field_name)
    return converted


def _quantize(value: Decimal, quantum: Decimal) -> Decimal:
    try:
        return value.quantize(quantum, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise BacktestError("backtest arithmetic exceeded Decimal range") from exc


def _quantized_ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    return _quantize(numerator / denominator, Decimal("0.00000001"))


def _drawdown(peak: Decimal, value: Decimal) -> Decimal:
    if peak <= 0:
        return Decimal("1")
    return max(Decimal("0"), (peak - value) / peak)


def _max_drawdown(curve: Sequence[Decimal]) -> Decimal:
    peak = curve[0]
    maximum = Decimal("0")
    for value in curve:
        peak = max(peak, value)
        maximum = max(maximum, _drawdown(peak, value))
    return _quantize(maximum, Decimal("0.00000001"))


def _sharpe(returns: Sequence[float]) -> float:
    if len(returns) < 2:
        return 0.0
    deviation = pstdev(returns)
    if deviation == 0:
        return 0.0 if mean(returns) == 0 else (10.0 if mean(returns) > 0 else -10.0)
    return max(-100.0, min(100.0, mean(returns) / deviation * math.sqrt(len(returns))))
