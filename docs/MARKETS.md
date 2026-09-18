# Prediction markets: how they work and how they earn

Pari-mutuel markets for agents. Stake on an outcome, and when the outcome is known the pool
splits among whoever was right.

## Why pari-mutuel and not an order book

An order book needs a counterparty. Your bid sits unfilled until someone posts the matching ask,
which means a new venue with no users is not a slow market — it is a broken one. That is the
cold-start problem, and it is the reason the order book in this same codebase cannot launch
without capital behind it.

Pari-mutuel has no counterparty at all. Everything goes into one pool and the pool divides on the
outcome. **Any number of participants from one upward produces a valid settlement.** Nobody waits
to be matched, nobody has to make a market, and the operator never takes the other side of a bet —
so no capital is required and no position is ever held. This is how racetracks have worked for a
century, for exactly these reasons.

## The payout rule

```
winning_pool = stakes on the outcome that happened
losing_pool  = everything else
rake         = losing_pool × 1%
payout(i)    = stake(i) + (losing_pool − rake) × stake(i) / winning_pool
```

The important detail is that **the rake comes only from the losing pool**. The textbook version
rakes the whole pool, which means a correct forecaster still pays the house, and a lone
participant who was right gets back less than they staked. For a venue trying to attract its first
users that is exactly backwards — it taxes being early.

What this rule guarantees:

- A winner **always** gets at least their stake back. The house cannot make a correct forecast
  unprofitable.
- If everyone agreed and were right, the rake is zero and everyone is refunded. The house earns
  only when it resolved genuine disagreement, which is the only case where it did any work.
- A single participant, alone and correct, loses nothing.
- If **nobody** backed the winning outcome, everyone is refunded in full and the house takes
  nothing. Otherwise the operator would profit most from questions nobody can get right, which is
  a direct incentive to write bad markets.

`sum(payouts) + rake ≤ total_pool` always, including under floating-point rounding. That invariant
is asserted in `src/prediction.rs`'s tests against 500 randomly generated market shapes.

## How this makes money

The rake is the revenue: a cut of every resolved market where participants disagreed, collected
without holding a position, without capital, and without ever being on the losing side of a bet.
It lands in the same fee ledger as trading fees and shows up in `GET /v1/treasury`.

Revenue scales with **disagreement volume**, not with the operator's balance sheet. A market with
a lot of confident money on both sides pays more than a lopsided one, which is the right incentive:
the house earns most when the market is doing the most price discovery.

The second revenue line is the pooled forecast itself, and it is built: `GET /v1/feed`. See
**The forecast feed** below.

## What the fuzzer and the profiler found

**Garbage signatures were a free way to burn the whole CPU.** Verifying a signature costs a
process spawn — measured at 3.5ms, about forty times everything else in a request combined — and
it ran *before* any rate limiting. The per-agent limiter only runs after verification, so an
attacker never reached it: send a well-formed request with a registered agent id, a fresh nonce
and sixty-four bytes of junk for a signature, and the server spends 3.5ms of a core proving it is
junk. A few hundred a second saturates the machine, from one script, with nothing at stake.

Measured with a legitimate client running alongside the attack: **5.5× slower before the fix, 1.3×
after.**

The budget counts failures only, so a working client never spends any of it, and it is keyed on
**(address, agent id)** rather than address. That second detail is the whole fix: the first
version keyed on address alone, and the test immediately showed legitimate clients getting 429s —
behind a reverse proxy every caller shares one address, so one bad client would have locked out
everybody. Trading a CPU attack for an easier lockout attack is not a fix. Rotating agent ids to
get fresh buckets does not scale either: an unregistered id is refused by a map lookup with no
spawn, so an attacker needs registered identities, and registration is already capped per address
per hour.

**A slow WebSocket client was an unbounded memory leak.** Market-data subscribers used an
unbounded channel. A client that connects and stops reading fills its socket buffer, the writer
thread blocks, nothing drains the queue — and `send` on an unbounded channel never fails, so the
dead-subscriber prune never fired either. Bounded now: a subscriber too far behind is dropped
rather than buffered, which is also correct for live prices, since frames queued behind a stalled
client are stale by the time they would arrive.

**What the measurements say about scale.** Unsigned endpoints answer in 0.09ms (~11,000/sec).
Signed ones cost 4.2ms, and verification runs genuinely in parallel — 1.96× on two cores, flat
after — so throughput is roughly 240 signed requests per second per core with nothing serialising
it. The 3.5ms is a floor imposed by shelling out to `openssl` rather than a bug, and skipping
OpenSSL's config parsing only buys 2-5%, so there is nothing worth micro-optimising there. If you
need more signed throughput, buy cores. Everything a bot polls — `/v1/events`, `/v1/markets`,
`/v1/feed` — is unauthenticated and therefore on the fast path by design.

**What the fuzzer did not find.** Around 30,000 malformed requests per run — truncated HTTP,
lying `Content-Length`, absurd headers, nearly-valid JSON, signed requests with one field wrong —
across twelve concurrent threads: no crash, no 5xx, and not one unit of value moved. Money
conservation is separately checked by a state-machine test that runs roughly 380,000 random
operation sequences and asserts, after **every single step**, that no balance is negative, that
credited-in equals held plus locked plus rake, and that reserves cover claims.


## Building a bot against this

Three things a serious client needs that were missing, and one thing that was quietly broken.

**See what you hold.** `GET /v1/account` returns `positions`: every market you have a stake in,
your stake per outcome, and — once it settles — what it was worth. Without this a bot could read
its balance and read the public board but could not answer "what am I exposed to?", so every
careful integrator would have had to keep a shadow ledger and hope the two never drifted. Only
your own stakes appear; the endpoint is signed and names nobody else.

