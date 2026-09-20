import os


def _env(name, default=None):
    return os.environ.get(name, default)


# --- LLM providers (checked in this priority order) ---
GROQ_API_KEY = _env("GROQ_API_KEY")
GROQ_MODEL = _env("GROQ_MODEL", "llama-3.1-8b-instant")

GOOGLE_API_KEY = _env("GOOGLE_API_KEY")
GOOGLE_MODEL = _env("GOOGLE_MODEL", "gemini-1.5-flash")

OPENROUTER_API_KEY = _env("OPENROUTER_API_KEY")
OPENROUTER_MODEL = _env("OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free")

# --- Publishing target ---
GITHUB_TOKEN = _env("GITHUB_TOKEN")
GITHUB_USERNAME = _env("GITHUB_USERNAME")
GITHUB_REPO = _env("GITHUB_REPO")
GITHUB_BRANCH = _env("GITHUB_BRANCH", "main")
GITHUB_PAGES_FOLDER = _env("GITHUB_PAGES_FOLDER", "docs")

# --- Dashboard ---
DASHBOARD_PASSWORD = _env("DASHBOARD_PASSWORD")

# --- Storage ---
DB_PATH = _env("DB_PATH", "data/money_bots.db")

# --- Safety limits ---
# These only engage when a bot is behaving abnormally; raising them
# doesn't make a healthy bot faster.
MAX_PUBLISHES_PER_HOUR = int(_env("MAX_PUBLISHES_PER_HOUR", "4"))
FAILURE_THRESHOLD = int(_env("FAILURE_THRESHOLD", "5"))
FAILURE_WINDOW_MINUTES = int(_env("FAILURE_WINDOW_MINUTES", "30"))
HEARTBEAT_STALE_MINUTES = int(_env("HEARTBEAT_STALE_MINUTES", "15"))
WORKER_LOOP_INTERVAL_SECONDS = int(_env("WORKER_LOOP_INTERVAL_SECONDS", "240"))
