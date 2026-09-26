# Agent DEX Aggregator — Sepolia Testnet Build

**Status: testnet only. Do not point this at mainnet or real funds — see
"Before real funds" at the bottom.**

An API-first swap aggregator for AI agents. Agents get real prices from
Uniswap V3 (via the on-chain Quoter) and get back an unsigned transaction
they sign with their own wallet — this service never holds agent private
keys or custodies funds.

## What's real vs. still a placeholder

- ✅ Real on-chain price quotes (Uniswap V3 QuoterV2 on Sepolia)
- ✅ Real unsigned swap transaction building (Uniswap V3 SwapRouter02 on Sepolia)
- ✅ API key auth (hashed, SQLite-backed), per-key rate limiting, nonce-based
  replay protection, slippage ceiling, token allow-list
- ⚠️ Token allow-list has only WETH filled in — see `app/tokens.py`, fill in
  the rest with verified Sepolia addresses before demoing multi-token swaps
- ⚠️ No billing/payment collection yet (not needed for a testnet demo)
- ⚠️ SQLite + in-memory nonce store — fine for one instance/testnet, not for
  production scale (see comments in `app/security.py`)

## Setup (local test first)

```bash
cd agent-dex
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: paste in a free Sepolia RPC URL from Alchemy or Infura,
# and generate real random secrets for API_KEY_PEPPER / JWT_SECRET:
python3 -c "import secrets; print(secrets.token_hex(32))"

python3 scripts/create_api_key.py my_first_agent
# copy the printed API key somewhere safe -- it's shown once

uvicorn app.main:app --reload
```

Visit `http://localhost:8000/health` — should show
`{"status": "ok", "network": "sepolia-testnet"}`.

Test a quote:
```bash
curl -X POST http://localhost:8000/quote \
  -H "X-API-Key: YOUR_KEY" -H "Content-Type: application/json" \
  -d '{"token_in": "0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14", "token_out": "0x...", "amount_in_wei": 1000000000000000}'
```

You'll need Sepolia testnet ETH (free from a faucet, e.g.
`sepoliafaucet.com`) in a test wallet to actually sign and broadcast swaps.

## Deploying so it's reachable on the internet tonight

Easiest path (free tier available): **Render** or **Railway**.

1. Push this folder to a new GitHub repo (`git init`, commit, push) —
   do this now regardless of hosting, so you don't lose the code again.
2. On Render: New → Web Service → connect the repo → Render auto-detects
   Python. Set the start command to:
   `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
3. Add your `.env` values as Environment Variables in Render's dashboard
   (never commit the real `.env` file).
4. Deploy. Render gives you a public URL — that's your platform, live.

## Before this ever touches real funds

This is the part not to skip, regardless of how the testnet demo goes:

1. **Security review** of this code by someone other than the person who
   wrote it — ideally a professional smart-contract/backend security
   audit, since swap logic bugs cost real money and this stack has not
   been audited.
2. **Legal review** — money transmitter and securities exposure, even for
   a non-custodial design, varies by jurisdiction and how the fee-taking
   mechanism is structured.
3. Move nonce storage to Redis and API keys to Postgres before any
   multi-instance or production deployment.
4. Fill in and independently verify the rest of the token allow-list —
   never add a token contract address without confirming it against an
   official source.

## Architecture

```
app/
  config.py    # env-driven settings, testnet-only safety switch
  chain.py     # web3 connection, hard-refuses non-Sepolia chain IDs
  security.py  # API keys (SQLite), rate limits, replay/nonce protection
  tokens.py    # allow-listed token contracts (fill in beyond WETH)
  prices.py    # real Uniswap V3 quote via on-chain Quoter
  swap.py      # builds unsigned swap tx; never signs/holds keys
  main.py      # FastAPI routes
scripts/
  create_api_key.py  # issue a new agent API key
```