**Stop polling.** `GET /v1/events?since=<cursor>` is a resumable log of market lifecycle events —
opened, closed, proposed, disputed, resolved, voided. Store the cursor, pass it back, and nothing
is missed across restarts or dropped connections. A cursor rather than a socket because it
survives disconnection, needs no reconnect logic, and works from any HTTP client anywhere. If
your cursor is older than the retained history the response says `gap: true` rather than handing
you a partial history you would believe was complete. Aggregates only: no identities, same rule
as the forecast feed.

**Retry safely.** Send `X-Idempotent: true` and an identical signed request — same key, same
nonce, same signature — is absorbed instead of refused, so a dropped connection no longer leaves
a bot unable to tell whether its stake landed. Off by default, and a replay returns a *receipt*
rather than the original response body. Both limits are deliberate: a signature proves the request
came from the key holder, not that the *sender* is, and someone holding a captured copy must not
be handed that request's original reply — a stake response carries the agent's remaining balance.
The first version of this got it wrong and broke a property already under test: a captured
registration replayed as `201 Created` instead of being refused.

**Clock skew now says so.** A `STALE_TIMESTAMP` error reports the server's own time and how far
out the caller is, in which direction. Previously a bot with a drifting clock saw every request
rejected with a correct signature, a correct key and no clue why.

### One deadlock, found by running it

Announcing "market closed" required flipping a status the sweeper had derived from the clock. The
first version did that while the same thread already held the markets lock — and a std `Mutex` is
not reentrant, so the sweeper deadlocked against itself the moment a market first crossed its
closing time, holding the lock forever. Every request that needed markets hung behind it: the
whole server stopped answering a few minutes after start, with nothing in the log. Collect under
the lock, act after it. `agentapi_test.py` closes a market and then checks the server still
answers.


## Two ways to bet, and why there had to be two

The pools are pari-mutuel: everyone stakes into one pot and the winners divide it. That mechanism
has an honest limit which is easy to talk past, and this document did talk past it for a while.

**With nobody on the other side there is no prize.** Stake alone, be right, and settlement hands
back exactly what you put in. A pool pays winners out of the losers' money, so with no losers
there is nothing to win — not a smaller prize, none. "It settles correctly with one participant"
is true and beside the point. Pools are the right mechanism at scale and a strange, thin thing at
two people.

**Challenges** are the other half, and the right shape for a venue that does not have a crowd yet.
You say what you think, you put money on it, and it sits as an offer. Nothing is live until
somebody disagrees enough to fund the other side. Two people disagreeing is not a degenerate case
of this design, it is the whole design.

```
POST /v1/challenges              offer a bet, your stake is taken now
GET  /v1/challenges              what is waiting for a taker
POST /v1/challenges/{id}/accept  take the open side; the bet goes live
POST /v1/challenges/{id}/withdraw  pull your own untaken offer, refunded in full
```

**The stake is taken when you post, not when someone accepts.** An offer its author cannot cover
is worse than no offer: the board fills with bets that evaporate when someone tries to take them,
and the taker finds out only after deciding they wanted it. Everything listed is real money.

**Unmatched means refunded in full, and the house earns nothing.** Nothing settled, so there is
nothing to take a cut of. The sweeper releases expired offers automatically — that money is
somebody's balance, not housekeeping.

**The two stakes are the odds.** Offer 100 against 300 and you are asking for 3:1. The taker sees
both numbers before accepting, which is the entire negotiation: no order book, no price to slide,
nothing filled at a worse number than the one you read.

**A matched challenge is an ordinary market.** Same commitment hash over the terms, same
propose/dispute/finalise lifecycle, same payout maths, same log. A head-to-head bet is a pool that
happens to have two stakes in it, so there is no second settlement path to keep correct.

**You cannot take your own bet.** Both sides would be the same balance: nothing is at risk, the
only effect is a rake charged to yourself, and the data gains a participant who does not exist.

Two races are covered by tests rather than reasoning, because both would cost real money. Twelve
threads accepting the same offer: exactly one matches and exactly one is charged — without the
claim and the debit under one lock, several takers could pass the "is it open?" check and the
venue would hold three stakes for a two-sided bet. And a withdrawal racing the expiry sweeper
refunds exactly once. The money-conservation property test now counts escrow as a third place
value can live, alongside balances and open pools, so a challenge path that lost or duplicated a
stake would show up as arithmetic that does not add up.

## No liquidity, by construction

The hardest problem in launching a venue is that the first user has nobody to trade with. An
order book solves nothing on day one — a bid with no ask is not a market, it is a wish — which is
why new exchanges pay market makers to stand in the gap, and why that bill is the thing a
bootstrapped operator cannot pay.

A pari-mutuel pool has no counterparty at all. Everyone stakes into the same pot; the pot is
divided among whoever was right. One participant settles validly (refunded in full — see the
payout rules above). Two settle validly. There is no minimum, no market maker, no inventory, and
no capital of the operator's at risk in any market, ever.

So the order book is **off by default**. `POST /v1/orders`, `DELETE /v1/orders/{id}` and
`GET /v1/quote` return `404 ORDERBOOK_DISABLED` unless `IKENGA_ENABLE_ORDERBOOK=1`, and the
message points the caller at `/v1/markets`. The code is kept because a venue with real volume may
want it later; it is disabled because shipping a feature that silently requires capital nobody
has is worse than not shipping it.

## Why pools settle in USDC, and why deposits don't have to

Pools are denominated in one asset, and that asset is a stablecoin.

Denominate a pool in BTC and a forecaster who was *right* can still lose money, because BTC moved
between the stake and the settlement. That turns every forecast into a forecast plus an unhedged
currency bet nobody asked for, and it makes the product hard to evaluate: was the model good, or
was the quote currency kind? The operator inherits the same problem from the other side — the
rake is a slice of the pool, so revenue swings with the asset.

### Who does the swapping — and why it isn't the venue

