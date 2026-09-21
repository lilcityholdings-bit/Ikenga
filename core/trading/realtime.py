"""Real-time (websocket) trading engine, via ccxt.pro.

Runs as its own process (trading_stream.py) — a persistent websocket
connection is fundamentally a different shape of program from the
polling loop in worker.py/core/trading/controller.py, and the two must
never run against the same account at once (see TRADING_MODE in
config/settings.py).

Coinbase's websocket API does not stream candles or balances
(ccxt.pro: watchOHLCV and watchBalance are both unsupported for
Coinbase) — only live ticker price, trades, and order updates. So "real
time" here means: the bot is notified of every price change the instant
it happens (watch_ticker blocks until the next update arrives, rather
than polling on a timer), and it builds its own rolling candles from
that stream locally. The moving-average STRATEGY still only produces a
new signal once a candle closes — that's what a moving average is — so
what actually makes this faster than the polling mode is a much shorter
candle interval (REALTIME_CANDLE_INTERVAL_SECONDS, default 60s, vs the
polling mode's default 1h), combined with zero delay noticing each one.

Every safety rule from core/trading/risk.py still applies here: same
per-order cap, same max-open-positions cap, same daily circuit breaker,
same trading_enabled dashboard toggle. Streaming and candle-building
always run so the bot is instantly ready to act — only order EXECUTION
is gated on trading_enabled, checked fresh on every signal.
"""

import asyncio
import time
from datetime import datetime, timezone

import ccxt.pro as ccxtpro

from config import settings
from core import database as db
from core.security import redact_secrets
from core.trading import risk, strategy

RECONNECT_BACKOFF_SECONDS = [1, 2, 5, 10, 30, 60]
HEARTBEAT_SETTING_KEY = "realtime_trading_heartbeat"
HEARTBEAT_INTERVAL_SECONDS = 30


class RealtimeConfigError(Exception):
    pass


def _configured_secrets() -> list:
    return [s for s in [settings.CRYPTO_API_KEY, settings.CRYPTO_API_SECRET] if s]


def _make_exchange():
    exchange_class = getattr(ccxtpro, settings.CRYPTO_EXCHANGE, None)
    if exchange_class is None:
        raise RealtimeConfigError(
            f"ccxt.pro has no exchange named {settings.CRYPTO_EXCHANGE!r}"
        )
    return exchange_class({
        "apiKey": settings.CRYPTO_API_KEY,
        "secret": settings.CRYPTO_API_SECRET,
        "enableRateLimit": True,
    })


