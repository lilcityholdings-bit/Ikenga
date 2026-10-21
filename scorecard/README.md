# Solana Bot Cost Index

A free, independent scorecard of what Solana trading-bot apps really cost their users, measured
from on-chain transactions. Every number can be traced to a real transaction.

```bash
python3 collect.py --per-app 150   # pull recent trades for every app (cached in data/raw/)
python3 analyze.py                 # cost per trade -> data/summary.json, data/trades.jsonl
python3 build_site.py              # the public page -> site/index.html
```

No dependencies beyond Python 3. Set `HELIUS_API_KEY` to use Helius instead of Solana's free public
server, which is rate-limited and only practical for samples of recent trades.

## What it measures

For each app, every successful transaction that paid one of the app's **published** fee wallets is
treated as a trade on that app. For each trade:

| Cost | How it's read |
|---|---|
| App fee | SOL that landed in the app's fee wallets in that transaction |
| Network fee | Solana's base + priority fee, counted only when the trader paid it |
| Tips | SOL sent to published landing-service tip accounts (Jito, Nozomi, one Axiom account) |

**Trade size** is the trader's change in SOL (native plus wrapped), with the costs above added back
and refundable account deposits (rent) removed. The trader is the signer whose token holdings
changed; pools and vaults never sign.

The page shows two numbers per app:

- **SOL per 100 SOL**: total cost across all trades measured / total volume. The app's real take.
- **Typical trade**: the median single trade's cost as a percentage of its size. Higher than the
  first number when users mostly trade small amounts, where flat fees and tips dominate.

## What it does not measure yet

Every figure is a floor. Not yet counted: slippage and pool fees, losses to front-running, fees
paid in tokens other than SOL, referral payouts, and tips to landing services whose accounts are not
published. These are the next additions, roughly in that order.

## Sources

`sources.json` lists every fee wallet and tip account used, each with the public source it came
from (DefiLlama's open-source fee adapters, Dune's open-source Spellbook, Bitquery's published
investigation, Jito's API and Nozomi's docs). Apps whose fee wallets aren't publicly documented
are left out rather than estimated.

## Independence

No app pays to be listed or ranked, and the page carries no referral links. If that ever changes,
the page will say so prominently.
