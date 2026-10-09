"""Local, simulation-only foundations for the trad project."""

__version__ = "0.1.0"

from .config import (
    ConfigurationError,
    Instrument,
    MarketDataSource,
    PerpetualFuturesConfig,
    ResourceConfig,
    RunConfig,
    RunMode,
    SafetyConfig,
    SpotConfig,
    load_config,
)

__all__ = [
    "ConfigurationError",
    "Instrument",
    "MarketDataSource",
    "PerpetualFuturesConfig",
    "ResourceConfig",
    "RunConfig",
    "RunMode",
    "SafetyConfig",
    "SpotConfig",
    "load_config",
]
