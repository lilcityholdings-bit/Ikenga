# Ikenga — Money Bots

An autonomous content system: bots research the web, decide what to do,
and publish real articles to a free GitHub Pages site. You control it from
a Streamlit dashboard.

## Two processes, not one

This is **two separate processes** — running only the web service means
the dashboard loads, Auto Mode flips ON, and nothing happens (no error, no
clue). The dashboard's health panel exists to catch exactly this.

| Process | Command | What it does |
|---|---|---|
| Web | `python run.py` (or `streamlit run dashboard/app.py`) | The dashboard you look at and click |
| Worker | `python worker.py` | The loop that actually runs the bots |

Both processes need the same environment variables. Deploy both as
separate services (e.g. two Railway services in one project) and give
each its own copy of the `.env` values.

## Setup

1. Copy `.env.example` to `.env` and fill it in:
   - At least one LLM key: `GROQ_API_KEY` (recommended free tier),
     `GOOGLE_API_KEY`, or `OPENROUTER_API_KEY`.
   - A **fine-grained** GitHub token scoped to only your public site repo,
     with Contents: Read and write — never a classic `repo`-scoped token,
     which would grant write access to every repo you own.
   - `DASHBOARD_PASSWORD` — required. The dashboard refuses to load
     without it.
2. In the target GitHub repo, enable Pages: **Settings → Pages → Deploy
   from a branch**, branch `main`, folder matching `GITHUB_PAGES_FOLDER`
   (default `docs`).
3. `pip install -r requirements.txt`
4. Run both processes locally:
   ```
   python run.py       # dashboard, in one terminal
   python worker.py    # bot loop, in another
   ```
5. Open the dashboard, log in, press **Start All Bots**, flip **Auto
   Mode** on.

## Bots

The default bot population lives in `agents/bot_template.py`. Edit that
list to change niches; new entries are seeded on next startup.

## What the code already handles

- **Published HTML is sanitized** (`core/security.py`) before it ever
  reaches the live site.
- **SSRF is blocked** — requests to private/loopback/link-local
  addresses are refused, including the cloud metadata endpoint
  (`169.254.169.254`).
- **Secrets are redacted** from generated content before it's stored or
  published.
- **Path traversal is blocked structurally** — every published file path
  is built from a slug of only `[a-z0-9-]`, so generated content can
  never write outside its own article folder.

## Known limitations

- **No revenue yet.** Bots publish genuine articles, but nothing inserts
  real affiliate links or tracks earnings — that needs your own affiliate
  account IDs after you manually join a program. Profit reads `$0.00`
  until then; that's expected.
- **Approved code proposals don't self-apply.** There's no engine that
  rewrites the bot's own files.
- **"Spending approval" moves no money.** There's no payment integration,
  so that gate is symbolic.
- **`retry_failed` behaves like `publish_article`** — it doesn't yet vary
  its approach on retry.
- **Account signup stays a human click.** Discovery is automated and
  grounded in real search results, but CAPTCHAs, verification, and
  identity/tax forms need a person, and most networks forbid bot-created
  accounts.
- **Prompt injection is unsolved.** The bot reads attacker-influenceable
  web pages and an LLM decides actions from them. Sanitizing stops it
  reaching your site; approval gates stop spending — but Auto Mode
  self-approves those gates. Watch the Recent Activity log.
- **SQLite is wiped on redeploy** on ephemeral disk (Railway/Render).
  Move `DB_PATH` to hosted Postgres once the system runs steadily.

## Tuning

Everything adjustable lives in `config/settings.py` (as env vars):
`MAX_PUBLISHES_PER_HOUR` (4), `FAILURE_THRESHOLD` (5),
`FAILURE_WINDOW_MINUTES` (30), `HEARTBEAT_STALE_MINUTES` (15),
`WORKER_LOOP_INTERVAL_SECONDS` (240). These limits only engage when a bot
behaves abnormally — raising them doesn't make a healthy bot faster.

## Troubleshooting

**Dashboard shows "Worker: 🔴 Not running" but Auto Mode is ON**
The worker process isn't running, or isn't sharing the same environment
variables as the web process. Start `python worker.py` as its own
service.

**"Publishing isn't set up yet" despite `.env` being right**
The GitHub variables didn't reach the worker process — set them on both
processes separately.

**Recent Activity shows `execution_skipped` repeatedly**
`GITHUB_TOKEN`, `GITHUB_USERNAME`, or `GITHUB_REPO` is missing/wrong, or
the bot is backing off after repeated failures, or the hourly publish cap
was hit.

**Recent Activity shows `execution_failed` repeatedly**
Usually the free LLM quota ran out. The bot backs off automatically after
`FAILURE_THRESHOLD` failures within `FAILURE_WINDOW_MINUTES` rather than
hammering it.

**Everything looks fine but nothing publishes**
Check bots are actually **Started**, not just that Auto Mode is on — both
gate the loop independently.
