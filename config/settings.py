"""All tunable settings. Environment variables override these at runtime."""
import os


def _int(name, default):
    try:
        return int(os.getenv(name, default))
    except Exception:
        return default


# --- population ---
# Hard cap, never unlimited. This is a measured tradeoff, not a guess:
# at 3 bots roughly one new genome is tried per ranking cycle, so selection is
# slow and noisy (the elite landed between 68% and 97% of optimum across test
# seeds). At 6 it converges faster and higher. Raise it ONLY if you give each
# bot a distinct niche — several near-identical sites on one domain is the
# doorway-page pattern search engines penalise.
MAX_BOTS = _int("MAX_BOTS", 3)
STARTING_BOTS = 1
COPIES_PER_PARENT = 1
RANKING_CYCLE_DAYS = float(os.getenv("RANKING_CYCLE_DAYS", "7"))

# --- worker ---
WORKER_INTERVAL_SECONDS = _int("WORKER_INTERVAL_SECONDS", 300)
MAX_PARALLEL_BOTS = _int("MAX_PARALLEL_BOTS", 4)
MAX_ARTICLES_PER_BOT_PER_HOUR = _int("MAX_ARTICLES_PER_BOT_PER_HOUR", 1)

# Backpressure. Production used to outrun review by roughly 70x, so the queue
# grew forever. Bots stop writing once this many drafts are already waiting.
MAX_PENDING_QUEUE = _int("MAX_PENDING_QUEUE", 12)

# --- content ---
MIN_ARTICLE_CHARS = 320

DEFAULT_OBJECTIVE = (
    "Build a small site that earns affiliate revenue by being genuinely more "
    "useful than the competition. Stay ethical and legal."
)

GUARDRAILS = [
    "Do not do anything illegal",
    "No black-hat SEO",
    "Follow affiliate program rules and disclose affiliate links",
    "Do not create harmful or deceptive content",
    "Always obey direct orders from the owner",
]

DATABASE_PATH = os.getenv("DB_PATH", "").strip() or "data/bots.db"

# --- crypto trading (real money once enabled in the dashboard) ---
# CRYPTO_API_KEY / CRYPTO_API_SECRET are NOT here — like GITHUB_TOKEN, they
# go through core.db.get_secret(), which checks the dashboard's Setup
# screen before falling back to an env var of the same name. Everything
# below is a plain tunable, not a credential, so it stays a static setting
# like the rest of this file.
CRYPTO_EXCHANGE = os.getenv("CRYPTO_EXCHANGE", "coinbase")
CRYPTO_QUOTE_CURRENCY = os.getenv("CRYPTO_QUOTE_CURRENCY", "USD")
CRYPTO_TRADING_PAIRS = os.getenv("CRYPTO_TRADING_PAIRS", "BTC/USD,ETH/USD")
TRADING_TIMEFRAME = os.getenv("TRADING_TIMEFRAME", "1h")

# Deliberately conservative defaults — there's no way to know the owner's
# risk tolerance or capital from here. Review before enabling.
TRADING_MAX_POSITION_USD = float(os.getenv("TRADING_MAX_POSITION_USD", "25"))
TRADING_DAILY_LOSS_LIMIT_USD = float(os.getenv("TRADING_DAILY_LOSS_LIMIT_USD", "50"))
TRADING_MAX_OPEN_POSITIONS = _int("TRADING_MAX_OPEN_POSITIONS", 2)
TRADING_LOOP_INTERVAL_SECONDS = _int("TRADING_LOOP_INTERVAL_SECONDS", 900)

# "poll" (default): the main worker checks prices on a timer, every
# TRADING_LOOP_INTERVAL_SECONDS. "realtime": a separate process
# (trading_stream.py) holds a live websocket connection instead. These are
# mutually exclusive against the same account — see core/worker.py.
TRADING_MODE = os.getenv("TRADING_MODE", "poll")
REALTIME_CANDLE_INTERVAL_SECONDS = _int("REALTIME_CANDLE_INTERVAL_SECONDS", 60)
