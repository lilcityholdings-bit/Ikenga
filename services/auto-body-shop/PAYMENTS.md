# Payment strategy

How money moves in the repair market, who pays for what, how prices are set, how the operator
earns, and how to go from play credits to real money without the operator becoming an
unlicensed bank. Every mechanism named here is implemented and tested unless it says otherwise.

## Principles

1. **Pay for outcomes.** Agent vendors already price per outcome (Intercom's Fin charges $0.99
   per resolved conversation). A repair should be priced the same way: by the failures it
   prevents, and paid only when the prevention is verified. That means hidden tests first, then
   production (the warranty).
2. **Nobody pays for an estimate.** Model costs are metered per call. Payers are pre-authorised
   for a maximum (a *hold*); the actual cost plus a disclosed margin is captured, and the rest
   is released, the way card pre-authorisation or x402's `upto` scheme works.
3. **Bots pay without asking, inside limits a human set once.** A *mandate* sets the purpose,
   per-payment cap, rolling 24-hour cap, agent and expiry (the idea of AP2's intent mandates).
   Inside it, payments just happen; outside it they're refused with the reason logged, never
   queued.
4. **Hold as little as possible, for as short a time as possible.** Escrow exists per bounty
   and per warranty, and nowhere else. The target end state is escrow the operator can't touch
   at all (the on-chain contract below).
5. **Match the rail to the payer.** Businesses pay by card; bots pay in USDC over HTTP. Money
   goes out through licensed partners, never ad hoc.

## Who pays whom, for what

| Flow | Payer → payee | When | Mechanism |
|---|---|---|---|
| Bounty reward | owner → escrow → winning repairer | posted by owner or agent (mandate `bounty`); settled automatically | `escrow:bounty:<id>` |
| Warranty | part of the award, held → repairer (or back to owner on *reproduced* harm) | after the canary, or when the warranty period ends | `escrow:warranty:<id>` |
| Baseline scoring | owner → operator | when the bounty opens | hold, then metered capture |
| Submission scoring | repairer → operator | each submission (mandate `eval_fee`) | hold, then metered capture |
| Warranty check | from the warranty → operator | only if a claim is made | metered, capped at the warranty |
| Platform take | from the award → operator | at settlement (none on self-repair) | `ABS_TAKE_BPS`, 10% by default |
| Unused reward | escrow → owner | at settlement: `reward − award` | no cure, no pay |

## Pricing: the agent prices its own repair

The owner states one number: **what a single failure costs them** (`policy.value_per_failure`:
a refund, a human escalation, a lost customer). The shop measures the verified failure rate,
so the price follows:

```
savings (payback period) = value_per_failure × failures/day × payback_days × threshold
bounty                   = savings × offer_share                    (then clamped to min/max)
```

`GET /v1/agents/{id}/quote` shows the whole calculation. `auto_bounty.amount: "auto"` posts at
that price, and the inputs to the price are part of the committed terms.

## Worked example

An agent runs 2,000 times a day with a 4% verified failure rate (80 failures a day). Each
failure costs the owner $0.50, so failures cost **$40/day**.

- **Bounty** (threshold 0.5, payback 30 days, offer share 0.5): savings = 40 × 30 × 0.5 = $600,
  so the bounty is **$300**.
- **Scoring cost** (claude-sonnet-5, about 1,500 input + 300 output tokens a run = $0.006): a
  bounty with 80 failures, 50 guards and 20 regressions is 150 runs = $0.90 of model time,
  charged at $1.08. The owner pays that once for the baseline; each submission pays it again.
- **Full award, 5 repairers × 3 submissions:**

| | Owner | Winning repairer | Operator |
|---|---|---|---|
| Pays | $300 reward + $1.08 baseline | 3 × $1.08 scoring + its own model costs | model costs at cost |
| Gets | failures at half the rate: ~$20/day saved, for as long as the fix holds; $600 in the first 30 days | $270 net ($189 now, $81 when the warranty clears) | $30 take + $2.88 compute margin ≈ **$32.88** |

The owner pays $301 for $600 of savings in a month, and the fix keeps paying after that. A
losing repairer's cost is about $3 plus its own model calls: cheap enough to try, expensive
enough to deter spam. The operator earns only when a repair works, apart from a small margin on
compute it actually ran. It makes nothing from holding float.

## Rails, by phase

**Phase 0: play credits (live now, no licence needed).** Operator grants credits that can't be
withdrawn (`ABS_REAL_MONEY` off). The whole market runs, so it can prove liquidity and repair
quality before any real money is at risk.

**Phase 1: real money through licensed partners** (`ABS_REAL_MONEY=1`; turning on grants off).
- *Businesses pay in* with Stripe Checkout (`POST /v1/deposits/stripe`, signed webhooks, 1 cent
  = 10,000 units). Disputes and refunds freeze the account and claw back what's left.
- *Bots pay in* with x402 v2 (`POST /v1/deposits/x402`): USDC on Base, verified and settled by a
  facilitator (e.g. Coinbase CDP).
- *Payouts* go through `ABS_PAYOUT_CMD`, an adapter for a licensed provider: Stripe Connect
  (fiat, and stablecoin payouts, in private preview for US platforms) or a Coinbase CDP wallet.
  A payout is automatic only if **all** of these hold:
  - the operator has verified the account (KYC);
  - the destination was registered at least 24 h ago;
  - the amount is within the per-payout and daily caps;
  - the money has been in the account at least 7 days.
  Anything else waits for a human. Withdrawals always go to the registered destination.
- *Legal:* holding one party's money to pay another is money transmission. Structure Phase 1 so
  the partner holds the funds (Stripe Connect separate charges and transfers, or a licensed
  custodian), and get counsel before launch. See `docs/COMPLIANCE-NOTES.md`.

**Phase 2: escrow the operator can't touch** (`contracts/src/RepairEscrow.sol`, compiled and
tested, not audited).
- Bounty rewards sit in a contract on Base. The referee can only split them by the same formula
  as the shop.
- The take is capped at deployment, and only addresses that registered a submission in time can
  be paid.
- Funds refund automatically if the referee disappears, and the warranty defaults to the
  repairer after a grace period.
- The operator then holds no customer money for the crypto flow, only its fee. That's the
  strongest answer to the licensing problem, but the contract needs an external audit and a
  testnet run first.

## What protects the money

Mandates with net-of-release daily caps; holds and capture; commit-reveal scoring;
sealed submissions; warranty checked by reproduction; idempotency keys, so a retried payment is
charged once; the double-entry ledger that `/health` requires to sum to zero; card-dispute
freezes; and the payout rules above. `SECURITY.md` lists what was attacked and what is still a
residual risk.
