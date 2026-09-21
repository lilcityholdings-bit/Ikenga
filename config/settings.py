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

# --- Site identity ---
# CUSTOM_DOMAIN publishes a CNAME file so GitHub Pages serves from your own
# domain. SITE_BASE_URL overrides the absolute base used in canonical URLs,
# sitemap entries, and Open Graph tags — set it when hosting somewhere other
# than Pages (Netlify/Cloudflare/Vercel). Falls back to the github.io URL.
CUSTOM_DOMAIN = _env("CUSTOM_DOMAIN")
SITE_BASE_URL = _env("SITE_BASE_URL")

# --- Measurement ---
# ANALYTICS_SNIPPET is raw HTML injected into every page's <head> — use
# whatever provider you like (Plausible, GA4, Fathom, Cloudflare).
# SEARCH_CONSOLE_VERIFICATION is just the content value of Google's
# google-site-verification meta tag.
ANALYTICS_SNIPPET = _env("ANALYTICS_SNIPPET", "")
SEARCH_CONSOLE_VERIFICATION = _env("SEARCH_CONSOLE_VERIFICATION", "")

# --- Article images ---
# Sourced from Openverse, filtered to licenses allowing commercial use and
# modification, downloaded into the site repo, and credited on the page.
ENABLE_ARTICLE_IMAGES = _env("ENABLE_ARTICLE_IMAGES", "true").lower() == "true"

# --- Dashboard ---
DASHBOARD_PASSWORD = _env("DASHBOARD_PASSWORD")

# --- Storage ---
DB_PATH = _env("DB_PATH", "data/money_bots.db")

# --- Safety limits ---
# These only engage when a bot is behaving abnormally; raising them
# doesn't make a healthy bot faster. MAX_PUBLISHES_PER_HOUR defaults low
# on purpose: search engines' spam systems specifically target high-volume,
# unedited content from new sites, so a lower steady rate is better for
# actually getting indexed than a higher one.
MAX_PUBLISHES_PER_HOUR = int(_env("MAX_PUBLISHES_PER_HOUR", "2"))
FAILURE_THRESHOLD = int(_env("FAILURE_THRESHOLD", "5"))
FAILURE_WINDOW_MINUTES = int(_env("FAILURE_WINDOW_MINUTES", "30"))
HEARTBEAT_STALE_MINUTES = int(_env("HEARTBEAT_STALE_MINUTES", "15"))
WORKER_LOOP_INTERVAL_SECONDS = int(_env("WORKER_LOOP_INTERVAL_SECONDS", "240"))