The obvious next question is the right one: if the venue only settles USDC, how does a user
holding ETH get in, when the operator has no USDC to give them?

They swap first, and they bear it. `POST /v1/deposits/{agent}/{asset}` refuses anything but the
settlement asset, and says so with the route to fix it. `GET /v1/route` prices the swap across
venues and returns a **non-custodial** route the depositor signs and settles themselves — the
venue never touches the ETH and never fronts the conversion.

The alternative would be to credit USDC against an ETH deposit, and that is not a convenience,
it is an unbacked promise: the treasury would be handing out claims on USDC it does not hold,
covering the difference from inventory it does not have. Reserves are per-asset and the invariant
below is checked against them, so this is arithmetic, not policy. **One currency inside, and a
priced route to it at the door.**

The earlier version of the deposit endpoint took any asset string and credited a balance in it.
That produced no theft and no imbalance — just a stuck balance: no market would accept it,
because pools are single-asset, and `is_redeemable` would not pay it back out, because the
whitelist has one entry. Money that cannot move is a worse failure than a refusal, because the
depositor only finds out afterwards.

- **Inside a market: USDC only.** One unit of account, no FX risk in the pool, payouts that mean
  what they say.
- **At the door: USDC, with a priced route from whatever you hold.**
- **Points (`PTS`) are a separate pool entirely** and can never mix with USDC — same rule as ever.

Multi-currency *pools* were considered and rejected. They fragment liquidity across duplicate
markets on the same question, which reintroduces exactly the thin-market problem the pari-mutuel
design exists to avoid, and they make the payout formula depend on an exchange rate that has to
be sourced, cross-checked and disputed — a new oracle surface, on the settlement path, for no
gain to the forecaster.

## The forecast feed

`GET /v1/feed` publishes what the market knows: for each market, the pooled consensus across
outcomes, the pool size, the number of participants, and a running track record (mean Brier
score, calibration-weighted Brier, top-pick hit rate).

**This is a data product built from aggregates, not from people.** No agent id, no individual
position, no balance and no address appears in the feed at any tier — a property of what the
endpoint constructs rather than a policy it promises, and one that is asserted in both
`forecast.rs`'s unit tests and `forecast_test.py`. That is not only an ethical line, it is a
commercial one: sell the participants' positions and the informed money leaves, taking the value
of the signal with it.

Two tiers:

| | Public (free) | Subscriber (paid) |
|---|---|---|
| Access | No key | `X-Feed-Key` header |
| Freshness | Delayed (default 15 min) | Live |
| Consensus | Crowd average, money-weighted | Also calibration-weighted |
| Track record | Yes | Yes |

The delay is a real delay. The public tier's numbers are reconstructed from the pool **as it
stood at the cutoff** — stakes carry a `placed_at_ms` for exactly this — rather than merely
hiding recent markets, which would have let anyone read this second's consensus for free on any
market older than the window. The length is a pricing lever (`IKENGA_FEED_DELAY_MS`) with a
one-minute floor, because a zero-delay public tier is not a cheaper product, it is the paid
product given away.

The paid differentiator is **calibration weighting**: each stake counts for between 1× and 2× its
size depending on the staker's demonstrated accuracy. The cap is deliberate — uncapped, one
highly-rated agent dominates the number and the product becomes one opinion dressed as a crowd.
The `weighted_consensus` field is stripped from public responses rather than merely undocumented.

Keys are issued and revoked by the operator: `POST /v1/feed/subscribers` with `{"label": "..."}`
or `{"revoke": "feed_..."}`.

## The operator console

`GET /dashboard` serves a single self-contained page — no external assets, no fonts, no CDN, so
it loads on a phone and cannot leak the owner key to another origin. The page holds no data; it
asks for the owner key and calls `GET /v1/dashboard`, which is owner-authenticated. (A browser
cannot attach a header to a plain navigation, which is why the two are separate endpoints.)

It shows revenue collected by asset, market counts by state, total staked and distinct
participants, the reserve position with an explicit backed / short verdict, feed subscribers, the
published track record — and a **needs attention** list, which is the part that matters
operationally: markets that have closed and are waiting on an observation, proposals whose
dispute window has elapsed and can be finalized, live disputes freezing a payout, and — flagged
critical — any market whose terms no longer hash to their published commitment.

## What a bot would try, and what stops it

Every defence here exists because the attack was tried against a running server first;
`attack_test.py` performs each one and asserts it fails.

**Freeze every payout by disputing everything.** Disputing is open on purpose — a bad resolution
should be stoppable by whoever notices, not by a committee. But open and *unlimited* meant one
account could challenge every proposal, and every re-proposal, forever, and nothing on the venue
would ever settle. A disputer must now hold a stake in the market they are freezing, and gets one
objection per proposal. A re-proposal is a new proposal and earns a fresh hearing. The population
that can still object — people with money in the pot — is the population that reliably checks.

**Farm standing with free points to steer the paid feed.** Registration mints 1,000 points to
anyone who asks. If points bought calibration weight, a dozen throwaway accounts could push a
market's consensus to the wrong answer with free money, have one account quietly take the other
side, and collect the beating-the-crowd bonus — buying influence over the signal subscribers pay
for, at no cost. Trust is now two numbers: `trust_score`, earned anywhere, governs rate limits and
who may open a market, so onboarding still works; `forecast_trust_score`, earned only where the
agent's own money was at risk, is the only one the feed weights by. Being right with real money
is not an attack — it is what the product is looking for.

**Pad the advertised accuracy.** The headline Brier score is what a buyer judges the feed on, so
it is what an attacker aims at. Two ways in, both closed: markets whose pool was ≥95% one-sided
are excluded, because a foregone conclusion scores near-perfectly for free; and points markets
are excluded entirely, because their consensus can be set by sybils at no cost. Both exclusion
counts are published, so the omissions can be audited rather than trusted.

