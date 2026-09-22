"""The only module that talks to the exchange. Every call here is against
real funds once CRYPTO_API_KEY is set, so this stays deliberately thin:
no retries (a retried order could double-submit), no silent fallback, no
guessing at defaults ccxt didn't give us.
"""

import ccxt

from config import settings
from core import db


class ExchangeError(Exception):
    pass


_exchange = None
_exchange_key = None


def get_exchange():
    """Rebuilds the client if the stored key changes — keys here can come
    from the dashboard's Setup screen (core.db.get_secret), not just env
    vars, and a stale cached client would silently keep using an old key."""
    global _exchange, _exchange_key
    api_key = db.get_secret("CRYPTO_API_KEY")
    api_secret = db.get_secret("CRYPTO_API_SECRET")
    if _exchange is not None and _exchange_key == (api_key, api_secret):
        return _exchange
    if not settings.CRYPTO_EXCHANGE:
        raise ExchangeError("CRYPTO_EXCHANGE is not set")
    exchange_class = getattr(ccxt, settings.CRYPTO_EXCHANGE, None)
    if exchange_class is None:
        raise ExchangeError(f"Unknown exchange id: {settings.CRYPTO_EXCHANGE}")
    _exchange = exchange_class({
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
    })
    _exchange_key = (api_key, api_secret)
    return _exchange


def is_configured() -> bool:
    return bool(
        settings.CRYPTO_EXCHANGE
        and db.get_secret("CRYPTO_API_KEY")
        and db.get_secret("CRYPTO_API_SECRET")
    )


def fetch_ohlcv(pair: str, timeframe: str = "1h", limit: int = 50) -> list:
    """Returns [timestamp, open, high, low, close, volume] rows, oldest first."""
    return get_exchange().fetch_ohlcv(pair, timeframe=timeframe, limit=limit)


def fetch_last_price(pair: str) -> float:
    return float(get_exchange().fetch_ticker(pair)["last"])


def fetch_free_balance(currency: str) -> float:
    balance = get_exchange().fetch_balance()
    return float(balance.get("free", {}).get(currency, 0) or 0)


def place_market_order(pair: str, side: str, amount: float) -> dict:
    if side not in ("buy", "sell"):
        raise ExchangeError(f"Invalid order side: {side}")
    if amount <= 0:
        raise ExchangeError(f"Refusing to place a {side} order for non-positive amount {amount}")
    return get_exchange().create_order(pair, "market", side, amount)
