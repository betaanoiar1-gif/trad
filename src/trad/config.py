"""Validated, simulation-only configuration for the trad project.

This module deliberately contains configuration and validation only.  It does
not connect to an exchange, read private credentials, submit orders, or run a
trading engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from pathlib import Path
import re
from typing import Any, Mapping, TypeVar
import tomllib


class ConfigurationError(ValueError):
    """Raised when a configuration is missing, unsafe, or malformed."""


class RunMode(str, Enum):
    """Supported non-live run modes."""

    BACKTEST = "backtest"
    PAPER = "paper"


class Instrument(str, Enum):
    """The two accounting domains kept separate by the configuration."""

    SPOT = "spot"
    PERPETUAL_FUTURES = "perpetual_futures"


class MarketDataSource(str, Enum):
    """Data-source declarations supported by the foundation layer."""

    REPLAY = "replay"
    PUBLIC_READ_ONLY = "public_read_only"


@dataclass(frozen=True)
class SafetyConfig:
    """Non-negotiable safety boundary for this project stage."""

    simulation_only: bool = True


@dataclass(frozen=True)
class SpotConfig:
    """Initial Spot balances; no derivatives fields are present here."""

    starting_quote_balance: float = 10_000.0
    starting_base_balance: float = 0.0


@dataclass(frozen=True)
class PerpetualFuturesConfig:
    """Initial futures collateral and conservative leverage settings.

    Funding, margin, liquidation, and venue-specific mechanics are deliberately
    not implemented in Step 2.  The 1x defaults are a conservative declaration
    for later simulator work, not a claim about a venue's rules.
    """

    starting_collateral: float = 10_000.0
    initial_leverage: float = 1.0
    max_leverage: float = 1.0


@dataclass(frozen=True)
class ResourceConfig:
    """Local CPU-only resource limits for future components."""

    max_cpu_workers: int = 1


@dataclass(frozen=True)
class RunConfig:
    """Validated configuration shared by future backtest and paper engines."""

    mode: RunMode = RunMode.BACKTEST
    instrument: Instrument = Instrument.SPOT
    symbol: str = "BTC/USDT"
    market_data_source: MarketDataSource = MarketDataSource.REPLAY
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    spot: SpotConfig = field(default_factory=SpotConfig)
    perpetual_futures: PerpetualFuturesConfig = field(
        default_factory=PerpetualFuturesConfig
    )
    resources: ResourceConfig = field(default_factory=ResourceConfig)

    @property
    def active_accounting_config(self) -> SpotConfig | PerpetualFuturesConfig:
        """Return only the accounting configuration for the selected instrument."""

        if self.instrument is Instrument.SPOT:
            return self.spot
        return self.perpetual_futures

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "RunConfig":
        """Build and validate a configuration from a TOML-like mapping."""

        if not isinstance(raw, Mapping):
            raise ConfigurationError("configuration root must be a TOML table")

        _reject_forbidden_keys(raw)
        _only_keys(
            raw,
            {"run", "safety", "spot", "perpetual_futures", "resources"},
            "root",
        )

        run = _table(raw, "run")
        _only_keys(
            run,
            {"mode", "instrument", "symbol", "market_data_source"},
            "run",
        )
        mode = _enum_value(
            run.get("mode", RunMode.BACKTEST.value), RunMode, "run.mode"
        )
        instrument = _enum_value(
            run.get("instrument", Instrument.SPOT.value),
            Instrument,
            "run.instrument",
        )
        symbol = _symbol(run.get("symbol", "BTC/USDT"))
        market_data_source = _enum_value(
            run.get("market_data_source", MarketDataSource.REPLAY.value),
            MarketDataSource,
            "run.market_data_source",
        )

        safety_table = _table(raw, "safety")
        _only_keys(safety_table, {"simulation_only"}, "safety")
        simulation_only = safety_table.get("simulation_only", True)
        if not isinstance(simulation_only, bool):
            raise ConfigurationError("safety.simulation_only must be a boolean")
        if not simulation_only:
            raise ConfigurationError(
                "safety.simulation_only must remain true; real trading is not supported"
            )
        safety = SafetyConfig(simulation_only=True)

        spot_present = "spot" in raw
        futures_present = "perpetual_futures" in raw
        if instrument is Instrument.SPOT and futures_present:
            raise ConfigurationError(
                "perpetual_futures settings cannot be combined with instrument=spot"
            )
        if instrument is Instrument.PERPETUAL_FUTURES and spot_present:
            raise ConfigurationError(
                "spot settings cannot be combined with instrument=perpetual_futures"
            )

        spot_table = _table(raw, "spot")
        _only_keys(
            spot_table,
            {"starting_quote_balance", "starting_base_balance"},
            "spot",
        )
        spot = SpotConfig(
            starting_quote_balance=_non_negative_float(
                spot_table.get("starting_quote_balance", 10_000.0),
                "spot.starting_quote_balance",
            ),
            starting_base_balance=_non_negative_float(
                spot_table.get("starting_base_balance", 0.0),
                "spot.starting_base_balance",
            ),
        )

        futures_table = _table(raw, "perpetual_futures")
        _only_keys(
            futures_table,
            {"starting_collateral", "initial_leverage", "max_leverage"},
            "perpetual_futures",
        )
        starting_collateral = _non_negative_float(
            futures_table.get("starting_collateral", 10_000.0),
            "perpetual_futures.starting_collateral",
        )
        initial_leverage = _bounded_float(
            futures_table.get("initial_leverage", 1.0),
            "perpetual_futures.initial_leverage",
            minimum=1.0,
            maximum=100.0,
        )
        max_leverage = _bounded_float(
            futures_table.get("max_leverage", 1.0),
            "perpetual_futures.max_leverage",
            minimum=1.0,
            maximum=100.0,
        )
        if initial_leverage > max_leverage:
            raise ConfigurationError(
                "perpetual_futures.initial_leverage cannot exceed "
                "perpetual_futures.max_leverage"
            )
        futures = PerpetualFuturesConfig(
            starting_collateral=starting_collateral,
            initial_leverage=initial_leverage,
            max_leverage=max_leverage,
        )

        resources_table = _table(raw, "resources")
        _only_keys(resources_table, {"max_cpu_workers"}, "resources")
        max_cpu_workers = _positive_int(
            resources_table.get("max_cpu_workers", 1),
            "resources.max_cpu_workers",
        )
        if max_cpu_workers > 64:
            raise ConfigurationError("resources.max_cpu_workers cannot exceed 64")

        return cls(
            mode=mode,
            instrument=instrument,
            symbol=symbol,
            market_data_source=market_data_source,
            safety=safety,
            spot=spot,
            perpetual_futures=futures,
            resources=ResourceConfig(max_cpu_workers=max_cpu_workers),
        )

    @classmethod
    def from_toml(cls, path: str | Path) -> "RunConfig":
        """Load and validate a TOML configuration file."""

        config_path = Path(path)
        try:
            with config_path.open("rb") as handle:
                raw = tomllib.load(handle)
        except FileNotFoundError as exc:
            raise ConfigurationError(f"configuration file not found: {config_path}") from exc
        except OSError as exc:
            raise ConfigurationError(
                f"could not read configuration file {config_path}: {exc}"
            ) from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationError(
                f"invalid TOML in configuration file {config_path}: {exc}"
            ) from exc
        return cls.from_mapping(raw)


def load_config(path: str | Path) -> RunConfig:
    """Convenience wrapper for loading a validated TOML configuration."""

    return RunConfig.from_toml(path)


_ENUM_T = TypeVar("_ENUM_T", bound=Enum)


def _enum_value(value: Any, enum_type: type[_ENUM_T], field_name: str) -> _ENUM_T:
    if not isinstance(value, str):
        raise ConfigurationError(f"{field_name} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in enum_type)
        raise ConfigurationError(
            f"{field_name} must be one of: {allowed}; got {value!r}"
        ) from exc


def _table(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{key} must be a TOML table")
    return value


def _only_keys(raw: Mapping[str, Any], allowed: set[str], section: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        names = ", ".join(repr(name) for name in unknown)
        raise ConfigurationError(f"unknown key(s) in {section}: {names}")


def _symbol(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigurationError("run.symbol must be a string")
    value = value.strip()
    if not value or len(value) > 64 or re.search(r"\s", value):
        raise ConfigurationError(
            "run.symbol must be 1-64 non-whitespace characters"
        )
    return value


def _non_negative_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{field_name} must be a finite non-negative number")
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        raise ConfigurationError(f"{field_name} must be a finite non-negative number")
    return converted


def _bounded_float(
    value: Any,
    field_name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    converted = _non_negative_float(value, field_name)
    if converted < minimum or converted > maximum:
        raise ConfigurationError(
            f"{field_name} must be between {minimum:g} and {maximum:g}"
        )
    return converted


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigurationError(f"{field_name} must be a positive integer")
    return value


_FORBIDDEN_KEY_NAMES = {
    "api_key",
    "api_secret",
    "apikey",
    "apisecret",
    "credentials",
    "credential",
    "password",
    "private_key",
    "privatekey",
    "secret",
    "token",
    "real",
    "real_trading",
    "live",
    "live_trading",
    "submit_orders",
    "order_submission",
}


def _reject_forbidden_keys(value: Any, path: str = "root") -> None:
    """Reject credential and real-order fields before interpreting the config."""

    if not isinstance(value, Mapping):
        return
    for key, child in value.items():
        if not isinstance(key, str):
            raise ConfigurationError(f"configuration key at {path} must be a string")
        normalized = key.lower().replace("-", "_").replace(" ", "_")
        if (
            normalized in _FORBIDDEN_KEY_NAMES
            or "api_key" in normalized
            or "api_secret" in normalized
            or "private_key" in normalized
            or "credential" in normalized
            or "password" in normalized
            or "secret" in normalized
            or "token" in normalized
        ):
            raise ConfigurationError(
                f"unsupported sensitive or real-trading key at {path}.{key}; "
                "this project does not accept private credentials or real orders"
            )
        _reject_forbidden_keys(child, f"{path}.{key}")