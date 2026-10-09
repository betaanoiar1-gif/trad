"""Deterministic, causal strategy registry for historical paper research.

The strategies in this module are deliberately small and transparent.  A
strategy receives a prefix of the candle history only; the backtest runner
executes a signal on the *next* candle open.  This makes accidental use of
future candles difficult and keeps strategy research separate from accounting.

This is a research catalogue, not a claim that these strategies are profitable.
Every strategy result is evaluated, including failures, before a runner may
select one for paper execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import math
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Sequence

from .market_data import OHLCV


class StrategyError(ValueError):
    """Raised when a strategy definition or history is invalid."""


class SignalAction(str, Enum):
    """Target position requested by a strategy at a completed candle."""

    HOLD = "hold"
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"

    @property
    def target(self) -> int | None:
        if self is SignalAction.HOLD:
            return None
        if self is SignalAction.LONG:
            return 1
        if self is SignalAction.SHORT:
            return -1
        return 0


@dataclass(frozen=True)
class StrategySignal:
    """A causal strategy decision for one completed candle."""

    strategy_name: str
    action: SignalAction
    timestamp: datetime
    reason: str
    confidence: float = 0.0
    indicators: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.strategy_name, str) or not self.strategy_name.strip():
            raise StrategyError("strategy_name must be non-empty")
        if not isinstance(self.timestamp, datetime) or self.timestamp.tzinfo is None:
            raise StrategyError("signal timestamp must be timezone-aware")
        try:
            action = SignalAction(self.action)
        except (TypeError, ValueError) as exc:
            raise StrategyError("signal action is invalid") from exc
        confidence = float(self.confidence)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise StrategyError("signal confidence must be between 0 and 1")
        normalized: dict[str, float] = {}
        for name, value in dict(self.indicators).items():
            converted = float(value)
            if not math.isfinite(converted):
                raise StrategyError(f"indicator {name!r} must be finite")
            normalized[str(name)] = converted
        object.__setattr__(self, "strategy_name", self.strategy_name.strip())
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "timestamp", self.timestamp.astimezone(timezone.utc))
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "indicators", MappingProxyType(normalized))


@dataclass(frozen=True)
class StrategyContext:
    """Only the historical prefix available to one strategy invocation."""

    candles: tuple[OHLCV, ...]
    index: int
    strategy_name: str

    def __post_init__(self) -> None:
        if not self.candles:
            raise StrategyError("strategy context requires at least one candle")
        if isinstance(self.index, bool) or not 0 <= self.index < len(self.candles):
            raise StrategyError("strategy context index is outside its candle prefix")
        if len(self.candles) != self.index + 1:
            raise StrategyError("strategy context must contain candles through index only")
        if not self.strategy_name.strip():
            raise StrategyError("strategy context strategy_name must be non-empty")

    @property
    def current(self) -> OHLCV:
        return self.candles[-1]

    @property
    def previous(self) -> OHLCV | None:
        return self.candles[-2] if len(self.candles) > 1 else None

    @property
    def closes(self) -> tuple[float, ...]:
        return tuple(candle.close for candle in self.candles)

    @property
    def highs(self) -> tuple[float, ...]:
        return tuple(candle.high for candle in self.candles)

    @property
    def lows(self) -> tuple[float, ...]:
        return tuple(candle.low for candle in self.candles)


StrategyFunction = Callable[[StrategyContext], StrategySignal]


@dataclass(frozen=True)
class StrategyDefinition:
    """Registered strategy metadata and causal signal function."""

    name: str
    display_name: str
    description: str
    required_bars: int
    signal: StrategyFunction
    supports_short: bool = True

    def __post_init__(self) -> None:
        name = self.name.strip().lower()
        if not name or any(character.isspace() for character in name):
            raise StrategyError("strategy name must be a non-empty slug")
        if not isinstance(self.required_bars, int) or isinstance(self.required_bars, bool) or self.required_bars < 2:
            raise StrategyError("strategy required_bars must be at least 2")
        if not callable(self.signal):
            raise StrategyError("strategy signal must be callable")
        object.__setattr__(self, "name", name)

    def evaluate(self, candles: Sequence[OHLCV], index: int | None = None) -> StrategySignal:
        """Evaluate against a prefix, never silently passing future candles."""

        prefix = tuple(candles if index is None else candles[: index + 1])
        if len(prefix) < self.required_bars:
            raise StrategyError(
                f"{self.name} requires {self.required_bars} candles; received {len(prefix)}"
            )
        context = StrategyContext(prefix, len(prefix) - 1, self.name)
        signal = self.signal(context)
        if signal.strategy_name != self.name:
            signal = StrategySignal(
                strategy_name=self.name,
                action=signal.action,
                timestamp=signal.timestamp,
                reason=signal.reason,
                confidence=signal.confidence,
                indicators=signal.indicators,
            )
        return signal


class StrategyRegistry:
    """Extensible registry of named, deterministic strategies."""

    def __init__(self, definitions: Iterable[StrategyDefinition] = ()) -> None:
        self._definitions: dict[str, StrategyDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: StrategyDefinition) -> None:
        if definition.name in self._definitions:
            raise StrategyError(f"strategy {definition.name!r} is already registered")
        self._definitions[definition.name] = definition

    def get(self, name: str) -> StrategyDefinition:
        try:
            return self._definitions[name.strip().lower()]
        except (KeyError, AttributeError) as exc:
            raise StrategyError(f"unknown strategy {name!r}") from exc

    def all(self) -> tuple[StrategyDefinition, ...]:
        return tuple(self._definitions.values())

    def names(self) -> tuple[str, ...]:
        return tuple(self._definitions)

    @classmethod
    def default(cls) -> "StrategyRegistry":
        return cls(
            (
                StrategyDefinition(
                    "ema_crossover",
                    "EMA crossover",
                    "Fast and slow exponential moving-average crossover.",
                    26,
                    _ema_crossover,
                ),
                StrategyDefinition(
                    "rsi_reversion",
                    "RSI mean reversion",
                    "Buys oversold RSI and shorts overbought RSI, then returns flat.",
                    15,
                    _rsi_reversion,
                ),
                StrategyDefinition(
                    "macd_crossover",
                    "MACD crossover",
                    "MACD line and signal-line crossover with a zero-line context.",
                    35,
                    _macd_crossover,
                ),
                StrategyDefinition(
                    "bollinger_bands",
                    "Bollinger Bands",
                    "Mean reversion from statistically extended Bollinger closes.",
                    20,
                    _bollinger_reversion,
                ),
                StrategyDefinition(
                    "breakout",
                    "Donchian breakout",
                    "Breaks above or below the prior rolling high/low range.",
                    21,
                    _breakout,
                ),
                StrategyDefinition(
                    "momentum",
                    "Price momentum",
                    "Directional lookback return with a neutral band.",
                    21,
                    _momentum,
                ),
                StrategyDefinition(
                    "trend_following",
                    "Trend following",
                    "EMA direction and price location confirm a persistent trend.",
                    30,
                    _trend_following,
                ),
                StrategyDefinition(
                    "mean_reversion",
                    "Z-score mean reversion",
                    "Fades sufficiently distant closes relative to a rolling mean.",
                    21,
                    _mean_reversion,
                ),
            )
        )


def sma(values: Sequence[float], period: int) -> tuple[float | None, ...]:
    """Simple moving average with ``None`` until a complete window exists."""

    _period(period)
    output: list[float | None] = []
    for index in range(len(values)):
        if index + 1 < period:
            output.append(None)
        else:
            window = values[index + 1 - period : index + 1]
            output.append(sum(window) / period)
    return tuple(output)


def ema(values: Sequence[float], period: int) -> tuple[float | None, ...]:
    """Wilder-independent EMA seeded by the first complete SMA window."""

    _period(period)
    output: list[float | None] = [None] * len(values)
    if len(values) < period:
        return tuple(output)
    previous = sum(values[:period]) / period
    output[period - 1] = previous
    multiplier = 2.0 / (period + 1.0)
    for index in range(period, len(values)):
        previous = (values[index] - previous) * multiplier + previous
        output[index] = previous
    return tuple(output)


def rsi(values: Sequence[float], period: int = 14) -> tuple[float | None, ...]:
    """Wilder-style RSI, causal and bounded to 0..100."""

    _period(period)
    output: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return tuple(output)
    gains = [max(values[index] - values[index - 1], 0.0) for index in range(1, len(values))]
    losses = [max(values[index - 1] - values[index], 0.0) for index in range(1, len(values))]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    output[period] = _rsi_value(average_gain, average_loss)
    for index in range(period + 1, len(values)):
        average_gain = (average_gain * (period - 1) + gains[index - 1]) / period
        average_loss = (average_loss * (period - 1) + losses[index - 1]) / period
        output[index] = _rsi_value(average_gain, average_loss)
    return tuple(output)


def macd(
    values: Sequence[float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> tuple[tuple[float | None, ...], tuple[float | None, ...], tuple[float | None, ...]]:
    """Return causal MACD line, signal line, and histogram."""

    _period(fast_period)
    _period(slow_period)
    _period(signal_period)
    if fast_period >= slow_period:
        raise StrategyError("MACD fast_period must be below slow_period")
    fast = ema(values, fast_period)
    slow = ema(values, slow_period)
    line: list[float | None] = [
        None if fast[index] is None or slow[index] is None else fast[index] - slow[index]
        for index in range(len(values))
    ]
    available = [value for value in line if value is not None]
    signal_values = ema(available, signal_period)
    signal: list[float | None] = [None] * len(values)
    cursor = 0
    for index, value in enumerate(line):
        if value is not None:
            signal[index] = signal_values[cursor]
            cursor += 1
    histogram = tuple(
        None if line[index] is None or signal[index] is None else line[index] - signal[index]
        for index in range(len(values))
    )
    return tuple(line), tuple(signal), histogram


def bollinger_bands(
    values: Sequence[float], period: int = 20, deviations: float = 2.0
) -> tuple[tuple[float | None, ...], tuple[float | None, ...], tuple[float | None, ...]]:
    """Return middle, upper, and lower rolling bands."""

    _period(period)
    if not math.isfinite(float(deviations)) or deviations <= 0:
        raise StrategyError("Bollinger deviations must be positive and finite")
    middle: list[float | None] = []
    upper: list[float | None] = []
    lower: list[float | None] = []
    for index in range(len(values)):
        if index + 1 < period:
            middle.append(None)
            upper.append(None)
            lower.append(None)
            continue
        window = values[index + 1 - period : index + 1]
        mean = sum(window) / period
        variance = sum((value - mean) ** 2 for value in window) / period
        deviation = math.sqrt(max(variance, 0.0))
        middle.append(mean)
        upper.append(mean + deviations * deviation)
        lower.append(mean - deviations * deviation)
    return tuple(middle), tuple(upper), tuple(lower)


def _ema_crossover(context: StrategyContext) -> StrategySignal:
    closes = context.closes
    fast = ema(closes, 12)
    slow = ema(closes, 26)
    current_fast, current_slow = fast[-1], slow[-1]
    previous_fast, previous_slow = fast[-2], slow[-2]
    if current_fast is None or current_slow is None:
        return _signal(context, SignalAction.HOLD, "EMA history is warming up")
    if previous_fast is not None and previous_slow is not None and previous_fast <= previous_slow < current_fast:
        return _signal(context, SignalAction.LONG, "fast EMA crossed above slow EMA", 0.7, fast=current_fast, slow=current_slow)
    if previous_fast is not None and previous_slow is not None and previous_fast >= previous_slow > current_fast:
        return _signal(context, SignalAction.SHORT, "fast EMA crossed below slow EMA", 0.7, fast=current_fast, slow=current_slow)
    return _signal(context, SignalAction.HOLD, "no EMA crossover", 0.2, fast=current_fast, slow=current_slow)


def _rsi_reversion(context: StrategyContext) -> StrategySignal:
    value = rsi(context.closes, 14)[-1]
    if value is None:
        return _signal(context, SignalAction.HOLD, "RSI history is warming up")
    if value <= 30.0:
        return _signal(context, SignalAction.LONG, "RSI is oversold", min(1.0, (30.0 - value) / 30.0 + 0.5), rsi=value)
    if value >= 70.0:
        return _signal(context, SignalAction.SHORT, "RSI is overbought", min(1.0, (value - 70.0) / 30.0 + 0.5), rsi=value)
    return _signal(context, SignalAction.FLAT, "RSI returned to its neutral band", 0.2, rsi=value)


def _macd_crossover(context: StrategyContext) -> StrategySignal:
    line, signal, histogram = macd(context.closes)
    if line[-1] is None or signal[-1] is None or line[-2] is None or signal[-2] is None:
        return _signal(context, SignalAction.HOLD, "MACD history is warming up")
    if line[-2] <= signal[-2] < line[-1]:
        return _signal(context, SignalAction.LONG, "MACD crossed above its signal line", 0.65, macd=line[-1], signal=signal[-1], histogram=histogram[-1])
    if line[-2] >= signal[-2] > line[-1]:
        return _signal(context, SignalAction.SHORT, "MACD crossed below its signal line", 0.65, macd=line[-1], signal=signal[-1], histogram=histogram[-1])
    return _signal(context, SignalAction.HOLD, "no MACD crossover", 0.2, macd=line[-1], signal=signal[-1], histogram=histogram[-1])


def _bollinger_reversion(context: StrategyContext) -> StrategySignal:
    middle, upper, lower = bollinger_bands(context.closes)
    if middle[-1] is None or upper[-1] is None or lower[-1] is None:
        return _signal(context, SignalAction.HOLD, "Bollinger history is warming up")
    close = context.current.close
    if close <= lower[-1]:
        return _signal(context, SignalAction.LONG, "close reached the lower Bollinger band", 0.65, close=close, middle=middle[-1], upper=upper[-1], lower=lower[-1])
    if close >= upper[-1]:
        return _signal(context, SignalAction.SHORT, "close reached the upper Bollinger band", 0.65, close=close, middle=middle[-1], upper=upper[-1], lower=lower[-1])
    return _signal(context, SignalAction.FLAT, "close is inside the Bollinger bands", 0.2, close=close, middle=middle[-1], upper=upper[-1], lower=lower[-1])


def _breakout(context: StrategyContext) -> StrategySignal:
    lookback = 20
    if len(context.candles) <= lookback:
        return _signal(context, SignalAction.HOLD, "breakout history is warming up")
    prior_high = max(context.highs[-lookback - 1 : -1])
    prior_low = min(context.lows[-lookback - 1 : -1])
    close = context.current.close
    if close > prior_high:
        return _signal(context, SignalAction.LONG, "close broke above the prior Donchian range", 0.7, close=close, prior_high=prior_high, prior_low=prior_low)
    if close < prior_low:
        return _signal(context, SignalAction.SHORT, "close broke below the prior Donchian range", 0.7, close=close, prior_high=prior_high, prior_low=prior_low)
    return _signal(context, SignalAction.HOLD, "price remains inside the prior range", 0.2, close=close, prior_high=prior_high, prior_low=prior_low)


def _momentum(context: StrategyContext) -> StrategySignal:
    lookback = 20
    if len(context.candles) <= lookback:
        return _signal(context, SignalAction.HOLD, "momentum history is warming up")
    prior = context.closes[-lookback - 1]
    current = context.current.close
    change = current / prior - 1.0
    if change >= 0.02:
        return _signal(context, SignalAction.LONG, "positive lookback momentum exceeded threshold", min(1.0, abs(change) * 10), return_pct=change)
    if change <= -0.02:
        return _signal(context, SignalAction.SHORT, "negative lookback momentum exceeded threshold", min(1.0, abs(change) * 10), return_pct=change)
    return _signal(context, SignalAction.FLAT, "momentum is inside its neutral band", 0.2, return_pct=change)


def _trend_following(context: StrategyContext) -> StrategySignal:
    fast = ema(context.closes, 10)[-1]
    slow = ema(context.closes, 30)[-1]
    previous_fast = ema(context.closes[:-1], 10)[-1] if len(context.candles) > 30 else None
    if fast is None or slow is None:
        return _signal(context, SignalAction.HOLD, "trend history is warming up")
    slope = 0.0 if previous_fast is None else fast - previous_fast
    if fast > slow and context.current.close > fast and slope >= 0:
        return _signal(context, SignalAction.LONG, "price and EMA slope confirm an uptrend", 0.6, fast=fast, slow=slow, slope=slope)
    if fast < slow and context.current.close < fast and slope <= 0:
        return _signal(context, SignalAction.SHORT, "price and EMA slope confirm a downtrend", 0.6, fast=fast, slow=slow, slope=slope)
    return _signal(context, SignalAction.FLAT, "trend confirmation is absent", 0.2, fast=fast, slow=slow, slope=slope)


def _mean_reversion(context: StrategyContext) -> StrategySignal:
    lookback = 20
    if len(context.candles) < lookback:
        return _signal(context, SignalAction.HOLD, "mean-reversion history is warming up")
    window = context.closes[-lookback:]
    mean = sum(window) / lookback
    deviation = math.sqrt(sum((value - mean) ** 2 for value in window) / lookback)
    z_score = 0.0 if deviation == 0 else (context.current.close - mean) / deviation
    if z_score <= -2.0:
        return _signal(context, SignalAction.LONG, "close is more than two standard deviations below its mean", 0.7, z_score=z_score, mean=mean)
    if z_score >= 2.0:
        return _signal(context, SignalAction.SHORT, "close is more than two standard deviations above its mean", 0.7, z_score=z_score, mean=mean)
    return _signal(context, SignalAction.FLAT, "close is within the mean-reversion band", 0.2, z_score=z_score, mean=mean)


def _signal(
    context: StrategyContext,
    action: SignalAction,
    reason: str,
    confidence: float = 0.0,
    **indicators: float,
) -> StrategySignal:
    return StrategySignal(
        strategy_name=context.strategy_name,
        action=action,
        timestamp=context.current.close_time or context.current.timestamp,
        reason=reason,
        confidence=confidence,
        indicators=indicators,
    )


def _period(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 2:
        raise StrategyError("indicator period must be an integer of at least 2")


def _rsi_value(average_gain: float, average_loss: float) -> float:
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    if average_gain == 0:
        return 0.0
    relative_strength = average_gain / average_loss
    return 100.0 - 100.0 / (1.0 + relative_strength)


DEFAULT_STRATEGY_REGISTRY = StrategyRegistry.default()


def discover_strategies() -> StrategyRegistry:
    """Return a fresh built-in registry ready for extension."""

    return StrategyRegistry.default()