class CandleBuilder:
    """Aggregates a live price stream into fixed-interval OHLCV candles,
    since the exchange doesn't stream candles directly here."""

    def __init__(self, interval_seconds: int, max_candles: int):
        self.interval_ms = interval_seconds * 1000
        self.max_candles = max_candles
        self.candles = []  # finalized candles only: [ts, o, h, l, c, v]
        self._current = None

    def add_tick(self, price: float, timestamp_ms: int) -> bool:
        """Feed one price tick. Returns True the moment this tick closes a
        candle (i.e. self.candles just gained a new finalized entry and a
        signal check is due)."""
        bucket = (timestamp_ms // self.interval_ms) * self.interval_ms
        if self._current is None:
            self._current = [bucket, price, price, price, price, 0]
            return False
        if bucket == self._current[0]:
            self._current[2] = max(self._current[2], price)
            self._current[3] = min(self._current[3], price)
            self._current[4] = price
            return False
        self.candles.append(self._current)
        if len(self.candles) > self.max_candles:
            self.candles = self.candles[-self.max_candles:]
        self._current = [bucket, price, price, price, price, 0]
        return True


async def _portfolio_value_usd(exchange, pairs: list, quote_currency: str) -> float:
    """Async counterpart to core.trading.risk.portfolio_value_usd — same
    logic, kept in sync deliberately since risk.py's version can't be
    reused directly (it calls the synchronous exchange module)."""
    balance = await exchange.fetch_balance()
    free = balance.get("free", {})
    total = float(free.get(quote_currency, 0) or 0)
    for pair in pairs:
        base = pair.split("/")[0]
        held = float(free.get(base, 0) or 0)
        if held > 0:
            ticker = await exchange.fetch_ticker(pair)
            total += held * float(ticker["last"])
    return total


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


async def _check_circuit_breaker(exchange, pairs: list) -> dict:
    """Async counterpart to core.trading.risk.check_circuit_breaker."""
    state = db.get_trading_state()
    today = _today()
    current_value = await _portfolio_value_usd(exchange, pairs, settings.CRYPTO_QUOTE_CURRENCY)

    if state is None or state["daily_date"] != today:
        db.reset_trading_day(today, current_value)
        return {"tripped": False, "reason": None}

    if state["tripped"]:
        return {"tripped": True, "reason": state["reason"]}

    loss = state["daily_start_value_usd"] - current_value
    if loss > settings.TRADING_DAILY_LOSS_LIMIT_USD:
        reason = (
            f"Portfolio down ${loss:.2f} today (started at "
            f"${state['daily_start_value_usd']:.2f}, now ${current_value:.2f}); "
            f"limit is ${settings.TRADING_DAILY_LOSS_LIMIT_USD:.2f}"
        )
        db.trip_circuit_breaker(reason)
        return {"tripped": True, "reason": reason}

    return {"tripped": False, "reason": None}


async def _handle_signal(exchange, pair: str, candles: list, pairs: list):
    signal = strategy.compute_signal(candles)
    db.log_trading_activity("signal", f'{{"pair": "{pair}", "signal": "{signal}", "mode": "realtime"}}')

    if signal == "hold":
        return
    if db.get_setting("trading_enabled", "false") != "true":
        db.log_trading_activity("skipped", f"{pair}: signal={signal} but trading is disabled")
        return

    breaker = await _check_circuit_breaker(exchange, pairs)
    if breaker["tripped"]:
        db.log_trading_activity("circuit_breaker", breaker["reason"])
        return

    balance = await exchange.fetch_balance()
    free = balance.get("free", {})
    base = pair.split("/")[0]
    held = float(free.get(base, 0) or 0)

    if signal == "buy":
        if held > 0:
            return  # already holding this pair
        open_count = sum(1 for p in pairs if float(free.get(p.split("/")[0], 0) or 0) > 0)
        if open_count >= settings.TRADING_MAX_OPEN_POSITIONS:
            db.log_trading_activity("skipped", f"{pair}: max open positions reached")
            return
        available = float(free.get(settings.CRYPTO_QUOTE_CURRENCY, 0) or 0)
        size_usd = risk.clamp_position_size(settings.TRADING_MAX_POSITION_USD, available)
        if size_usd <= 0:
            db.log_trading_activity("skipped", f"{pair}: no available balance")
            return
        ticker = await exchange.fetch_ticker(pair)
        price = float(ticker["last"])
        amount = size_usd / price
        order = await exchange.create_order(pair, "market", "buy", amount)
        db.record_trade(
            pair, "buy", amount, price, size_usd, order.get("id"), "submitted",
            "Realtime SMA crossover buy signal",
        )
        db.log_trading_activity(
            "trade", f"BUY {amount:.6f} {base} (~${size_usd:.2f}) on {pair} [realtime]"
        )

    elif signal == "sell":
        if held <= 0:
            return
        ticker = await exchange.fetch_ticker(pair)
        price = float(ticker["last"])
        order = await exchange.create_order(pair, "market", "sell", held)
        db.record_trade(
            pair, "sell", held, price, held * price, order.get("id"), "submitted",
            "Realtime SMA crossover sell signal",
        )
        db.log_trading_activity(
            "trade", f"SELL {held:.6f} {base} (~${held * price:.2f}) on {pair} [realtime]"
        )


async def _watch_pair(exchange, pair: str, builder: CandleBuilder, pairs: list):
    """Runs forever for one pair: blocks on the next price tick, feeds the
    candle builder, and checks for a signal whenever a candle closes.
    Reconnects with backoff on any error rather than dying — a dropped
    websocket is routine, not exceptional."""
    backoff_idx = 0
    while True:
        try:
            ticker = await exchange.watch_ticker(pair)
            backoff_idx = 0
        except Exception as exc:
            delay = RECONNECT_BACKOFF_SECONDS[min(backoff_idx, len(RECONNECT_BACKOFF_SECONDS) - 1)]
            db.log_trading_activity(
                "stream_error",
                f"{pair}: {redact_secrets(str(exc), _configured_secrets())} "
                f"— reconnecting in {delay}s",
            )
            backoff_idx += 1
            await asyncio.sleep(delay)
            continue

        price = ticker.get("last")
        ts = ticker.get("timestamp") or int(time.time() * 1000)
        if price is None:
            continue

        candle_closed = builder.add_tick(float(price), int(ts))
        if not candle_closed:
            continue

        try:
            await _handle_signal(exchange, pair, builder.candles, pairs)
        except Exception as exc:
            db.log_trading_activity(
                "error", f"{pair}: {redact_secrets(str(exc), _configured_secrets())}"
            )


async def _heartbeat_loop():
    while True:
        db.set_setting(HEARTBEAT_SETTING_KEY, datetime.now(timezone.utc).isoformat())
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


async def main():
    db.init_db()

    if settings.TRADING_MODE != "realtime":
        raise RealtimeConfigError(
            f"TRADING_MODE is {settings.TRADING_MODE!r}, not 'realtime'. Set "
            "TRADING_MODE=realtime before running trading_stream.py, and make sure "
            "worker.py isn't also running its own trading tick against this account."
        )
    if not (settings.CRYPTO_API_KEY and settings.CRYPTO_API_SECRET):
        raise RealtimeConfigError("CRYPTO_API_KEY / CRYPTO_API_SECRET are not set")

    pairs = [p.strip() for p in settings.CRYPTO_TRADING_PAIRS.split(",") if p.strip()]
    if not pairs:
        raise RealtimeConfigError("CRYPTO_TRADING_PAIRS is empty")

    exchange = _make_exchange()
    builders = {
        pair: CandleBuilder(settings.REALTIME_CANDLE_INTERVAL_SECONDS, strategy.LONG_PERIOD * 3)
        for pair in pairs
    }

    print(f"Realtime trading stream starting for {pairs} on {settings.CRYPTO_EXCHANGE}...")
    try:
        await asyncio.gather(
            _heartbeat_loop(),
            *[_watch_pair(exchange, pair, builders[pair], pairs) for pair in pairs],
        )
    finally:
        await exchange.close()
