"""Deterministic Perpetual Futures paper engine with local SQLite durability.

This module is simulation-only.  It accepts explicit prices and timestamps,
uses the existing market-data safety monitor as a fail-closed gate, and never
calls an exchange account or order endpoint.  The persistence store contains
only Futures state and audit records; it has no connection to the Spot engine's
wallet or ledger.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from enum import Enum
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterator, Mapping

from .config import Instrument, RunConfig
from .market_data import DataHealth, MarketData, MarketDataSafetyMonitor


ZERO = Decimal("0")
ONE = Decimal("1")
DEFAULT_FUTURES_COLLATERAL = Decimal("1000")
SCHEMA_VERSION = 1


class FuturesError(RuntimeError):
    """Base class for Futures paper-engine failures."""


class FuturesValidationError(FuturesError, ValueError):
    """Raised for malformed or unsupported Futures inputs."""


class FuturesPersistenceError(FuturesError):
    """Raised when durable state is missing, corrupt, or inconsistent."""


class FuturesOrderStateError(FuturesError):
    """Raised when an order lifecycle operation is invalid."""


class FuturesExecutionError(FuturesError):
    """Raised when a deterministic fill cannot be applied atomically."""


class FuturesRiskError(FuturesError):
    """Raised when an unsafe mark, funding, or collateral operation is rejected."""


class FuturesDuplicateError(FuturesError, ValueError):
    """Raised when an idempotency key is reused with different inputs."""


class FuturesDuplicateOrderError(FuturesDuplicateError):
    """Raised when a client order id is reused with different inputs."""


class FuturesDuplicateFillError(FuturesDuplicateError):
    """Raised when a fill id is reused with different inputs."""


class FuturesDuplicateFundingError(FuturesDuplicateError):
    """Raised when a funding payment id is reused with different inputs."""


class FuturesReconciliationError(FuturesError):
    """Raised when durable or in-memory accounting cannot be reconciled."""


class FuturesPositionSide(str, Enum):
    """Direction of a net isolated position."""

    LONG = "long"
    SHORT = "short"


class FuturesOrderAction(str, Enum):
    """Whether an order adds to or reduces a position."""

    OPEN = "open"
    REDUCE = "reduce"


class FuturesOrderStatus(str, Enum):
    """Futures paper-order lifecycle states."""

    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class FuturesRiskCode(str, Enum):
    """Stable rejection reasons for logs and future UI consumers."""

    MARKET_DATA_UNSAFE = "market_data_unsafe"
    INSUFFICIENT_COLLATERAL = "insufficient_collateral"
    LEVERAGE_LIMIT = "leverage_limit"
    POSITION_NOT_FOUND = "position_not_found"
    POSITION_SIDE_MISMATCH = "position_side_mismatch"
    POSITION_LIMIT = "position_limit"
    EXPOSURE_LIMIT = "exposure_limit"
    OPEN_ORDER_LIMIT = "open_order_limit"
    LIQUIDATION_RISK = "liquidation_risk"
    INVALID_MARK_PRICE = "invalid_mark_price"


class FuturesLedgerType(str, Enum):
    """Kinds of durable Futures ledger entries."""

    INITIALIZATION = "initialization"
    RESERVATION = "reservation"
    RELEASE = "release"
    FILL = "fill"
    FEE = "fee"
    FUNDING = "funding"
    LIQUIDATION = "liquidation"
    REJECTION = "rejection"


class FuturesEventType(str, Enum):
    """Durable audit event types."""

    INITIALIZED = "initialized"
    ORDER_ACCEPTED = "order_accepted"
    ORDER_REJECTED = "order_rejected"
    ORDER_CANCELLED = "order_cancelled"
    FILL_APPLIED = "fill_applied"
    MARK_UPDATED = "mark_updated"
    FUNDING_APPLIED = "funding_applied"
    LIQUIDATED = "liquidated"


def _decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise FuturesValidationError(f"{field_name} must be a finite decimal")
    try:
        converted = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FuturesValidationError(f"{field_name} must be a finite decimal") from exc
    if not converted.is_finite():
        raise FuturesValidationError(f"{field_name} must be a finite decimal")
    return converted


def _non_negative(value: Any, field_name: str) -> Decimal:
    converted = _decimal(value, field_name)
    if converted < ZERO:
        raise FuturesValidationError(f"{field_name} must be non-negative")
    return converted


def _positive(value: Any, field_name: str) -> Decimal:
    converted = _decimal(value, field_name)
    if converted <= ZERO:
        raise FuturesValidationError(f"{field_name} must be positive")
    return converted


def _parse_enum(value: Any, enum_type: type[Enum], field_name: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise FuturesValidationError(
            f"{field_name} must be one of: {allowed}"
        ) from exc


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise FuturesValidationError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise FuturesValidationError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _asset(value: Any, field_name: str = "asset") -> str:
    if not isinstance(value, str):
        raise FuturesValidationError(f"{field_name} must be a string")
    normalized = value.strip().upper()
    if not normalized or not normalized.isalnum():
        raise FuturesValidationError(
            f"{field_name} must contain only non-empty letters and digits"
        )
    return normalized


def _contract_symbol(value: Any, field_name: str = "symbol") -> tuple[str, str, str, str]:
    if not isinstance(value, str):
        raise FuturesValidationError(f"{field_name} must look like BASE/QUOTE[:SETTLE]")
    raw = value.strip().upper()
    parts = raw.split(":")
    if len(parts) > 2:
        raise FuturesValidationError(f"{field_name} has too many settlement separators")
    pair = parts[0]
    settlement = parts[1] if len(parts) == 2 else ""
    pair_parts = pair.split("/")
    if len(pair_parts) != 2:
        raise FuturesValidationError(f"{field_name} must look like BASE/QUOTE[:SETTLE]")
    base = _asset(pair_parts[0], f"{field_name} base asset")
    quote = _asset(pair_parts[1], f"{field_name} quote asset")
    collateral = _asset(settlement, f"{field_name} settlement asset") if settlement else quote
    if base == collateral:
        raise FuturesValidationError(f"{field_name} base and collateral assets must differ")
    normalized = f"{base}/{quote}:{collateral}"
    return normalized, base, quote, collateral


def _quantum(precision: int) -> Decimal:
    return ONE.scaleb(-precision)


def _quantize(value: Decimal, precision: int, field_name: str) -> Decimal:
    try:
        return value.quantize(_quantum(precision), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise FuturesValidationError(
            f"{field_name} exceeds supported decimal precision or range"
        ) from exc


def _exact(value: Decimal, precision: int, field_name: str) -> Decimal:
    try:
        quantized = value.quantize(_quantum(precision), rounding=ROUND_DOWN)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise FuturesValidationError(
            f"{field_name} exceeds supported decimal precision or range"
        ) from exc
    if quantized != value:
        raise FuturesValidationError(
            f"{field_name} has more than {precision} supported decimal places"
        )
    return quantized


def _timestamp_text(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat()


def _parse_timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise FuturesPersistenceError(f"{field_name} timestamp is not text")
    try:
        return _utc(datetime.fromisoformat(value), field_name)
    except (ValueError, FuturesValidationError) as exc:
        raise FuturesPersistenceError(f"invalid {field_name} timestamp") from exc


def _json_value(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_object(value: str, field_name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise FuturesPersistenceError(f"invalid JSON in {field_name}") from exc
    if not isinstance(parsed, dict):
        raise FuturesPersistenceError(f"{field_name} payload must be an object")
    return parsed


@dataclass(frozen=True)
class FuturesContractRules:
    """Precision and contract-multiplier rules for one isolated contract."""

    symbol: str = "BTC/USDT:USDT"
    base_asset: str = "BTC"
    quote_asset: str = "USDT"
    collateral_asset: str = "USDT"
    quantity_precision: int = 8
    price_precision: int = 2
    collateral_precision: int = 2
    contract_multiplier: Decimal = ONE
    min_quantity: Decimal = ZERO
    max_quantity: Decimal | None = None

    def __post_init__(self) -> None:
        symbol, base, quote, collateral = _contract_symbol(self.symbol)
        if (_asset(self.base_asset, "base_asset"), _asset(self.quote_asset, "quote_asset"), _asset(self.collateral_asset, "collateral_asset")) != (base, quote, collateral):
            raise FuturesValidationError(
                "symbol must match base_asset, quote_asset, and collateral_asset"
            )
        if quote != collateral:
            raise FuturesValidationError(
                "quote and collateral assets must match; no conversion price is invented"
            )
        for name in ("quantity_precision", "price_precision", "collateral_precision"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 18:
                raise FuturesValidationError(f"{name} must be an integer from 0 through 18")
        multiplier = _positive(self.contract_multiplier, "contract_multiplier")
        minimum = _non_negative(self.min_quantity, "min_quantity")
        minimum = _exact(minimum, self.quantity_precision, "min_quantity")
        maximum = None
        if self.max_quantity is not None:
            maximum = _positive(self.max_quantity, "max_quantity")
            maximum = _exact(maximum, self.quantity_precision, "max_quantity")
            if maximum < minimum:
                raise FuturesValidationError("max_quantity cannot be below min_quantity")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "base_asset", base)
        object.__setattr__(self, "quote_asset", quote)
        object.__setattr__(self, "collateral_asset", collateral)
        object.__setattr__(self, "contract_multiplier", multiplier)
        object.__setattr__(self, "min_quantity", minimum)
        object.__setattr__(self, "max_quantity", maximum)

    @property
    def market_data_symbol(self) -> str:
        return f"{self.base_asset}/{self.quote_asset}"

    @classmethod
    def for_symbol(
        cls,
        symbol: str,
        *,
        quantity_precision: int = 8,
        price_precision: int = 2,
        collateral_precision: int = 2,
        contract_multiplier: Decimal | str | int | float = ONE,
        min_quantity: Decimal | str | int | float = ZERO,
        max_quantity: Decimal | str | int | float | None = None,
    ) -> "FuturesContractRules":
        normalized, base, quote, collateral = _contract_symbol(symbol)
        return cls(
            symbol=normalized,
            base_asset=base,
            quote_asset=quote,
            collateral_asset=collateral,
            quantity_precision=quantity_precision,
            price_precision=price_precision,
            collateral_precision=collateral_precision,
            contract_multiplier=contract_multiplier,
            min_quantity=min_quantity,
            max_quantity=max_quantity,
        )


@dataclass(frozen=True)
class FuturesMarginConfig:
    """Explicit initial/maintenance margin and liquidation assumptions."""

    initial_margin_rate: Decimal | None = None
    maintenance_margin_rate: Decimal = Decimal("0.05")
    liquidation_fee_rate: Decimal = ZERO

    def __post_init__(self) -> None:
        initial = None if self.initial_margin_rate is None else _positive(self.initial_margin_rate, "initial_margin_rate")
        maintenance = _positive(self.maintenance_margin_rate, "maintenance_margin_rate")
        liquidation_fee = _non_negative(self.liquidation_fee_rate, "liquidation_fee_rate")
        if (
            initial is not None and initial > ONE
        ) or maintenance > ONE or liquidation_fee >= ONE:
            raise FuturesValidationError("margin rates must be within their valid range")
        object.__setattr__(self, "initial_margin_rate", initial)
        object.__setattr__(self, "maintenance_margin_rate", maintenance)
        object.__setattr__(self, "liquidation_fee_rate", liquidation_fee)

    def initial_margin(self, notional: Decimal, leverage: Decimal, precision: int) -> Decimal:
        raw = notional * self.initial_margin_rate if self.initial_margin_rate is not None else notional / leverage
        return _quantize(raw, precision, "initial margin")

    def maintenance_margin(self, notional: Decimal, precision: int) -> Decimal:
        return _quantize(notional * self.maintenance_margin_rate, precision, "maintenance margin")


@dataclass(frozen=True)
class FuturesFeeConfig:
    """Collateral-denominated trading-fee policy."""

    rate: Decimal = Decimal("0.001")

    def __post_init__(self) -> None:
        rate = _non_negative(self.rate, "fee rate")
        if rate >= ONE:
            raise FuturesValidationError("fee rate must be below 1")
        object.__setattr__(self, "rate", rate)

    def amount(self, notional: Decimal, precision: int) -> Decimal:
        return _quantize(notional * self.rate, precision, "trading fee")


@dataclass(frozen=True)
class FuturesRiskLimits:
    """Explicit Futures risk limits; ``None`` means no local cap."""

    default_leverage: Decimal = ONE
    max_leverage: Decimal = ONE
    max_order_notional: Decimal | None = None
    max_position_notional: Decimal | None = None
    max_open_orders: int | None = None

    def __post_init__(self) -> None:
        default = _positive(self.default_leverage, "default_leverage")
        maximum = _positive(self.max_leverage, "max_leverage")
        if default > maximum:
            raise FuturesValidationError("default_leverage cannot exceed max_leverage")
        object.__setattr__(self, "default_leverage", default)
        object.__setattr__(self, "max_leverage", maximum)
        for name in ("max_order_notional", "max_position_notional"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _positive(value, name))
        if self.max_open_orders is not None and (
            isinstance(self.max_open_orders, bool)
            or not isinstance(self.max_open_orders, int)
            or self.max_open_orders < 1
        ):
            raise FuturesValidationError("max_open_orders must be a positive integer")


@dataclass(frozen=True)
class FuturesBalance:
    """Available, reserved, and total Futures collateral."""

    asset: str
    available: Decimal
    reserved: Decimal = ZERO

    def __post_init__(self) -> None:
        asset = _asset(self.asset)
        available = _non_negative(self.available, f"{asset} available")
        reserved = _non_negative(self.reserved, f"{asset} reserved")
        object.__setattr__(self, "asset", asset)
        object.__setattr__(self, "available", available)
        object.__setattr__(self, "reserved", reserved)

    @property
    def total(self) -> Decimal:
        return self.available + self.reserved


@dataclass(frozen=True)
class FuturesPosition:
    """One net isolated long or short position."""

    symbol: str
    side: FuturesPositionSide
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    leverage: Decimal
    margin: Decimal
    realized_pnl: Decimal
    maintenance_margin: Decimal
    opened_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        symbol, _, _, _ = _contract_symbol(self.symbol)
        side = _parse_enum(self.side, FuturesPositionSide, "position side")
        quantity = _positive(self.quantity, "position quantity")
        entry = _positive(self.entry_price, "entry price")
        mark = _positive(self.mark_price, "mark price")
        leverage = _positive(self.leverage, "position leverage")
        margin = _non_negative(self.margin, "position margin")
        realized = _decimal(self.realized_pnl, "realized P&L")
        maintenance = _non_negative(self.maintenance_margin, "maintenance margin")
        opened = _utc(self.opened_at, "position opened_at")
        updated = _utc(self.updated_at, "position updated_at")
        if updated < opened:
            raise FuturesValidationError("position updated_at cannot precede opened_at")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "entry_price", entry)
        object.__setattr__(self, "mark_price", mark)
        object.__setattr__(self, "leverage", leverage)
        object.__setattr__(self, "margin", margin)
        object.__setattr__(self, "realized_pnl", realized)
        object.__setattr__(self, "maintenance_margin", maintenance)
        object.__setattr__(self, "opened_at", opened)
        object.__setattr__(self, "updated_at", updated)

    @property
    def initial_margin(self) -> Decimal:
        """Currently allocated isolated initial margin for this position.

        The value decreases proportionally on a partial reduction and is
        therefore also the position's current reserved collateral.  The
        opening amount is retained by the fill and ledger history.
        """

        return self.margin

    def notional(self, rules: FuturesContractRules) -> Decimal:
        return _quantize(
            self.quantity * self.mark_price * rules.contract_multiplier,
            rules.collateral_precision,
            "position notional",
        )

    def unrealized_pnl(self, rules: FuturesContractRules) -> Decimal:
        difference = self.mark_price - self.entry_price
        if self.side is FuturesPositionSide.SHORT:
            difference = -difference
        return _quantize(
            difference * self.quantity * rules.contract_multiplier,
            rules.collateral_precision,
            "unrealized P&L",
        )

    def equity(self, rules: FuturesContractRules) -> Decimal:
        return _quantize(
            self.margin + self.unrealized_pnl(rules),
            rules.collateral_precision,
            "position equity",
        )


@dataclass(frozen=True)
class FuturesOrder:
    """Immutable public Futures order view."""

    order_id: str
    client_order_id: str | None
    symbol: str
    action: FuturesOrderAction
    position_side: FuturesPositionSide
    quantity: Decimal
    price: Decimal
    leverage: Decimal
    status: FuturesOrderStatus
    filled_quantity: Decimal
    average_fill_price: Decimal | None
    fee_paid: Decimal
    reserved_collateral: Decimal
    created_at: datetime
    updated_at: datetime
    rejection_code: FuturesRiskCode | None = None
    rejection_reason: str | None = None
    fill_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        symbol, _, _, _ = _contract_symbol(self.symbol)
        action = _parse_enum(self.action, FuturesOrderAction, "order action")
        side = _parse_enum(self.position_side, FuturesPositionSide, "position side")
        status = _parse_enum(self.status, FuturesOrderStatus, "order status")
        quantity = _positive(self.quantity, "order quantity")
        price = _positive(self.price, "order price")
        leverage = _positive(self.leverage, "order leverage")
        filled = _non_negative(self.filled_quantity, "filled quantity")
        fee = _non_negative(self.fee_paid, "fee paid")
        reserved = _non_negative(self.reserved_collateral, "reserved collateral")
        created = _utc(self.created_at, "order created_at")
        updated = _utc(self.updated_at, "order updated_at")
        if not isinstance(self.order_id, str) or not self.order_id.strip():
            raise FuturesValidationError("order_id must be non-empty")
        if updated < created:
            raise FuturesValidationError("order updated_at cannot precede created_at")
        if filled > quantity:
            raise FuturesValidationError("filled quantity cannot exceed order quantity")
        if status is FuturesOrderStatus.ACCEPTED and filled != ZERO:
            raise FuturesValidationError("accepted order cannot have fills")
        if status is FuturesOrderStatus.PARTIALLY_FILLED and not ZERO < filled < quantity:
            raise FuturesValidationError("partial order must have an incomplete fill")
        if status is FuturesOrderStatus.FILLED and filled != quantity:
            raise FuturesValidationError("filled order must contain its full quantity")
        if filled == ZERO and self.average_fill_price is not None:
            raise FuturesValidationError("an order without fills cannot have an average fill price")
        if filled > ZERO and self.average_fill_price is None:
            raise FuturesValidationError("an order with fills must have an average fill price")
        if status is FuturesOrderStatus.REJECTED and (filled != ZERO or reserved != ZERO):
            raise FuturesValidationError("rejected order cannot hold fills or collateral")
        if self.client_order_id is not None and (
            not isinstance(self.client_order_id, str) or not self.client_order_id.strip()
        ):
            raise FuturesValidationError("client_order_id must be non-empty")
        average = None if self.average_fill_price is None else _positive(self.average_fill_price, "average fill price")
        rejection_code = None if self.rejection_code is None else _parse_enum(self.rejection_code, FuturesRiskCode, "rejection code")
        if any(not isinstance(item, str) or not item.strip() for item in self.fill_ids):
            raise FuturesValidationError("fill_ids must contain non-empty strings")
        if len(set(self.fill_ids)) != len(self.fill_ids):
            raise FuturesValidationError("fill_ids cannot contain duplicates")
        object.__setattr__(self, "order_id", self.order_id.strip())
        object.__setattr__(self, "client_order_id", None if self.client_order_id is None else self.client_order_id.strip())
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "position_side", side)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "leverage", leverage)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "filled_quantity", filled)
        object.__setattr__(self, "average_fill_price", average)
        object.__setattr__(self, "fee_paid", fee)
        object.__setattr__(self, "reserved_collateral", reserved)
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)
        object.__setattr__(self, "rejection_code", rejection_code)
        object.__setattr__(self, "fill_ids", tuple(item.strip() for item in self.fill_ids))

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            FuturesOrderStatus.FILLED,
            FuturesOrderStatus.CANCELLED,
            FuturesOrderStatus.REJECTED,
        }


@dataclass(frozen=True)
class FuturesFill:
    """One idempotent deterministic Futures execution."""

    fill_id: str
    order_id: str
    symbol: str
    action: FuturesOrderAction
    position_side: FuturesPositionSide
    quantity: Decimal
    price: Decimal
    notional: Decimal
    fee: Decimal
    realized_pnl: Decimal
    margin_released: Decimal
    executed_at: datetime

    def __post_init__(self) -> None:
        symbol, _, _, _ = _contract_symbol(self.symbol)
        action = _parse_enum(self.action, FuturesOrderAction, "fill action")
        side = _parse_enum(self.position_side, FuturesPositionSide, "fill position side")
        quantity = _positive(self.quantity, "fill quantity")
        price = _positive(self.price, "fill price")
        notional = _positive(self.notional, "fill notional")
        fee = _non_negative(self.fee, "fill fee")
        realized = _decimal(self.realized_pnl, "fill realized P&L")
        released = _non_negative(self.margin_released, "margin released")
        executed = _utc(self.executed_at, "fill executed_at")
        if not isinstance(self.fill_id, str) or not self.fill_id.strip():
            raise FuturesValidationError("fill_id must be non-empty")
        if not isinstance(self.order_id, str) or not self.order_id.strip():
            raise FuturesValidationError("order_id must be non-empty")
        object.__setattr__(self, "fill_id", self.fill_id.strip())
        object.__setattr__(self, "order_id", self.order_id.strip())
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "position_side", side)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "notional", notional)
        object.__setattr__(self, "fee", fee)
        object.__setattr__(self, "realized_pnl", realized)
        object.__setattr__(self, "margin_released", released)
        object.__setattr__(self, "executed_at", executed)


@dataclass(frozen=True)
class FuturesLedgerPosting:
    """Available/reserved collateral deltas for one ledger entry."""

    asset: str
    available_delta: Decimal = ZERO
    reserved_delta: Decimal = ZERO

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset", _asset(self.asset))
        object.__setattr__(self, "available_delta", _decimal(self.available_delta, "available delta"))
        object.__setattr__(self, "reserved_delta", _decimal(self.reserved_delta, "reserved delta"))

    @property
    def total_delta(self) -> Decimal:
        return self.available_delta + self.reserved_delta


@dataclass(frozen=True)
class FuturesLedgerEntry:
    """Immutable audit explanation for one collateral change."""

    entry_id: str
    entry_type: FuturesLedgerType
    timestamp: datetime
    order_id: str | None
    fill_id: str | None
    postings: tuple[FuturesLedgerPosting, ...]
    description: str

    def __post_init__(self) -> None:
        if not isinstance(self.entry_id, str) or not self.entry_id.strip():
            raise FuturesPersistenceError("ledger entry id must be non-empty")
        entry_type = _parse_enum(self.entry_type, FuturesLedgerType, "ledger type")
        timestamp = _utc(self.timestamp, "ledger timestamp")
        postings = tuple(self.postings)
        if any(not isinstance(item, FuturesLedgerPosting) for item in postings):
            raise FuturesPersistenceError("ledger postings must be FuturesLedgerPosting objects")
        assets = [item.asset for item in postings]
        if len(assets) != len(set(assets)):
            raise FuturesPersistenceError("ledger entry cannot repeat an asset")
        if not isinstance(self.description, str) or not self.description.strip():
            raise FuturesPersistenceError("ledger description must be non-empty")
        object.__setattr__(self, "entry_id", self.entry_id.strip())
        object.__setattr__(self, "entry_type", entry_type)
        object.__setattr__(self, "timestamp", timestamp)
        object.__setattr__(self, "postings", postings)
        object.__setattr__(self, "description", self.description.strip())


@dataclass(frozen=True)
class FuturesFundingPayment:
    """One explicit funding payment, positive when credited to collateral."""

    payment_id: str
    symbol: str
    position_side: FuturesPositionSide
    rate: Decimal
    notional: Decimal
    amount: Decimal
    applied_at: datetime

    def __post_init__(self) -> None:
        symbol, _, _, _ = _contract_symbol(self.symbol)
        side = _parse_enum(self.position_side, FuturesPositionSide, "funding position side")
        rate = _decimal(self.rate, "funding rate")
        notional = _positive(self.notional, "funding notional")
        amount = _decimal(self.amount, "funding amount")
        applied = _utc(self.applied_at, "funding timestamp")
        if not isinstance(self.payment_id, str) or not self.payment_id.strip():
            raise FuturesValidationError("payment_id must be non-empty")
        object.__setattr__(self, "payment_id", self.payment_id.strip())
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "position_side", side)
        object.__setattr__(self, "rate", rate)
        object.__setattr__(self, "notional", notional)
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "applied_at", applied)


@dataclass(frozen=True)
class FuturesLiquidation:
    """A conservative simulated isolated-margin liquidation record."""

    liquidation_id: str
    symbol: str
    position_side: FuturesPositionSide
    quantity: Decimal
    mark_price: Decimal
    realized_pnl: Decimal
    liquidation_fee: Decimal
    collateral_released: Decimal
    shortfall: Decimal
    liquidated_at: datetime

    def __post_init__(self) -> None:
        symbol, _, _, _ = _contract_symbol(self.symbol)
        side = _parse_enum(self.position_side, FuturesPositionSide, "liquidation side")
        quantity = _positive(self.quantity, "liquidation quantity")
        mark = _positive(self.mark_price, "liquidation mark price")
        pnl = _decimal(self.realized_pnl, "liquidation realized P&L")
        fee = _non_negative(self.liquidation_fee, "liquidation fee")
        released = _non_negative(self.collateral_released, "collateral released")
        shortfall = _non_negative(self.shortfall, "liquidation shortfall")
        timestamp = _utc(self.liquidated_at, "liquidation timestamp")
        if not isinstance(self.liquidation_id, str) or not self.liquidation_id.strip():
            raise FuturesValidationError("liquidation_id must be non-empty")
        object.__setattr__(self, "liquidation_id", self.liquidation_id.strip())
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "position_side", side)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "mark_price", mark)
        object.__setattr__(self, "realized_pnl", pnl)
        object.__setattr__(self, "liquidation_fee", fee)
        object.__setattr__(self, "collateral_released", released)
        object.__setattr__(self, "shortfall", shortfall)
        object.__setattr__(self, "liquidated_at", timestamp)


@dataclass(frozen=True)
class FuturesAuditEvent:
    """Durable event history entry."""

    event_id: str
    event_type: FuturesEventType
    timestamp: datetime
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise FuturesPersistenceError("event id must be non-empty")
        event_type = _parse_enum(self.event_type, FuturesEventType, "event type")
        timestamp = _utc(self.timestamp, "event timestamp")
        if not isinstance(self.payload, Mapping):
            raise FuturesPersistenceError("event payload must be a mapping")
        object.__setattr__(self, "event_id", self.event_id.strip())
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(self, "timestamp", timestamp)
        object.__setattr__(self, "payload", dict(self.payload))


@dataclass(frozen=True)
class FuturesReconciliationReport:
    """Result of an explicit wallet, ledger, order, and position check."""

    is_consistent: bool
    issues: tuple[str, ...]
    ledger_balance: FuturesBalance
    actual_balance: FuturesBalance
    position: FuturesPosition | None


class FuturesPaperEngine:
    """Deterministic isolated-margin Futures paper engine.

    A database path enables durable SQLite state.  ``None`` selects an isolated
    in-memory SQLite database, which is still transactional but does not survive
    process exit.  Reopening a file path restores the persisted Futures state
    and configuration; the market-data safety monitor is intentionally not
    trusted across restart and must receive a fresh validated event.
    """

    def __init__(
        self,
        *,
        contract_rules: FuturesContractRules | None = None,
        margin_config: FuturesMarginConfig | None = None,
        fee_config: FuturesFeeConfig | None = None,
        risk_limits: FuturesRiskLimits | None = None,
        initial_collateral: Decimal | str | int | float = DEFAULT_FUTURES_COLLATERAL,
        database_path: str | Path | None = None,
        safety_monitor: MarketDataSafetyMonitor | None = None,
        clock: Callable[[], datetime] | None = None,
        failure_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.database_path = ":memory:" if database_path is None else str(database_path)
        try:
            self._conn = sqlite3.connect(self.database_path, isolation_level=None)
        except sqlite3.Error as exc:
            raise FuturesPersistenceError(
                f"could not open Futures database {self.database_path!r}"
            ) from exc
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA synchronous = FULL")
        self._closed = False
        self._failure_hook = failure_hook
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.safety_monitor = safety_monitor or MarketDataSafetyMonitor()
        self._create_or_validate_schema()

        supplied_rules = contract_rules or FuturesContractRules()
        supplied_margin = margin_config or FuturesMarginConfig()
        supplied_fee = fee_config or FuturesFeeConfig()
        supplied_risk = risk_limits or FuturesRiskLimits()
        if self._has_engine_config():
            self.contract_rules, self.margin_config, self.fee_config, self.risk_limits = self._load_config()
            self._load_state()
            report = self.reconcile()
            if not report.is_consistent:
                raise FuturesPersistenceError(
                    "persisted Futures state is inconsistent: " + "; ".join(report.issues)
                )
        else:
            if self._has_persisted_rows():
                raise FuturesPersistenceError(
                    "database contains Futures state but no engine configuration"
                )
            self.contract_rules = supplied_rules
            self.margin_config = supplied_margin
            self.fee_config = supplied_fee
            self.risk_limits = supplied_risk
            self._balances: dict[str, FuturesBalance] = {}
            self._orders: dict[str, FuturesOrder] = {}
            self._positions: dict[str, FuturesPosition] = {}
            self._fills: dict[str, FuturesFill] = {}
            self._ledger: list[FuturesLedgerEntry] = []
            self._events: dict[str, FuturesAuditEvent] = {}
            self._funding: dict[str, FuturesFundingPayment] = {}
            self._liquidations: dict[str, FuturesLiquidation] = {}
            collateral = _exact(
                _non_negative(initial_collateral, "initial_collateral"),
                self.contract_rules.collateral_precision,
                "initial_collateral",
            )
            self._balances[self.contract_rules.collateral_asset] = FuturesBalance(
                self.contract_rules.collateral_asset,
                collateral,
                ZERO,
            )
            timestamp = self._now()
            self._ledger.append(
                FuturesLedgerEntry(
                    entry_id="initialization:collateral",
                    entry_type=FuturesLedgerType.INITIALIZATION,
                    timestamp=timestamp,
                    order_id=None,
                    fill_id=None,
                    postings=(FuturesLedgerPosting(self.contract_rules.collateral_asset, available_delta=collateral),),
                    description=f"initialized {collateral} {self.contract_rules.collateral_asset} collateral",
                )
            )
            self._events["engine:initialized"] = FuturesAuditEvent(
                event_id="engine:initialized",
                event_type=FuturesEventType.INITIALIZED,
                timestamp=timestamp,
                payload={"collateral": str(collateral), "asset": self.contract_rules.collateral_asset},
            )
            self._commit_state(
                "initialize",
                balances=dict(self._balances),
                orders=dict(self._orders),
                positions=dict(self._positions),
                fills=dict(self._fills),
                ledger=list(self._ledger),
                events=dict(self._events),
                funding=dict(self._funding),
                liquidations=dict(self._liquidations),
            )

    @classmethod
    def from_run_config(
        cls,
        config: RunConfig,
        **kwargs: Any,
    ) -> "FuturesPaperEngine":
        """Create a Futures engine from the existing validated config."""

        if config.instrument is not Instrument.PERPETUAL_FUTURES:
            raise FuturesValidationError(
                "FuturesPaperEngine requires instrument=perpetual_futures"
            )
        if not config.safety.simulation_only:
            raise FuturesValidationError("Futures paper execution requires simulation_only=true")
        leverage = Decimal(str(config.perpetual_futures.initial_leverage))
        maximum = Decimal(str(config.perpetual_futures.max_leverage))
        return cls(
            contract_rules=FuturesContractRules.for_symbol(config.symbol),
            risk_limits=FuturesRiskLimits(
                default_leverage=leverage,
                max_leverage=maximum,
            ),
            initial_collateral=Decimal(str(config.perpetual_futures.starting_collateral)),
            **kwargs,
        )

    def _now(self, value: datetime | None = None) -> datetime:
        return _utc(value if value is not None else self._clock(), "engine time")

    def close(self) -> None:
        if not self._closed:
            self._conn.close()
            self._closed = True

    def __enter__(self) -> "FuturesPaperEngine":
        self._ensure_open()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise FuturesPersistenceError("Futures engine database is closed")

    def record_market_data(self, event: MarketData, *, now: datetime | None = None) -> DataHealth:
        """Feed a validated event to the shared fail-closed safety monitor."""

        self._ensure_open()
        checked_at = self._now(now)
        if not isinstance(event, MarketData):
            self.safety_monitor.report_invalid_data(
                "market-data event is not a validated MarketData object",
                now=checked_at,
            )
            raise FuturesValidationError(
                "market-data event must be a validated MarketData object"
            )
        if event.symbol.strip().upper() not in {
            self.contract_rules.market_data_symbol,
            self.contract_rules.symbol,
        }:
            self.safety_monitor.report_invalid_data(
                "market-data symbol does not match the Futures contract",
                now=checked_at,
            )
            raise FuturesValidationError(
                "market-data symbol does not match the Futures contract"
            )
        return self.safety_monitor.ingest(event, now=checked_at)

    def report_invalid_market_data(
        self,
        reason: str,
        *,
        now: datetime | None = None,
        kind: Any = None,
        symbol: str | None = None,
    ) -> DataHealth:
        self._ensure_open()
        return self.safety_monitor.report_invalid_data(
            reason,
            now=self._now(now),
            kind=kind,
            symbol=symbol,
        )

    def market_data_health(self, *, now: datetime | None = None) -> DataHealth:
        self._ensure_open()
        return self.safety_monitor.health(self._now(now))

    def balances(self) -> tuple[FuturesBalance, ...]:
        self._ensure_open()
        return tuple(self._balances.values())

    def balance(self, asset: str | None = None) -> FuturesBalance:
        self._ensure_open()
        normalized = asset or self.contract_rules.collateral_asset
        normalized = _asset(normalized)
        return self._balances.get(normalized, FuturesBalance(normalized, ZERO, ZERO))

    def position(self, symbol: str | None = None) -> FuturesPosition | None:
        self._ensure_open()
        key = self.contract_rules.symbol if symbol is None else _contract_symbol(symbol)[0]
        return self._positions.get(key)

    def positions(self) -> tuple[FuturesPosition, ...]:
        self._ensure_open()
        return tuple(self._positions.values())

    def orders(self) -> tuple[FuturesOrder, ...]:
        self._ensure_open()
        return tuple(self._orders.values())

    def order(self, order_id: str) -> FuturesOrder:
        self._ensure_open()
        try:
            return self._orders[order_id]
        except KeyError as exc:
            raise FuturesOrderStateError(f"unknown Futures order {order_id!r}") from exc

    def fills(self, order_id: str | None = None) -> tuple[FuturesFill, ...]:
        self._ensure_open()
        result = tuple(self._fills.values())
        if order_id is not None:
            result = tuple(item for item in result if item.order_id == order_id)
        return result

    def ledger(self) -> tuple[FuturesLedgerEntry, ...]:
        self._ensure_open()
        return tuple(self._ledger)

    def audit_events(self) -> tuple[FuturesAuditEvent, ...]:
        self._ensure_open()
        return tuple(self._events.values())

    def funding_payments(self) -> tuple[FuturesFundingPayment, ...]:
        self._ensure_open()
        return tuple(self._funding.values())

    def liquidations(self) -> tuple[FuturesLiquidation, ...]:
        self._ensure_open()
        return tuple(self._liquidations.values())

    def open_position(
        self,
        side: FuturesPositionSide | str,
        *,
        quantity: Decimal | str | int | float,
        price: Decimal | str | int | float,
        leverage: Decimal | str | int | float | None = None,
        symbol: str | None = None,
        client_order_id: str | None = None,
        now: datetime | None = None,
    ) -> FuturesOrder:
        return self.submit_order(
            action=FuturesOrderAction.OPEN,
            position_side=side,
            quantity=quantity,
            price=price,
            leverage=leverage,
            symbol=symbol,
            client_order_id=client_order_id,
            now=now,
        )

    def reduce_position(
        self,
        side: FuturesPositionSide | str,
        *,
        quantity: Decimal | str | int | float,
        price: Decimal | str | int | float,
        symbol: str | None = None,
        client_order_id: str | None = None,
        now: datetime | None = None,
    ) -> FuturesOrder:
        return self.submit_order(
            action=FuturesOrderAction.REDUCE,
            position_side=side,
            quantity=quantity,
            price=price,
            leverage=None,
            symbol=symbol,
            client_order_id=client_order_id,
            now=now,
        )

    def submit_order(
        self,
        *,
        action: FuturesOrderAction | str,
        position_side: FuturesPositionSide | str,
        quantity: Decimal | str | int | float,
        price: Decimal | str | int | float,
        leverage: Decimal | str | int | float | None = None,
        symbol: str | None = None,
        client_order_id: str | None = None,
        now: datetime | None = None,
    ) -> FuturesOrder:
        """Validate and reserve an explicit open or reduce order."""

        self._ensure_open()
        timestamp = self._now(now)
        action_value = _parse_enum(action, FuturesOrderAction, "order action")
        side_value = _parse_enum(position_side, FuturesPositionSide, "position side")
        request_symbol = self.contract_rules.symbol if symbol is None else _contract_symbol(symbol)[0]
        if request_symbol != self.contract_rules.symbol:
            raise FuturesValidationError(
                f"order symbol must be {self.contract_rules.symbol} for this engine"
            )
        request_quantity = _positive(quantity, "order quantity")
        request_price = _positive(price, "order price")
        existing_position = self._positions.get(request_symbol)
        if client_order_id is not None:
            if not isinstance(client_order_id, str) or not client_order_id.strip():
                raise FuturesValidationError("client_order_id must be non-empty")
            client_order_id = client_order_id.strip()
        leverage_value = (
            self.risk_limits.default_leverage
            if leverage is None
            else _positive(leverage, "order leverage")
        )
        request_quantity = _validate_quantity(
            request_quantity, self.contract_rules, "order quantity"
        )
        request_price = _validate_price(
            request_price, self.contract_rules, "order price"
        )
        if action_value is FuturesOrderAction.REDUCE:
            if leverage is not None:
                raise FuturesValidationError("reduce orders cannot supply leverage")
            if existing_position is not None:
                leverage_value = existing_position.leverage
        elif existing_position is not None and leverage is None:
            # Increasing a net position keeps its original leverage unless the
            # caller explicitly supplies the same value.
            leverage_value = existing_position.leverage

        if client_order_id is not None and client_order_id in {
            order.client_order_id for order in self._orders.values() if order.client_order_id is not None
        }:
            existing = next(order for order in self._orders.values() if order.client_order_id == client_order_id)
            if not self._same_order_request(existing, action_value, side_value, request_symbol, request_quantity, request_price, leverage_value):
                raise FuturesDuplicateOrderError(
                    f"client order id {client_order_id!r} was reused with different parameters"
                )
            return existing

        order_id = self._next_order_id()
        decision, reservation = self._assess_order(
            action_value,
            side_value,
            request_quantity,
            request_price,
            leverage_value,
            timestamp,
        )
        if not decision[0]:
            code, reason = decision[1], decision[2]
            rejected = FuturesOrder(
                order_id=order_id,
                client_order_id=client_order_id,
                symbol=request_symbol,
                action=action_value,
                position_side=side_value,
                quantity=request_quantity,
                price=request_price,
                leverage=leverage_value,
                status=FuturesOrderStatus.REJECTED,
                filled_quantity=ZERO,
                average_fill_price=None,
                fee_paid=ZERO,
                reserved_collateral=ZERO,
                created_at=timestamp,
                updated_at=timestamp,
                rejection_code=code,
                rejection_reason=reason,
            )
            orders = dict(self._orders)
            orders[order_id] = rejected
            events = dict(self._events)
            event_id = f"order:{order_id}:rejected"
            events[event_id] = FuturesAuditEvent(
                event_id=event_id,
                event_type=FuturesEventType.ORDER_REJECTED,
                timestamp=timestamp,
                payload={"code": code.value, "reason": reason},
            )
            ledger = list(self._ledger)
            ledger.append(
                FuturesLedgerEntry(
                    entry_id=f"rejection:{order_id}",
                    entry_type=FuturesLedgerType.REJECTION,
                    timestamp=timestamp,
                    order_id=order_id,
                    fill_id=None,
                    postings=(),
                    description=reason,
                )
            )
            self._commit_state(
                "reject_order",
                balances=dict(self._balances),
                orders=orders,
                positions=dict(self._positions),
                fills=dict(self._fills),
                ledger=ledger,
                events=events,
                funding=dict(self._funding),
                liquidations=dict(self._liquidations),
            )
            return rejected

        balance = self.balance()
        if balance.available < reservation:
            raise FuturesRiskError("risk assessment approved a reservation unavailable in the wallet")
        balances = dict(self._balances)
        balances[self.contract_rules.collateral_asset] = FuturesBalance(
            self.contract_rules.collateral_asset,
            balance.available - reservation,
            balance.reserved + reservation,
        )
        accepted = FuturesOrder(
            order_id=order_id,
            client_order_id=client_order_id,
            symbol=request_symbol,
            action=action_value,
            position_side=side_value,
            quantity=request_quantity,
            price=request_price,
            leverage=leverage_value,
            status=FuturesOrderStatus.ACCEPTED,
            filled_quantity=ZERO,
            average_fill_price=None,
            fee_paid=ZERO,
            reserved_collateral=reservation,
            created_at=timestamp,
            updated_at=timestamp,
        )
        orders = dict(self._orders)
        orders[order_id] = accepted
        ledger = list(self._ledger)
        ledger.append(
            FuturesLedgerEntry(
                entry_id=f"reservation:{order_id}",
                entry_type=FuturesLedgerType.RESERVATION,
                timestamp=timestamp,
                order_id=order_id,
                fill_id=None,
                postings=(FuturesLedgerPosting(self.contract_rules.collateral_asset, available_delta=-reservation, reserved_delta=reservation),),
                description=f"reserved {reservation} {self.contract_rules.collateral_asset} for {action_value.value} order",
            )
        )
        events = dict(self._events)
        event_id = f"order:{order_id}:accepted"
        events[event_id] = FuturesAuditEvent(
            event_id=event_id,
            event_type=FuturesEventType.ORDER_ACCEPTED,
            timestamp=timestamp,
            payload={"action": action_value.value, "side": side_value.value, "quantity": str(request_quantity), "price": str(request_price), "reservation": str(reservation)},
        )
        self._commit_state(
            "accept_order",
            balances=balances,
            orders=orders,
            positions=dict(self._positions),
            fills=dict(self._fills),
            ledger=ledger,
            events=events,
            funding=dict(self._funding),
            liquidations=dict(self._liquidations),
        )
        return accepted

    def cancel_order(self, order_id: str, *, now: datetime | None = None) -> FuturesOrder:
        self._ensure_open()
        timestamp = self._now(now)
        order = self.order(order_id)
        if order.status not in {FuturesOrderStatus.ACCEPTED, FuturesOrderStatus.PARTIALLY_FILLED}:
            raise FuturesOrderStateError(
                f"order {order_id} cannot be cancelled from {order.status.value}"
            )
        balance = self.balance()
        if balance.reserved < order.reserved_collateral:
            raise FuturesReconciliationError("order reservation exceeds wallet reserved collateral")
        balances = dict(self._balances)
        balances[self.contract_rules.collateral_asset] = FuturesBalance(
            self.contract_rules.collateral_asset,
            balance.available + order.reserved_collateral,
            balance.reserved - order.reserved_collateral,
        )
        cancelled = replace(
            order,
            status=FuturesOrderStatus.CANCELLED,
            reserved_collateral=ZERO,
            updated_at=timestamp,
        )
        orders = dict(self._orders)
        orders[order_id] = cancelled
        ledger = list(self._ledger)
        ledger.append(
            FuturesLedgerEntry(
                entry_id=f"release:{order_id}",
                entry_type=FuturesLedgerType.RELEASE,
                timestamp=timestamp,
                order_id=order_id,
                fill_id=None,
                postings=(FuturesLedgerPosting(self.contract_rules.collateral_asset, available_delta=order.reserved_collateral, reserved_delta=-order.reserved_collateral),),
                description=f"released reservation for cancelled order {order_id}",
            )
        )
        events = dict(self._events)
        event_id = f"order:{order_id}:cancelled"
        events[event_id] = FuturesAuditEvent(
            event_id=event_id,
            event_type=FuturesEventType.ORDER_CANCELLED,
            timestamp=timestamp,
            payload={"released": str(order.reserved_collateral)},
        )
        self._commit_state(
            "cancel_order",
            balances=balances,
            orders=orders,
            positions=dict(self._positions),
            fills=dict(self._fills),
            ledger=ledger,
            events=events,
            funding=dict(self._funding),
            liquidations=dict(self._liquidations),
        )
        return cancelled

    def execute_fill(
        self,
        order_id: str,
        *,
        quantity: Decimal | str | int | float,
        price: Decimal | str | int | float,
        fill_id: str | None = None,
        now: datetime | None = None,
    ) -> FuturesFill:
        """Apply one explicit-price fill transactionally."""

        self._ensure_open()
        timestamp = self._now(now)
        order = self.order(order_id)
        fill_quantity = _positive(quantity, "fill quantity")
        fill_price = _positive(price, "fill price")
        if fill_id is None:
            fill_id = f"{order_id}-fill-{len(order.fill_ids) + 1:06d}"
        if not isinstance(fill_id, str) or not fill_id.strip():
            raise FuturesExecutionError("fill_id must be non-empty")
        fill_id = fill_id.strip()
        existing = self._fills.get(fill_id)
        if existing is not None:
            if existing.order_id != order_id or existing.quantity != fill_quantity or existing.price != fill_price:
                raise FuturesDuplicateFillError(
                    f"fill id {fill_id!r} was reused with different parameters"
                )
            return existing
        if order.status not in {FuturesOrderStatus.ACCEPTED, FuturesOrderStatus.PARTIALLY_FILLED}:
            raise FuturesOrderStateError(f"order {order_id} cannot be filled from {order.status.value}")
        fill_quantity = _validate_quantity(
            fill_quantity, self.contract_rules, "fill quantity"
        )
        fill_price = _validate_price(fill_price, self.contract_rules, "fill price")
        if fill_quantity > order.remaining_quantity:
            raise FuturesExecutionError("fill quantity exceeds order remaining quantity")
        if fill_price != order.price:
            raise FuturesExecutionError("Futures paper fills must use the order's explicitly supplied price")
        health = self.safety_monitor.health(timestamp)
        if not health.allow_new_positions:
            raise FuturesExecutionError(f"market-data safety is {health.status.value}: {health.reason}")
        position = self._positions.get(order.symbol)
        if order.action is FuturesOrderAction.OPEN:
            if position is not None and position.side is not order.position_side:
                raise FuturesExecutionError("cannot open through an opposite existing position")
            new_position, new_margin, realized_pnl, margin_released = self._apply_open_fill(position, order, fill_quantity, fill_price, timestamp)
        else:
            if position is None or position.side is not order.position_side:
                raise FuturesExecutionError("reduce fill requires a matching open position")
            new_position, new_margin, realized_pnl, margin_released = self._apply_reduce_fill(position, order, fill_quantity, fill_price, timestamp)
        notional = _quantize(fill_quantity * fill_price * self.contract_rules.contract_multiplier, self.contract_rules.collateral_precision, "fill notional")
        fee = self.fee_config.amount(notional, self.contract_rules.collateral_precision)
        remaining = order.remaining_quantity - fill_quantity
        next_reservation = (
            _quantize(
                order.reserved_collateral * remaining / order.remaining_quantity,
                self.contract_rules.collateral_precision,
                "remaining order reservation",
            )
            if remaining > ZERO
            else ZERO
        )
        old_balance = self.balance()
        if order.action is FuturesOrderAction.OPEN:
            margin_added = new_margin - (position.margin if position is not None else ZERO)
            available_delta = order.reserved_collateral - margin_added - fee - next_reservation
            reserved_delta = margin_added + next_reservation - order.reserved_collateral
        else:
            available_delta = order.reserved_collateral - fee - next_reservation + margin_released + realized_pnl
            reserved_delta = next_reservation - order.reserved_collateral - margin_released
        new_available = old_balance.available + available_delta
        new_reserved = old_balance.reserved + reserved_delta
        if new_available < ZERO or new_reserved < ZERO:
            raise FuturesExecutionError("fill would create negative available or reserved collateral")
        balances = dict(self._balances)
        balances[self.contract_rules.collateral_asset] = FuturesBalance(
            self.contract_rules.collateral_asset,
            new_available,
            new_reserved,
        )
        new_filled = order.filled_quantity + fill_quantity
        average = fill_price if order.average_fill_price is None else _quantize((order.average_fill_price * order.filled_quantity + fill_price * fill_quantity) / new_filled, self.contract_rules.price_precision, "average fill price")
        status = FuturesOrderStatus.FILLED if new_filled == order.quantity else FuturesOrderStatus.PARTIALLY_FILLED
        updated_order = replace(
            order,
            status=status,
            filled_quantity=new_filled,
            average_fill_price=average,
            fee_paid=order.fee_paid + fee,
            reserved_collateral=next_reservation,
            updated_at=timestamp,
            fill_ids=order.fill_ids + (fill_id,),
        )
        fill = FuturesFill(
            fill_id=fill_id,
            order_id=order_id,
            symbol=order.symbol,
            action=order.action,
            position_side=order.position_side,
            quantity=fill_quantity,
            price=fill_price,
            notional=notional,
            fee=fee,
            realized_pnl=realized_pnl,
            margin_released=margin_released,
            executed_at=timestamp,
        )
        orders = dict(self._orders)
        orders[order_id] = updated_order
        positions = dict(self._positions)
        if new_position is None:
            positions.pop(order.symbol, None)
        else:
            positions[order.symbol] = new_position
        fills = dict(self._fills)
        fills[fill_id] = fill
        ledger = list(self._ledger)
        ledger.append(
            FuturesLedgerEntry(
                entry_id=f"fill:{order_id}:{fill_id}",
                entry_type=FuturesLedgerType.FILL,
                timestamp=timestamp,
                order_id=order_id,
                fill_id=fill_id,
                postings=(FuturesLedgerPosting(self.contract_rules.collateral_asset, available_delta=available_delta, reserved_delta=reserved_delta),),
                description=f"{order.action.value} {order.position_side.value} fill at {fill_price}; fee {fee}; realized P&L {realized_pnl}",
            )
        )
        events = dict(self._events)
        event_id = f"fill:{fill_id}"
        events[event_id] = FuturesAuditEvent(
            event_id=event_id,
            event_type=FuturesEventType.FILL_APPLIED,
            timestamp=timestamp,
            payload={"order_id": order_id, "quantity": str(fill_quantity), "price": str(fill_price), "fee": str(fee), "realized_pnl": str(realized_pnl), "margin_released": str(margin_released)},
        )
        self._commit_state(
            "execute_fill",
            balances=balances,
            orders=orders,
            positions=positions,
            fills=fills,
            ledger=ledger,
            events=events,
            funding=dict(self._funding),
            liquidations=dict(self._liquidations),
        )
        return fill

    def mark_to_market(
        self,
        mark_price: Decimal | str | int | float,
        *,
        now: datetime | None = None,
        symbol: str | None = None,
    ) -> FuturesPosition | None:
        """Update the mark and liquidate when equity reaches maintenance margin.

        This is an intentionally conservative isolated-margin model, not a
        claim to reproduce any exchange's liquidation engine.
        """

        self._ensure_open()
        timestamp = self._now(now)
        price = _positive(mark_price, "mark price")
        price = _validate_price(price, self.contract_rules, "mark price")
        health = self.safety_monitor.health(timestamp)
        if not health.allow_new_positions:
            raise FuturesRiskError(f"market-data safety is {health.status.value}: {health.reason}")
        key = self.contract_rules.symbol if symbol is None else _contract_symbol(symbol)[0]
        position = self._positions.get(key)
        if position is None:
            return None
        notional = _quantize(position.quantity * price * self.contract_rules.contract_multiplier, self.contract_rules.collateral_precision, "mark notional")
        maintenance = self.margin_config.maintenance_margin(notional, self.contract_rules.collateral_precision)
        updated = replace(position, mark_price=price, maintenance_margin=maintenance, updated_at=timestamp)
        equity = updated.equity(self.contract_rules)
        if equity <= maintenance:
            return self._liquidate(updated, timestamp)
        positions = dict(self._positions)
        positions[key] = updated
        events = dict(self._events)
        # Include the explicit price in the durable identity: a repeated mark
        # is idempotent, while two different marks at one timestamp remain
        # auditable rather than silently replacing one another.
        event_id = f"mark:{key}:{_timestamp_text(timestamp)}:{price}"
        events[event_id] = FuturesAuditEvent(
            event_id=event_id,
            event_type=FuturesEventType.MARK_UPDATED,
            timestamp=timestamp,
            payload={"symbol": key, "mark_price": str(price), "equity": str(equity), "maintenance_margin": str(maintenance)},
        )
        self._commit_state(
            "mark_to_market",
            balances=dict(self._balances),
            orders=dict(self._orders),
            positions=positions,
            fills=dict(self._fills),
            ledger=list(self._ledger),
            events=events,
            funding=dict(self._funding),
            liquidations=dict(self._liquidations),
        )
        return updated

    def apply_funding(
        self,
        rate: Decimal | str | int | float,
        *,
        payment_id: str,
        now: datetime | None = None,
        symbol: str | None = None,
    ) -> FuturesFundingPayment:
        """Apply one explicit funding event; positive rates charge longs."""

        self._ensure_open()
        timestamp = self._now(now)
        if not isinstance(payment_id, str) or not payment_id.strip():
            raise FuturesValidationError("payment_id must be non-empty")
        payment_id = payment_id.strip()
        funding_rate = _decimal(rate, "funding rate")
        key = self.contract_rules.symbol if symbol is None else _contract_symbol(symbol)[0]
        if key != self.contract_rules.symbol:
            raise FuturesValidationError(
                f"funding symbol must be {self.contract_rules.symbol} for this engine"
            )
        existing = self._funding.get(payment_id)
        if existing is not None:
            if existing.rate != funding_rate or existing.symbol != key:
                raise FuturesDuplicateFundingError(
                    f"funding payment {payment_id!r} was reused with different parameters"
                )
            return existing
        health = self.safety_monitor.health(timestamp)
        if not health.allow_new_positions:
            raise FuturesRiskError(f"market-data safety is {health.status.value}: {health.reason}")
        position = self._positions.get(key)
        if position is None:
            raise FuturesRiskError("funding requires an open position")
        notional = _quantize(position.quantity * position.mark_price * self.contract_rules.contract_multiplier, self.contract_rules.collateral_precision, "funding notional")
        absolute = _quantize(notional * funding_rate.copy_abs(), self.contract_rules.collateral_precision, "funding amount")
        signed = -absolute if position.side is FuturesPositionSide.LONG else absolute
        balances = dict(self._balances)
        balance = self.balance()
        position_after = position
        available_delta = signed
        reserved_delta = ZERO
        if signed < ZERO and balance.available + signed < ZERO:
            margin_use = -(balance.available + signed)
            if margin_use > position.margin:
                raise FuturesRiskError("funding payment exceeds available collateral and position margin")
            position_after = replace(position, margin=position.margin - margin_use, updated_at=timestamp)
            available_delta = -balance.available
            reserved_delta = -margin_use
        new_available = balance.available + available_delta
        new_reserved = balance.reserved + reserved_delta
        if new_available < ZERO or new_reserved < ZERO:
            raise FuturesRiskError("funding would create a negative collateral balance")
        balances[self.contract_rules.collateral_asset] = FuturesBalance(self.contract_rules.collateral_asset, new_available, new_reserved)
        payment = FuturesFundingPayment(
            payment_id=payment_id,
            symbol=key,
            position_side=position.side,
            rate=funding_rate,
            notional=notional,
            amount=signed,
            applied_at=timestamp,
        )
        funding = dict(self._funding)
        funding[payment_id] = payment
        positions = dict(self._positions)
        positions[key] = position_after
        ledger = list(self._ledger)
        ledger.append(
            FuturesLedgerEntry(
                entry_id=f"funding:{payment_id}",
                entry_type=FuturesLedgerType.FUNDING,
                timestamp=timestamp,
                order_id=None,
                fill_id=None,
                postings=(FuturesLedgerPosting(self.contract_rules.collateral_asset, available_delta=available_delta, reserved_delta=reserved_delta),),
                description=f"funding payment {signed} at rate {funding_rate}",
            )
        )
        events = dict(self._events)
        events[f"funding:{payment_id}"] = FuturesAuditEvent(
            event_id=f"funding:{payment_id}",
            event_type=FuturesEventType.FUNDING_APPLIED,
            timestamp=timestamp,
            payload={"rate": str(funding_rate), "amount": str(signed), "notional": str(notional)},
        )
        self._commit_state(
            "apply_funding",
            balances=balances,
            orders=dict(self._orders),
            positions=positions,
            fills=dict(self._fills),
            ledger=ledger,
            events=events,
            funding=funding,
            liquidations=dict(self._liquidations),
        )
        return payment

    def reconcile(self) -> FuturesReconciliationReport:
        """Check wallet/ledger balances, reservations, and position invariants."""

        self._ensure_open()
        expected_available = ZERO
        expected_reserved = ZERO
        for entry in self._ledger:
            for posting in entry.postings:
                if posting.asset == self.contract_rules.collateral_asset:
                    expected_available += posting.available_delta
                    expected_reserved += posting.reserved_delta
                elif posting.available_delta != ZERO or posting.reserved_delta != ZERO:
                    return FuturesReconciliationReport(False, (f"unexpected asset posting {posting.asset}",), self.balance(), self.balance(), self.position())
        actual = self.balance()
        issues: list[str] = []
        if expected_available != actual.available:
            issues.append(f"ledger available {expected_available} != actual {actual.available}")
        if expected_reserved != actual.reserved:
            issues.append(f"ledger reserved {expected_reserved} != actual {actual.reserved}")
        if actual.available < ZERO or actual.reserved < ZERO:
            issues.append("wallet balance is negative")
        position = self.position()
        position_margin = position.margin if position is not None else ZERO
        order_reservations = sum(
            order.reserved_collateral
            for order in self._orders.values()
            if not order.is_terminal
        )
        if position_margin + order_reservations != actual.reserved:
            issues.append(
                f"reserved collateral {actual.reserved} != position margin {position_margin} plus open order reservations {order_reservations}"
            )
        if position is not None:
            if position.quantity <= ZERO or position.margin < ZERO:
                issues.append("position quantity or margin is invalid")
            if position.maintenance_margin < ZERO:
                issues.append("maintenance margin is negative")
        for order in self._orders.values():
            if order.is_terminal and order.reserved_collateral != ZERO:
                issues.append(f"terminal order {order.order_id} retains collateral reservation")
        return FuturesReconciliationReport(
            is_consistent=not issues,
            issues=tuple(issues),
            ledger_balance=FuturesBalance(actual.asset, expected_available, expected_reserved),
            actual_balance=actual,
            position=position,
        )

    def _assess_order(
        self,
        action: FuturesOrderAction,
        side: FuturesPositionSide,
        quantity: Decimal,
        price: Decimal,
        leverage: Decimal,
        timestamp: datetime,
    ) -> tuple[tuple[bool, FuturesRiskCode, str], Decimal]:
        health = self.safety_monitor.health(timestamp)
        if not health.allow_new_positions:
            return (False, FuturesRiskCode.MARKET_DATA_UNSAFE, f"market-data safety is {health.status.value}: {health.reason}"), ZERO
        if leverage > self.risk_limits.max_leverage:
            return (False, FuturesRiskCode.LEVERAGE_LIMIT, f"leverage {leverage} exceeds maximum {self.risk_limits.max_leverage}"), ZERO
        notional = _quantize(
            quantity * price * self.contract_rules.contract_multiplier,
            self.contract_rules.collateral_precision,
            "order notional",
        )
        if notional <= ZERO:
            return (
                False,
                FuturesRiskCode.EXPOSURE_LIMIT,
                "order notional rounds below collateral precision",
            ), ZERO
        if self.risk_limits.max_order_notional is not None and notional > self.risk_limits.max_order_notional:
            return (False, FuturesRiskCode.EXPOSURE_LIMIT, f"order notional {notional} exceeds configured maximum"), ZERO
        current = self._positions.get(self.contract_rules.symbol)
        if action is FuturesOrderAction.REDUCE:
            if current is None:
                return (False, FuturesRiskCode.POSITION_NOT_FOUND, "reduce order requires an open position"), ZERO
            if current.side is not side:
                return (False, FuturesRiskCode.POSITION_SIDE_MISMATCH, "reduce side does not match the open position"), ZERO
            if quantity > current.quantity:
                return (False, FuturesRiskCode.POSITION_LIMIT, "reduce quantity exceeds the open position"), ZERO
        else:
            if current is not None and current.side is not side:
                return (False, FuturesRiskCode.POSITION_SIDE_MISMATCH, "cannot open an opposite position without closing first"), ZERO
            existing_quantity = current.quantity if current is not None else ZERO
            projected_notional = _quantize((existing_quantity + quantity) * price * self.contract_rules.contract_multiplier, self.contract_rules.collateral_precision, "projected position notional")
            if self.risk_limits.max_position_notional is not None and projected_notional > self.risk_limits.max_position_notional:
                return (False, FuturesRiskCode.POSITION_LIMIT, "projected position exceeds configured maximum"), ZERO
            if current is not None and current.leverage != leverage:
                return (False, FuturesRiskCode.LEVERAGE_LIMIT, "increasing a position must keep its existing leverage"), ZERO
        open_count = sum(not order.is_terminal for order in self._orders.values())
        if self.risk_limits.max_open_orders is not None and open_count >= self.risk_limits.max_open_orders:
            return (False, FuturesRiskCode.OPEN_ORDER_LIMIT, "maximum open Futures orders reached"), ZERO
        fee = self.fee_config.amount(notional, self.contract_rules.collateral_precision)
        if action is FuturesOrderAction.OPEN:
            reservation = self.margin_config.initial_margin(notional, leverage, self.contract_rules.collateral_precision) + fee
        else:
            assert current is not None
            margin_release = _quantize(current.margin * quantity / current.quantity, self.contract_rules.collateral_precision, "estimated margin release")
            realized = self._realized_pnl(current, quantity, price)
            reservation = max(ZERO, fee - margin_release - realized)
            reservation = _quantize(reservation, self.contract_rules.collateral_precision, "reduce reservation")
        if self.balance().available < reservation:
            return (False, FuturesRiskCode.INSUFFICIENT_COLLATERAL, f"available collateral {self.balance().available} is below required reservation {reservation}"), reservation
        return (True, FuturesRiskCode.INSUFFICIENT_COLLATERAL, "risk checks passed"), reservation

    def _apply_open_fill(
        self,
        position: FuturesPosition | None,
        order: FuturesOrder,
        quantity: Decimal,
        price: Decimal,
        timestamp: datetime,
    ) -> tuple[FuturesPosition, Decimal, Decimal, Decimal]:
        notional = quantity * price * self.contract_rules.contract_multiplier
        added_margin = self.margin_config.initial_margin(notional, order.leverage, self.contract_rules.collateral_precision)
        if position is None:
            total_quantity = quantity
            entry = price
            margin = added_margin
            realized = ZERO
            opened = timestamp
        else:
            total_quantity = position.quantity + quantity
            entry = _quantize((position.entry_price * position.quantity + price * quantity) / total_quantity, self.contract_rules.price_precision, "weighted entry price")
            margin = position.margin + added_margin
            realized = position.realized_pnl
            opened = position.opened_at
        mark_notional = _quantize(total_quantity * price * self.contract_rules.contract_multiplier, self.contract_rules.collateral_precision, "position notional")
        maintenance = self.margin_config.maintenance_margin(mark_notional, self.contract_rules.collateral_precision)
        return FuturesPosition(order.symbol, order.position_side, total_quantity, entry, price, order.leverage, margin, realized, maintenance, opened, timestamp), margin, ZERO, ZERO

    def _apply_reduce_fill(
        self,
        position: FuturesPosition,
        order: FuturesOrder,
        quantity: Decimal,
        price: Decimal,
        timestamp: datetime,
    ) -> tuple[FuturesPosition | None, Decimal, Decimal, Decimal]:
        realized = self._realized_pnl(position, quantity, price)
        margin_released = _quantize(position.margin * quantity / position.quantity, self.contract_rules.collateral_precision, "margin released")
        remaining = position.quantity - quantity
        if remaining == ZERO:
            return None, ZERO, realized, margin_released
        new_margin = position.margin - margin_released
        mark_notional = _quantize(remaining * price * self.contract_rules.contract_multiplier, self.contract_rules.collateral_precision, "position notional")
        maintenance = self.margin_config.maintenance_margin(mark_notional, self.contract_rules.collateral_precision)
        updated = replace(
            position,
            quantity=remaining,
            mark_price=price,
            margin=new_margin,
            realized_pnl=position.realized_pnl + realized,
            maintenance_margin=maintenance,
            updated_at=timestamp,
        )
        return updated, new_margin, realized, margin_released

    def _realized_pnl(self, position: FuturesPosition, quantity: Decimal, exit_price: Decimal) -> Decimal:
        difference = exit_price - position.entry_price
        if position.side is FuturesPositionSide.SHORT:
            difference = -difference
        return _quantize(difference * quantity * self.contract_rules.contract_multiplier, self.contract_rules.collateral_precision, "realized P&L")

    def _liquidate(self, position: FuturesPosition, timestamp: datetime) -> FuturesPosition | None:
        notional = _quantize(position.quantity * position.mark_price * self.contract_rules.contract_multiplier, self.contract_rules.collateral_precision, "liquidation notional")
        pnl = _quantize(position.unrealized_pnl(self.contract_rules), self.contract_rules.collateral_precision, "liquidation P&L")
        fee = _quantize(notional * self.margin_config.liquidation_fee_rate, self.contract_rules.collateral_precision, "liquidation fee")
        net = position.margin + pnl - fee
        released = max(ZERO, net)
        shortfall = max(ZERO, -net)
        balance = self.balance()
        balances = dict(self._balances)
        new_reserved = balance.reserved - position.margin
        new_available = balance.available + released
        if new_reserved < ZERO:
            raise FuturesReconciliationError("position margin exceeds reserved collateral during liquidation")
        balances[self.contract_rules.collateral_asset] = FuturesBalance(self.contract_rules.collateral_asset, new_available, new_reserved)
        orders = dict(self._orders)
        ledger = list(self._ledger)
        events = dict(self._events)
        for order in tuple(orders.values()):
            if not order.is_terminal and order.symbol == position.symbol:
                orders[order.order_id] = replace(order, status=FuturesOrderStatus.CANCELLED, reserved_collateral=ZERO, updated_at=timestamp)
                ledger.append(
                    FuturesLedgerEntry(
                        entry_id=f"liquidation-release:{order.order_id}",
                        entry_type=FuturesLedgerType.RELEASE,
                        timestamp=timestamp,
                        order_id=order.order_id,
                        fill_id=None,
                        postings=(FuturesLedgerPosting(self.contract_rules.collateral_asset, available_delta=order.reserved_collateral, reserved_delta=-order.reserved_collateral),),
                        description="released order reservation during liquidation",
                    )
                )
                events[f"order:{order.order_id}:liquidation-cancel"] = FuturesAuditEvent(
                    event_id=f"order:{order.order_id}:liquidation-cancel",
                    event_type=FuturesEventType.ORDER_CANCELLED,
                    timestamp=timestamp,
                    payload={"reason": "position liquidated"},
                )
                new_available += order.reserved_collateral
                new_reserved -= order.reserved_collateral
        balances[self.contract_rules.collateral_asset] = FuturesBalance(self.contract_rules.collateral_asset, new_available, new_reserved)
        liquidation_prefix = f"liquidation:{position.symbol}:{_timestamp_text(timestamp)}"
        liquidation_id = liquidation_prefix
        suffix = 1
        while liquidation_id in self._liquidations:
            suffix += 1
            liquidation_id = f"{liquidation_prefix}:{suffix}"
        liquidation = FuturesLiquidation(
            liquidation_id,
            position.symbol,
            position.side,
            position.quantity,
            position.mark_price,
            pnl,
            fee,
            released,
            shortfall,
            timestamp,
        )
        liquidations = dict(self._liquidations)
        liquidations[liquidation_id] = liquidation
        ledger.append(
            FuturesLedgerEntry(
                entry_id=liquidation_id,
                entry_type=FuturesLedgerType.LIQUIDATION,
                timestamp=timestamp,
                order_id=None,
                fill_id=None,
                postings=(FuturesLedgerPosting(self.contract_rules.collateral_asset, available_delta=released, reserved_delta=-position.margin),),
                description=f"simulated liquidation at mark {position.mark_price}; shortfall {shortfall}",
            )
        )
        events[f"liquidation:{liquidation_id}"] = FuturesAuditEvent(
            event_id=f"liquidation:{liquidation_id}",
            event_type=FuturesEventType.LIQUIDATED,
            timestamp=timestamp,
            payload={"mark_price": str(position.mark_price), "realized_pnl": str(pnl), "fee": str(fee), "shortfall": str(shortfall)},
        )
        positions = dict(self._positions)
        positions.pop(position.symbol, None)
        self._commit_state(
            "liquidate",
            balances=balances,
            orders=orders,
            positions=positions,
            fills=dict(self._fills),
            ledger=ledger,
            events=events,
            funding=dict(self._funding),
            liquidations=liquidations,
        )
        return None

    def _same_order_request(
        self,
        order: FuturesOrder,
        action: FuturesOrderAction,
        side: FuturesPositionSide,
        symbol: str,
        quantity: Decimal,
        price: Decimal,
        leverage: Decimal,
    ) -> bool:
        return order.action is action and order.position_side is side and order.symbol == symbol and order.quantity == quantity and order.price == price and order.leverage == leverage

    def _next_order_id(self) -> str:
        number = 1
        while f"order-{number:06d}" in self._orders:
            number += 1
        return f"order-{number:06d}"

    def _commit_state(
        self,
        operation: str,
        *,
        balances: dict[str, FuturesBalance],
        orders: dict[str, FuturesOrder],
        positions: dict[str, FuturesPosition],
        fills: dict[str, FuturesFill],
        ledger: list[FuturesLedgerEntry],
        events: dict[str, FuturesAuditEvent],
        funding: dict[str, FuturesFundingPayment],
        liquidations: dict[str, FuturesLiquidation],
    ) -> None:
        self._ensure_open()
        try:
            with self._transaction():
                self._write_state(
                    balances=balances,
                    orders=orders,
                    positions=positions,
                    fills=fills,
                    ledger=ledger,
                    events=events,
                    funding=funding,
                    liquidations=liquidations,
                )
                if self._failure_hook is not None:
                    self._failure_hook(operation)
        except Exception:
            raise
        self._balances = balances
        self._orders = orders
        self._positions = positions
        self._fills = fills
        self._ledger = ledger
        self._events = events
        self._funding = funding
        self._liquidations = liquidations

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _create_or_validate_schema(self) -> None:
        schema_exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        ).fetchone() is not None
        if not schema_exists:
            with self._transaction():
                self._create_tables()
                self._conn.execute("INSERT INTO schema_meta(id, version) VALUES(1, ?)", (SCHEMA_VERSION,))
            return
        row = self._conn.execute("SELECT version FROM schema_meta WHERE id=1").fetchone()
        if row is None:
            raise FuturesPersistenceError("schema_meta is missing its singleton version row")
        try:
            version = int(row[0])
        except (TypeError, ValueError) as exc:
            raise FuturesPersistenceError("schema version is not an integer") from exc
        if version < 0:
            raise FuturesPersistenceError(f"database schema version {version} is invalid")
        if version > SCHEMA_VERSION:
            raise FuturesPersistenceError(f"database schema version {version} is newer than supported {SCHEMA_VERSION}")
        if version == 0:
            with self._transaction():
                self._create_tables()
                self._conn.execute("UPDATE schema_meta SET version=? WHERE id=1", (SCHEMA_VERSION,))
        elif version == SCHEMA_VERSION:
            tables = {row[0] for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = sorted(self._required_tables() - tables)
            if missing:
                raise FuturesPersistenceError("database is incomplete; missing tables: " + ", ".join(missing))

    @staticmethod
    def _required_tables() -> set[str]:
        return {"schema_meta", "engine_config", "wallet_balances", "orders", "positions", "fills", "ledger_entries", "events", "funding_payments", "liquidations"}

    def _create_tables(self) -> None:
        # Keep DDL on the caller's transaction.  sqlite3.executescript()
        # implicitly commits a pending transaction, which would make a schema
        # creation failure impossible to roll back cleanly.
        statements = (
            """CREATE TABLE IF NOT EXISTS schema_meta (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                version INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS engine_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS wallet_balances (
                asset TEXT PRIMARY KEY,
                available TEXT NOT NULL,
                reserved TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS fills (
                fill_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS ledger_entries (
                entry_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                payload TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS funding_payments (
                payment_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS liquidations (
                liquidation_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            )""",
        )
        for statement in statements:
            self._conn.execute(statement)

    def _has_engine_config(self) -> bool:
        return self._conn.execute("SELECT 1 FROM engine_config LIMIT 1").fetchone() is not None

    def _has_persisted_rows(self) -> bool:
        """Reject partially initialized stores instead of overwriting them."""

        for table in (
            "wallet_balances",
            "orders",
            "positions",
            "fills",
            "ledger_entries",
            "events",
            "funding_payments",
            "liquidations",
        ):
            if self._conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
                return True
        return False

    def _config_payload(self) -> dict[str, str]:
        return {
            "contract_rules": _json_value(_rules_to_dict(self.contract_rules)),
            "margin_config": _json_value(_margin_to_dict(self.margin_config)),
            "fee_config": _json_value({"rate": str(self.fee_config.rate)}),
            "risk_limits": _json_value(_risk_to_dict(self.risk_limits)),
        }

    def _load_config(self) -> tuple[FuturesContractRules, FuturesMarginConfig, FuturesFeeConfig, FuturesRiskLimits]:
        rows = {row[0]: row[1] for row in self._conn.execute("SELECT key, value FROM engine_config")}
        required = {"contract_rules", "margin_config", "fee_config", "risk_limits"}
        missing = sorted(required - set(rows))
        if missing:
            raise FuturesPersistenceError(
                "engine configuration is incomplete: " + ", ".join(missing)
            )
        unexpected = sorted(set(rows) - required)
        if unexpected:
            raise FuturesPersistenceError(
                "engine configuration has unknown keys: " + ", ".join(unexpected)
            )
        try:
            rules_data = _json_object(rows["contract_rules"], "contract_rules")
            margin_data = _json_object(rows["margin_config"], "margin_config")
            fee_data = _json_object(rows["fee_config"], "fee_config")
            risk_data = _json_object(rows["risk_limits"], "risk_limits")
            return _rules_from_dict(rules_data), _margin_from_dict(margin_data), _fee_from_dict(fee_data), _risk_from_dict(risk_data)
        except (FuturesValidationError, KeyError, TypeError) as exc:
            raise FuturesPersistenceError("persisted engine configuration is invalid") from exc

    def _load_state(self) -> None:
        try:
            self._balances = {}
            for row in self._conn.execute("SELECT asset, available, reserved FROM wallet_balances ORDER BY asset"):
                item = FuturesBalance(row[0], _decimal(row[1], "persisted available"), _decimal(row[2], "persisted reserved"))
                if item.asset != row[0]:
                    raise FuturesPersistenceError("wallet table key is not normalized")
                self._balances[item.asset] = item
            self._orders = {}
            for row in self._conn.execute("SELECT order_id, payload FROM orders ORDER BY order_id"):
                item = _order_from_dict(_json_object(row[1], "order"))
                if item.order_id != row[0]:
                    raise FuturesPersistenceError("order table key does not match payload")
                self._orders[item.order_id] = item
            self._positions = {}
            for row in self._conn.execute("SELECT symbol, payload FROM positions ORDER BY symbol"):
                item = _position_from_dict(_json_object(row[1], "position"))
                if item.symbol != row[0]:
                    raise FuturesPersistenceError("position table key does not match payload")
                self._positions[item.symbol] = item
            self._fills = {}
            for row in self._conn.execute("SELECT fill_id, payload FROM fills ORDER BY fill_id"):
                item = _fill_from_dict(_json_object(row[1], "fill"))
                if item.fill_id != row[0]:
                    raise FuturesPersistenceError("fill table key does not match payload")
                self._fills[item.fill_id] = item
            self._ledger = []
            for row in self._conn.execute("SELECT entry_id, payload FROM ledger_entries ORDER BY rowid"):
                item = _ledger_from_dict(_json_object(row[1], "ledger entry"))
                if item.entry_id != row[0]:
                    raise FuturesPersistenceError("ledger table key does not match payload")
                self._ledger.append(item)
            self._events = {}
            for row in self._conn.execute("SELECT event_id, event_type, timestamp, payload FROM events ORDER BY rowid"):
                item = FuturesAuditEvent(row[0], row[1], _parse_timestamp(row[2], "event"), _json_object(row[3], "event payload"))
                if item.event_id != row[0]:
                    raise FuturesPersistenceError("event table key does not match payload")
                self._events[item.event_id] = item
            self._funding = {}
            for row in self._conn.execute("SELECT payment_id, payload FROM funding_payments ORDER BY payment_id"):
                item = _funding_from_dict(_json_object(row[1], "funding"))
                if item.payment_id != row[0]:
                    raise FuturesPersistenceError("funding table key does not match payload")
                self._funding[item.payment_id] = item
            self._liquidations = {}
            for row in self._conn.execute("SELECT liquidation_id, payload FROM liquidations ORDER BY liquidation_id"):
                item = _liquidation_from_dict(_json_object(row[1], "liquidation"))
                if item.liquidation_id != row[0]:
                    raise FuturesPersistenceError("liquidation table key does not match payload")
                self._liquidations[item.liquidation_id] = item
            self._validate_loaded_state()
        except (sqlite3.Error, FuturesValidationError, FuturesPersistenceError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, FuturesPersistenceError):
                raise
            raise FuturesPersistenceError("persisted Futures state is corrupt") from exc

    def _validate_loaded_state(self) -> None:
        """Validate cross-table relationships before a recovered engine is used."""

        collateral = self.contract_rules.collateral_asset
        if set(self._balances) != {collateral}:
            raise FuturesPersistenceError(
                "persisted wallet must contain exactly the configured collateral asset"
            )
        ledger_ids = [entry.entry_id for entry in self._ledger]
        if len(ledger_ids) != len(set(ledger_ids)):
            raise FuturesPersistenceError("persisted ledger entry identifiers are not unique")
        if "initialization:collateral" not in set(ledger_ids):
            raise FuturesPersistenceError("persisted ledger has no collateral initialization entry")
        if set(self._positions) - {self.contract_rules.symbol}:
            raise FuturesPersistenceError("persisted position uses an unsupported contract symbol")
        for key, order in self._orders.items():
            if order.order_id != key or order.symbol != self.contract_rules.symbol:
                raise FuturesPersistenceError("persisted order identity or symbol is invalid")
            if order.action is FuturesOrderAction.REDUCE and order.leverage <= ZERO:
                raise FuturesPersistenceError("persisted reduce order has invalid leverage")
        for key, fill in self._fills.items():
            if fill.fill_id != key or fill.order_id not in self._orders:
                raise FuturesPersistenceError("persisted fill is not attached to an order")
            if fill.symbol != self.contract_rules.symbol:
                raise FuturesPersistenceError("persisted fill uses an unsupported contract symbol")
        for order in self._orders.values():
            attached = [fill for fill in self._fills.values() if fill.order_id == order.order_id]
            attached_ids = tuple(fill.fill_id for fill in attached)
            if set(order.fill_ids) != set(attached_ids) or len(order.fill_ids) != len(attached_ids):
                raise FuturesPersistenceError(
                    f"persisted fill linkage is invalid for order {order.order_id}"
                )
            if sum((fill.quantity for fill in attached), ZERO) != order.filled_quantity:
                raise FuturesPersistenceError(
                    f"persisted fill quantity does not match order {order.order_id}"
                )
            if order.status is FuturesOrderStatus.ACCEPTED and attached:
                raise FuturesPersistenceError("accepted order contains persisted fills")
            if order.status is FuturesOrderStatus.REJECTED and attached:
                raise FuturesPersistenceError("rejected order contains persisted fills")
        for key, position in self._positions.items():
            if position.symbol != key or position.symbol != self.contract_rules.symbol:
                raise FuturesPersistenceError("persisted position identity is invalid")
        for key, payment in self._funding.items():
            if payment.payment_id != key or payment.symbol != self.contract_rules.symbol:
                raise FuturesPersistenceError("persisted funding identity is invalid")
        for key, liquidation in self._liquidations.items():
            if liquidation.liquidation_id != key or liquidation.symbol != self.contract_rules.symbol:
                raise FuturesPersistenceError("persisted liquidation identity is invalid")
        event_ids = set(self._events)
        if len(event_ids) != len(self._events):
            raise FuturesPersistenceError("persisted event identifiers are not unique")
        for key, event in self._events.items():
            if event.event_id != key:
                raise FuturesPersistenceError("persisted event identity is invalid")

    def _write_state(
        self,
        *,
        balances: Mapping[str, FuturesBalance],
        orders: Mapping[str, FuturesOrder],
        positions: Mapping[str, FuturesPosition],
        fills: Mapping[str, FuturesFill],
        ledger: list[FuturesLedgerEntry],
        events: Mapping[str, FuturesAuditEvent],
        funding: Mapping[str, FuturesFundingPayment],
        liquidations: Mapping[str, FuturesLiquidation],
    ) -> None:
        config = self._config_payload()
        self._conn.execute("DELETE FROM engine_config")
        self._conn.executemany("INSERT INTO engine_config(key, value) VALUES(?, ?)", config.items())
        for table in ("wallet_balances", "orders", "positions", "fills", "ledger_entries", "events", "funding_payments", "liquidations"):
            self._conn.execute(f"DELETE FROM {table}")
        self._conn.executemany(
            "INSERT INTO wallet_balances(asset, available, reserved) VALUES(?, ?, ?)",
            [(asset, str(item.available), str(item.reserved)) for asset, item in balances.items()],
        )
        self._conn.executemany(
            "INSERT INTO orders(order_id, payload) VALUES(?, ?)",
            [(key, _json_value(_order_to_dict(item))) for key, item in orders.items()],
        )
        self._conn.executemany(
            "INSERT INTO positions(symbol, payload) VALUES(?, ?)",
            [(key, _json_value(_position_to_dict(item))) for key, item in positions.items()],
        )
        self._conn.executemany(
            "INSERT INTO fills(fill_id, payload) VALUES(?, ?)",
            [(key, _json_value(_fill_to_dict(item))) for key, item in fills.items()],
        )
        self._conn.executemany(
            "INSERT INTO ledger_entries(entry_id, payload) VALUES(?, ?)",
            [(item.entry_id, _json_value(_ledger_to_dict(item))) for item in ledger],
        )
        self._conn.executemany(
            "INSERT INTO events(event_id, event_type, timestamp, payload) VALUES(?, ?, ?, ?)",
            [(key, item.event_type.value, _timestamp_text(item.timestamp), _json_value(dict(item.payload))) for key, item in events.items()],
        )
        self._conn.executemany(
            "INSERT INTO funding_payments(payment_id, payload) VALUES(?, ?)",
            [(key, _json_value(_funding_to_dict(item))) for key, item in funding.items()],
        )
        self._conn.executemany(
            "INSERT INTO liquidations(liquidation_id, payload) VALUES(?, ?)",
            [(key, _json_value(_liquidation_to_dict(item))) for key, item in liquidations.items()],
        )


def _validate_quantity(value: Decimal, rules: FuturesContractRules, field_name: str) -> Decimal:
    exact = _exact(value, rules.quantity_precision, field_name)
    if exact <= ZERO:
        raise FuturesValidationError(f"{field_name} must be positive")
    if exact < rules.min_quantity:
        raise FuturesValidationError(f"{field_name} is below the contract minimum")
    if rules.max_quantity is not None and exact > rules.max_quantity:
        raise FuturesValidationError(f"{field_name} exceeds the contract maximum")
    return exact


def _validate_price(value: Decimal, rules: FuturesContractRules, field_name: str) -> Decimal:
    exact = _exact(value, rules.price_precision, field_name)
    if exact <= ZERO:
        raise FuturesValidationError(f"{field_name} must be positive")
    return exact


# Serialization is intentionally explicit: only validated financial state is
# persisted, never arbitrary objects or credentials.
def _rules_to_dict(item: FuturesContractRules) -> dict[str, Any]:
    return {"symbol": item.symbol, "base_asset": item.base_asset, "quote_asset": item.quote_asset, "collateral_asset": item.collateral_asset, "quantity_precision": item.quantity_precision, "price_precision": item.price_precision, "collateral_precision": item.collateral_precision, "contract_multiplier": str(item.contract_multiplier), "min_quantity": str(item.min_quantity), "max_quantity": None if item.max_quantity is None else str(item.max_quantity)}


def _rules_from_dict(data: Mapping[str, Any]) -> FuturesContractRules:
    return FuturesContractRules(**data)


def _margin_to_dict(item: FuturesMarginConfig) -> dict[str, Any]:
    return {"initial_margin_rate": None if item.initial_margin_rate is None else str(item.initial_margin_rate), "maintenance_margin_rate": str(item.maintenance_margin_rate), "liquidation_fee_rate": str(item.liquidation_fee_rate)}


def _margin_from_dict(data: Mapping[str, Any]) -> FuturesMarginConfig:
    return FuturesMarginConfig(**data)


def _fee_from_dict(data: Mapping[str, Any]) -> FuturesFeeConfig:
    return FuturesFeeConfig(**data)


def _risk_to_dict(item: FuturesRiskLimits) -> dict[str, Any]:
    return {"default_leverage": str(item.default_leverage), "max_leverage": str(item.max_leverage), "max_order_notional": None if item.max_order_notional is None else str(item.max_order_notional), "max_position_notional": None if item.max_position_notional is None else str(item.max_position_notional), "max_open_orders": item.max_open_orders}


def _risk_from_dict(data: Mapping[str, Any]) -> FuturesRiskLimits:
    return FuturesRiskLimits(**data)


def _order_to_dict(item: FuturesOrder) -> dict[str, Any]:
    return {"order_id": item.order_id, "client_order_id": item.client_order_id, "symbol": item.symbol, "action": item.action.value, "position_side": item.position_side.value, "quantity": str(item.quantity), "price": str(item.price), "leverage": str(item.leverage), "status": item.status.value, "filled_quantity": str(item.filled_quantity), "average_fill_price": None if item.average_fill_price is None else str(item.average_fill_price), "fee_paid": str(item.fee_paid), "reserved_collateral": str(item.reserved_collateral), "created_at": _timestamp_text(item.created_at), "updated_at": _timestamp_text(item.updated_at), "rejection_code": None if item.rejection_code is None else item.rejection_code.value, "rejection_reason": item.rejection_reason, "fill_ids": list(item.fill_ids)}


def _order_from_dict(data: Mapping[str, Any]) -> FuturesOrder:
    data = dict(data)
    data["quantity"] = _decimal(data["quantity"], "order quantity")
    data["price"] = _decimal(data["price"], "order price")
    data["leverage"] = _decimal(data["leverage"], "order leverage")
    data["filled_quantity"] = _decimal(data["filled_quantity"], "filled quantity")
    data["average_fill_price"] = None if data.get("average_fill_price") is None else _decimal(data["average_fill_price"], "average fill price")
    data["fee_paid"] = _decimal(data["fee_paid"], "fee paid")
    data["reserved_collateral"] = _decimal(data["reserved_collateral"], "reserved collateral")
    data["created_at"] = _parse_timestamp(data["created_at"], "order created")
    data["updated_at"] = _parse_timestamp(data["updated_at"], "order updated")
    return FuturesOrder(**data)


def _position_to_dict(item: FuturesPosition) -> dict[str, Any]:
    return {"symbol": item.symbol, "side": item.side.value, "quantity": str(item.quantity), "entry_price": str(item.entry_price), "mark_price": str(item.mark_price), "leverage": str(item.leverage), "margin": str(item.margin), "realized_pnl": str(item.realized_pnl), "maintenance_margin": str(item.maintenance_margin), "opened_at": _timestamp_text(item.opened_at), "updated_at": _timestamp_text(item.updated_at)}


def _position_from_dict(data: Mapping[str, Any]) -> FuturesPosition:
    data = dict(data)
    for key in ("quantity", "entry_price", "mark_price", "leverage", "margin", "realized_pnl", "maintenance_margin"):
        data[key] = _decimal(data[key], f"position {key}")
    data["opened_at"] = _parse_timestamp(data["opened_at"], "position opened")
    data["updated_at"] = _parse_timestamp(data["updated_at"], "position updated")
    return FuturesPosition(**data)


def _fill_to_dict(item: FuturesFill) -> dict[str, Any]:
    return {"fill_id": item.fill_id, "order_id": item.order_id, "symbol": item.symbol, "action": item.action.value, "position_side": item.position_side.value, "quantity": str(item.quantity), "price": str(item.price), "notional": str(item.notional), "fee": str(item.fee), "realized_pnl": str(item.realized_pnl), "margin_released": str(item.margin_released), "executed_at": _timestamp_text(item.executed_at)}


def _fill_from_dict(data: Mapping[str, Any]) -> FuturesFill:
    data = dict(data)
    for key in ("quantity", "price", "notional", "fee", "realized_pnl", "margin_released"):
        data[key] = _decimal(data[key], f"fill {key}")
    data["executed_at"] = _parse_timestamp(data["executed_at"], "fill")
    return FuturesFill(**data)


def _ledger_to_dict(item: FuturesLedgerEntry) -> dict[str, Any]:
    return {"entry_id": item.entry_id, "entry_type": item.entry_type.value, "timestamp": _timestamp_text(item.timestamp), "order_id": item.order_id, "fill_id": item.fill_id, "postings": [{"asset": posting.asset, "available_delta": str(posting.available_delta), "reserved_delta": str(posting.reserved_delta)} for posting in item.postings], "description": item.description}


def _ledger_from_dict(data: Mapping[str, Any]) -> FuturesLedgerEntry:
    postings = tuple(FuturesLedgerPosting(item["asset"], _decimal(item["available_delta"], "available delta"), _decimal(item["reserved_delta"], "reserved delta")) for item in data["postings"])
    return FuturesLedgerEntry(data["entry_id"], data["entry_type"], _parse_timestamp(data["timestamp"], "ledger"), data.get("order_id"), data.get("fill_id"), postings, data["description"])


def _funding_to_dict(item: FuturesFundingPayment) -> dict[str, Any]:
    return {"payment_id": item.payment_id, "symbol": item.symbol, "position_side": item.position_side.value, "rate": str(item.rate), "notional": str(item.notional), "amount": str(item.amount), "applied_at": _timestamp_text(item.applied_at)}


def _funding_from_dict(data: Mapping[str, Any]) -> FuturesFundingPayment:
    data = dict(data)
    for key in ("rate", "notional", "amount"):
        data[key] = _decimal(data[key], f"funding {key}")
    data["applied_at"] = _parse_timestamp(data["applied_at"], "funding")
    return FuturesFundingPayment(**data)


def _liquidation_to_dict(item: FuturesLiquidation) -> dict[str, Any]:
    return {"liquidation_id": item.liquidation_id, "symbol": item.symbol, "position_side": item.position_side.value, "quantity": str(item.quantity), "mark_price": str(item.mark_price), "realized_pnl": str(item.realized_pnl), "liquidation_fee": str(item.liquidation_fee), "collateral_released": str(item.collateral_released), "shortfall": str(item.shortfall), "liquidated_at": _timestamp_text(item.liquidated_at)}


def _liquidation_from_dict(data: Mapping[str, Any]) -> FuturesLiquidation:
    data = dict(data)
    for key in ("quantity", "mark_price", "realized_pnl", "liquidation_fee", "collateral_released", "shortfall"):
        data[key] = _decimal(data[key], f"liquidation {key}")
    data["liquidated_at"] = _parse_timestamp(data["liquidated_at"], "liquidation")
    return FuturesLiquidation(**data)
