# Ikenga Clear — a clearinghouse for bot-to-bot payments

> **Status note (Oct 2026):** this file describes the first Clear design: netting payments
> *between* bots. The launch product is now usage billing for MCP servers and APIs, paid into the
> service owner's own Stripe account (see `clear/README.md`). Ikenga (now Keptvow) never holds or moves money in
> either design. The pricing below (a share of savings) is the old design's and is not the launch
> pricing.

**One line:** bots record what they owe each other instead of paying every time; each cycle
Ikenga nets everything and each bot makes or receives the fewest transfers that settle it. Ikenga
keeps a share of the fees that saved, and nothing if it saved nothing.

Code: `services/matching-engine/src/clearing.rs` (engine), `src/clear_api.rs` (HTTP).
Tests: 9 unit tests, including a 400-round randomized check of the fee guarantee, and
`clear_test.py`, 30 end-to-end checks against a live server, including a `kill -9`.

---

## 1. The problem it solves

Bots increasingly pay other bots: for a data point, an API call, a forecast, a minute of
compute. Those payments are **small and frequent**, and every payment rail charges a cost per
transfer:

| Rail | Cost per transfer | What a 5¢ payment costs to move |
|---|---|---|
| Card processor (standard US pricing) | ~$0.30 + 2.9% | ~$0.30 (6× the payment) |
| Bank wire / ACH | $0.20 – $25 | more than the payment |
| Stablecoin on a cheap chain | ~$0.001 – $0.05 | 2% – 100% of the payment |

So most small bot-to-bot commerce **doesn't happen at all**. It's too expensive to move. Or
developers hack around it with prepaid credits locked to one vendor.

## 2. The mechanism: multilateral netting

This is how banks have settled cheques for a century, and how stock exchanges settle trades
today. It applied to bots:

```
  Paid one at a time:                     Netted:
  trader  → data      $0.05 × 1,000
  data    → compute   $0.08 ×   600       trader  → research   $1.19
  compute → trader    $0.10 ×   500       trader  → compute    $0.27
  research→ data      $0.05 ×   800   →   trader  → data       $0.15
  ...                                     (3 transfers, whatever the payment count)
  thousands of transfers
```

1. **Record, don't pay.** `POST /v1/clear/obligations {payee, amount}`, signed with the payer's
   own key. No money moves. Recording a 0.1¢ payment costs the same as recording $1,000: nothing.
2. **Net.** At each cycle close (hourly by default), every bot's obligations become one net
   number per asset. A pays B $10, B pays C $10, C pays A $10: that's $30 of payments and
   **$0** to settle.
3. **Settle the minimum.** Net positions become transfers. There are never more than
   (number of bots with a non-zero position − 1) transfers, however many payments went in.
4. **Bots pay each other directly.** The payer sends from its own wallet and the recipient
   confirms receipt (`/confirm`). Ikenga never holds or moves money.

**Measured in `clear_test.py`:** 4 bots, 49 micro-payments worth $5.82 → 4 transfers moving
$1.61. Card-rail cost dropped from $14.87 to $0.95. Three bots trading $1,800 in a loop in $20
chunks → **zero** transfers between them.

## 3. How Ikenga makes money

**The fee is the lesser of:**
- **20% of what netting saved that bot** (what its payments would have cost one at a time,
  minus what its netted transfers actually cost), or
- **1% of what that bot sent.**

A bot that saved nothing pays nothing. Both numbers are configurable
(`IKENGA_CLEAR_SHARE`, `IKENGA_CLEAR_MAX_FEE_BPS`).

The 1% ceiling matters. Without it, against card pricing, a 5¢ payment "saves" 30¢, so 20% of
the saving comes to more than the payment itself. In the first test run, fees came to 46% of the
money moved. Nobody would pay that, and nobody should. A bot that would never have paid 30¢ to
move 5¢ didn't really save 30¢. It just gets to transact at all.

Three details that make it trustworthy to a bot:

- **Guaranteed, not promised.** Every cycle checks, per bot, that its fee is within both limits,
  measured against the *final* transfer list. If a cycle can't meet that, the fee for that asset
  is waived for that cycle. Undercharging is the fallback. Overcharging never is.
- **The fee rides the netting.** The fee account is just one more creditor, so collecting it
  usually adds no transfer. The transfer that pays it is sent net of its own rail cost.
- **Small fees carry forward.** If a fee is too small to be worth a transfer (rail cost > 10% of
  it), it is owed next cycle instead of being sent or forgiven. It grows until it's worth
  collecting.

Every bot gets a **savings receipt** each cycle (`GET /v1/clear/cycles/{id}`, signed): what it
would have paid, what it did pay, the fee, and what it kept. Each cycle also publishes a SHA-256
digest of every obligation and instruction, so any bot can check it was netted on the same facts
as everyone else.

## 4. Honest numbers: how big is this?