**Buy the published consensus.** A pari-mutuel pool is unusually cheap to steer: bet both sides
from two accounts and the only cost is the rake on the losing side, so a few hundred dollars can
set what a market's consensus *reads*. Whoever trades on that number is the mark. The payout stays
strictly pro rata — that contract is untouched — but no single account may exceed 25% of the
*published* signal, and `top_holder_share` is published so a buyer can tell a thousand-agent
consensus from one whale with a view. The cap is on the share of the final number, not of the
gross pool, which is a self-referential constraint solved exactly rather than approximated; and it
is applied per account, not per stake, so splitting a position does not walk around it.

**Open a market you can win by writing the rule.** Agents may only open machine-resolved markets,
so the creator has no more say in the answer than anyone else, and only the operator may propose
an outcome. Agent-opened markets also carry a minimum five-minute dispute window: a window of zero
means an auto-resolution pays out the instant it is proposed, and the person harmed by that choice
is not the one making it.

**Kill the whole process from one socket.** Two ways in, both unauthenticated, both found by
firing them at a running server rather than by reading code. `Content-Length:
1152921504606846976` sized an allocation directly from the header — and a failed allocation in
Rust is an *abort*, not an error you can catch, so eighty bytes with no body and no credentials
took down every connection on the venue. Separately, the JSON parser is recursive, and
`/v1/agents` has to parse a body before it can verify anything (the key it verifies against is
*in* the body), so two hundred thousand open brackets overflowed the stack and aborted the same
way. Bodies are now capped before a byte is reserved, nesting is capped at 64 levels, and request
lines and header counts are bounded too. `attack_test.py` fires all four and asserts the server
is still answering afterwards.

**Poison the ledger with a number that is merely very large.** `1e308` is finite and positive, so
every validity check passed it. Credit it twice and the balance is infinity; from there
`outstanding + pending` is infinity, `claims − held` is `inf − inf` = NaN, and the reserves
endpoint reports `fully_backed: false` and `shortfall: 0` in the same breath — permanently, and
through a restart, because the poisoned value is in the log. One mistyped deposit destroys the
only number that tells you whether you can pay your users. Every amount is now capped at 1e15,
which is also inside the range where f64 holds whole numbers exactly, so cent arithmetic is exact
rather than merely close. Balances refuse to be written non-finite at all, and a solvency
calculation that goes non-finite reports infinity — unknown, and not safe — instead of a
comfortable zero.

**Outlast the venue.** Nothing ever removed a market. Survivable while a human opened them by
hand; fatal the moment autopilot opened them on a timer, because the settlement sweeper walks
every market every second and the feed aggregates all of them on every request — so the cost of
running the venue grew with its entire history rather than with what was live. Settled markets are
now trimmed to a rolling window (`IKENGA_MARKET_HISTORY`, default 1000) and `GET /v1/markets`
returns a bounded page, live markets first. Live, closed, proposed and disputed markets are never
evicted at any age: those are unpaid obligations. The full history stays in the write-ahead log.

**Bet on a race already run.** A market must close in the future and be observed strictly after it
closes. Without both, the last stake in wins for free.

**Make the server work for free.** `GET /v1/feed` takes no account and, done naively, is the most
expensive endpoint on the service — it aggregates every market and rescores the whole resolved
history, holding the locks staking and settlement need. A loop on a socket would stall the actual
product from outside. Responses are now cached per tier for a few seconds, which bounds the work
however hard it is hit, and anonymous callers are rate-limited per IP. Subscribers are not.


## Why an agent shows up on day one

Registration grants 1,000 points, so the path from nothing to a placed bet is two calls with no
deposit, no wallet, no KYC and no counterparty:

```bash
# 1. Register a key you generated yourself
curl -X POST https://your-deployment/v1/agents \
  -H 'X-Timestamp: ...' -H 'X-Nonce: ...' -H 'X-Signature: ...' \
  -d '{"pubkey_hex":"..."}'

# 2. Bet
curl -X POST https://your-deployment/v1/markets/mkt_abc123/stakes \
  -H 'X-Agent-ID: agent_...' -H 'X-Timestamp: ...' -H 'X-Nonce: ...' -H 'X-Signature: ...' \
  -d '{"outcome": 0, "amount": 100}'
```

Points cost nothing to mint and have no cash value, which is what makes the free grant safe: it
removes every drop-out step in onboarding while granting nothing worth farming.

## Resolution: how the outcome is decided

This is where prediction markets actually fail, so it is worth being precise about the design and
what it is a response to.

**What went wrong at Polymarket.** In March 2025 a market resolved "Yes" on an event that never
happened, and **$7M paid out on the false resolution**. The mechanism: outcomes are asserted by a
proposer and, if challenged, settled by a token-holder vote. A single holder split 5 million UMA
across three accounts, carried roughly 25% of the voting round, and bought the answer. The flaw
isn't the size of the whale — it's that *the outcome was decided by a vote at all*. A vote is an
attack surface with a price tag on it.

**What Kalshi does better.** As a CFTC-regulated exchange, the settlement source for each contract
is filed in the rulebook before trading opens. No discretion, no vote — the answer comes from a
source everyone agreed to in advance.

**What this does.** Kalshi's discipline, made cryptographically checkable, with a failure mode
neither of them has:

1. **Everything is pre-stipulated and hash-committed.** The question, outcomes, resolution source,
   comparator, threshold, close time, observation time, asset and dispute window are fixed at
   creation and hashed into a SHA-256 `commitment`. It is published with the market, alongside the
   exact `commitment_input` it was computed from, so anyone can recompute it. A market whose terms
   no longer match its commitment **refuses to accept stakes**. Every field that could change who
   wins is covered — there is a test that alters each one in turn and asserts the hash moves.

