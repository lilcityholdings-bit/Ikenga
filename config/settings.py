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
