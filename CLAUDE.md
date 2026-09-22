# Revenue Bots — working notes for Claude Code

Autonomous bots research a niche, write articles, and grow one site each,
published to GitHub Pages. The owner (or an automated critic) decides what
goes live. Bots that produce accepted work breed; the rest are culled.

## Commands

    python tests/run_all.py    # 47 checks, fully offline
    python run.py              # worker + dashboard on $PORT (default 8501)

## Rules

- **Run the tests before and after every change.** They mock only the network;
  all real logic executes. Never weaken an assertion to get green.
- **Never make live network calls in tests.** `tests/fake_net.py` fakes the
  LLM, GitHub and search. `tests/fake_streamlit.py` is a headless Streamlit
  that can click buttons by name. Extend those.
- **Model output and scraped text are hostile.** Everything reaching a page
  goes through `core/safe_html.py`. The critic's stage 1 is pure regex so
  injection is caught before a token is spent. Don't relax either.
- Standard library first. Deps: streamlit, requests, beautifulsoup4, lxml,
  python-dotenv. Nothing else without asking.
- Tunables live in `config/settings.py`, env-overridable. No new config files.
- Comments say *why*, especially for non-obvious choices.

## Invariants — each one exists because removing it broke something

- `MAX_BOTS` is a hard cap, never unlimited.
- New bots get 48h grace; a bot needs >= 3 reviewed articles before it can be
  culled. Culling on fewer made the population degrade instead of improve.
- The current leader is never culled — one bad window would lose the best
  genome for good.
- Acceptance rate is lifetime; output volume is recent-window only. Lifetime
  volume gave incumbents an uncatchable lead.
- 12-25% of new bots get a fresh random genome, or the gene pool collapses.
- Repetition uses the sliding-window `_mattr`, not unique/total — a raw ratio
  falls with length and silently rejected longer articles.
- One site per bot. Per-article sites destroy the SEO value of the work.
- A rejection removes the page from the site, not just a database row.
- `run_once()` takes a file lock; dashboard and worker are separate processes.

## Measured behaviour (from tests/run_all.py)

- At the default 3 bots, about one new genome is tried per ranking cycle, so
  selection is slow and noisy — the elite landed between 68% and 97% of
  optimum across seeds. At 6 bots it converges faster and higher. The test
  asserts the elite never degrades and new genomes keep being tried; it
  deliberately does not claim selection beats exhaustive random search at this
  population size, because it does not.
- The critic's approval counts toward fitness even when publishing is
  unavailable. Without that, a user with no GitHub set up had one bot that
  could never become "proven", so nothing bred and evolution froze forever.

## Known limits — don't silently "fix" these

- DuckDuckGo blocks datacenter IPs, so hosted research is thin. A real fix
  needs a paid search API — a cost decision, ask first.
- Revenue is logged by hand; until then fitness optimises prose, not money.
- Writer and critic are the same model and share blind spots.
- Affiliate programs usually reject `github.io` URLs.
