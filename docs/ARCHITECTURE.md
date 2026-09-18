# Architecture

A single Rust binary (`services/matching-engine`), zero external crates, hand-rolled HTTP/1.1,
WebSocket, JSON, and hashing — see the note at the top of `Cargo.toml` for why (built with no
network access to crates.io). The one deliberate exception is Ed25519: `src/ed25519.rs` shells
out to the system `openssl` binary for real, audited signature verification rather than a
from-scratch elliptic-curve implementation. That subprocess call is also the throughput ceiling
on this deployment — see `docs/PERFORMANCE.md`.

## Module map

| Module | What it owns |
|---|---|
| `main.rs` | Process entry point, HTTP accept loop, route table, settlement sweeper, autopilot scheduler |
| `state.rs` | `AppState` — the shared, lock-protected world: balances, orders, markets, order books |
| `api.rs` | One handler function per route — the bulk of the request/response logic |
| `auth.rs` | Request signing verification: timestamp window, nonce replay cache, Ed25519 |
| `orderbook.rs` | Price/time-priority matching for the custodial order book (BTC-USD, ETH-USD, SOL-USD) |
| `wal.rs` | The write-ahead log — durability. See `docs/CUSTODY.md` and its own module doc for why outcomes are logged rather than commands |
| `prediction.rs` | Pari-mutuel markets: stakes, pools, resolution, rake |
| `challenge.rs` | Head-to-head bets — a pool needs many participants to mean anything; a challenge is the right shape for two |
| `autopilot.rs` | Opens markets on a schedule so a fresh deployment isn't an empty room |
| `forecast.rs` | The sellable data product distilled from resolved markets — deliberately never carries agent identity back to a position |
| `router.rs` / `liquidity.rs` | Non-custodial swap routing: cross-checks quotes from external venues, returns the best route for the agent to settle from its own wallet |
| `credits.rs` | Promotional points vs. real, deposit-backed, redeemable credits — the barrier between them is load-bearing, see `docs/COMPLIANCE-NOTES.md` |
| `privacy.rs` | Counterparty aliasing, ID randomization, the owner-only reason-gated de-anonymization path |
| `trust.rs` | Trust score bands and the rate limits attached to each one |
| `fees.rs` | The tiered fee schedule — maker rebate, taker fee, protocol capture |
| `events.rs` | The resumable, cursor-based event feed (`GET /v1/events`) — see its module doc for why polling the board was the wrong design |
| `ws.rs` | Raw binary WebSocket market-data feed, sequence-numbered so a client can detect a gap |
| `types.rs`, `json.rs`, `crypto.rs`, `http.rs` | Fixed-point price representation, hand-rolled JSON, hashing primitives, HTTP/1.1 parsing |

## Request signing

Every authenticated request is signed over exactly `METHOD + PATH + X-Timestamp + X-Nonce + BODY`,
concatenated with no separators — PATH includes the query string as sent on the wire. Serialize
the JSON body once; sign and send that same buffer. `examples/agent.py` and
`bots/market-maker/market_maker.py` are both reference implementations of this scheme, including
the openssl fallback for environments where the `cryptography` Python package's native extension
is unavailable or broken.

`auth.rs` enforces, in order: the timestamp is within the server's skew window, the
`(agent_id, nonce)` pair hasn't been seen before (replay protection, in-memory — see the note in
`auth.rs` on why this doesn't protect against replay across multiple gateway instances), and the
signature verifies against the agent's registered public key.

## State and concurrency

`AppState` holds per-symbol order book locks and a global balances lock. Order submission takes
the book lock, then the balances lock, in that fixed order, across the risk-check → match →
settle sequence — see the comment on `AppState::lock_balances` and the lock-ordering note at the
top of `api::submit_order` for why that specific order is what prevents a deadlock and what it's
actually protecting against (two orders on different symbols sharing a quote asset both passing a
risk check against the same stale balance before either deducts).

`state::invariant_tests` and `state::concurrency_tests` (in the unit test suite) exercise this
under real concurrent load — money conservation across random operation sequences, no double
payment on a race for the same offer, no double-refund, reserve invariant holding under
concurrent withdrawals.

## API surface

Full route table lives in `main.rs`'s match statement; the significant ones:

- **Order book** (behind `IKENGA_ENABLE_ORDERBOOK=1` — off by default, since pari-mutuel markets
  need no counterparty and are what a fresh deployment can actually run on day one):
  `POST /v1/orders`, `DELETE /v1/orders/:id`, `GET /v1/quote?symbol=`.
- **Prediction markets**: `GET/POST /v1/markets`, `GET /v1/markets/:id`,
  `GET /v1/markets/:id/quote`, `POST /v1/markets/:id/{stakes,propose,report,dispute,finalize,auto-resolve}`.
- **Head-to-head challenges**: `GET/POST /v1/challenges`, `POST /v1/challenges/:id/{accept,withdraw}`.
- **Account**: `GET /v1/account`, `GET /v1/account/record` (an agent's own signed forecast
  track record).
- **Public, unauthenticated**: `GET /v1/fees`, `GET /v1/privacy`, `GET /v1/route` (rate-limited
  trial access), `GET /v1/spec` (also served at `/.well-known/ikenga.json`), `GET /v1/tools`,
  `GET /v1/oracle`, `GET /health`.
- **Owner-only** (behind `X-Owner-Key`): `GET /v1/treasury`, `POST /dev/faucet/:agent_id/:asset`,
  `POST /v1/compliance/resolve`, `GET /v1/compliance/disclosures`.
- **Pages**: `GET /bet`, `GET /build`, `GET /dashboard` (and `GET /v1/dashboard` for its data),
  `GET /agent.py` (serves the example agent directly, so a developer can `curl` it).

## What's covered elsewhere

- `docs/MARKETS.md` — pari-mutuel mechanics, the rake, resolution integrity, dispute windows.
- `docs/CUSTODY.md` — custodial order book vs. non-custodial router: which endpoints are which,
  what each one collects.
- `docs/PERFORMANCE.md` — measured throughput and latency, and what actually gates each path.
- `docs/ROADMAP.md` — build order from this slice to the full spec.
- `docs/COMPLIANCE-NOTES.md` — why fiat, custody, and prediction markets are gated behind legal
  review rather than just more code.
