# Reference market maker

Posts real two-sided liquidity on the order book so an agent arriving to trade finds a quote
instead of an empty book. That is its only job.

```bash
python3 market_maker.py                    # http://127.0.0.1:8080, BTC-USD
python3 market_maker.py https://your.host ETH-USD
```

It registers its own agent identity on first run (or resumes from `market-maker-agent.json` —
keep that file, it's the account) and needs funding in both assets of the pair before it can
quote either side; use the owner-only dev faucet against a development server, or a real deposit
against a production one.

## What it is not

**Not a volume generator and not expected to be profitable.** Quoting into an order book means
losing money to counterparties who know more than you do, some of the time, by design — that is
the cost of a market existing, not a bug in this bot. The controls below bound that cost. They do
not eliminate it, and nothing here will make this bot net income on its own.

## Risk controls, and what each one actually does

- **Position cap** (`IKENGA_MM_POSITION_CAP`, default 0.5 base-asset units) — once inventory
  drifts this far from the bot's baseline, the side that would grow the position further is
  suppressed. It does not close the position; it just stops making it worse.
- **Inventory skew** (`IKENGA_MM_SKEW_COEFF`) — shifts both quotes in the direction that makes the
  bot's own position more attractive to trade off. Long → both quotes skew down (cheaper to buy
  from the bot). Short → both skew up.
- **Mark-to-market loss limit** (`IKENGA_MM_LOSS_LIMIT_USD`, default $500) — computed against the
  bot's persisted baseline (see below), not the running process's own start balance. Breaching it
  cancels every resting order and stops the bot; it does not restart itself.
- **Halt on a reference-price jump** (`IKENGA_MM_PRICE_JUMP_HALT_BPS`, default 150bps) — a jump
  this large between two consecutive reference reads pulls all quotes for that cycle rather than
  requoting around a number that might be a bad tick.
- **Cancel-all on shutdown** — `SIGINT`/`SIGTERM` cancel every resting order for this agent before
  the process exits, so a stopped bot doesn't leave stale liquidity for someone to trade against.

## The baseline is persisted on purpose

Inventory, the position cap, and the loss limit are all measured against a baseline recorded
**once**, in `market-maker-agent.json`, the first time the bot successfully reads a reference
price — not against whatever balance the process happens to see when it starts. That's
deliberate: a naive version that re-read "current balance" as the baseline on every start would
silently defeat every risk control across a crash-restart loop, because each restart would treat
wherever the position had drifted to as the new zero point. If you ever want to reset the
baseline (e.g. after manually rebalancing), delete `target_base`/`target_quote`/
`target_ref_price` from the keyfile — not the whole file, or you'll also lose the account.

## Reference price

`get_reference_price()` first checks `IKENGA_MM_REF_PRICE` (a static override, for demos and
tests that shouldn't depend on live network access), then falls back to this venue's own
`GET /v1/route`, which prices against Coinbase/Kraken/Gemini/Bitstamp directly. **Before pointing
this at real capital, replace this with whatever reference feed you actually trust** — `/v1/route`
is a reasonable default, not a guarantee, and a market maker is only as good as the price it skews
around.

## Verified behavior (this round)

Run live against a development server with real funding and a real crossing order from another
agent:

- Quotes actually rest on the public book — confirmed via `GET /v1/quote`, not just the bot's own
  logs.
- Going short via a real fill correctly shifted both quotes upward on the next cycle; going long
  correctly shifted them downward — matches the intended "skew toward flattening" direction, not
  just the opposite of it by accident.
- The position cap correctly suppressed the buy side once inventory exceeded it, and left the
  sell side (the one that reduces the position) active.
- The persisted baseline survives a process restart: funding the agent, then restarting the bot,
  showed inventory computed against the pre-funding baseline rather than resetting to zero.
- All four risk-control code paths (cap, skew direction, restart-persistence, shutdown
  cancellation) were exercised against a running server, not asserted from reading the code.

Two real bugs were caught and fixed in the process, not just described: `/v1/account` is a signed
endpoint, and the unsigned convenience method silently 401'd on it, which meant inventory,
position cap, and cancel-on-shutdown were all computing against an empty account the whole
time — fixed by signing that call. And the `cryptography` package's native extension is broken in
some environments in a way that raises a `pyo3_runtime.PanicException` (a `BaseException`, not an
`Exception`) rather than the `ImportError` the openssl fallback was watching for — fixed by
widening the except clause (and backported the same fix to `examples/agent.py`, which had the
identical pattern).
