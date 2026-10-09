"""Deterministic, simulation-only Spot portfolio accounting and execution.

This module deliberately has no exchange-order path.  It accepts explicitly
supplied prices, uses :class:`~trad.market_data.MarketDataSafetyMonitor` as a
fail-closed gate, and records every balance-affecting operation in an
append-only in-memory ledger.  It is designed to be usable offline by a later
strategy, persistence, or UI layer without changing the accounting contract.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from enum import Enum
from typing import Any, Callable, Mapping

from .config import Instrument, RunConfig
from .market_data import DataHealth, MarketData, MarketDataSafetyMonitor


ZERO = Decimal("0")
ONE = Decimal("1")
DEFAULT_PAPER_QUOTE_BALANCE = Decimal("1000")


class SpotPaperError(RuntimeError):
    """Base class for simulation-engine failures."""


class OrderValidationError(SpotPaperError, ValueError):
    """Raised when an order request is malformed or outside symbol rules."""


class OrderNotFoundError(SpotPaperError, KeyError):
    """Raised when an order identifier is unknown."""


class OrderStateError(SpotPaperError):
    """Raised when an order operation is invalid for its current state."""


class DuplicateOrderError(SpotPaperError, ValueError):
    """Raised when one client order id is reused with different parameters."""


class DuplicateFillError(SpotPaperError, ValueError):
    """Raised when a fill id is reused with different parameters."""


class ExecutionError(SpotPaperError):
    """Raised when a supplied execution cannot be applied atomically."""


class ValuationError(SpotPaperError, ValueError):
    """Raised when a supplied portfolio valuation price is invalid."""


class AccountingError(SpotPaperError):
    """Raised when an internal accounting invariant would be violated."""


class OrderSide(str, Enum):
    """Supported Spot order directions."""

    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    """Explicit paper-order lifecycle states."""

    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class FeeCurrency(str, Enum):
    """Asset in which a simulated fee is charged."""

    QUOTE = "quote"
    BASE = "base"


class FeeRounding(str, Enum):
    """Deterministic fee rounding policies."""

    DOWN = "down"
    HALF_UP = "half_up"
    UP = "up"


class RiskRejectionCode(str, Enum):
    """Stable machine-readable reasons for a rejected order."""

    MARKET_DATA_UNSAFE = "market_data_unsafe"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    ORDER_NOTIONAL_TOO_SMALL = "order_notional_too_small"
    ORDER_NOTIONAL_TOO_LARGE = "order_notional_too_large"
    POSITION_LIMIT = "position_limit"
    EXPOSURE_LIMIT = "exposure_limit"
    OPEN_ORDER_LIMIT = "open_order_limit"
    UNVALUED_ASSET = "unvalued_asset"


class LedgerEntryType(str, Enum):
    """Kinds of auditable ledger entries."""

    INITIALIZATION = "initialization"
    RESERVATION = "reservation"
    RELEASE = "release"
    FILL = "fill"
    REJECTION = "rejection"


def _decimal(value: Any, field_name: str) -> Decimal:
    """Convert an API value without doing arithmetic in binary float."""

    if isinstance(value, bool) or value is None:
        raise OrderValidationError(f"{field_name} must be a finite decimal")
    try:
        converted = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OrderValidationError(f"{field_name} must be a finite decimal") from exc
    if not converted.is_finite():
        raise OrderValidationError(f"{field_name} must be a finite decimal")
    return converted


def _non_negative_decimal(value: Any, field_name: str) -> Decimal:
    converted = _decimal(value, field_name)
    if converted < ZERO:
        raise OrderValidationError(f"{field_name} must be non-negative")
    return converted


def _positive_decimal(value: Any, field_name: str) -> Decimal:
    converted = _decimal(value, field_name)
    if converted <= ZERO:
        raise OrderValidationError(f"{field_name} must be positive")
    return converted


def _parse_enum(value: Any, enum_type: type[Enum], field_name: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(member.value for member in enum_type)
        raise OrderValidationError(
            f"{field_name} must be one of: {allowed}"
        ) from exc


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise OrderValidationError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise OrderValidationError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _normalize_asset(value: Any, field_name: str = "asset") -> str:
    if not isinstance(value, str):
        raise OrderValidationError(f"{field_name} must be a string")
    normalized = value.strip().upper()
    if not normalized or not normalized.isalnum():
        raise OrderValidationError(
            f"{field_name} must contain only non-empty letters and digits"
        )
    return normalized


def _split_symbol(value: Any, field_name: str = "symbol") -> tuple[str, str, str]:
    if not isinstance(value, str):
        raise OrderValidationError(f"{field_name} must look like BASE/QUOTE")
    raw = value.strip().upper()
    parts = raw.split("/")
    if len(parts) != 2:
        raise OrderValidationError(f"{field_name} must look like BASE/QUOTE")
    base = _normalize_asset(parts[0], f"{field_name} base asset")
    quote = _normalize_asset(parts[1], f"{field_name} quote asset")
    if base == quote:
        raise OrderValidationError(f"{field_name} base and quote assets must differ")
    return f"{base}/{quote}", base, quote


def _quantum(precision: int) -> Decimal:
    return ONE.scaleb(-precision)


def _quantize(
    value: Decimal,
    precision: int,
    field_name: str,
    rounding: str,
) -> Decimal:
    try:
        return value.quantize(_quantum(precision), rounding=rounding)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise OrderValidationError(
            f"{field_name} exceeds supported decimal precision or range"
        ) from exc


def _exact_precision(value: Decimal, precision: int, field_name: str) -> Decimal:
    quantized = _quantize(value, precision, field_name, ROUND_DOWN)
    if quantized != value:
        raise OrderValidationError(
            f"{field_name} has more than {precision} supported decimal places"
        )
    return quantized


def _rounding_name(value: FeeRounding) -> str:
    return {
        FeeRounding.DOWN: ROUND_DOWN,
        FeeRounding.HALF_UP: ROUND_HALF_UP,
        FeeRounding.UP: ROUND_UP,
    }[value]


@dataclass(frozen=True)
class SpotSymbolRules:
    """Validated precision and notional rules for one Spot pair.

    ``quantity_precision`` applies to the base asset quantity, while
    ``price_precision`` applies to an order/execution price and
    ``quote_precision`` applies to cash balance movements.  The default rules
    are intentionally explicit and can be replaced for another symbol.
    """

    symbol: str = "BTC/USDT"
    base_asset: str = "BTC"
    quote_asset: str = "USDT"
    quantity_precision: int = 8
    price_precision: int = 2
    quote_precision: int = 2
    min_quantity: Decimal = ZERO
    min_notional: Decimal = ZERO
    max_quantity: Decimal | None = None

    def __post_init__(self) -> None:
        symbol, base, quote = _split_symbol(self.symbol)
        supplied_base = _normalize_asset(self.base_asset, "base_asset")
        supplied_quote = _normalize_asset(self.quote_asset, "quote_asset")
        if (supplied_base, supplied_quote) != (base, quote):
            raise OrderValidationError(
                "symbol must match its base_asset and quote_asset fields"
            )
        for name in (
            "quantity_precision",
            "price_precision",
            "quote_precision",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 18:
                raise OrderValidationError(f"{name} must be an integer from 0 through 18")
        min_quantity = _non_negative_decimal(self.min_quantity, "min_quantity")
        min_notional = _non_negative_decimal(self.min_notional, "min_notional")
        if self.max_quantity is not None:
            max_quantity = _positive_decimal(self.max_quantity, "max_quantity")
            if max_quantity < min_quantity:
                raise OrderValidationError("max_quantity cannot be below min_quantity")
            max_quantity = _exact_precision(
                max_quantity,
                self.quantity_precision,
                "max_quantity",
            )
        else:
            max_quantity = None
        min_quantity = _exact_precision(
            min_quantity,
            self.quantity_precision,
            "min_quantity",
        )
        min_notional = _exact_precision(
            min_notional,
            self.quote_precision,
            "min_notional",
        )
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "base_asset", base)
        object.__setattr__(self, "quote_asset", quote)
        object.__setattr__(self, "min_quantity", min_quantity)
        object.__setattr__(self, "min_notional", min_notional)
        object.__setattr__(self, "max_quantity", max_quantity)

    @classmethod
    def for_symbol(
        cls,
        symbol: str,
        *,
        quantity_precision: int = 8,
        price_precision: int = 2,
        quote_precision: int = 2,
        min_quantity: Decimal | str | int | float = ZERO,
        min_notional: Decimal | str | int | float = ZERO,
        max_quantity: Decimal | str | int | float | None = None,
    ) -> "SpotSymbolRules":
        normalized, base, quote = _split_symbol(symbol)
        return cls(
            symbol=normalized,
            base_asset=base,
            quote_asset=quote,
            quantity_precision=quantity_precision,
            price_precision=price_precision,
            quote_precision=quote_precision,
            min_quantity=min_quantity,
            min_notional=min_notional,
            max_quantity=max_quantity,
        )


@dataclass(frozen=True)
class FeeConfig:
    """Deterministic paper fee policy.

    The default is a documented 0.10% fee charged in the quote asset and
    rounded down to the configured fee asset precision.  A caller may select
    the base asset and/or another explicit rounding policy.
    """

    rate: Decimal = Decimal("0.001")
    currency: FeeCurrency = FeeCurrency.QUOTE
    rounding: FeeRounding = FeeRounding.DOWN

    def __post_init__(self) -> None:
        rate = _non_negative_decimal(self.rate, "fee rate")
        if rate >= ONE:
            raise OrderValidationError("fee rate must be below 1")
        currency = _parse_enum(self.currency, FeeCurrency, "fee currency")
        rounding = _parse_enum(self.rounding, FeeRounding, "fee rounding")
        object.__setattr__(self, "rate", rate)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "rounding", rounding)

    def amount(
        self,
        *,
        gross_quote: Decimal,
        base_quantity: Decimal,
        rules: SpotSymbolRules,
    ) -> Decimal:
        raw = gross_quote * self.rate if self.currency is FeeCurrency.QUOTE else base_quantity * self.rate
        precision = (
            rules.quote_precision
            if self.currency is FeeCurrency.QUOTE
            else rules.quantity_precision
        )
        return _quantize(raw, precision, "fee", _rounding_name(self.rounding))


@dataclass(frozen=True)
class SpotRiskLimits:
    """Optional, explicit capital and exposure limits.

    ``None`` means that a limit is not imposed by this layer.  There are no
    hidden risk caps; callers choose and document limits appropriate to their
    simulation.
    """

    min_order_notional: Decimal | None = None
    max_order_notional: Decimal | None = None
    max_position_value: Decimal | None = None
    max_portfolio_exposure: Decimal | None = None
    max_open_orders: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "min_order_notional",
            "max_order_notional",
            "max_position_value",
        ):
            value = getattr(self, name)
            if value is not None:
                value = _positive_decimal(value, name)
                object.__setattr__(self, name, value)
        if (
            self.min_order_notional is not None
            and self.max_order_notional is not None
            and self.min_order_notional > self.max_order_notional
        ):
            raise OrderValidationError(
                "min_order_notional cannot exceed max_order_notional"
            )
        if self.max_portfolio_exposure is not None:
            exposure = _decimal(
                self.max_portfolio_exposure,
                "max_portfolio_exposure",
            )
            if exposure <= ZERO or exposure > ONE:
                raise OrderValidationError(
                    "max_portfolio_exposure must be greater than 0 and at most 1"
                )
            object.__setattr__(self, "max_portfolio_exposure", exposure)
        if self.max_open_orders is not None:
            if (
                isinstance(self.max_open_orders, bool)
                or not isinstance(self.max_open_orders, int)
                or self.max_open_orders < 1
            ):
                raise OrderValidationError("max_open_orders must be a positive integer")


@dataclass(frozen=True)
class Balance:
    """Available, reserved, and total quantity for one asset."""

    asset: str
    available: Decimal
    reserved: Decimal = ZERO

    def __post_init__(self) -> None:
        asset = _normalize_asset(self.asset)
        available = _non_negative_decimal(self.available, f"{asset} available")
        reserved = _non_negative_decimal(self.reserved, f"{asset} reserved")
        object.__setattr__(self, "asset", asset)
        object.__setattr__(self, "available", available)
        object.__setattr__(self, "reserved", reserved)

    @property
    def total(self) -> Decimal:
        return self.available + self.reserved


@dataclass(frozen=True)
class OrderRequest:
    """Validated, but not yet symbol-precision-checked, order input."""

    symbol: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    client_order_id: str | None = None

    def __post_init__(self) -> None:
        symbol, _, _ = _split_symbol(self.symbol)
        side = _parse_enum(self.side, OrderSide, "order side")
        quantity = _positive_decimal(self.quantity, "order quantity")
        price = _positive_decimal(self.price, "order price")
        if self.client_order_id is not None:
            if not isinstance(self.client_order_id, str) or not self.client_order_id.strip():
                raise OrderValidationError("client_order_id must be non-empty when supplied")
            if len(self.client_order_id.strip()) > 128:
                raise OrderValidationError("client_order_id cannot exceed 128 characters")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "price", price)
        if self.client_order_id is not None:
            object.__setattr__(self, "client_order_id", self.client_order_id.strip())


@dataclass(frozen=True)
class RiskDecision:
    """Result of the independent risk gate."""

    accepted: bool
    code: RiskRejectionCode | None
    reason: str
    required_asset: str | None = None
    required_amount: Decimal | None = None
    available_amount: Decimal | None = None
    health: DataHealth | None = None


@dataclass(frozen=True)
class Order:
    """Immutable public view of one paper order."""

    order_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    status: OrderStatus
    created_at: datetime
    updated_at: datetime
    client_order_id: str | None = None
    filled_quantity: Decimal = ZERO
    average_fill_price: Decimal | None = None
    fee_paid: Decimal = ZERO
    fee_currency: FeeCurrency = FeeCurrency.QUOTE
    rejection_code: RiskRejectionCode | None = None
    rejection_reason: str | None = None
    fill_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        symbol, _, _ = _split_symbol(self.symbol)
        side = _parse_enum(self.side, OrderSide, "order side")
        status = _parse_enum(self.status, OrderStatus, "order status")
        quantity = _positive_decimal(self.quantity, "order quantity")
        price = _positive_decimal(self.price, "order price")
        filled = _non_negative_decimal(self.filled_quantity, "filled quantity")
        fee_paid = _non_negative_decimal(self.fee_paid, "fee paid")
        created = _utc(self.created_at, "order created_at")
        updated = _utc(self.updated_at, "order updated_at")
        if updated < created:
            raise OrderValidationError("order updated_at cannot precede created_at")
        if filled > quantity:
            raise OrderValidationError("filled quantity cannot exceed order quantity")
        if status is OrderStatus.REJECTED and filled != ZERO:
            raise OrderValidationError("rejected orders cannot contain fills")
        if status is OrderStatus.ACCEPTED and filled != ZERO:
            raise OrderValidationError("accepted orders cannot contain fills")
        if status is OrderStatus.PARTIALLY_FILLED and not ZERO < filled < quantity:
            raise OrderValidationError(
                "partially filled orders must have a non-zero incomplete fill"
            )
        if status is OrderStatus.FILLED and filled != quantity:
            raise OrderValidationError("filled orders must contain the full quantity")
        if status is OrderStatus.CANCELLED and filled >= quantity:
            raise OrderValidationError("cancelled orders cannot be fully filled")
        if filled and self.average_fill_price is None:
            raise OrderValidationError("filled orders require an average fill price")
        average = (
            None
            if self.average_fill_price is None
            else _positive_decimal(self.average_fill_price, "average fill price")
        )
        currency = _parse_enum(self.fee_currency, FeeCurrency, "fee currency")
        rejection_code = (
            None
            if self.rejection_code is None
            else _parse_enum(self.rejection_code, RiskRejectionCode, "rejection code")
        )
        if not isinstance(self.order_id, str) or not self.order_id.strip():
            raise OrderValidationError("order_id must be non-empty")
        if self.client_order_id is not None and (
            not isinstance(self.client_order_id, str)
            or not self.client_order_id.strip()
        ):
            raise OrderValidationError("client_order_id cannot be blank")
        if any(not isinstance(fill_id, str) or not fill_id.strip() for fill_id in self.fill_ids):
            raise OrderValidationError("order fill_ids must be non-empty strings")
        object.__setattr__(self, "order_id", self.order_id.strip())
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "filled_quantity", filled)
        object.__setattr__(self, "fee_paid", fee_paid)
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)
        object.__setattr__(self, "average_fill_price", average)
        object.__setattr__(self, "fee_currency", currency)
        object.__setattr__(self, "rejection_code", rejection_code)
        object.__setattr__(
            self,
            "client_order_id",
            None if self.client_order_id is None else self.client_order_id.strip(),
        )
        object.__setattr__(
            self,
            "fill_ids",
            tuple(fill_id.strip() for fill_id in self.fill_ids),
        )

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        }


@dataclass(frozen=True)
class Fill:
    """One idempotent simulated execution."""

    fill_id: str
    order_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    gross_quote: Decimal
    fee_amount: Decimal
    fee_currency: FeeCurrency
    base_delta: Decimal
    quote_delta: Decimal
    executed_at: datetime

    def __post_init__(self) -> None:
        symbol, _, _ = _split_symbol(self.symbol)
        side = _parse_enum(self.side, OrderSide, "fill side")
        quantity = _positive_decimal(self.quantity, "fill quantity")
        price = _positive_decimal(self.price, "fill price")
        gross = _positive_decimal(self.gross_quote, "fill gross quote")
        fee = _non_negative_decimal(self.fee_amount, "fill fee")
        currency = _parse_enum(self.fee_currency, FeeCurrency, "fee currency")
        base_delta = _decimal(self.base_delta, "fill base delta")
        quote_delta = _decimal(self.quote_delta, "fill quote delta")
        executed_at = _utc(self.executed_at, "fill executed_at")
        if not isinstance(self.fill_id, str) or not self.fill_id.strip():
            raise OrderValidationError("fill_id must be non-empty")
        if not isinstance(self.order_id, str) or not self.order_id.strip():
            raise OrderValidationError("order_id must be non-empty")
        object.__setattr__(self, "fill_id", self.fill_id.strip())
        object.__setattr__(self, "order_id", self.order_id.strip())
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "gross_quote", gross)
        object.__setattr__(self, "fee_amount", fee)
        object.__setattr__(self, "fee_currency", currency)
        object.__setattr__(self, "base_delta", base_delta)
        object.__setattr__(self, "quote_delta", quote_delta)
        object.__setattr__(self, "executed_at", executed_at)


@dataclass(frozen=True)
class LedgerPosting:
    """Available/reserved deltas for one asset in one ledger entry."""

    asset: str
    available_delta: Decimal = ZERO
    reserved_delta: Decimal = ZERO

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset", _normalize_asset(self.asset))
        object.__setattr__(
            self,
            "available_delta",
            _decimal(self.available_delta, "available delta"),
        )
        object.__setattr__(
            self,
            "reserved_delta",
            _decimal(self.reserved_delta, "reserved delta"),
        )

    @property
    def total_delta(self) -> Decimal:
        return self.available_delta + self.reserved_delta


@dataclass(frozen=True)
class LedgerEntry:
    """Append-only explanation of one accounting event."""

    entry_id: str
    entry_type: LedgerEntryType
    timestamp: datetime
    order_id: str | None
    fill_id: str | None
    postings: tuple[LedgerPosting, ...]
    description: str

    def __post_init__(self) -> None:
        if not isinstance(self.entry_id, str) or not self.entry_id.strip():
            raise AccountingError("ledger entry id must be non-empty")
        entry_type = _parse_enum(self.entry_type, LedgerEntryType, "ledger entry type")
        timestamp = _utc(self.timestamp, "ledger timestamp")
        postings = tuple(self.postings)
        if any(not isinstance(posting, LedgerPosting) for posting in postings):
            raise AccountingError("ledger postings must be LedgerPosting objects")
        assets = [posting.asset for posting in postings]
        if len(assets) != len(set(assets)):
            raise AccountingError("ledger entry cannot contain duplicate asset postings")
        if not isinstance(self.description, str) or not self.description.strip():
            raise AccountingError("ledger description must be non-empty")
        object.__setattr__(self, "entry_type", entry_type)
        object.__setattr__(self, "timestamp", timestamp)
        object.__setattr__(self, "postings", postings)
        object.__setattr__(self, "description", self.description.strip())


@dataclass(frozen=True)
class AssetValuation:
    """One explicitly priced asset in a portfolio snapshot."""

    asset: str
    quantity: Decimal
    price: Decimal
    value: Decimal


@dataclass(frozen=True)
class PortfolioSnapshot:
    """A point-in-time valuation that never hides unpriced holdings."""

    timestamp: datetime
    valuation_asset: str
    balances: tuple[Balance, ...]
    valued_assets: tuple[AssetValuation, ...]
    unpriced_assets: tuple[str, ...]
    valued_value: Decimal
    total_value: Decimal | None

    @property
    def fully_valued(self) -> bool:
        return not self.unpriced_assets and self.total_value is not None


@dataclass(frozen=True)
class ReconciliationReport:
    """Comparison between the ledger-derived and live wallet balances."""

    is_consistent: bool
    ledger_balances: tuple[Balance, ...]
    actual_balances: tuple[Balance, ...]
    differences: tuple[LedgerPosting, ...]


@dataclass(frozen=True)
class _Reservation:
    asset: str
    amount: Decimal


@dataclass(frozen=True)
class _TradeAmounts:
    debit_asset: str
    debit_amount: Decimal
    credit_asset: str
    credit_amount: Decimal
    gross_quote: Decimal
    fee_amount: Decimal
    base_delta: Decimal
    quote_delta: Decimal


@dataclass
class _MutableBalance:
    available: Decimal
    reserved: Decimal


class SpotRiskManager:
    """Independently testable risk gate for a Spot paper engine."""

    def __init__(
        self,
        *,
        limits: SpotRiskLimits | None = None,
        safety_monitor: MarketDataSafetyMonitor | None = None,
    ) -> None:
        self.limits = limits or SpotRiskLimits()
        self.safety_monitor = safety_monitor or MarketDataSafetyMonitor()

    def market_data_decision(self, now: datetime) -> RiskDecision:
        health = self.safety_monitor.health(_utc(now, "risk check time"))
        if not health.allow_new_positions:
            return RiskDecision(
                accepted=False,
                code=RiskRejectionCode.MARKET_DATA_UNSAFE,
                reason=(
                    f"market-data safety is {health.status.value}: {health.reason}"
                ),
                health=health,
            )
        return RiskDecision(
            accepted=True,
            code=None,
            reason="market-data safety is healthy",
            health=health,
        )

    def assess(
        self,
        request: OrderRequest,
        *,
        balances: Mapping[str, Balance],
        rules: SpotSymbolRules,
        fee_config: FeeConfig,
        now: datetime,
        open_order_count: int = 0,
    ) -> RiskDecision:
        """Return a decision without mutating balances or orders."""

        market_decision = self.market_data_decision(now)
        if not market_decision.accepted:
            return market_decision
        _validate_request_for_rules(request, rules)
        if (
            self.limits.max_open_orders is not None
            and open_order_count >= self.limits.max_open_orders
        ):
            return self._reject(
                RiskRejectionCode.OPEN_ORDER_LIMIT,
                "configured maximum number of open orders has been reached",
            )

        notional = _order_gross_quote(request, rules)
        if (
            self.limits.min_order_notional is not None
            and notional < self.limits.min_order_notional
        ):
            return self._reject(
                RiskRejectionCode.ORDER_NOTIONAL_TOO_SMALL,
                f"order notional {notional} is below the configured minimum",
            )
        if (
            self.limits.max_order_notional is not None
            and notional > self.limits.max_order_notional
        ):
            return self._reject(
                RiskRejectionCode.ORDER_NOTIONAL_TOO_LARGE,
                f"order notional {notional} exceeds the configured maximum",
            )

        amounts = _trade_amounts(
            request.side,
            request.quantity,
            request.price,
            rules,
            fee_config,
        )
        required_asset, required_amount = _reservation_requirement(
            request,
            rules,
            fee_config,
        )
        available = balances.get(required_asset, Balance(required_asset, ZERO)).available
        if available < required_amount:
            return RiskDecision(
                accepted=False,
                code=RiskRejectionCode.INSUFFICIENT_FUNDS,
                reason=(
                    f"available {required_asset} balance {available} is below "
                    f"required {required_amount}"
                ),
                required_asset=required_asset,
                required_amount=required_amount,
                available_amount=available,
            )

        base_total = balances.get(rules.base_asset, Balance(rules.base_asset, ZERO)).total
        quote_total = balances.get(rules.quote_asset, Balance(rules.quote_asset, ZERO)).total
        projected_base = base_total + amounts.base_delta
        projected_quote = quote_total + amounts.quote_delta
        if projected_base < ZERO or projected_quote < ZERO:
            return self._reject(
                RiskRejectionCode.INSUFFICIENT_FUNDS,
                "projected trade would create a negative asset balance",
            )

        if self.limits.max_position_value is not None:
            position_value = projected_base * request.price
            if position_value > self.limits.max_position_value:
                return self._reject(
                    RiskRejectionCode.POSITION_LIMIT,
                    f"projected base position value {position_value} exceeds "
                    f"the configured maximum {self.limits.max_position_value}",
                )

        if self.limits.max_portfolio_exposure is not None:
            for balance in balances.values():
                if (
                    balance.asset not in {rules.base_asset, rules.quote_asset}
                    and balance.total > ZERO
                ):
                    return self._reject(
                        RiskRejectionCode.UNVALUED_ASSET,
                        f"cannot calculate exposure with unpriced asset {balance.asset}",
                    )
            equity = projected_quote + (projected_base * request.price)
            if equity <= ZERO:
                return self._reject(
                    RiskRejectionCode.EXPOSURE_LIMIT,
                    "projected portfolio equity must be positive",
                )
            exposure = (projected_base * request.price) / equity
            if exposure > self.limits.max_portfolio_exposure:
                return self._reject(
                    RiskRejectionCode.EXPOSURE_LIMIT,
                    f"projected base exposure {exposure} exceeds configured "
                    f"maximum {self.limits.max_portfolio_exposure}",
                )

        return RiskDecision(
            accepted=True,
            code=None,
            reason="order passed market-data and capital risk checks",
            required_asset=required_asset,
            required_amount=required_amount,
            available_amount=available,
            health=market_decision.health,
        )

    @staticmethod
    def _reject(code: RiskRejectionCode, reason: str) -> RiskDecision:
        return RiskDecision(accepted=False, code=code, reason=reason)


class SpotPaperEngine:
    """A deterministic, in-memory Spot paper wallet and execution ledger.

    Orders are limit-style paper orders.  ``submit_order`` reserves funds after
    the risk gate accepts the request.  ``execute_fill`` accepts an explicitly
    supplied price and quantity; it never calls an exchange.  A safety monitor
    must have a healthy, validated market-data event before an order can be
    accepted or filled.
    """

    def __init__(
        self,
        *,
        symbol: str | None = None,
        symbol_rules: SpotSymbolRules | None = None,
        starting_quote_balance: Decimal | str | int | float = DEFAULT_PAPER_QUOTE_BALANCE,
        starting_base_balance: Decimal | str | int | float = ZERO,
        initial_balances: Mapping[str, Decimal | str | int | float] | None = None,
        fee_config: FeeConfig | None = None,
        risk_limits: SpotRiskLimits | None = None,
        safety_monitor: MarketDataSafetyMonitor | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if symbol_rules is None:
            symbol_rules = SpotSymbolRules.for_symbol(symbol or "BTC/USDT")
        elif symbol is not None and _split_symbol(symbol)[0] != symbol_rules.symbol:
            raise OrderValidationError("symbol and symbol_rules must refer to the same pair")
        self.symbol_rules = symbol_rules
        self.fee_config = fee_config or FeeConfig()
        self.safety_monitor = safety_monitor or MarketDataSafetyMonitor()
        self.risk_manager = SpotRiskManager(
            limits=risk_limits,
            safety_monitor=self.safety_monitor,
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._balances: dict[str, _MutableBalance] = {}
        self._orders: dict[str, Order] = {}
        self._client_orders: dict[str, str] = {}
        self._fills: dict[str, Fill] = {}
        self._reservations: dict[str, _Reservation] = {}
        self._ledger: list[LedgerEntry] = []
        self._next_order_number = 1

        starting: dict[str, Decimal] = {
            self.symbol_rules.quote_asset: _non_negative_decimal(
                starting_quote_balance,
                "starting quote balance",
            ),
            self.symbol_rules.base_asset: _non_negative_decimal(
                starting_base_balance,
                "starting base balance",
            ),
        }
        if initial_balances is not None:
            for asset, amount in initial_balances.items():
                normalized = _normalize_asset(asset)
                starting[normalized] = _non_negative_decimal(
                    amount,
                    f"starting {normalized} balance",
                )
        for asset, amount in starting.items():
            if asset == self.symbol_rules.quote_asset:
                amount = _exact_precision(
                    amount,
                    self.symbol_rules.quote_precision,
                    f"starting {asset} balance",
                )
            elif asset == self.symbol_rules.base_asset:
                amount = _exact_precision(
                    amount,
                    self.symbol_rules.quantity_precision,
                    f"starting {asset} balance",
                )
            self._balances[asset] = _MutableBalance(amount, ZERO)
            self._append_entry(
                entry_id=f"initialization:{asset}",
                entry_type=LedgerEntryType.INITIALIZATION,
                timestamp=self._now(),
                order_id=None,
                fill_id=None,
                postings=(LedgerPosting(asset, available_delta=amount),),
                description=f"initialized {asset} available balance",
            )

    @classmethod
    def from_run_config(
        cls,
        config: RunConfig,
        **kwargs: Any,
    ) -> "SpotPaperEngine":
        """Create a Spot engine from the existing simulation-only config."""

        if config.instrument is not Instrument.SPOT:
            raise OrderValidationError(
                "SpotPaperEngine requires a configuration with instrument=spot"
            )
        if not config.safety.simulation_only:
            raise OrderValidationError("paper execution requires simulation_only=true")
        return cls(
            symbol=config.symbol,
            starting_quote_balance=Decimal(str(config.spot.starting_quote_balance)),
            starting_base_balance=Decimal(str(config.spot.starting_base_balance)),
            **kwargs,
        )

    def _now(self, value: datetime | None = None) -> datetime:
        return _utc(value if value is not None else self._clock(), "engine time")

    def record_market_data(
        self,
        event: MarketData,
        *,
        now: datetime | None = None,
    ) -> DataHealth:
        """Feed one already validated event into the existing safety monitor."""

        return self.safety_monitor.ingest(event, now=self._now(now))

    def report_invalid_market_data(
        self,
        reason: str,
        *,
        now: datetime | None = None,
        kind: Any = None,
        symbol: str | None = None,
    ) -> DataHealth:
        """Fail closed when an upstream adapter cannot construct a model."""

        return self.safety_monitor.report_invalid_data(
            reason,
            now=self._now(now),
            kind=kind,
            symbol=symbol,
        )

    def market_data_health(self, *, now: datetime | None = None) -> DataHealth:
        return self.safety_monitor.health(self._now(now))

    def balances(self) -> tuple[Balance, ...]:
        """Return immutable balances, including reserved quantities."""

        return tuple(
            Balance(asset, state.available, state.reserved)
            for asset, state in sorted(self._balances.items())
        )

    def balance(self, asset: str) -> Balance:
        normalized = _normalize_asset(asset)
        state = self._balances.get(normalized, _MutableBalance(ZERO, ZERO))
        return Balance(normalized, state.available, state.reserved)

    def acquired_assets(self) -> tuple[Balance, ...]:
        """Return non-quote holdings with positive total quantities."""

        return tuple(
            balance
            for balance in self.balances()
            if balance.asset != self.symbol_rules.quote_asset and balance.total > ZERO
        )

    def orders(self) -> tuple[Order, ...]:
        return tuple(self._orders.values())

    def order(self, order_id: str) -> Order:
        try:
            return self._orders[order_id]
        except KeyError as exc:
            raise OrderNotFoundError(f"unknown order {order_id!r}") from exc

    def fills(self, order_id: str | None = None) -> tuple[Fill, ...]:
        values = tuple(self._fills.values())
        if order_id is not None:
            values = tuple(fill for fill in values if fill.order_id == order_id)
        return values

    def ledger(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._ledger)

    def submit_order(
        self,
        *,
        side: OrderSide | str,
        quantity: Decimal | str | int | float,
        price: Decimal | str | int | float,
        symbol: str | None = None,
        client_order_id: str | None = None,
        now: datetime | None = None,
    ) -> Order:
        """Validate, risk-check, reserve, and accept or reject one order."""

        timestamp = self._now(now)
        request = OrderRequest(
            symbol=(self.symbol_rules.symbol if symbol is None else symbol),
            side=side,
            quantity=_decimal(quantity, "order quantity"),
            price=_decimal(price, "order price"),
            client_order_id=client_order_id,
        )
        _validate_request_for_rules(request, self.symbol_rules)
        if request.client_order_id is not None:
            existing_id = self._client_orders.get(request.client_order_id)
            if existing_id is not None:
                existing = self._orders[existing_id]
                if not _same_order_request(existing, request):
                    raise DuplicateOrderError(
                        f"client order id {request.client_order_id!r} was reused "
                        "with different parameters"
                    )
                return existing

        order_id = f"order-{self._next_order_number:06d}"
        self._next_order_number += 1
        open_order_count = sum(
            not order.is_terminal for order in self._orders.values()
        )
        decision = self.risk_manager.assess(
            request,
            balances=self._balance_views(),
            rules=self.symbol_rules,
            fee_config=self.fee_config,
            now=timestamp,
            open_order_count=open_order_count,
        )
        if not decision.accepted:
            rejected = Order(
                order_id=order_id,
                symbol=request.symbol,
                side=request.side,
                quantity=request.quantity,
                price=request.price,
                status=OrderStatus.REJECTED,
                created_at=timestamp,
                updated_at=timestamp,
                client_order_id=request.client_order_id,
                fee_currency=self.fee_config.currency,
                rejection_code=decision.code,
                rejection_reason=decision.reason,
            )
            self._orders[order_id] = rejected
            if request.client_order_id is not None:
                self._client_orders[request.client_order_id] = order_id
            self._append_entry(
                entry_id=f"rejection:{order_id}",
                entry_type=LedgerEntryType.REJECTION,
                timestamp=timestamp,
                order_id=order_id,
                fill_id=None,
                postings=(),
                description=decision.reason,
            )
            return rejected

        reservation_asset, reservation_amount = _reservation_requirement(
            request,
            self.symbol_rules,
            self.fee_config,
        )
        updated_balances = self._clone_balances()
        state = updated_balances.setdefault(
            reservation_asset,
            _MutableBalance(ZERO, ZERO),
        )
        if state.available < reservation_amount:
            raise AccountingError(
                "risk approval and reservation calculation disagreed on available funds"
            )
        state.available -= reservation_amount
        state.reserved += reservation_amount
        accepted = Order(
            order_id=order_id,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            price=request.price,
            status=OrderStatus.ACCEPTED,
            created_at=timestamp,
            updated_at=timestamp,
            client_order_id=request.client_order_id,
            fee_currency=self.fee_config.currency,
        )
        posting = LedgerPosting(
            reservation_asset,
            available_delta=-reservation_amount,
            reserved_delta=reservation_amount,
        )
        entry = LedgerEntry(
            entry_id=f"reservation:{order_id}",
            entry_type=LedgerEntryType.RESERVATION,
            timestamp=timestamp,
            order_id=order_id,
            fill_id=None,
            postings=(posting,),
            description=f"reserved {reservation_amount} {reservation_asset} for order",
        )
        self._balances = updated_balances
        self._orders[order_id] = accepted
        self._reservations[order_id] = _Reservation(
            reservation_asset,
            reservation_amount,
        )
        if request.client_order_id is not None:
            self._client_orders[request.client_order_id] = order_id
        self._ledger.append(entry)
        return accepted

    def cancel_order(
        self,
        order_id: str,
        *,
        now: datetime | None = None,
    ) -> Order:
        """Cancel an unfilled or partially filled order and release its reserve."""

        timestamp = self._now(now)
        order = self.order(order_id)
        if order.status not in {
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
        }:
            raise OrderStateError(
                f"order {order_id} cannot be cancelled from state {order.status.value}"
            )
        reservation = self._reservations.get(order_id)
        if reservation is None:
            raise AccountingError(f"order {order_id} has no reservation to release")
        updated_balances = self._clone_balances()
        state = updated_balances[reservation.asset]
        if state.reserved < reservation.amount:
            raise AccountingError("reserved balance is below the order reservation")
        state.reserved -= reservation.amount
        state.available += reservation.amount
        cancelled = replace(order, status=OrderStatus.CANCELLED, updated_at=timestamp)
        entry = LedgerEntry(
            entry_id=f"release:{order_id}",
            entry_type=LedgerEntryType.RELEASE,
            timestamp=timestamp,
            order_id=order_id,
            fill_id=None,
            postings=(
                LedgerPosting(
                    reservation.asset,
                    available_delta=reservation.amount,
                    reserved_delta=-reservation.amount,
                ),
            ),
            description=f"released reservation for cancelled order {order_id}",
        )
        self._balances = updated_balances
        self._orders[order_id] = cancelled
        del self._reservations[order_id]
        self._ledger.append(entry)
        return cancelled

    def execute_fill(
        self,
        order_id: str,
        *,
        quantity: Decimal | str | int | float,
        price: Decimal | str | int | float,
        fill_id: str | None = None,
        now: datetime | None = None,
    ) -> Fill:
        """Apply one explicit fill atomically and return its immutable record."""

        timestamp = self._now(now)
        order = self.order(order_id)
        fill_quantity = _decimal(quantity, "fill quantity")
        fill_price = _decimal(price, "fill price")
        if fill_id is None:
            fill_id = f"{order_id}-fill-{len(order.fill_ids) + 1:06d}"
        if not isinstance(fill_id, str) or not fill_id.strip():
            raise ExecutionError("fill_id must be non-empty")
        fill_id = fill_id.strip()
        existing = self._fills.get(fill_id)
        if existing is not None:
            if (
                existing.order_id != order_id
                or existing.quantity != fill_quantity
                or existing.price != fill_price
            ):
                raise DuplicateFillError(
                    f"fill id {fill_id!r} was reused with different parameters"
                )
            return existing
        if order.status not in {
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
        }:
            raise OrderStateError(
                f"order {order_id} cannot be filled from state {order.status.value}"
            )
        _validate_quantity(fill_quantity, self.symbol_rules, "fill quantity")
        _validate_price(fill_price, self.symbol_rules, "fill price")
        if fill_quantity > order.remaining_quantity:
            raise ExecutionError("fill quantity exceeds the order's remaining quantity")
        if order.side is OrderSide.BUY and fill_price > order.price:
            raise ExecutionError("buy fill price exceeds the order limit price")
        if order.side is OrderSide.SELL and fill_price < order.price:
            raise ExecutionError("sell fill price is below the order limit price")
        decision = self.risk_manager.market_data_decision(timestamp)
        if not decision.accepted:
            raise ExecutionError(decision.reason)

        amounts = _trade_amounts(
            order.side,
            fill_quantity,
            fill_price,
            self.symbol_rules,
            self.fee_config,
        )
        reservation = self._reservations.get(order_id)
        if reservation is None:
            raise AccountingError(f"order {order_id} has no reservation")
        remaining = order.remaining_quantity - fill_quantity
        next_reservation_amount = (
            _reservation_requirement_for_values(
                order.side,
                remaining,
                order.price,
                self.symbol_rules,
                self.fee_config,
            )
            if remaining > ZERO
            else ZERO
        )
        if reservation.asset != amounts.debit_asset:
            raise AccountingError("order reservation asset does not match fill debit asset")
        release = reservation.amount - amounts.debit_amount - next_reservation_amount
        if release < ZERO:
            raise ExecutionError(
                "fill would require more reserved funds than the accepted order holds"
            )
        updated_balances = self._clone_balances()
        source = updated_balances[amounts.debit_asset]
        if source.reserved < amounts.debit_amount + next_reservation_amount:
            raise AccountingError("reserved balance cannot fund the fill")
        source.reserved = next_reservation_amount
        source.available += release
        target = updated_balances.setdefault(
            amounts.credit_asset,
            _MutableBalance(ZERO, ZERO),
        )
        target.available += amounts.credit_amount
        self._validate_non_negative_balances(updated_balances)

        new_filled = order.filled_quantity + fill_quantity
        average = (
            fill_price
            if order.average_fill_price is None
            else (order.average_fill_price * order.filled_quantity + fill_price * fill_quantity)
            / new_filled
        )
        average = _quantize(
            average,
            self.symbol_rules.price_precision,
            "average fill price",
            ROUND_HALF_UP,
        )
        status = (
            OrderStatus.FILLED
            if new_filled == order.quantity
            else OrderStatus.PARTIALLY_FILLED
        )
        updated_order = replace(
            order,
            status=status,
            updated_at=timestamp,
            filled_quantity=new_filled,
            average_fill_price=average,
            fee_paid=order.fee_paid + amounts.fee_amount,
            fill_ids=order.fill_ids + (fill_id,),
        )
        fill = Fill(
            fill_id=fill_id,
            order_id=order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=fill_quantity,
            price=fill_price,
            gross_quote=amounts.gross_quote,
            fee_amount=amounts.fee_amount,
            fee_currency=self.fee_config.currency,
            base_delta=amounts.base_delta,
            quote_delta=amounts.quote_delta,
            executed_at=timestamp,
        )
        postings = [
            LedgerPosting(
                amounts.debit_asset,
                available_delta=release,
                reserved_delta=next_reservation_amount - reservation.amount,
            ),
            LedgerPosting(
                amounts.credit_asset,
                available_delta=amounts.credit_amount,
            ),
        ]
        entry = LedgerEntry(
            entry_id=f"fill:{order_id}:{fill_id}",
            entry_type=LedgerEntryType.FILL,
            timestamp=timestamp,
            order_id=order_id,
            fill_id=fill_id,
            postings=tuple(postings),
            description=(
                f"{order.side.value} fill of {fill_quantity} {self.symbol_rules.base_asset} "
                f"at {fill_price} {self.symbol_rules.quote_asset}"
            ),
        )
        self._balances = updated_balances
        self._orders[order_id] = updated_order
        self._fills[fill_id] = fill
        if status is OrderStatus.FILLED:
            del self._reservations[order_id]
        else:
            self._reservations[order_id] = _Reservation(
                reservation.asset,
                next_reservation_amount,
            )
        self._ledger.append(entry)
        return fill

    def portfolio_snapshot(
        self,
        prices: Mapping[str, Decimal | str | int | float],
        *,
        now: datetime | None = None,
        valuation_asset: str | None = None,
    ) -> PortfolioSnapshot:
        """Value balances using only explicit positive prices.

        ``prices`` maps asset symbols to prices in ``valuation_asset``.  The
        valuation asset itself is always valued at exactly one.  If any
        positive holding lacks a price, ``total_value`` is ``None`` and the
        missing assets are listed rather than treated as zero.
        """

        timestamp = self._now(now)
        try:
            valuation = _normalize_asset(
                valuation_asset or self.symbol_rules.quote_asset,
                "valuation_asset",
            )
        except OrderValidationError as exc:
            raise ValuationError(str(exc)) from exc
        if not isinstance(prices, Mapping):
            raise ValuationError("prices must be a mapping of asset to positive price")
        normalized_prices: dict[str, Decimal] = {}
        for asset, price in prices.items():
            try:
                if (
                    isinstance(asset, str)
                    and asset.strip().upper() == self.symbol_rules.symbol
                ):
                    normalized = self.symbol_rules.base_asset
                else:
                    normalized = _normalize_asset(asset, "valuation price asset")
                converted = _positive_decimal(price, f"price for {normalized}")
            except OrderValidationError as exc:
                raise ValuationError(str(exc)) from exc
            normalized_prices[normalized] = converted
        balances = self.balances()
        valued: list[AssetValuation] = []
        unpriced: list[str] = []
        valued_total = ZERO
        for balance in balances:
            if balance.total == ZERO:
                continue
            if balance.asset == valuation:
                price = ONE
            else:
                price = normalized_prices.get(balance.asset)
            if price is None:
                unpriced.append(balance.asset)
                continue
            value = balance.total * price
            valued.append(AssetValuation(balance.asset, balance.total, price, value))
            valued_total += value
        total = None if unpriced else valued_total
        return PortfolioSnapshot(
            timestamp=timestamp,
            valuation_asset=valuation,
            balances=balances,
            valued_assets=tuple(valued),
            unpriced_assets=tuple(sorted(unpriced)),
            valued_value=valued_total,
            total_value=total,
        )

    def reconcile(self) -> ReconciliationReport:
        """Reconcile current balances against all ledger postings."""

        expected: dict[str, _MutableBalance] = {}
        for entry in self._ledger:
            for posting in entry.postings:
                state = expected.setdefault(posting.asset, _MutableBalance(ZERO, ZERO))
                state.available += posting.available_delta
                state.reserved += posting.reserved_delta
        assets = set(expected) | set(self._balances)
        ledger_balances: list[Balance] = []
        actual_balances: list[Balance] = []
        differences: list[LedgerPosting] = []
        for asset in sorted(assets):
            expected_state = expected.get(asset, _MutableBalance(ZERO, ZERO))
            actual_state = self._balances.get(asset, _MutableBalance(ZERO, ZERO))
            ledger_balance = Balance(asset, expected_state.available, expected_state.reserved)
            actual_balance = Balance(asset, actual_state.available, actual_state.reserved)
            ledger_balances.append(ledger_balance)
            actual_balances.append(actual_balance)
            available_delta = actual_balance.available - ledger_balance.available
            reserved_delta = actual_balance.reserved - ledger_balance.reserved
            if available_delta != ZERO or reserved_delta != ZERO:
                differences.append(
                    LedgerPosting(
                        asset,
                        available_delta=available_delta,
                        reserved_delta=reserved_delta,
                    )
                )
        return ReconciliationReport(
            is_consistent=not differences,
            ledger_balances=tuple(ledger_balances),
            actual_balances=tuple(actual_balances),
            differences=tuple(differences),
        )

    def _balance_views(self) -> dict[str, Balance]:
        return {balance.asset: balance for balance in self.balances()}

    def _clone_balances(self) -> dict[str, _MutableBalance]:
        return {
            asset: _MutableBalance(state.available, state.reserved)
            for asset, state in self._balances.items()
        }

    @staticmethod
    def _validate_non_negative_balances(
        balances: Mapping[str, _MutableBalance],
    ) -> None:
        for asset, state in balances.items():
            if state.available < ZERO or state.reserved < ZERO:
                raise AccountingError(f"{asset} balance would become negative")

    def _append_entry(
        self,
        *,
        entry_id: str,
        entry_type: LedgerEntryType,
        timestamp: datetime,
        order_id: str | None,
        fill_id: str | None,
        postings: tuple[LedgerPosting, ...],
        description: str,
    ) -> None:
        if any(entry.entry_id == entry_id for entry in self._ledger):
            raise AccountingError(f"duplicate ledger entry id {entry_id}")
        self._ledger.append(
            LedgerEntry(
                entry_id=entry_id,
                entry_type=entry_type,
                timestamp=timestamp,
                order_id=order_id,
                fill_id=fill_id,
                postings=postings,
                description=description,
            )
        )


def _validate_quantity(quantity: Decimal, rules: SpotSymbolRules, field_name: str) -> Decimal:
    if quantity <= ZERO:
        raise OrderValidationError(f"{field_name} must be positive")
    exact = _exact_precision(quantity, rules.quantity_precision, field_name)
    if exact < rules.min_quantity:
        raise OrderValidationError(f"{field_name} is below the symbol minimum")
    if rules.max_quantity is not None and exact > rules.max_quantity:
        raise OrderValidationError(f"{field_name} exceeds the symbol maximum")
    return exact


def _validate_price(price: Decimal, rules: SpotSymbolRules, field_name: str) -> Decimal:
    if price <= ZERO:
        raise OrderValidationError(f"{field_name} must be positive")
    return _exact_precision(price, rules.price_precision, field_name)


def _validate_request_for_rules(
    request: OrderRequest,
    rules: SpotSymbolRules,
) -> None:
    if request.symbol != rules.symbol:
        raise OrderValidationError(
            f"order symbol {request.symbol} does not match engine symbol {rules.symbol}"
        )
    _validate_quantity(request.quantity, rules, "order quantity")
    _validate_price(request.price, rules, "order price")
    notional = _order_gross_quote(request, rules)
    if notional < rules.min_notional:
        raise OrderValidationError("order notional is below the symbol minimum")


def _order_gross_quote(request: OrderRequest, rules: SpotSymbolRules) -> Decimal:
    rounding = ROUND_UP if request.side is OrderSide.BUY else ROUND_DOWN
    return _quantize(
        request.quantity * request.price,
        rules.quote_precision,
        "order notional",
        rounding,
    )


def _trade_amounts(
    side: OrderSide,
    quantity: Decimal,
    price: Decimal,
    rules: SpotSymbolRules,
    fee_config: FeeConfig,
) -> _TradeAmounts:
    gross_raw = quantity * price
    gross_quote = _quantize(
        gross_raw,
        rules.quote_precision,
        "gross quote amount",
        ROUND_UP if side is OrderSide.BUY else ROUND_DOWN,
    )
    if gross_quote <= ZERO:
        raise ExecutionError("trade gross quote amount rounds to zero")
    fee_amount = fee_config.amount(
        gross_quote=gross_raw,
        base_quantity=quantity,
        rules=rules,
    )
    if fee_config.currency is FeeCurrency.QUOTE:
        if side is OrderSide.BUY:
            debit_asset = rules.quote_asset
            debit_amount = gross_quote + fee_amount
            credit_asset = rules.base_asset
            credit_amount = quantity
            base_delta = quantity
            quote_delta = -debit_amount
        else:
            debit_asset = rules.base_asset
            debit_amount = quantity
            credit_asset = rules.quote_asset
            credit_amount = gross_quote - fee_amount
            if credit_amount <= ZERO:
                raise ExecutionError("quote fee consumes the sell proceeds")
            base_delta = -quantity
            quote_delta = credit_amount
    else:
        if side is OrderSide.BUY:
            debit_asset = rules.quote_asset
            debit_amount = gross_quote
            credit_asset = rules.base_asset
            credit_amount = quantity - fee_amount
            if credit_amount <= ZERO:
                raise ExecutionError("base fee consumes the entire purchased quantity")
            base_delta = credit_amount
            quote_delta = -gross_quote
        else:
            debit_asset = rules.base_asset
            debit_amount = quantity + fee_amount
            credit_asset = rules.quote_asset
            credit_amount = gross_quote
            base_delta = -debit_amount
            quote_delta = gross_quote
    return _TradeAmounts(
        debit_asset=debit_asset,
        debit_amount=debit_amount,
        credit_asset=credit_asset,
        credit_amount=credit_amount,
        gross_quote=gross_quote,
        fee_amount=fee_amount,
        base_delta=base_delta,
        quote_delta=quote_delta,
    )


def _reservation_requirement(
    request: OrderRequest,
    rules: SpotSymbolRules,
    fee_config: FeeConfig,
) -> tuple[str, Decimal]:
    return (
        (rules.base_asset, _reservation_requirement_for_values(
            request.side,
            request.quantity,
            request.price,
            rules,
            fee_config,
        ))
        if request.side is OrderSide.SELL
        else (rules.quote_asset, _reservation_requirement_for_values(
            request.side,
            request.quantity,
            request.price,
            rules,
            fee_config,
        ))
    )


def _reservation_requirement_for_values(
    side: OrderSide,
    quantity: Decimal,
    price: Decimal,
    rules: SpotSymbolRules,
    fee_config: FeeConfig,
) -> Decimal:
    if quantity <= ZERO:
        return ZERO
    gross_quote = _quantize(
        quantity * price,
        rules.quote_precision,
        "reservation gross quote amount",
        ROUND_UP,
    )
    fee_amount = fee_config.amount(
        gross_quote=quantity * price,
        base_quantity=quantity,
        rules=rules,
    )
    if side is OrderSide.BUY:
        return gross_quote + fee_amount if fee_config.currency is FeeCurrency.QUOTE else gross_quote
    return quantity + fee_amount if fee_config.currency is FeeCurrency.BASE else quantity


def _same_order_request(order: Order, request: OrderRequest) -> bool:
    return (
        order.symbol == request.symbol
        and order.side is request.side
        and order.quantity == request.quantity
        and order.price == request.price
    )
