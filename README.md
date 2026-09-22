# Revenue Bots

Autonomous bots that research a niche, write articles, and grow one site each.
You (or autopilot) decide what gets published. Selection breeds from whatever
actually gets accepted.

## Run it

    pip install -r requirements.txt
    cp .env.example .env      # add DASHBOARD_PASSWORD + one LLM key
    python run.py

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
