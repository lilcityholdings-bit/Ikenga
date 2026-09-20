"""The loop that actually runs the bots — deploy this as its own process,
separate from the dashboard. Crash-proof by design: any exception during a
tick is logged and the loop keeps going rather than exiting, so a bad
response from a free LLM tier can't take the whole worker down.
"""

import time
import traceback

from agents.bot_template import ensure_bots_seeded
from config import settings
from core import controller, database as db


def main():
    db.init_db()
    ensure_bots_seeded()
    print("Worker started.")
    while True:
        try:
            controller.run_worker_tick()
        except Exception:
            traceback.print_exc()
        time.sleep(settings.WORKER_LOOP_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
