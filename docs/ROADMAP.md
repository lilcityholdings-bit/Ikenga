# Roadmap: from this core slice to something that can take a real first trade

This reconciles two things that used to disagree: an earlier strategy review (Sep 14) that said
"nothing here persists, there's no hosting, and no market maker exists," and the actual code in
this repo, which — as of this round — has a real write-ahead log (verified surviving `kill -9`)
and a real, risk-controlled reference market maker (`bots/market-maker/`, also verified live).
The disagreement wasn't a lie in either direction; it was two people working on this in separate,
disconnected sessions with no shared repo, each unaware of the other's progress. That's the
reason this file — and getting this whole project into one durable git history — exists at all.

## Status, honestly, right now

**Done and verified this round or earlier, not just claimed:**
- Matching engine: price/time priority, self-trade prevention, 236 unit tests, crash-recovery
  tested with real `kill -9`.
- Durability: write-ahead log, `fsync`-by-default. **Not missing** — see `docs/CUSTODY.md` and
  `docs/PERFORMANCE.md`.
- Fee schedule matching the stated "thin margin, high volume" model: 7bps base taker, maker
  rebate, published unauthenticated at `GET /v1/fees`.
- Non-custodial router (`GET /v1/route`), now verified against **live** external venues
  (Coinbase/Kraken/Gemini), not just a local mock — see `docs/CUSTODY.md`.
- Reference market maker with real risk controls (position cap, inventory skew, mark-to-market
  loss limit, halt-on-price-jump, cancel-all-on-shutdown), verified live against a running
  server this round.
- Self-service onboarding: `POST /v1/agents`, unauthenticated reads of the board/spec/fees.

**Still genuinely missing:**
- A domain, TLS, and an always-on host. Everything above runs; nothing is reachable by anyone
  outside a machine someone is sitting at. This is the actual remaining blocker, not persistence.
- `sdk/ts` — referenced in `README.md`, not present in this repo. `examples/agent.py` and
  `bots/market-maker/market_maker.py` are the working Python references in the meantime.
- Fiat rails, KYC/AML, real-money prediction markets, BTC treasury conversion, a real
  multi-operator control center — all gated behind legal/ops work, not just code. See
  `docs/COMPLIANCE-NOTES.md`.

## Phased build order

### Phase 1 — Put it on the internet
Domain, TLS (a Cloudflare tunnel or Caddy in front of the port — see `RUN.md`), and a host that
doesn't erase itself on close. This blocks everything below it that involves a second, real user.

### Phase 2 — Never show an empty book
The router (`GET /v1/route`) already inverts the classic cold-start problem: an agent can get a
good fill routed to an external venue before this venue has any liquidity of its own. Pairing it
more tightly with the order book — falling through to a route when the local book can't fill — is
the highest-leverage remaining integration work, and most of both pieces already exist.

### Phase 3 — Five-minute onboarding
`POST /v1/agents` and the unauthenticated reads already remove the friction that matters most
(no deposit, no KYC, no wallet before an agent can see whether the prices are any good). What's
left: a real `sdk/ts` alongside the Python examples, and a single quickstart page that walks
straight through to a first signed order.

### Phase 4 — Run the market maker for real
`bots/market-maker/` is built and verified against a dev server. Before real capital: replace its
`IKENGA_MM_REF_PRICE` fallback with a reference feed you actually trust, size the position cap
and loss limit from real fill data instead of the current conservative defaults, and read its
README's "what it is not" section again.

### Phase 5 — Go where agent developers already are
Publish the checkable things — the fee table, `docs/PERFORMANCE.md`'s real numbers, the
self-trade-prevention tests, the privacy posture — rather than a landing page. Programmatic
traders respond to a benchmark, not a pitch; this repo can already produce the benchmark.

### Phase 6 — Revenue that isn't per-trade
Data subscriptions on `forecast.rs`'s output, priority infrastructure, eventually listing fees.
All of these need real trading volume to sell access *to* — building them before Phase 1-2 land
is a toll booth on a road nobody's on yet.

## What deliberately isn't in this phase list

Fiat on/off ramps, KYC/AML, real-money prediction markets, BTC treasury conversion, and a real
multi-operator control center are all cut from this build order on purpose, not forgotten — see
`docs/COMPLIANCE-NOTES.md` for why each one gates on something other than more code.
