"""Real-time crypto trading process — a persistent websocket connection,
deployed as its own service, separate from both run.py (dashboard) and
worker.py (content bots + polling-mode trading).

Only meaningful with TRADING_MODE=realtime. worker.py automatically skips
its own trading tick in that mode so the two can't double-trade the same
account. See README's Trading section before running this against a
funded account.
"""

import asyncio

from core.trading.realtime import main

if __name__ == "__main__":
    asyncio.run(main())
