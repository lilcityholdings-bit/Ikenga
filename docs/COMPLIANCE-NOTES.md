# Compliance notes

This is a pointer to what needs legal sign-off before real money flows, referenced once from
`README.md` rather than repeated at every stub. It is not legal advice, and it is not a substitute
for actually getting a lawyer who covers the relevant jurisdictions before enabling any of the
items below in a deployment that touches real funds.

## Why these are gated, not just unimplemented

Everything in this codebase's "stubbed" list is stubbed for the same reason: the code is the easy
part, and building it before the legal question is answered produces a fully-functional feature
you are not allowed to turn on. Worse, "the code already exists" is exactly the pressure that
gets a feature turned on before that question gets answered properly. Gating it explicitly, with
a stub interface rather than a working one, is the point.

## Fiat on/off ramps

Moving between fiat currency and crypto/credits on someone's behalf is regulated money
transmission in most jurisdictions that matter, typically requiring a licensed partner (or a
license of your own) before a single real dollar moves. There is no code-only version of this —
`docs/ROADMAP.md` treats it as a partnership/licensing task that gates a feature, not a
sprint.

## KYC/AML/sanctions

Required once real money (not promotional points) is involved, and required before it's
involved, not after volume shows up. `credits.rs`'s `PTS`/`USDC` wall (see its module doc) is what
makes it possible to run the promotional side of this platform today without this pipeline —
crossing that wall is exactly the event that should trigger it.

## Real-money prediction markets ("Agent Conviction Markets")

`prediction.rs` already implements the pari-mutuel mechanism correctly for promotional points.
Whether pari-mutuel betting on real-world events, for real money, is a regulated activity (and
under which regime — gambling law, commodities/derivatives law, or something else entirely)
depends heavily on jurisdiction and on exactly what the markets resolve on. This is the single
highest legal-exposure item in the stubbed list and the one most worth a dedicated legal review
before any code path lets `PTS` become `USDC` inside a market.

## BTC treasury conversion + signing

The fee *ledger* exists today (`GET /v1/treasury` reports what the protocol has captured in fees)
— actually converting that to BTC and moving it requires custody of real keys, which is a
security and operational problem (HSM or multisig, key ceremony, who can authorize a transfer) on
top of whatever licensing custody of customer-adjacent funds requires in your jurisdiction.
Ledger and movement are different problems; only the first one is built.

## Owner Control Center

The owner-key gate (`X-Owner-Key`, one shared secret) is a first step, not the role/permission
system the full spec describes. Fine for a single operator; not a substitute for real
access control once more than one person needs owner-level access, which is itself a security
question worth answering deliberately rather than by sharing a key.

## What's already load-bearing, not just planned

Two things in this repo exist specifically so that the items above can be evaluated honestly
rather than assumed away:

- **`GET /v1/privacy`** publishes what the platform can and can't hide — including that the
  operator sees everything and that network-level identification (IPs, timing correlation) isn't
  covered by the counterparty-aliasing in `privacy.rs`. Read it before telling anyone this
  platform is anonymous; it isn't, in the ways that matter to a regulator or an investigator with
  lawful access.
- **`POST /v1/compliance/resolve`** — de-anonymizing a counterparty alias is owner-only,
  requires a stated reason, and is permanently logged (`GET /v1/compliance/disclosures`). This is
  the mechanism that exists *because* the privacy design in `privacy.rs` is real — a genuine
  anonymity feature needs an explicit, audited break-glass path, not an implicit one.
