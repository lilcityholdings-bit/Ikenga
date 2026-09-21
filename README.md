# Ikenga — Money Bots

An autonomous content system: bots research the web, decide what to do,
and publish real articles to a free GitHub Pages site. It also includes
an optional crypto trading module that places real orders with real
money — read that section carefully before enabling it. You control it
all from a Streamlit dashboard.

## Crypto trading — read this before enabling

`core/trading/` places real market orders on a real exchange, with real
money, once you set `trading_enabled` in the dashboard. Understand what
it actually is before turning it on:

**What it is.** A moving-average crossover strategy (SMA-10 crosses
SMA-30) computed from real price data — a simple, well-known, and
*deterministic* momentum signal. It is not an LLM deciding trades.
That's deliberate: a model that can hallucinate reasoning has no
business sizing an order. The LLM stack elsewhere in this repo never
touches the trading path at all.

**What it is not.** A profitable strategy, verified or otherwise. SMA
crossover is a textbook starting point, not an edge. Nothing about this
system implies it makes money — the same honesty that applies to the
content/affiliate side applies here, more so, because losses here are
real and immediate rather than just wasted time.

**The three hard limits, all enforced in code regardless of what the
strategy signals** (`core/trading/risk.py`):

1. `TRADING_MAX_POSITION_USD` — a hard ceiling per order. The strategy
   never gets to size its own trade.
2. `TRADING_MAX_OPEN_POSITIONS` — a hard ceiling on how many pairs can be
   held at once.
3. **A daily circuit breaker.** Before every tick, the bot checks the
   current value of the pairs and cash it manages against that day's
   starting value. If the loss exceeds `TRADING_DAILY_LOSS_LIMIT_USD`,
   trading halts completely — no more orders — until you manually clear
   it from the dashboard. It does **not** auto-resume if the price
   recovers; that's intentional, so a volatile day can't quietly flip the
   breaker on and off while you're not watching.

**Defaults are conservative on purpose** (`$25`/trade, `$50`/day, 2 open
positions) because I have no visibility into your risk tolerance or how
much capital you're using. Review and adjust them — via env vars, not in
the dashboard — before enabling.

**Before you set `CRYPTO_API_KEY`/`CRYPTO_API_SECRET`:** on whatever
exchange you use, create a key scoped to **trade + read only**. Do **not**
enable withdrawal permission on it. This is the single most important
setting here — if that key ever leaks (a misconfigured env var, a
compromised host, a bug), a trade-only key limits the damage to bad
trades inside the account, which is recoverable. A withdrawal-capable key
can empty the account outright.

### Two trading modes — pick exactly one

Set `TRADING_MODE`. These are mutually exclusive against the same
account — running both means the same balance could get traded twice.
`worker.py` automatically refuses to run its own trading tick when
`TRADING_MODE=realtime`, specifically to prevent that.

| | `poll` (default) | `realtime` |
|---|---|---|
| Process | `worker.py` (shared with content bots) | `trading_stream.py` (its own process) |
| How it notices price moves | Checks on a timer, every `TRADING_LOOP_INTERVAL_SECONDS` (15 min default) | A live websocket connection — notified the instant a price updates, no polling delay |
| Candle size | `TRADING_TIMEFRAME` (1h default) | `REALTIME_CANDLE_INTERVAL_SECONDS` (60s default) |
| Connection | None held open | Persistent, with automatic reconnect + backoff on drops |

**What "real time" actually means here, precisely**, because it's easy to
overstate: the moving-average strategy only ever produces a new signal
when a candle closes — that's what a moving average is, there's nothing
new to compute between candles. So `realtime` mode doesn't make the
*strategy* react to every single price tick (that would be meaningless
for an SMA signal); it makes the bot (a) use a much shorter candle — a
minute instead of an hour — and (b) notice the instant that candle
closes instead of finding out up to 15 minutes late. Both together are
what "real time" buys you.

