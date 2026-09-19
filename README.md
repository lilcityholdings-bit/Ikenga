# Ikenga

Human + AI agent trading platform. Greenfield, separate from any prior/legacy project.

## Run it

```bash
chmod +x ikenga seed_demo.py
./ikenga start     # builds, starts, prints your dashboard link and owner key
./ikenga demo      # optional: fills it with example markets
```

Step-by-step instructions, including how to do it from a phone with no computer, are in
**[RUN.md](RUN.md)**.


## What's actually implemented right now

This repo is a **working core slice**, not the full 23-section vision in one shot. Building the
whole spec (fiat rails, KYC/AML, real-money prediction markets, BTC treasury with HSM signing,
owner control center) is a multi-person, multi-month effort — see `docs/ROADMAP.md` for how it's
phased.

Implemented and running today:

- `services/matching-engine` — in-memory price/time-priority order book, agent order API,
  real Ed25519 request signing (verified via the system `openssl` binary — see `src/ed25519.rs`;
  the server only ever holds agents' public keys) with replay protection, trust-tiered rate
  limiting, a raw binary WebSocket market-data feed with sequence-numbered gap detection, and a
  protocol fee ledger (`GET /v1/treasury`). Zero external crates — hand-rolled HTTP/WebSocket/JSON
  and hash primitives, because this was built with no network access to crates.io; see the note
  at the top of `Cargo.toml`. Ed25519 is the one exception to "hand-rolled": that's real,
  audited crypto via a subprocess, not a from-scratch elliptic-curve implementation, at a real
  measured latency cost — see `docs/PERFORMANCE.md`.
- `examples/agent.py` and `bots/market-maker/market_maker.py` — reference clients with no
  dependencies beyond a stdlib Python and `openssl` on `PATH`. A TypeScript SDK is planned
  (see `docs/ROADMAP.md`) but doesn't exist in this repo yet — don't reference `sdk/ts`.
- Owner-only routes (`GET /v1/treasury`, `POST /dev/faucet/...`, `POST /v1/compliance/resolve`,
  `GET /v1/compliance/disclosures`) gated behind a generated `X-Owner-Key` — one shared secret,
  not the spec's real admin/role system.
- **Crash recovery** (`src/wal.rs`) — an append-only write-ahead log. Every balance move, fill,
  fee and order state change is written and fsynced before the client is told it happened, and
  replayed at startup. Verified by `crash_test.py`, which `kill -9`s a live server and checks
  that balances, fees, agent credentials and partially-filled resting orders all come back
  exactly. `GET /health` reports `durable: false` with a 503 if the log isn't active.
- **Pari-mutuel prediction markets** (`src/prediction.rs`) — stake points on an outcome; the pool
  splits among whoever was right. **No counterparty**, so a bet is valid with one participant or a
  thousand — which is why this can launch where the order book can't. The rake comes only from the
  losing pool, so a correct forecast never pays out less than it staked and a market where
  everyone agreed costs nobody anything. That rake is the revenue, collected without holding a
  position or putting up capital. See `docs/MARKETS.md`; `market_test.py` covers the money math,
  the abuse cases and crash recovery of live stakes end to end.
- **Resolution integrity** — every market's terms (question, outcomes, resolution source,
  threshold, close/observation times, dispute window) are hash-committed at creation and
  published; a market whose terms no longer match its commitment refuses stakes. Outcomes are
  *proposed*, not instantly paid, and any agent can dispute; machine-resolved markets settle from
  the router's cross-checked median, and **void rather than guess** when sources disagree. Built
  directly against the Polymarket failure mode where a bought vote paid out $7M on an event that
  never happened.
- **Redeemable credits with a hard promo barrier** (`src/credits.rs`) — free points can never be
  withdrawn by any route; only deposit-backed credits can. Reserve invariant published at
  `GET /v1/reserves`. Real money refuses to switch on without a stated licence reference.
  `credits_test.py` covers deposit → bet → win → cash out plus every barrier.
- **Self-service onboarding** — `POST /v1/agents` lets an agent register a public key it
  generated itself (self-signed, so it proves it holds the private half), and `GET /` describes
  the whole API surface without authentication. `GET /v1/route` also answers unauthenticated
  callers a limited number of times per hour, so someone can see whether the prices are any good
  before signing up. Before this existed a production deployment seeded no agents and had no way
  to create one — it could not onboard a single customer. `onboarding_test.py` walks the whole
  journey, including in production mode, and checks registrations survive a restart.
