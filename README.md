# Revenue Bots

Autonomous bots that research a niche, write articles, and grow one site each.
You (or autopilot) decide what gets published. Selection breeds from whatever
actually gets accepted.

## Run it

    pip install -r requirements.txt
    python run.py

Opens a Setup screen on first load — password, AI key, and GitHub token all
get pasted in there, not into a `.env` file. See `INSTALL.md` for the full
phone-only walkthrough.

Deploy: one Railway service, start command `python run.py`, add a volume at
`/data` and set `DB_PATH=/data/bots.db` so redeploys don't wipe your history.

## How it works

Each cycle, every running bot researches its niche, writes one article shaped
by its **genome** (angle, audience, structure, opening), and adds that article
to its own single growing site.

Articles land in the review queue. You publish or reject them — or turn on
**autopilot**, which scores each draft 0-100 against a fixed rubric and
publishes above 72, rejects below 45, and escalates the rest to you.

**Fitness** is your verdicts, autopilot's verdicts at 35% weight, and real
revenue at 50x. Every ranking cycle the worst genuine underperformer is culled
and proven bots breed children with mutated genomes. New bots get 48 hours
grace so the system never kills its own offspring.

## Tuning

Default is 3 bots, which tries roughly one new writing style per ranking
cycle — slow and noisy. Across test seeds the best style found landed between
68% and 97% of the achievable optimum. Six bots converges faster and more
reliably, but every bot publishes to the same domain, so give each one a
**distinct niche** before raising `MAX_BOTS`. Several near-identical sites on
one domain is the doorway-page pattern search engines penalise.

## Files

    config/settings.py   every tunable value
    core/db.py           schema + all queries
    core/genome.py       heritable writing traits
    core/fitness.py      scoring, culling, breeding rules
    core/critic.py       autopilot's reviewer (regex stage + LLM rubric)
    core/autopilot.py    unattended review loop
    core/site.py         one growing site per bot
    core/publisher.py    GitHub Pages publish + takedown
    core/controller.py   owner controls + selection cycle
    core/worker.py       the loop
    agents/bot.py        one bot, one cycle
    dashboard/app.py     control panel
    core/trading/        crypto trading — separate subsystem, see below

## Trading

`core/trading/` is a second, unrelated subsystem in this repo: it places
real market orders with real money on a crypto exchange. It shares the
dashboard and the database file, nothing else — no content, no genomes, no
fitness.

**Strategy is deterministic, not LLM-decided.** A moving-average crossover
(SMA-10 vs SMA-30) on real price data — simple, well-known, and reproducible
given the same prices. A model that can hallucinate reasoning has no
business sizing a real order, so the LLM stack that writes articles never
touches this path.

**Three hard limits, enforced in code regardless of what the strategy
signals** (`core/trading/risk.py`):

1. `TRADING_MAX_POSITION_USD` — a per-order ceiling.
2. `TRADING_MAX_OPEN_POSITIONS` — a ceiling on pairs held at once.
3. A **daily circuit breaker** — if the pairs/cash this bot manages drop
   past `TRADING_DAILY_LOSS_LIMIT_USD` from that day's starting value,
   trading halts completely until manually cleared from the dashboard. It
   does not auto-resume on recovery — that's intentional.

Defaults (`$25`/trade, `$50`/day, 2 open positions) are conservative on
purpose; there's no way to know your risk tolerance or capital from here.

**Two modes**, set via `TRADING_MODE`, mutually exclusive against the same
account:

- `poll` (default) — the main worker checks prices on a timer
  (`TRADING_LOOP_INTERVAL_SECONDS`, 15 min default).
- `realtime` — a separate process (`python trading_stream.py`) holds a live
  websocket connection and reacts the instant a short candle
  (`REALTIME_CANDLE_INTERVAL_SECONDS`, 60s default) closes, instead of
  waiting on a timer. Deploy it as a third service alongside the worker and
  dashboard. The worker refuses to run its own trading tick while
  `TRADING_MODE=realtime`, so the same account can never be traded by both
  at once.

**Credentials**: `CRYPTO_API_KEY` / `CRYPTO_API_SECRET` follow the same
pattern as `GITHUB_TOKEN` — set them as env vars, or paste them in via
`core.db.set_secret` (a dashboard field for this can be added the same way
Setup handles the GitHub token). Whatever exchange you use, scope the key
to **trade + read only** — never withdrawal. If it ever leaks, a trade-only
key limits the damage to bad trades inside the account; a withdrawal-capable
key can empty it outright.

This was verified with mocked exchange tests (SMA crossover math, risk
clamping, circuit-breaker behavior, buy/sell/error-isolation, the realtime
candle builder and reconnect-with-backoff) but never against a live
exchange connection with real credentials. Start with the smallest
`TRADING_MAX_POSITION_USD` you're fully comfortable losing.