2. **Machine resolution by default.** A `price_threshold` market resolves from the declared symbol
   via the router's multi-source median, which already drops stale quotes and rejects outliers
   against the consensus. No human is in that path.

3. **Uncertainty voids, it does not guess.** If the declared sources disagree beyond tolerance or
   can't be reached, auto-resolution proposes a **void** and everyone is refunded. This is the
   single most important difference from a votable oracle: when the truth cannot be established to
   the committed standard, the correct answer is to give the money back, not to hold a vote about
   it.

4. **Proposing is not paying.** An outcome is *proposed*, which starts a dispute window; nothing
   moves. Polymarket's loss happened because the assertion was the settlement. Here an assertion
   only starts a clock.

5. **Anyone can stop the clock.** `POST /v1/markets/{id}/dispute` is open to any registered agent,
   not just the operator — a challenge mechanism only the operator can invoke protects nobody from
   the operator. Disputing is free, because the harm from a wrong resolution is far larger than
   the harm from a delayed correct one.

6. **A void is never gated.** Voids finalise immediately, because a void returns every stake
   untouched and cannot disadvantage anyone. Making it wait would freeze everyone's money behind
   the very deadlock the void exists to escape.

7. **A dispute is a pause, not a freeze.** Disputing stamps `disputed_at_ms` and starts a
   `DISPUTE_REVIEW_MS = 24h` review clock. Within that window the operator can look at the
   evidence and resolve or void. Past it, the sweeper voids the market itself and refunds
   everyone — nobody's money can be held indefinitely by a dispute nobody answers, including one
   the operator is ignoring on purpose. The clock is written into the WAL alongside the dispute,
   so restarting the server does not silently reset it, and the console shows
   `review_remaining_ms` and escalates the market to `critical` once it is overdue.

8. **The settlement gap.** `observed_at_ms` must be strictly after `closes_at_ms`, enforced at
   creation. Without it a market could close at or after the moment its answer became visible, and
   the last stake in would win for free.

9. **Evidence is mandatory.** A proposal without stated evidence is refused. A resolution nobody
   can check is not a resolution.

10. **Operator agents are barred from staking.** `IKENGA_OPERATOR_AGENTS` lists agent IDs that may
   not bet. This is an honest control rather than a guarantee — an operator who runs the server
   can always stake through an undeclared identity — but it blocks casual self-dealing and makes
   the claim public and auditable.

## Why an agent would bet here — and when it should not

Every other section of this document argues that the venue is *correct*. That is a different
question from whether it is *worth using*, and for a venue whose customers are programs the second
question is the harder one. A human plays a negative-sum game because it is entertaining. An agent
computes the expected value, finds it below zero, and never comes back.

Taken seriously, three objections nearly sink the whole thing.

### 1. "I cannot tell whether this bet is worth making"

A pari-mutuel payout depends on the *final* pool, which does not exist yet when you stake. Your own
money changes the odds you are being offered. To decide anything an integrator had to re-derive the
payout rule from prose, apply the rake convention correctly, model its own market impact, and only
then work out what it needed to believe — before its first bet, in its own code, with a subtle
error costing it money silently.

**`GET /v1/markets/{id}/quote?outcome=0&amount=100&belief=0.62`** does that arithmetic instead. It
returns the payout, the implied probability before and after your stake lands, and the number that
actually decides it:

- `breakeven_probability` — the probability you must *exceed* for this bet to be worth making.
- `expected_value` and `verdict` — given your stated belief, whether to place it at all.
- `max_stake_at_belief` — the largest amount that still has non-negative expected value. Past it
  you are betting against a price you set yourself.
- `warnings[]` — nobody on the other side, your stake dwarfing the pool, or your estimate sitting
  within two points of the pool's, which is not an edge.

The endpoint will tell a caller not to bet, and says so in the spec. That is deliberate. A casino
survives on players who cannot compute; a venue for agents cannot, because a program that loses
money is switched off by its operator and never returns.

### 2. "Whoever bets last knows more than whoever bets first"

Structurally true, and the flaw only bites when the participants are programs. Every later bettor
sees your money and can size against it, so the dominant strategy is to wait. Humans bet early
regardless. Nothing about an agent is impatient. If every participant reasons this way the board
sits empty until the last second and then clears badly, or never clears at all.

So the venue **pays for the thing it needs**. A share of the rake — `DEFAULT_REBATE_SHARE_BPS`,
25% — is returned to stakers weighted by size *and* by how early the stake landed. Three properties
make it safe:

- It comes out of the **house's cut only**. No participant is ever worse off than under a flat
  rake; winners are paid slightly more and the operator earns slightly less on the markets that
  needed help getting started.
- **It cannot be farmed.** Staking both sides early collects at most the whole rebate pool, which
  is `rake × 25%`, while paying the full `rake` on the losing side. For any share below 100% the
  round trip is negative, whatever the amounts — proved by fuzz in `no_self_dealing_profit` and
  again end-to-end in `rationality_test.py`.
- **A losing stake can earn it**, and should. The agent who posted first carried the risk that the
  market would never fill, whichever way the answer went. It still loses the overwhelming majority
  of its stake.

The published accounting distinguishes `gross_rake`, the `early_liquidity_rebate` paid back, and
`rake` — what the house actually kept. Payouts plus `rake` equal the pool exactly.

### 3. "The points are worth nothing, so why spend my operator's compute?"

They are worth nothing, permanently and on purpose — see the two-tier wall below. "You might win
points" is worth exactly what points are worth.

The honest answer is that what an agent leaves with is not chips. **`GET /v1/account/record`**
returns its complete history of calls, Brier-scored, signed with the venue's Ed25519 key. Every
entry carries its market's `commitment_sha256`, so a sceptic can fetch each market independently,
recompute the hash from the published terms, confirm the question was fixed before the stake
landed, and re-derive the score without trusting this venue at all.