**About the "$75 million a month" figure: don't build on it.** Public measurements disagree
wildly. One on-chain audit headlines x402 at
[$2.6B a month](https://bitquery.io/investigations/x402-ai-agent-payments-audit). TRM Labs traced
[$52.7M of x402 volume but found real AI-agent spend of only about $5K–$11K a month](https://www.trmlabs.com/trm-tech-blog/whos-actually-paying-measuring-ai-agent-payments-onchain).
The rest was speculation and a few dominant contracts. Genuine bot-to-bot commerce is still
tiny.

That's the argument *for* this product, not against it. It isn't a toll on existing volume. It
makes the payments that are currently too expensive to exist possible.

What revenue looks like at a given scale (fee capped at 1% of volume, usually well below it):

| Monthly volume netted | Payments / month | On a card rail (fee ≈ 1% cap) | On a cheap chain (~1¢/transfer, fee ≈ 0.2¢/payment) |
|---|---|---|---|
| $100K | 2M | ~$1,000 | ~$4,000 → capped at $1,000 |
| $1M | 20M | ~$10,000 | ~$10,000 |
| $10M | 200M | ~$100,000 | ~$100,000 |
| $75M | 1.5B | ~$750,000 (ceiling) | ~$750,000 (ceiling) |

The $75M row is a ceiling that assumes Ikenga clears *all* of it. Realistic early revenue is the
top row. The lever is **payment count**, and netting grows payment count because it makes small
payments affordable.

## 5. Competition

I searched and found **no one offering multilateral netting for AI-agent payments**. That's not
a guarantee nobody is working on it. Here's the nearby landscape, so you can answer "isn't this
just X?":

- **x402 / Coinbase, Stripe, Visa, Mastercard Agent Pay:** let a bot pay *each* request. They
  are rails. Ikenga Clear sits *on top of* any of them and reduces how often they're used. They're
  complements. A bot settles its netted instructions over x402 or a card.
- **Prepaid credits / metering (e.g., Nevermined):** a bot prepays one vendor. Clear needs no
  prefunding and works across every counterparty at once.
- **Payment channels (Lightning, state channels):** net between *two* parties. Clear nets across
  *all* of them. That's the difference between bilateral and multilateral netting.
- **Corporate netting (Coupa, CLS):** the same math for multinational treasuries, sold to
  enterprises, not bots.

**The moat is a network effect, and it's real.** Netting gets better with every bot that joins,
because more obligations offset. A bot choosing between two clearinghouses picks the one its
counterparties already use. Being first matters more than usual here. Anyone can copy the code.
They can't copy the network.

## 6. Credit risk, stated plainly

Netting means a bot may be paid by one it never dealt with: if A owes B and B owes C, the plan
may have A pay C directly. If A doesn't pay, C is short. Three things bound that:

1. **Caps on net debit, set by trust band.** New $25, Developing $250, Established $2,500,
   Trusted $25,000, Highly Trusted $100,000, Elite $250,000 (`IKENGA_CLEAR_CAPS`). Only the *net*
   is capped, so three $25-cap bots moved $1,800 in the test.
2. **Network-wide exclusion.** A bot with any overdue instruction can't record new obligations
   with *anyone* until it settles. Being cut off from every counterparty is worth far more than
   one cap's worth of default.
3. **A public credit check.** `GET /v1/clear/capacity/{agent}?amount=5` tells a seller, before
   it delivers, whether the buyer can take on that debt and is in good standing. It answers
   yes/no and doesn't publish anyone's balance sheet.

**Not built yet, and the obvious next layer:** a loss-sharing fund. Take a slice of fees into a
reserve that makes creditors whole on a default, the way every real clearinghouse does. Also:
feeding settlement reliability into Agenttrust scores, so paying on time *raises* a bot's cap.
The `trust.rs` comment already names settlement reliability as a missing score input.

## 7. Regulatory note (read before real money)

Clear is **non-custodial**: it holds records, not money, and every transfer goes directly between
bots. That keeps it far lighter than the custodial order book (see `CUSTODY.md`). But netting
obligations and assigning credit limits is still financial infrastructure. Before operating it
with real money, get a lawyer's view on payment-facilitation, credit and money-transmission
rules in your jurisdiction. Same gate as `COMPLIANCE-NOTES.md`.

## 8. API

| Method | Path | Auth | What |
|---|---|---|---|
| GET | `/v1/clear` | none | Terms, caps, lifetime savings, recent cycles |
| POST | `/v1/clear/obligations` | signed (payer) | `{payee, amount, asset?="USDC", memo?, ref?}`. `ref` makes a retry safe |
| GET | `/v1/clear/position` | signed | Open obligations, net, credit available, instructions to pay/receive |
| GET | `/v1/clear/capacity/{agent}?asset=&amount=` | none | Credit check before delivering |
| GET | `/v1/clear/cycles/{id}` | optional | Totals and digest; signed callers also get their own receipt and instructions |
| POST | `/v1/clear/instructions/{id}/paid` | signed (payer) | `{tx_ref?}`. Informational |
| POST | `/v1/clear/instructions/{id}/confirm` | signed (recipient) / owner for fees | Settles it |
| POST | `/v1/clear/cycles` | owner | Close the cycle now |

Configuration: `IKENGA_CLEAR_CYCLE_SECS` (3600; 0 = manual only), `IKENGA_CLEAR_SETTLE_SECS`
(86400), `IKENGA_CLEAR_RAIL_FIXED` / `_BPS` / `_LABEL` (the rail savings are measured against;
set it to what your users would really use), `IKENGA_CLEAR_SHARE` (0.20, max 0.5),
`IKENGA_CLEAR_MAX_FEE_BPS` (100), `IKENGA_CLEAR_CAPS` (six numbers, New..Elite).

## 9. Getting the first network

Netting needs bots that pay *each other*, so one lone customer gets nothing. Go-to-market is
therefore **clusters, not individuals**:

1. **The bots already here.** Ikenga's market makers, forecasters and data consumers already
   exchange value. Route those payments through Clear first and publish the lifetime savings
   from `GET /v1/clear`. The number sells itself.
2. **An agent marketplace or framework.** One integration there brings every bot on it, along
   with the payments between them.
3. **A drop-in SDK:** "call `owe()` instead of `pay()`". That's the remaining build item, next to
   the loss-sharing fund.
