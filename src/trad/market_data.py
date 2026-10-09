"""Validated market-data models and a conservative safety monitor.

This module is deliberately limited to market-data safety.  It does not fetch
from an exchange, submit orders, simulate fills, or maintain an account.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import math
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping


class DataValidationError(ValueError):
    """Raised when a market-data object violates its data contract."""


class DataSafetyError(RuntimeError):
    """Raised when a caller tries to open a position with unsafe data."""

    def __init__(self, health: "DataHealth") -> None:
        self.health = health
        super().__init__(
            f"new simulated positions are blocked: {health.status.value}; "
            f"{health.reason}"
        )


class MarketDataKind(str, Enum):
    """Supported normalized market-data event types."""

    OHLCV = "ohlcv"
    TICKER = "ticker"
    TRADE = "trade"
    BID_ASK = "bid_ask"
    ORDER_BOOK = "order_book"


class TradeSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class DataHealthStatus(str, Enum):
    """Health states that can affect simulated position opening."""

    SAFE = "safe"
    NO_DATA = "no_data"
    STALE = "stale"
    DELAYED = "delayed"
    INVALID = "invalid"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"
    GAP = "gap"
    SEQUENCE_GAP = "sequence_gap"


@dataclass(frozen=True, kw_only=True)
class MarketData:
    """Common fields shared by every normalized market-data event.

    ``timestamp`` is the source/exchange event timestamp.  ``received_at`` is
    the local ingestion timestamp.  Both must be timezone-aware; they are
    normalized to UTC and a source timestamp after receipt is rejected.
    """

    symbol: str
    timestamp: datetime
    received_at: datetime

    def __post_init__(self) -> None:
        _validate_symbol(self.symbol)
        timestamp = _utc_timestamp(self.timestamp, "timestamp")
        received_at = _utc_timestamp(self.received_at, "received_at")
        if timestamp > received_at:
            raise DataValidationError(
                "timestamp cannot be later than received_at"
            )
        object.__setattr__(self, "timestamp", timestamp)
        object.__setattr__(self, "received_at", received_at)


@dataclass(frozen=True, kw_only=True)
class OHLCV(MarketData):
    """A closed or replayable candle identified by its opening timestamp."""

    timeframe_seconds: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        super().__post_init__()
        _positive_int(self.timeframe_seconds, "timeframe_seconds")
        open_price = _positive_float(self.open, "open")
        high_price = _positive_float(self.high, "high")
        low_price = _positive_float(self.low, "low")
        close_price = _positive_float(self.close, "close")
        volume = _non_negative_float(self.volume, "volume")
        if high_price < max(open_price, close_price):
            raise DataValidationError("high must be at least open and close")
        if low_price > min(open_price, close_price):
            raise DataValidationError("low must be at most open and close")
        object.__setattr__(self, "open", open_price)
        object.__setattr__(self, "high", high_price)
        object.__setattr__(self, "low", low_price)
        object.__setattr__(self, "close", close_price)
        object.__setattr__(self, "volume", volume)


@dataclass(frozen=True, kw_only=True)
class Ticker(MarketData):
    """Last-traded price with an optional best bid/ask quote."""

    last_price: float
    bid_price: float | None = None
    ask_price: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        last_price = _positive_float(self.last_price, "last_price")
        quote_prices_present = self.bid_price is not None or self.ask_price is not None
        quote_sizes_present = self.bid_size is not None or self.ask_size is not None
        if quote_prices_present and (self.bid_price is None or self.ask_price is None):
            raise DataValidationError("ticker bid_price and ask_price must be paired")
        if quote_sizes_present and (self.bid_size is None or self.ask_size is None):
            raise DataValidationError("ticker bid_size and ask_size must be paired")

        bid_price = ask_price = bid_size = ask_size = None
        if quote_prices_present:
            bid_price = _positive_float(self.bid_price, "bid_price")
            ask_price = _positive_float(self.ask_price, "ask_price")
            if bid_price > ask_price:
                raise DataValidationError("ticker bid_price cannot exceed ask_price")
        if quote_sizes_present:
            bid_size = _positive_float(self.bid_size, "bid_size")
            ask_size = _positive_float(self.ask_size, "ask_size")
        object.__setattr__(self, "last_price", last_price)
        object.__setattr__(self, "bid_price", bid_price)
        object.__setattr__(self, "ask_price", ask_price)
        object.__setattr__(self, "bid_size", bid_size)
        object.__setattr__(self, "ask_size", ask_size)


@dataclass(frozen=True, kw_only=True)
class Trade(MarketData):
    """One normalized public trade event."""

    price: float
    quantity: float
    side: TradeSide | str
    trade_id: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        price = _positive_float(self.price, "price")
        quantity = _positive_float(self.quantity, "quantity")
        try:
            side = TradeSide(self.side)
        except (TypeError, ValueError) as exc:
            raise DataValidationError("side must be 'buy' or 'sell'") from exc
        if self.trade_id is not None:
            if not isinstance(self.trade_id, str) or not self.trade_id.strip():
                raise DataValidationError("trade_id must be a non-empty string")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "side", side)


@dataclass(frozen=True, kw_only=True)
class BidAsk(MarketData):
    """A best-bid/best-ask quote suitable for spread checks."""

    bid_price: float
    ask_price: float
    bid_size: float
    ask_size: float

    def __post_init__(self) -> None:
        super().__post_init__()
        bid_price = _positive_float(self.bid_price, "bid_price")
        ask_price = _positive_float(self.ask_price, "ask_price")
        bid_size = _positive_float(self.bid_size, "bid_size")
        ask_size = _positive_float(self.ask_size, "ask_size")
        if bid_price > ask_price:
            raise DataValidationError("bid_price cannot exceed ask_price")
        object.__setattr__(self, "bid_price", bid_price)
        object.__setattr__(self, "ask_price", ask_price)
        object.__setattr__(self, "bid_size", bid_size)
        object.__setattr__(self, "ask_size", ask_size)


@dataclass(frozen=True)
class OrderBookLevel:
    """One positive price/quantity level in an order-book snapshot."""

    price: float
    quantity: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", _positive_float(self.price, "level.price"))
        object.__setattr__(
            self,
            "quantity",
            _positive_float(self.quantity, "level.quantity"),
        )


@dataclass(frozen=True, kw_only=True)
class OrderBook(MarketData):
    """A validated order-book snapshot with optional contiguous sequence."""

    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]
    sequence: int | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        bids = _levels(self.bids, "bids")
        asks = _levels(self.asks, "asks")
        if not bids or not asks:
            raise DataValidationError("order book must contain both bids and asks")
        if len({level.price for level in bids}) != len(bids):
            raise DataValidationError("order book contains duplicate bid prices")
        if len({level.price for level in asks}) != len(asks):
            raise DataValidationError("order book contains duplicate ask prices")
        bid_prices = [level.price for level in bids]
        ask_prices = [level.price for level in asks]
        if bid_prices != sorted(bid_prices, reverse=True):
            raise DataValidationError("bids must be sorted from high to low")
        if ask_prices != sorted(ask_prices):
            raise DataValidationError("asks must be sorted from low to high")
        if max(bid_prices) > min(ask_prices):
            raise DataValidationError("order book bids cannot cross asks")
        if self.sequence is not None:
            _non_negative_int(self.sequence, "sequence")
        object.__setattr__(self, "bids", bids)
        object.__setattr__(self, "asks", asks)


def event_kind(event: object) -> MarketDataKind:
    """Return the normalized kind for a supported event."""

    if isinstance(event, OHLCV):
        return MarketDataKind.OHLCV
    if isinstance(event, Ticker):
        return MarketDataKind.TICKER
    if isinstance(event, Trade):
        return MarketDataKind.TRADE
    if isinstance(event, BidAsk):
        return MarketDataKind.BID_ASK
    if isinstance(event, OrderBook):
        return MarketDataKind.ORDER_BOOK
    raise DataValidationError(f"unsupported market-data event type: {type(event).__name__}")


@dataclass(frozen=True)
class MarketDataPolicy:
    """Conservative thresholds used by :class:`MarketDataSafetyMonitor`.

    OHLCV gaps use each candle's ``timeframe_seconds``.  Other event streams
    only use gap detection when an expected interval is explicitly configured.
    When an order-book sequence is supplied, it is assumed to be a contiguous
    update stream and sequence jumps are unsafe until the monitor is reset.
    """

    max_age_seconds: float = 30.0
    max_source_lag_seconds: float = 10.0
    gap_tolerance_seconds: float = 0.0
    expected_intervals: Mapping[MarketDataKind | str, float] = field(
        default_factory=dict
    )
    required_kinds: frozenset[MarketDataKind | str] = frozenset()

    def __post_init__(self) -> None:
        max_age = _positive_float(self.max_age_seconds, "max_age_seconds")
        max_lag = _non_negative_float(
            self.max_source_lag_seconds, "max_source_lag_seconds"
        )
        tolerance = _non_negative_float(
            self.gap_tolerance_seconds, "gap_tolerance_seconds"
        )
        intervals: dict[MarketDataKind, float] = {}
        for kind, interval in self.expected_intervals.items():
            normalized_kind = _kind_value(kind, "expected_intervals key")
            intervals[normalized_kind] = _positive_float(
                interval, f"expected_intervals[{normalized_kind.value}]"
            )
        required = frozenset(
            _kind_value(kind, "required_kinds entry") for kind in self.required_kinds
        )
        object.__setattr__(self, "max_age_seconds", max_age)
        object.__setattr__(self, "max_source_lag_seconds", max_lag)
        object.__setattr__(self, "gap_tolerance_seconds", tolerance)
        object.__setattr__(self, "expected_intervals", MappingProxyType(intervals))
        object.__setattr__(self, "required_kinds", required)


@dataclass(frozen=True)
class DataHealth:
    """A point-in-time safety decision for simulated new positions."""

    status: DataHealthStatus
    allow_new_positions: bool
    checked_at: datetime
    reason: str
    kind: MarketDataKind | None = None
    symbol: str | None = None
    stream_key: str | None = None
    latest_timestamp: datetime | None = None

    @property
    def is_safe(self) -> bool:
        return self.status is DataHealthStatus.SAFE and self.allow_new_positions


@dataclass
class _StreamState:
    kind: MarketDataKind
    symbol: str
    key: str
    last_timestamp: datetime | None = None
    last_fingerprint: tuple[Any, ...] | None = None
    latest_timestamp: datetime | None = None
    latest_received_at: datetime | None = None
    last_sequence: int | None = None
    same_timestamp_ids: set[tuple[Any, ...]] = field(default_factory=set)
    status: DataHealthStatus = DataHealthStatus.SAFE
    reason: str = ""


class MarketDataSafetyMonitor:
    """Track normalized data and fail closed when its integrity is uncertain.

    The monitor starts in ``NO_DATA``.  A data-integrity failure remains
    blocking until ``reset`` is called after a verified reconnect/replay
    boundary and fresh data is ingested.  This prevents a later isolated tick
    from silently clearing a gap, duplicate, or sequence failure.
    """

    def __init__(self, policy: MarketDataPolicy | None = None) -> None:
        self.policy = policy or MarketDataPolicy()
        self._streams: dict[str, _StreamState] = {}
        self._global_issue: tuple[DataHealthStatus, str, MarketDataKind | None, str | None] | None = None

    def ingest(self, event: object, *, now: datetime | None = None) -> DataHealth:
        """Validate and record one event, returning the aggregate safety state."""

        checked_at = _utc_timestamp(
            now if now is not None else getattr(event, "received_at", None),
            "now",
        )
        try:
            kind = event_kind(event)
        except DataValidationError as exc:
            self._global_issue = (
                DataHealthStatus.INVALID,
                str(exc),
                None,
                None,
            )
            return self.health(checked_at)

        assert isinstance(event, MarketData)
        key = _stream_key(event, kind)
        state = self._streams.setdefault(
            key,
            _StreamState(kind=kind, symbol=event.symbol, key=key),
        )

        if checked_at < event.received_at or checked_at < event.timestamp:
            return self._mark_issue(
                state,
                DataHealthStatus.INVALID,
                "event timestamp is in the future relative to the check time",
                checked_at,
                event,
            )

        source_lag = (event.received_at - event.timestamp).total_seconds()
        if source_lag > self.policy.max_source_lag_seconds:
            return self._mark_issue(
                state,
                DataHealthStatus.DELAYED,
                f"source lag {source_lag:.3f}s exceeds "
                f"{self.policy.max_source_lag_seconds:.3f}s",
                checked_at,
                event,
            )

        source_age = (checked_at - event.timestamp).total_seconds()
        if source_age > self.policy.max_age_seconds:
            return self._mark_issue(
                state,
                DataHealthStatus.STALE,
                f"event age {source_age:.3f}s exceeds "
                f"{self.policy.max_age_seconds:.3f}s",
                checked_at,
                event,
            )

        if state.status is not DataHealthStatus.SAFE:
            return self.health(checked_at)

        issue = self._ordering_issue(state, event, kind)
        if issue is not None:
            status, reason = issue
            return self._mark_issue(state, status, reason, checked_at, event)

        interval = _expected_interval(event, kind, self.policy)
        if (
            state.last_timestamp is not None
            and interval is not None
            and (event.timestamp - state.last_timestamp).total_seconds()
            > interval + self.policy.gap_tolerance_seconds
        ):
            delta = (event.timestamp - state.last_timestamp).total_seconds()
            return self._mark_issue(
                state,
                DataHealthStatus.GAP,
                f"timestamp gap {delta:.3f}s exceeds expected {interval:.3f}s",
                checked_at,
                event,
            )

        fingerprint = _fingerprint(event, kind)
        if state.last_timestamp != event.timestamp:
            state.same_timestamp_ids.clear()
        state.latest_timestamp = event.timestamp
        state.latest_received_at = event.received_at
        state.last_timestamp = event.timestamp
        state.last_fingerprint = fingerprint
        state.same_timestamp_ids.add(_same_timestamp_identity(event, kind))
        if isinstance(event, OrderBook) and event.sequence is not None:
            state.last_sequence = event.sequence
        state.reason = "latest event passed validation"
        return self.health(checked_at)

    def report_invalid_data(
        self,
        reason: str,
        *,
        now: datetime,
        kind: MarketDataKind | str | None = None,
        symbol: str | None = None,
    ) -> DataHealth:
        """Record a malformed raw payload before model construction succeeds.

        A future adapter can call this when parsing raises
        ``DataValidationError``.  The monitor then fails closed immediately
        instead of continuing to treat the previous quote as safe.
        """

        if not isinstance(reason, str) or not reason.strip():
            raise DataValidationError("invalid-data reason must be non-empty")
        checked_at = _utc_timestamp(now, "now")
        normalized_kind = None if kind is None else _kind_value(kind, "kind")
        if symbol is not None:
            _validate_symbol(symbol)
        self._global_issue = (
            DataHealthStatus.INVALID,
            reason.strip(),
            normalized_kind,
            symbol,
        )
        return self.health(checked_at)

    def health(self, now: datetime) -> DataHealth:
        """Return the aggregate health at ``now`` without ingesting data."""

        checked_at = _utc_timestamp(now, "now")
        if self._global_issue is not None:
            status, reason, kind, symbol = self._global_issue
            return DataHealth(
                status=status,
                allow_new_positions=False,
                checked_at=checked_at,
                reason=reason,
                kind=kind,
                symbol=symbol,
            )

        if not self._streams:
            return DataHealth(
                status=DataHealthStatus.NO_DATA,
                allow_new_positions=False,
                checked_at=checked_at,
                reason="no validated market-data event has been received",
            )

        for state in sorted(self._streams.values(), key=lambda item: item.key):
            if state.status is not DataHealthStatus.SAFE:
                return self._state_health(state, checked_at)
            if state.latest_timestamp is None or state.latest_received_at is None:
                return self._state_health(
                    state,
                    checked_at,
                    status=DataHealthStatus.NO_DATA,
                    reason="stream has no validated event",
                )
            if checked_at < state.latest_received_at:
                return self._state_health(
                    state,
                    checked_at,
                    status=DataHealthStatus.INVALID,
                    reason="latest receipt timestamp is in the future",
                )
            age = (checked_at - state.latest_timestamp).total_seconds()
            if age > self.policy.max_age_seconds:
                return self._state_health(
                    state,
                    checked_at,
                    status=DataHealthStatus.STALE,
                    reason=f"latest event age {age:.3f}s exceeds "
                    f"{self.policy.max_age_seconds:.3f}s",
                )
            lag = (state.latest_received_at - state.latest_timestamp).total_seconds()
            if lag > self.policy.max_source_lag_seconds:
                return self._state_health(
                    state,
                    checked_at,
                    status=DataHealthStatus.DELAYED,
                    reason=f"latest source lag {lag:.3f}s exceeds "
                    f"{self.policy.max_source_lag_seconds:.3f}s",
                )

        observed_kinds = {state.kind for state in self._streams.values()}
        missing = sorted(
            self.policy.required_kinds - observed_kinds,
            key=lambda item: item.value,
        )
        if missing:
            names = ", ".join(kind.value for kind in missing)
            return DataHealth(
                status=DataHealthStatus.NO_DATA,
                allow_new_positions=False,
                checked_at=checked_at,
                reason=f"required market-data kind(s) missing: {names}",
            )

        return DataHealth(
            status=DataHealthStatus.SAFE,
            allow_new_positions=True,
            checked_at=checked_at,
            reason="all observed and required market-data streams are healthy",
        )

    def can_open_new_positions(self, now: datetime) -> bool:
        """Return whether a future paper engine may open a new position."""

        return self.health(now).allow_new_positions

    def require_safe_for_new_position(self, now: datetime) -> DataHealth:
        """Fail closed for callers that need a hard position-opening guard."""

        health = self.health(now)
        if not health.allow_new_positions:
            raise DataSafetyError(health)
        return health

    def reset(self) -> None:
        """Clear unsafe stream state after an externally verified recovery.

        Resetting does not make data safe by itself: the monitor returns
        ``NO_DATA`` until fresh events are ingested.
        """

        self._streams.clear()
        self._global_issue = None

    def _ordering_issue(
        self,
        state: _StreamState,
        event: MarketData,
        kind: MarketDataKind,
    ) -> tuple[DataHealthStatus, str] | None:
        fingerprint = _fingerprint(event, kind)
        if state.last_timestamp is not None:
            if event.timestamp < state.last_timestamp:
                return (
                    DataHealthStatus.OUT_OF_ORDER,
                    "event timestamp is earlier than the last accepted timestamp",
                )
            if event.timestamp == state.last_timestamp:
                identity = _same_timestamp_identity(event, kind)
                if identity in state.same_timestamp_ids:
                    return (
                        DataHealthStatus.DUPLICATE,
                        "event duplicates an already accepted event",
                    )
                if kind is MarketDataKind.OHLCV:
                    return (
                        DataHealthStatus.DUPLICATE,
                        "OHLCV timestamp was already accepted with different values",
                    )
                if isinstance(event, OrderBook):
                    if event.sequence is None or state.last_sequence is None:
                        return (
                            DataHealthStatus.DUPLICATE,
                            "order-book timestamp was already accepted",
                        )
                if isinstance(event, Trade) and event.trade_id is not None:
                    return None
        if isinstance(event, OrderBook):
            if event.sequence is not None and state.last_sequence is not None:
                if event.sequence < state.last_sequence:
                    return (
                        DataHealthStatus.OUT_OF_ORDER,
                        "order-book sequence is earlier than the last accepted sequence",
                    )
                if event.sequence == state.last_sequence:
                    return (
                        DataHealthStatus.DUPLICATE,
                        "order-book sequence duplicates the last accepted sequence",
                    )
                if event.sequence > state.last_sequence + 1:
                    return (
                        DataHealthStatus.SEQUENCE_GAP,
                        "order-book sequence is not contiguous",
                    )
        if state.last_timestamp == event.timestamp and fingerprint == state.last_fingerprint:
            return (
                DataHealthStatus.DUPLICATE,
                "event fingerprint duplicates the last accepted event",
            )
        return None

    def _mark_issue(
        self,
        state: _StreamState,
        status: DataHealthStatus,
        reason: str,
        checked_at: datetime,
        event: MarketData,
    ) -> DataHealth:
        state.status = status
        state.reason = reason
        state.latest_timestamp = event.timestamp
        state.latest_received_at = event.received_at
        return self.health(checked_at)

    @staticmethod
    def _state_health(
        state: _StreamState,
        checked_at: datetime,
        *,
        status: DataHealthStatus | None = None,
        reason: str | None = None,
    ) -> DataHealth:
        return DataHealth(
            status=status or state.status,
            allow_new_positions=False,
            checked_at=checked_at,
            reason=reason or state.reason,
            kind=state.kind,
            symbol=state.symbol,
            stream_key=state.key,
            latest_timestamp=state.latest_timestamp,
        )


class DeterministicReplay:
    """Replay an in-memory event sequence without network access or randomness."""

    def __init__(self, events: Iterable[MarketData]) -> None:
        self.events = tuple(events)
        for event in self.events:
            event_kind(event)

    def __iter__(self):
        return iter(self.events)

    def replay(
        self,
        monitor: MarketDataSafetyMonitor,
        *,
        clock: Callable[[MarketData], datetime] | None = None,
    ) -> tuple[DataHealth, ...]:
        """Feed events in their supplied order and return each health result."""

        results: list[DataHealth] = []
        for event in self.events:
            now = clock(event) if clock is not None else event.received_at
            results.append(monitor.ingest(event, now=now))
        return tuple(results)


def _utc_timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise DataValidationError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DataValidationError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _validate_symbol(value: Any) -> None:
    if not isinstance(value, str) or not value.strip() or any(ch.isspace() for ch in value):
        raise DataValidationError("symbol must be a non-empty non-whitespace string")
    if len(value) > 64:
        raise DataValidationError("symbol cannot exceed 64 characters")


def _positive_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataValidationError(f"{field_name} must be a finite positive number")
    converted = float(value)
    if not math.isfinite(converted) or converted <= 0:
        raise DataValidationError(f"{field_name} must be a finite positive number")
    return converted


def _non_negative_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataValidationError(
            f"{field_name} must be a finite non-negative number"
        )
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        raise DataValidationError(
            f"{field_name} must be a finite non-negative number"
        )
    return converted


def _positive_int(value: Any, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DataValidationError(f"{field_name} must be a positive integer")


def _non_negative_int(value: Any, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DataValidationError(f"{field_name} must be a non-negative integer")


def _kind_value(value: MarketDataKind | str, field_name: str) -> MarketDataKind:
    try:
        return MarketDataKind(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(kind.value for kind in MarketDataKind)
        raise DataValidationError(
            f"{field_name} must be one of: {allowed}"
        ) from exc


def _levels(value: Iterable[OrderBookLevel], field_name: str) -> tuple[OrderBookLevel, ...]:
    try:
        levels = tuple(value)
    except TypeError as exc:
        raise DataValidationError(f"{field_name} must be an iterable of levels") from exc
    if any(not isinstance(level, OrderBookLevel) for level in levels):
        raise DataValidationError(f"{field_name} must contain OrderBookLevel objects")
    return levels


def _stream_key(event: MarketData, kind: MarketDataKind) -> str:
    if isinstance(event, OHLCV):
        return f"{kind.value}:{event.symbol}:timeframe={event.timeframe_seconds}"
    return f"{kind.value}:{event.symbol}"


def _expected_interval(
    event: MarketData,
    kind: MarketDataKind,
    policy: MarketDataPolicy,
) -> float | None:
    if isinstance(event, OHLCV):
        return float(event.timeframe_seconds)
    return policy.expected_intervals.get(kind)


def _fingerprint(event: MarketData, kind: MarketDataKind) -> tuple[Any, ...]:
    base: tuple[Any, ...] = (kind.value, event.symbol, event.timestamp)
    if isinstance(event, OHLCV):
        return base + (
            event.timeframe_seconds,
            event.open,
            event.high,
            event.low,
            event.close,
            event.volume,
        )
    if isinstance(event, Ticker):
        return base + (
            event.last_price,
            event.bid_price,
            event.ask_price,
            event.bid_size,
            event.ask_size,
        )
    if isinstance(event, Trade):
        return base + (event.price, event.quantity, event.side.value, event.trade_id)
    if isinstance(event, BidAsk):
        return base + (
            event.bid_price,
            event.ask_price,
            event.bid_size,
            event.ask_size,
        )
    if isinstance(event, OrderBook):
        return base + (
            tuple((level.price, level.quantity) for level in event.bids),
            tuple((level.price, level.quantity) for level in event.asks),
            event.sequence,
        )
    raise DataValidationError(f"unsupported market-data event type: {type(event).__name__}")


def _same_timestamp_identity(
    event: MarketData,
    kind: MarketDataKind,
) -> tuple[Any, ...]:
    if isinstance(event, Trade) and event.trade_id is not None:
        return (kind.value, event.symbol, "trade_id", event.trade_id)
    return _fingerprint(event, kind)