- **Non-custodial swap routing** (`src/router.rs`, `src/liquidity.rs`) — `GET /v1/route` prices a
  trade across external venues and returns the best route for the agent to settle **from its own
  wallet**. No deposit, no balance here, no capital needed to launch it. Cross-checks every quote
  against the median so a single manipulated feed can't win a route, drops stale prices, and
  refuses to answer when sources disagree rather than guessing. Fee is reported as gross/fee/net
  rather than folded into the rate. This is the mode that doesn't need liquidity to already
  exist — **read `docs/CUSTODY.md`**, which lays out how it differs from the order book and what
  it does and doesn't collect today.
- **Anonymous trading** (`src/privacy.rs`) — counterparties see a single-use alias per fill
  instead of each other's agent IDs, order/trade IDs are random rather than sequential, and
  de-anonymization is owner-only, requires a stated reason, and is permanently audit-logged.
  `GET /v1/privacy` publishes the posture, including what it can't hide (the operator sees
  everything; IPs are visible; timing correlation still works). Read `src/privacy.rs`'s threat
  model before relying on any of it.

Stubbed with clear interfaces, not implemented (see `docs/ROADMAP.md` + `docs/COMPLIANCE-NOTES.md`
for why these aren't just "more code"):

- Fiat on/off ramp adapters
- KYC/AML/sanctions pipeline
- Real-money prediction markets ("Agent Conviction Markets")
- BTC treasury conversion + HSM/multisig signing (the fee *ledger* exists; conversion + transfer
  don't)
- Owner Control Center dashboard (the owner-key gate is a first step, not the dashboard)
- Postgres settlement layer for history and reporting (durability itself is now handled by the
  write-ahead log below — Postgres is for querying, which nothing needs yet)

## Running it

```bash
cd services/matching-engine
cargo run
```

Server listens on `:8080`, prints two demo agents' private key seeds AND an owner key on startup
(only the derived public keys are ever registered/stored — see `src/ed25519.rs`), and seeds each
agent with 100,000 USD / 5 BTC in-memory. See `docs/ARCHITECTURE.md` for the API surface.

An end-to-end smoke test (`services/matching-engine/smoke_test.py`, stdlib-only Python) exercises
signed order submission, matching, protocol fees, owner-key access control, replay protection,
quotes, accounts, and the raw binary WebSocket feed (including its sequence numbers) against a
running server:

```bash
cargo run --release &
SEED_A=<from startup output> SEED_B=<from startup output> OWNER_KEY=<from startup output> \
  python3 smoke_test.py
```

Three more suites start their own servers, so they need no setup beyond a release build:

```bash
cargo build --release
python3 onboarding_test.py   # the customer journey, including onboarding in production mode
python3 market_test.py       # prediction markets: payouts, rake, resolution integrity, crash recovery
python3 credits_test.py      # deposits, cash out, reserve invariant, the promo/real-money barrier
python3 route_test.py        # non-custodial routing: pricing, refusals, trial access
python3 crash_test.py        # kill -9 a live server, prove nothing was lost
```

There's also `bots/market-maker/market_maker.py` — a reference market maker that posts real
two-sided liquidity with real risk controls (position cap, inventory skew, loss limit, halt on a
reference-price jump, cancel-all on shutdown). See `bots/market-maker/README.md` for what it is
and, more importantly, what it isn't.

## Docs

- `docs/MARKETS.md` — how the prediction markets work, how the rake earns, why there is no
  liquidity requirement, how the forecast feed is sold, and why real money
  needs a licence before it needs code.
- `docs/CUSTODY.md` — the two modes (custodial order book vs non-custodial router), which
  endpoints are which, how each one earns, and what's verified versus assumed. Read this before
  describing the platform to anyone as custodial or non-custodial.
- `docs/ARCHITECTURE.md` — full technical spec, cleaned up, with corrections noted inline.
- `bots/market-maker/` — reference market maker: posts real two-sided liquidity so the book is
  never empty. Read its README on why faking volume is both blocked and pointless here.
- `docs/PERFORMANCE.md` — measured throughput/latency (in-process matching: 0.66-3.67µs/op; the
  real ceiling is Ed25519 verification via `openssl` subprocess at ~4,823/sec), what actually
  gates each path, and why an earlier "32.5k orders/sec" claim didn't hold up.
- `docs/ROADMAP.md` — phased build order from this core slice to the full vision.
- `docs/COMPLIANCE-NOTES.md` — the regulatory gating items (fiat, custody, prediction markets)
  that need legal sign-off before any real money flows, referenced once here rather than repeated
  throughout the code.
- `DEPLOY.md` — the Dockerfile and `docker-compose.yml`, both actually built and run this round
  (not just written): what env vars matter, what production mode requires, and two real bugs that
  only surfaced from running the deploy path rather than reading the code.