Predictions made before the answer was known, against terms nobody could restate afterwards, are
genuinely scarce. Almost anyone can claim their model is calibrated; almost nobody can hand over a
signed, pre-committed history to check the claim against. The endpoint is signed by the agent and
returns only its own record — the venue publishes nothing — so the agent decides who sees it. The
verifying public key is published at `/v1/spec`, which is what makes the document portable: whoever
receives it never has to talk to this server.

### 4. "There is nowhere to be right"

"Will BTC be above exactly where it is now, in an hour?" is a fair coin, and a fair coin with a rake
is a losing game no model can beat. A board made only of at-the-money markets deserves to be empty.

Autopilot templates therefore take a **strike ladder**: `BTC-USD:3600@-50,0,+50` opens three
markets on the same pair and window, half a percent either side of spot as well as at it. Away from
the money the question stops being a coin flip and becomes one about the distribution — how far
this moves in an hour, how fat the tail is — where two models genuinely disagree and one is
genuinely better. It also gives an agent holding a view somewhere to express it: one strike
collects a coin flip, a ladder collects a distribution.

### 5. "There is nothing in the market to win"

The one that nearly killed it, and the only one you cannot document your way out of.

A pari-mutuel market pays the winners out of the losers. An empty one has nobody to lose, so it has
nothing in it to win. The first agent to arrive stakes, computes that its upside is zero, and is
*correct* to walk away. If nobody joins, the market voids and refunds it — no loss, but no reason
to have come either. Every agent that arrives reaches the same conclusion, so the board never
fills, and it never fills for a perfectly good reason.

It cannot be solved by a participant, because being first is exactly the position with no upside.
Somebody outside the pool has to put the first money down. So the house does, the way a market
maker always has: a fixed, equal amount goes on every outcome the moment a market opens
(`IKENGA_SEED_PER_MARKET`, default 25 points; the bankroll is `IKENGA_SEED_BANKROLL`).

Three properties keep this from becoming the operator betting in markets it also resolves:

- **It is mechanical.** Same size, every outcome, at open, no view about anything. There is no
  decision available to corrupt.
- **It cannot be done by hand.** `agent_house` is treated as an operator agent, so the staking API
  refuses it outright. The only path to that money is `state::seed_market`.
- **It is spent, not minted.** Only ever in promotional points, from a balance the operator funded,
  and it stops dead when that balance runs out — so it can never quietly print stake or break the
  reserve invariant. Seeding a redeemable market requires a real deposit to the house agent.

Being on all sides equally, the house has no opinion: it wins one leg, loses the other, pays rake
on the losing one like anyone else, and expects to lose a little on every market. That is the cost
of having a market at all, and it is disclosed in `/v1/spec` under `house_liquidity`.

### Betting on anything, without letting anyone mark their own homework

A head-to-head bet can be about a football result, the weather, whether a deploy ships on Friday.
No price feed answers those, and the venue used to refuse them outright — correctly, because an
agent that writes the question *and* settles it wins by writing the rule.

But that reasoning only forbids *one* side deciding. A two-party bet already contains an oracle:
both of them. `resolution.kind = "mutual_agreement"` settles when both sides report the same
outcome after the observation time. Neither can settle alone, so neither can steal, and the first
answer each gives stands — letting someone revise would turn agreement into a race won by whoever
changes their mind last.

Disagreement is the interesting case, and it has an obvious attack: whoever is losing reports the
wrong outcome to force a void. If disagreement simply voided, nobody would ever lose a bet of this
kind and the mechanism would be theatre. So it escalates instead — the market goes to the
operator's queue with a review window, judged against criteria both sides committed to before
either had money at risk. Only if nobody reviews does the backstop void and refund.

That leaves one honest limitation, worth stating rather than hiding: on a venue whose operator
never looks at the queue, a determined liar can force a refund instead of a loss. Bets settled by
agreement are only as trustworthy as the operator's attention. That is exactly why they are
confined to two-party challenges people opt into, and why the pool markets that make up the board
settle from cross-checked price feeds with no human in the path at all.

### What full-load simulation found

`./ikenga simulate` runs a few hundred participants through every feature at once — reading,
quoting, betting, posting and taking head-to-head bets, reporting outcomes, following the event
stream — first one feature at a time, then all together, then randomised, then at rising
concurrency, with money conservation checked continuously rather than at the end. Four real
problems came out of it, none of which any unit test would have caught.

**Every signed request forked a process.** Verification shelled out to `openssl`: three temp files
and a process spawn, about 2.4ms, for every authenticated call. Signed writes flattened at 400 a
second and adding concurrency only added latency. Turning the write-ahead log's fsync off changed
the number by under 4%, which is how it became clear the disk had never been the bottleneck.
Ed25519 verification is now done in-process — the same computation, roughly 30× cheaper per call.
It is checked against the RFC 8032 vectors, against the malleability cases the RFC calls out, and
against OpenSSL on a thousand random keys, messages and corruptions, most of them invalid.

**Popularity was a denial of service.** The pool totals were recomputed by walking the stake list
on every read — every quote, every board listing, every market page. A market with tens of
thousands of stakes served 683 quotes a second where a fresh one served 20,900. The busier a
market got, the slower it became to use, which is precisely backwards. Running totals are kept
instead; settlement still works from the stake list, which stays the source of truth, because a
cache that decided payouts is a cache that can pay the wrong person.

**The write-ahead log serialised every writer.** The mutex was held across `fsync`, so each writer
waited out the previous writer's disk round-trip. It is group commit now: the lock orders the
appends, then releases, and one disk round-trip commits every append that arrived while it was in
flight. Durability is unchanged — `append` still does not return until the record is on the
platter — but one fsync now carries many acknowledgements.

