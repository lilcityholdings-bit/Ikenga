"""The safety engine. Every real-money action goes through here first.

Three independent limits, enforced regardless of what the strategy
signals:
1. A hard cap on how much a single order can be worth
   (TRADING_MAX_POSITION_USD) — the strategy never gets to size its own
   trade.
2. A hard cap on how many pairs can be held open at once
   (TRADING_MAX_OPEN_POSITIONS).
3. A circuit breaker that halts ALL trading for the rest of the UTC day
   if the bot-managed portion of the portfolio has dropped by more than
   TRADING_DAILY_LOSS_LIMIT_USD since the day's starting value.

"Bot-managed portion" matters: portfolio_value_usd only looks at the
quote currency and the configured trading pairs' base assets, not the
whole exchange account, which may hold unrelated funds this bot has no
business measuring or limiting.
"""

from datetime import datetime, timezone

from config import settings
from core import db
from core.trading import exchange


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def portfolio_value_usd(pairs: list, quote_currency: str) -> float:
    total = exchange.fetch_free_balance(quote_currency)
    for pair in pairs:
        base = pair.split("/")[0]
        held = exchange.fetch_free_balance(base)
        if held > 0:
            total += held * exchange.fetch_last_price(pair)
    return total


def check_circuit_breaker(pairs: list, quote_currency: str) -> dict:
    """Returns {'tripped': bool, 'reason': str|None}. Resets the daily
    baseline automatically the first time this runs on a new UTC day."""
    state = db.get_trading_state()
    today = _today()
    current_value = portfolio_value_usd(pairs, quote_currency)

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


def clamp_position_size(desired_usd: float, available_usd: float) -> float:
    """Never trust a caller's requested size — always clamp to the
    configured hard cap and to what's actually available."""
    return max(0.0, min(desired_usd, settings.TRADING_MAX_POSITION_USD, available_usd))


def open_position_count(pairs: list) -> int:
    count = 0
    for pair in pairs:
        base = pair.split("/")[0]
        if exchange.fetch_free_balance(base) > 0:
            count += 1
    return count


def can_open_new_position(pairs: list) -> bool:
    return open_position_count(pairs) < settings.TRADING_MAX_OPEN_POSITIONS
