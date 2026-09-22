"""Trading tick — mirrors core/controller.py's pattern for content bots,
but every action here can move real money, so nothing self-approves
beyond the trading_enabled toggle a human sets in the dashboard.
"""

import json

from config import settings
from core import db
from core.trading.util import redact_secrets
from core.trading import exchange, risk, strategy


def _configured_secrets() -> list:
    return [s for s in [db.get_secret("CRYPTO_API_KEY"), db.get_secret("CRYPTO_API_SECRET")] if s]


def _pairs() -> list:
    return [p.strip() for p in settings.CRYPTO_TRADING_PAIRS.split(",") if p.strip()]


def run_trading_tick():
    if not exchange.is_configured():
        return
    if db.get_setting("trading_enabled", "false") != "true":
        return

    pairs = _pairs()
    if not pairs:
        return

    try:
        breaker = risk.check_circuit_breaker(pairs, settings.CRYPTO_QUOTE_CURRENCY)
    except Exception as exc:
        db.log_trading_activity("error", redact_secrets(str(exc), _configured_secrets()))
        return

    if breaker["tripped"]:
        db.log_trading_activity("circuit_breaker", breaker["reason"])
        return

    for pair in pairs:
        try:
            _evaluate_pair(pair, pairs)
        except Exception as exc:
            db.log_trading_activity(
                "error", f"{pair}: {redact_secrets(str(exc), _configured_secrets())}"
            )


def _evaluate_pair(pair: str, pairs: list):
    ohlcv = exchange.fetch_ohlcv(pair, timeframe=settings.TRADING_TIMEFRAME)
    signal = strategy.compute_signal(ohlcv)
    db.log_trading_activity("signal", json.dumps({"pair": pair, "signal": signal}))

    if signal == "hold":
        return

    base = pair.split("/")[0]
    held = exchange.fetch_free_balance(base)

    if signal == "buy":
        if held > 0:
            return  # already holding this pair — the strategy only opens, never adds
        if not risk.can_open_new_position(pairs):
            db.log_trading_activity("skipped", f"{pair}: max open positions reached")
            return
        available = exchange.fetch_free_balance(settings.CRYPTO_QUOTE_CURRENCY)
        size_usd = risk.clamp_position_size(settings.TRADING_MAX_POSITION_USD, available)
        if size_usd <= 0:
            db.log_trading_activity("skipped", f"{pair}: no available balance")
            return
        price = exchange.fetch_last_price(pair)
        amount = size_usd / price
        order = exchange.place_market_order(pair, "buy", amount)
        db.record_trade(
            pair, "buy", amount, price, size_usd, order.get("id"), "submitted",
            "SMA crossover buy signal",
        )
        db.log_trading_activity(
            "trade", f"BUY {amount:.6f} {base} (~${size_usd:.2f}) on {pair}"
        )

    elif signal == "sell":
        if held <= 0:
            return  # nothing held on this pair to sell
        price = exchange.fetch_last_price(pair)
        order = exchange.place_market_order(pair, "sell", held)
        db.record_trade(
            pair, "sell", held, price, held * price, order.get("id"), "submitted",
            "SMA crossover sell signal",
        )
        db.log_trading_activity(
            "trade", f"SELL {held:.6f} {base} (~${held * price:.2f}) on {pair}"
        )