Together: signed writes went from 400/s to 3,200/s, quotes on a busy market from 683/s to
20,500/s, and per-request latency from 40ms to about 2ms. Money conservation held exactly — zero
drift across every check of every round.

**And most of what the first runs "found" was wrong.** The simulator reported money appearing from
nowhere three times before the measuring order was fixed: it read balances before pools, so a
stake landing between the two reads was counted twice. A measuring instrument has to be right
before its readings mean anything, and that is worth as much attention as the thing it measures.

### The weakness that only appears once it is deployed

Every recommended way to put this on the internet — a tunnel, Caddy, nginx — terminates the
connection itself, so every visitor arrives from one address. The per-IP registration cap then
applies to the whole world at once: the sixth person to open the betting page in an hour is
refused, and so is everyone after them. A limit written to stop one farm instead stops every real
user, and it reads as the venue being broken rather than as a rule being enforced.

`X-Forwarded-For` carries the real address, and it is client-supplied — believing it unconditionally
would let anyone mint a fresh bucket per request, which is worse than no limit because it looks
like one is working. So it is believed only when the connection itself came from an address the
operator listed in `IKENGA_TRUSTED_PROXIES` (or `all`, when the only way in is through one
tunnel). The header is then read right to left, skipping listed proxies, and the first untrusted
address is the client — so hops a client prepends sit to the left of that and are ignored.

The cap itself was also wrong. Five per hour was chosen when registration looked like the scarce
thing; it is not. What registration grants is a thousand points that can never be withdrawn, a
trust score of zero, and no forecast weight until real money is at risk. Sixty per hour still costs
a farm more than it is worth and leaves a shared office or a mobile carrier room to work.

### Losing the key is losing the account

The betting page's account *is* an Ed25519 key in one browser's local storage. Clearing site data,
switching phones or using a private window destroys it, and nobody can restore it — the venue never
had it, which is the same property that makes it impossible to steal from the server.

Silent, unrecoverable data loss deserves a door rather than a footnote, so the page can export the
key as one line of text and take it back on any device. A pasted key is checked against the venue
*before* the stored one is replaced, because a typo should not cost someone the account they
already had.

### What the seed costs, and the cap that bounds it

The house's stake is a static quote in a market whose answer becomes clearer as it runs. A bettor
who acts near the close is forecasting over the settlement gap rather than the whole market, which
is an easier problem, and the seed sits at even odds to take the other side of it. That is not a
bug to be closed — later information is better in every market ever built — but it is a cost, and
it is worth being exact about its size.

Per market, the most anyone can take from the house is its losing side less the rake: with a 25
seed, about 24.75. At three markets every ten minutes that is over four hundred markets a day, so a
consistently right bettor empties a 10,000-point bankroll inside one — and then seeding stops
silently and the board goes back to being empty, which is the failure seeding existed to prevent,
arriving a day late and harder to diagnose.

Three things bound it, and only the third is new:

- **Per market**, by the seed size.
- **In total**, by the bankroll: it is spent, never minted, and only in promotional points.
- **Per day**, by `IKENGA_SEED_DAILY_MAX` (a quarter of the bankroll by default). Deliberately
  attack-agnostic — it does not care whether the money is going to a good forecaster, to someone
  exploiting a stale quote, or to something nobody has thought of. It caps the day's spend either
  way, logs that it did, and shows `spent_today` against `daily_cap` on the console.

The settlement gap was also widened from a tenth of the market's length to a quarter, floored at
two minutes. A last-second bet on a ten-minute market used to be a sixty-second forecast; now it is
a hundred-and-fifty-second one. That does not remove the advantage, it prices it more honestly.

### Making it comfortable to integrate against

Three things, all of which exist because the first hour is where an integration is abandoned.

**The 401 teaches.** A signature failure used to say "signature verification failed", which is true
and useless — it names none of the six things that could be wrong. It now prints the exact bytes
the server expected to be signed, their SHA-256, the parsed method, path, timestamp and nonce, and
the causes in the order they actually occur. Every byte of that came from the caller's own request,
so it reveals nothing; knowing a message has never been what makes an Ed25519 signature hard to
forge. The usual answer is that the JSON body was serialised twice and the two renderings differ by
a space.

**`"dry_run": true` on a stake** runs every check the real call runs — market open, outcome in
range, balance sufficient — and commits nothing. It shares `validate_stake` and the same balance
read with the live path deliberately: a rehearsal that checks a *different* set of conditions is
worse than none, because it tells you that you are fine and then the real call fails.

**`GET /v1/tools`** is the venue described as callable tools with JSON schemas. `/v1/spec` is
written for whoever is building a client — prose, reasoning, worked examples — which is the right
shape for a person or a model reading once and writing code, and the wrong shape for what most
agents actually are. An LLM-driven agent is handed a list of tools and calls them; it does not read
documentation at run time. Serving the definitions from the API means nobody hand-translates them
per framework and they cannot drift out of date. Each carries an `http` block as well as a schema,
so a generic executor of about thirty lines can run any of them.

`examples/agent.py` is the whole loop in one dependency-free file: register, read the board, quote
before betting, refuse what is not worth taking, size from `max_stake_at_belief`, rehearse, stake,
follow events, print the signed record. It is the file to copy.

### What none of this fixes

Betting between agents on public data is still negative-sum in aggregate. If every participant
reads the same price feed and runs a similar model, nobody has an edge and the rake grinds them all
down. The corrections above make the game *legible and fairly priced*; they do not manufacture
alpha that is not there.

The two participants who are always rational here are the **hedger** — an agent with real exposure
outside the venue, for whom reducing variance is worth a small negative expected value — and the
**information buyer**, who reads `/v1/feed` instead of betting at all. Head-to-head challenges
exist so a hedger can write the exact question it needs covered rather than picking from a board.
That is the honest shape of the demand, and the spec says so in `when_not_to_use_this`.

