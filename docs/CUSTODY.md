# Custody: two modes, and what each one actually holds

Read this before describing the platform to anyone as custodial or non-custodial — it's both,
depending on which endpoint they're calling, and conflating the two is the single easiest way to
misrepresent what this platform does with someone's funds.

## Mode 1 — the custodial order book (`orderbook.rs`, `state.rs`)

`POST /v1/orders` against `BTC-USD`/`ETH-USD`/`SOL-USD` (behind `IKENGA_ENABLE_ORDERBOOK=1`) and
the prediction-market stakes in `prediction.rs` are both custodial: an agent deposits, this
server's ledger owns the balance (`AppState.balances`, protected by the lock-ordering described
in `docs/ARCHITECTURE.md`), and a trade is two numbers moving inside that ledger. This needs
capital to exist before it's useful — someone has to be quoting — and it needs everything real
custody implies: the write-ahead log making balances durable across a crash (verified by
`crash_test.py` — see `docs/PERFORMANCE.md` and the WAL's own module doc for why it logs outcomes
rather than commands), the reserve invariant on redeemable credits (`credits.rs`,
`GET /v1/reserves`), and the regulatory exposure that comes with holding anyone's money — see
`docs/COMPLIANCE-NOTES.md`.

## Mode 2 — the non-custodial router (`router.rs`, `liquidity.rs`)

`GET /v1/route?sell=X&buy=Y&amount=Z` prices the same trade across external venues and hands the
agent back a route **it executes itself, from its own wallet, with its own keys**. Nothing is
deposited here, so nothing is held here — there is no balance in `state.rs` for this path to
touch. The response's `custody` field says exactly this (`"none — you settle this from your own
wallet"`), and `execution` is explicit that this server never submits the transaction; put
`min_buy_amount` in your own swap as the minimum-output floor.

Three refusals are what make this a real product instead of "call one API, add a markup":
refuse a quote older than a few seconds, refuse a lone source (every quote is cross-checked
against the median of everything answering, and an outlier is dropped before the best price is
picked), and refuse to route at all when the survivors don't agree closely enough to trust. A
router earns less per trade than a custodial venue and lives or dies on quote quality, not
captive order flow — see `router.rs`'s own module doc for the full reasoning.

**Update from this round:** `liquidity.rs`'s own doc comment states the code was written in a
sandbox with no outbound network access, so its `HttpSource` adapters had never actually spoken
to a real price API — only the request-building and parsing logic was tested, against a local
stand-in server. That caveat no longer fully holds: this round's environment *does* have outbound
network access, and `GET /v1/route` was exercised live against real Coinbase/Kraken/Gemini
endpoints while testing `bots/market-maker/` (see its README) — real prices came back, e.g.
`net_buy_amount` around $80,800 for 1 BTC, sourced correctly per-call from whichever venue's quote
won the median check. The router's actual response shape and cross-check logic are now verified
against live data, not just a local mock. What's still unverified: sustained reliability of each
adapter's URL/parsing against those vendors' APIs over time — a thing to keep checking on
deploy, not something one successful test run settles permanently.

## What this means for a deployment decision

Pick per-endpoint, not per-platform: `/v1/route` can go live with no capital, no reserve
invariant to maintain, and a much smaller regulatory footprint, while the order book and
prediction markets carry the custodial obligations above regardless of how small the deployment
is. `docs/ROADMAP.md` treats these as separable for exactly this reason.