**Coinbase-specific note:** Coinbase's websocket API doesn't stream
candles or account balances directly (`watchOHLCV` and `watchBalance`
are both unsupported there in ccxt). So `realtime` mode streams live
ticker price and builds its own candles from that price stream locally
(`core/trading/realtime.py`'s `CandleBuilder`), and reads balances via a
plain (fast) API call at signal time rather than a stream. This is
normal practice when an exchange doesn't offer streaming candles, not a
workaround for a bug.

**Running `realtime` mode:** deploy `trading_stream.py` as its own
service (e.g. a third Railway service, alongside the dashboard and
worker), with the same environment variables plus `TRADING_MODE=realtime`
on that service specifically. It holds a websocket connection for as
long as it runs — if it dies (crash, redeploy, host restart), no trades
happen until it's back up; the dashboard's trading panel shows whether
the stream's heartbeat is current or stale.

**Testing note:** this was verified with extensive mocked tests — the SMA
math, the risk engine's clamping/circuit-breaker/position-cap logic, both
controllers' buy/sell/error-isolation paths, the realtime candle builder,
and the realtime engine's reconnect-with-backoff behavior all pass — but
never against a live exchange connection with real credentials, because I
don't have any. Start with the smallest `TRADING_MAX_POSITION_USD` you're
comfortable losing entirely, watch the dashboard's trade log for at least
a few real cycles, and only raise the limits once you trust what you're
seeing.

## Hosting

### Why GitHub Pages restricts this

GitHub Pages is free static hosting bundled with a code host. The
restriction on running a business off it comes down to three things:

1. **Cost-shifting.** Pages serves generous bandwidth for free, on free
   accounts, with GitHub absorbing the CDN bill. That works when Pages is
   used for what it's for — project docs, portfolios, open-source sites.
   It stops working if it becomes free production hosting for businesses
   that would otherwise pay someone for it.
2. **It's a developer tool, not a hosting company.** Pages exists to
   publish docs next to your code. GitHub doesn't want to own uptime,
   payment flows, or commercial disputes.
3. **Spam and domain reputation — the real driver.** `github.io` has
   enormous accumulated domain authority, and free instant static hosting
   on a high-authority domain is exactly what SEO spam, affiliate spam
   networks, and scam sites want. The "get-rich-quick schemes" clause is
   an anti-abuse provision.

Worth understanding point 3 clearly: the rule isn't aimed at developers
making money. It's aimed at people renting GitHub's domain reputation to
rank spam. An automated affiliate site has legitimate intent but is
*architecturally identical* to the abuse pattern, which is why it lands
on the wrong side of the line.

### How to run it anyway — serve the site off Pages

**The code barely changes.** Cloudflare Pages and Netlify both deploy
*from a GitHub repo*, which is exactly what this publisher already writes
to. So the flow becomes:

```
bot --(GitHub Contents API)--> public site repo --(auto-deploy)--> Cloudflare Pages --> your domain
```

GitHub goes back to being what it's for — storing source. Nothing in
GitHub's terms restricts *keeping the source of an affiliate site in a
repo*; the restriction is on Pages serving it. Pages just gets switched
off.

**Recommended hosts** (both allow commercial/affiliate content on free
tiers):

| Host | Free tier | Notes |
|---|---|---|
| **Cloudflare Pages** | Unlimited bandwidth, 500 builds/mo | Best fit |
| **Netlify** | 100 GB/mo bandwidth | Also fine |

**Avoid Vercel's free tier for this.** Vercel's Hobby plan is
non-commercial only and names this case explicitly: *"Affiliate linking
is the primary purpose of the site"* is listed as commercial usage
requiring a paid plan. It's a worse fit than Pages, not a better one.

### Setup

1. In the **public site repo**: Settings → Pages → set source to **None**
   (stop Pages serving it).
2. Cloudflare Pages → Create project → connect that repo. Build command:
   none. Output directory: whatever `GITHUB_PAGES_FOLDER` is (`docs`).
3. Add your domain in Cloudflare Pages (or use the free
   `*.pages.dev` subdomain to start).
4. Set `SITE_BASE_URL` to the live URL on **both** services, e.g.
   `SITE_BASE_URL=https://yoursite.com`. Everything — canonicals,
   sitemap, Open Graph, IndexNow — follows it automatically.
5. Leave `CUSTOM_DOMAIN` unset unless you're still on GitHub Pages; it
   only writes the Pages-specific `CNAME` file.

Every bot commit to the repo triggers a Cloudflare deploy. No code change
required.

### About the domain

A domain you own is worth the ~$10/yr regardless of host: the authority
you build accrues to *you*, and you can change hosts later without losing
it. On a shared subdomain you're building someone else's asset.

## Two processes, not one (three with realtime trading)

This is **two separate processes** at minimum — running only the web
service means the dashboard loads, Auto Mode flips ON, and nothing
happens (no error, no clue). The dashboard's health panel exists to catch
exactly this.

| Process | Command | What it does |
|---|---|---|
| Web | `python run.py` (or `streamlit run dashboard/app.py`) | The dashboard you look at and click |
| Worker | `python worker.py` | The loop that actually runs the content bots, and the crypto trading tick if `TRADING_MODE=poll` |
| Trading stream (optional) | `python trading_stream.py` | Only if `TRADING_MODE=realtime` — a persistent websocket connection for the crypto trading module. Don't run this alongside a worker also doing polling-mode trading against the same account. |

All processes need the same environment variables. Deploy each as a
separate service (e.g. two or three Railway services in one project) and
give each its own copy of the `.env` values.

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
- **SSRF is blocked**, including redirect-based bypasses — requests to
  private/loopback/link-local addresses are refused (including the cloud
  metadata endpoint `169.254.169.254`), and redirects are followed
  manually with the same check re-applied on every hop rather than
  trusting the first URL alone.
- **Secrets are redacted** from generated content and from exception text
  before either reaches logs, bot memory, or the dashboard.
- **Path traversal is blocked structurally** — every published file path
  is built from a slug of only `[a-z0-9-]`, so generated content can
  never write outside its own article folder.
- **Thin/malformed articles are never published.** A bad or too-short LLM
  response fails the cycle instead of shipping a one-sentence stub as a
  real page — search engines specifically penalize exactly that pattern.
- **Topics are grounded in real search queries**, not LLM invention
  (`core/keywords.py`, via DuckDuckGo's free autocomplete) — long-tail,
  specific queries are the realistic path for a brand-new site with no
  domain authority to rank for anything at all.
- **Affiliate links actually reach articles.** Once you mark a program
  `active` with its real tracking link (dashboard → Affiliate Programs),
  the content generator can weave it into new articles, and the publisher
  force-adds `rel="sponsored nofollow noopener"` on any link that points
  at one of your affiliate URLs regardless of what the LLM produced.
- **An FTC disclosure is on every article automatically** — not
  LLM-dependent, added by the publisher itself.
- **Basic on-page SEO**: meta description, canonical URL, Open Graph
  tags, and JSON-LD `Article` structured data on every page; a
  `sitemap.xml` regenerated on every publish; a `robots.txt` pointing at
  it; a static `about.html` disclosing that content is AI-assisted.
- **FAQ sections with `FAQPage` structured data.** Each article gets 3–5
  generated Q&A pairs, rendered visibly on the page *and* marked up as
  JSON-LD. (Google restricted FAQ rich results to authoritative
  health/government sites in 2023, so don't expect the rich snippet —
  the value is answering real question-shaped queries and being
  extractable by featured snippets and AI answer engines.)
- **Article images from Openverse** (`core/images.py`), filtered to
  licenses permitting commercial use and modification, downloaded into
  the repo rather than hotlinked, credited on-page as the license
  requires, and wired into `og:image` and the `Article` schema. Set
  `ENABLE_ARTICLE_IMAGES=false` to turn off.
- **IndexNow** pushes each new URL to Bing/Yandex immediately instead of
  waiting for organic re-crawl (`core/indexnow.py`). Google doesn't
  participate in IndexNow — for Google, the sitemap + robots.txt above is
  what discovery relies on.

## Will this actually make money?

**Realistically, on its own: no — or not enough to matter.** The code is
now mechanically sound, but the remaining blockers are not code problems
and cannot be fixed by changing this repo.

**The arithmetic.** Affiliate revenue is a funnel, and every stage
multiplies down:

| Stage | Typical rate |
|---|---|
| Visitors who click an affiliate link | ~2–5% |
| Clicks that convert to a sale | ~2–5% |
| Commission per sale | ~$1–30 |

So roughly **1,000 organic visits/month ≈ 1 sale ≈ $5–20/month.** Earning
even $500/month needs tens of thousands of monthly visits. At a realistic
20–50 visits/month for a long-tail article that actually ranks, that's
*hundreds* of ranking articles — and "ranking" is the hard part, not
"published."

**Why traffic is the wall:**

- **No backlinks, no authority, no history.** Nothing in this repo creates
  those. Good on-page SEO makes a site *eligible* to rank; links and
  reputation are what actually rank it.
- **Scaled content generation is explicitly targeted by search spam
  policy.** Mass-produced pages made primarily to rank are penalized
  regardless of whether a human or an AI wrote them. Volume is a
  liability here, not an asset — which is why the publish cap defaults low.
- **Realistic timeline is 6–12+ months** before you could even judge
  whether it's working, assuming it works at all.
- **Affiliate approval is not guaranteed.** Most networks reject sites with
  no traffic. Amazon Associates in particular closes accounts that don't
  make 3 qualifying sales within 180 days.

**What would actually be required** (mostly not code):

1. A domain you own, on a host that permits monetization (see the warning
   at the top of this file).
2. Genuine differentiation — first-hand testing, original data, real
   expertise — something a reader can't get from the ten existing articles
   on the same query.
3. A distribution channel that isn't organic search, built by a human.
4. Months of consistent effort before any signal.

Steps 2 and 3 are the ones that decide the outcome, and neither is
automatable — step 3 especially, since automated posting to social
platforms violates their terms and gets accounts banned.

**Honest framing:** this is a well-built piece of automation and a good
way to learn the mechanics of content pipelines, SEO plumbing, and
autonomous agents. It is not a reliable income source, and no further
change to this codebase would make it one.

## Other known limitations

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
`MAX_PUBLISHES_PER_HOUR` (2), `FAILURE_THRESHOLD` (5),
`FAILURE_WINDOW_MINUTES` (30), `HEARTBEAT_STALE_MINUTES` (15),
`WORKER_LOOP_INTERVAL_SECONDS` (240). `MAX_PUBLISHES_PER_HOUR` defaults low
on purpose — search engines' spam systems specifically target high-volume
unedited content from new sites, so a lower steady rate is better for
getting indexed than a higher one. The others only engage when a bot
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