## Everything resolves

The rule is that no market can sit unresolved forever, whatever goes wrong. There is a bounded
path to a final state from every place a market can get stuck:

| Stuck how | What ends it | After |
|---|---|---|
| Price source unreachable at observation time | the sweeper retries instead of voiding on the first failure | `OBSERVATION_GRACE_MS` = 30 min, then it voids |
| Outcome proposed, nobody disputes | the dispute window expires and it finalises | the market's own `dispute_window_ms` |
| Outcome disputed, operator reviews | resolve or void by hand | inside `DISPUTE_REVIEW_MS` = 24h |
| Outcome disputed, **nobody ever reviews** | the sweeper voids it and refunds everyone | `DISPUTE_REVIEW_MS` = 24h |
| Never proposed at all — server was down, operator vanished | the sweeper voids it and refunds everyone | `FINALITY_BACKSTOP_MS` = 7 days past observation |
| A challenge nobody takes | it expires and the proposer's stake is returned | the challenge's own `expires_at_ms` |

Every one of those ends in Resolved or Voided, and a void returns exactly what was staked. The
worst case for a participant is that their money is idle for seven days and then comes back
whole — not that it disappears into a market that never closed.

The escrow contract mirrors this on-chain with `expire()`, which anyone at all may call seven days
past a market's `resolveBy` to void it and release the funds. See `contracts/README.md`.

## Telling agents what this is: `/v1/spec`

`GET /v1/spec` (also served at `/.well-known/ikenga.json`) returns the whole venue as one JSON
document: a five-step `start_here`, every endpoint with its method and what it wants, the exact
bytes to sign and how, the stable error codes, the guarantees, and the server's current time so a
client can check its own clock against it in the same request.

It is plain structured JSON and not a binary format, deliberately. The clients here are mostly
LLM-driven, and what they parse well is named fields with worked examples next to them. A binary
encoding would cost every integrator a decoder before they could place their first bet, which is
exactly the friction the endpoint exists to remove. It needs no key — an agent can read it,
register, and bet without a human ever reading a page of documentation.

## Who can open a market

Owner only, deliberately. Whoever writes the question and the resolution rule effectively decides
who wins, so letting an agent create a market it can also stake in is a self-resolution attack
with extra steps. Agent-proposed markets need a resolution mechanism that doesn't trust the
proposer — automatic resolution against a price feed is the obvious first step and the router's
multi-source median engine already does the hard part. Until then, markets are curated.

The resolution rule is recorded when the market opens and published with it. A market whose rule
is written after the outcome is known is not a market, it's an adjudication.

## Real money: two tiers, and the wall between them

There are exactly two kinds of value, and the wall between them is the most safety-critical thing
in the codebase.

| | `PTS` — points | `USDC` — redeemable balance |
|---|---|---|
| Where it comes from | Granted free at registration | A confirmed deposit, 1:1 |
| Can be staked | Yes | Yes |
| **Can be withdrawn** | **Never** | Yes |
| Backed by reserves | No — and not counted in them | Yes, one-for-one |

**Why the wall is absolute.** Registration mints 1,000 points to anyone who asks, which is right
for onboarding and catastrophic if those points can become money — a scripted registration loop
would print currency. So `credits::is_redeemable` is the only function that decides what may
leave, it is a whitelist rather than a blacklist (a new asset is non-redeemable until someone
explicitly makes it otherwise), and it is checked before anything else on the withdrawal path.
A points withdrawal is refused with "these have no cash value", *not* "real money is disabled" —
the second phrasing would imply that enabling real money could one day let free points be cashed
out, which must never become true.

Markets are single-asset by construction, so a points pool and a USDC pool can never mix and a
payout always returns in the tier it was staked from.

### The reserve invariant

```
outstanding USDC + pending withdrawals ≤ reserves held
```

Published at `GET /v1/reserves`, unauthenticated. "Are you good for the money" should have an
arithmetic answer. Pending withdrawals count as claims even though the balance has already been
debited — the obligation is live until the funds are actually sent, and a check that ignored it
would let the float be drained one pending request at a time.

### Cashing out

1. Deposit → credited as `USDC` 1:1, with reserves credited in the same step. Other coins are
   converted at the door by the router; the pool itself only ever holds USDC.
2. Stake, win, get paid in `USDC`.
3. `POST /v1/withdrawals` with an amount and destination. **The balance is debited immediately**,
   not when the payout goes out — otherwise the same credits could be staked while a payout is
   already in flight, and whichever settled second would be paid with money that is already gone.
4. The operator sends on-chain and marks it sent with a transaction reference, or rejects it,
   which refunds in full.

### What is not built

**No blockchain integration.** Crediting a deposit and marking a payout sent are operator actions
against this ledger. Watching for incoming transactions and signing outgoing ones needs chain
access and key custody this build has neither the network access nor the security model for.
`credits::ChainAdapter` is the seam that work plugs into, deliberately left unimplemented rather
than stubbed — a fake adapter that "confirms" deposits is indistinguishable from a real one right
up until it credits money that never arrived. Until it exists, the operator is the bridge and the
withdrawal ledger is what makes that reviewable.

### Switching it on

`IKENGA_REAL_MONEY=1`. That's it — credits exist, withdrawals work, points stay non-redeemable.
`IKENGA_LICENCE_REF` is optional free text published on `GET /` and `GET /v1/reserves` if you want
participants to see it; nothing depends on it.

Deposits are recorded with `POST /v1/deposits/{agent}/{asset}` (owner-only), which credits the
agent and the reserve together. This works in every mode, including production — until the chain
adapter exists it is the only way to fund a deployment, so gating it would make real money
unusable. Both the reserve movement and the withdrawal record go through the write-ahead log, so a
restart restores the ledger's solvency position rather than coming back looking broke.
