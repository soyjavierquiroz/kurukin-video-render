"""Runtime availability for stock providers owned by MoneyPrinterTurbo.

This module deliberately does not implement provider clients.  It only reads
the configuration keys consumed by MPT's native material functions so policy
resolution and Human Review can state when a stock source is not ready.
"""

from __future__ import annotations

from app.config import config


NATIVE_STOCK_CONFIG_KEYS = {
    "pexels": "pexels_api_keys",
    "pixabay": "pixabay_api_keys",
    "coverr": "coverr_api_keys",
}


def native_stock_config_key(provider: str) -> str | None:
    return NATIVE_STOCK_CONFIG_KEYS.get(str(provider or "").strip().lower())


def native_stock_provider_configured(provider: str) -> bool:
    """Whether MPT's required native key list has at least one usable value."""
    config_key = native_stock_config_key(provider)
    if not config_key:
        return False
    value = config.app.get(config_key)
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (tuple, list)):
        return any(str(item or "").strip() for item in value)
    return False
