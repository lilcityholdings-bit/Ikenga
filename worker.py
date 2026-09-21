"""The loop that actually runs the bots — deploy this as its own process,
separate from the dashboard. Crash-proof by design: any exception during a
tick is logged and the loop keeps going rather than exiting, so a bad
response from a free LLM tier can't take the whole worker down.

Runs two independent ticks off the same loop: the content bots (every
WORKER_LOOP_INTERVAL_SECONDS) and, if configured, the crypto trading tick
(every TRADING_LOOP_INTERVAL_SECONDS, gated separately since it should run
far less often than the content loop).

The trading tick only runs in TRADING_MODE=poll. In TRADING_MODE=realtime,
trading_stream.py owns it instead (a persistent websocket connection, not
a fit for this loop) — this file must stay out of its way entirely, or
the same account could get traded by both at once.
"""

import time
import traceback
from datetime import datetime, timezone

from agents.bot_template import ensure_bots_seeded
from config import settings
from core import controller, database as db
from core.trading import controller as trading_controller


def _maybe_run_trading_tick():
    if settings.TRADING_MODE != "poll":
        return  # owned by trading_stream.py in realtime mode
    last = db.get_setting("trading_last_tick")
    now = datetime.now(timezone.utc)
    if last:
        elapsed = (now - datetime.fromisoformat(last)).total_seconds()
        if elapsed < settings.TRADING_LOOP_INTERVAL_SECONDS:
            return
    db.set_setting("trading_last_tick", now.isoformat())
    trading_controller.run_trading_tick()


def main():
    db.init_db()
    ensure_bots_seeded()
    print("Worker started.")
    while True:
        try:
            controller.run_worker_tick()
        except Exception:
            traceback.print_exc()
        try:
            _maybe_run_trading_tick()
        except Exception:
            traceback.print_exc()
        time.sleep(settings.WORKER_LOOP_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
