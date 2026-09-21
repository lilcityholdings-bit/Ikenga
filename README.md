# Ikenga — Money Bots

An autonomous content system: bots research the web, decide what to do,
and publish real articles to a free GitHub Pages site. You control it from
a Streamlit dashboard.

## Hosting

Publishing works against any static host that serves from a git repo.
GitHub Pages is the default; Netlify, Cloudflare Pages, and Vercel all
work the same way and all have free tiers.

Set `CUSTOM_DOMAIN` to publish a `CNAME` file and serve from a domain you
own, and/or `SITE_BASE_URL` to control the absolute URLs used in
canonicals, sitemap, and Open Graph tags. A domain you own is worth the
~$10/yr — the authority you build accrues to you instead of to a
subfolder on `github.io`, and you can move hosts later without losing it.

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
