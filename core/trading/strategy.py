"""Deterministic moving-average-crossover signal.

Trade timing is intentionally NOT decided by an LLM — free-tier models
can hallucinate reasoning, and a decision that risks real money needs to
produce the exact same output every time given the same price data. An
LLM can narrate *why* a signal fired for the activity log; it never picks
the trade or the size.

This is a simple, well-known momentum strategy. Being deterministic and
auditable is not the same as being profitable — nothing here implies an
edge. It's a defensible, testable starting point, not a guarantee.
"""

SHORT_PERIOD = 10
LONG_PERIOD = 30


def _sma(closes: list, period: int):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def compute_signal(ohlcv: list) -> str:
    """ohlcv: [[ts, open, high, low, close, volume], ...], oldest first.
    Returns 'buy', 'sell', or 'hold'.

    A crossover, not a level, triggers the signal: comparing the current
    short/long SMA relationship against the previous candle's, so it only
    fires once at the moment the trend actually flips rather than firing
    every tick the short average happens to sit above the long one.
    """
    closes = [candle[4] for candle in ohlcv]
    if len(closes) < LONG_PERIOD + 1:
        return "hold"

    short_now = _sma(closes, SHORT_PERIOD)
    long_now = _sma(closes, LONG_PERIOD)
    short_prev = _sma(closes[:-1], SHORT_PERIOD)
    long_prev = _sma(closes[:-1], LONG_PERIOD)

    if None in (short_now, long_now, short_prev, long_prev):
        return "hold"

    crossed_up = short_prev <= long_prev and short_now > long_now
    crossed_down = short_prev >= long_prev and short_now < long_now

    if crossed_up:
        return "buy"
    if crossed_down:
        return "sell"
    return "hold"
