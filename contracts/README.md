# Ikenga settlement contract

This is the on-chain half of Ikenga. The Rust engine runs the venue — it opens markets, takes
bets, watches prices and works out who was right. This contract holds the money while that
happens and pays it out afterwards, so that "who was right" and "who gets paid" are decided in
two different places by two different things.

---

## READ THIS FIRST

**This contract has never been compiled.** There is no Solidity compiler and no chain access in
the environment it was written in. It has not been compiled, deployed, run, or audited. The test
suite in `test/` has never been executed.

Carefully written and verified are different things, and the difference is where money is lost.
Before this holds a single real dollar it needs, in this order:

1. `forge build` — it may not even compile.
2. `forge test` — the tests in `test/` are written against the code but have never run.
3. A testnet deployment (Base Sepolia) with real transactions through the whole lifecycle.
4. An audit by someone who does this for a living.

Everything below describes the design as written, not as proven.

---

## 1. What it does, in order

1. The engine opens a market on-chain: `openMarket(marketId, outcomeCount, closesAt, resolveBy)`.
2. Agents stake: `stake(marketId, outcome, amount)`. Tokens move into the contract.
3. The market closes. No more stakes are accepted.
4. The engine declares the winner: `resolve(marketId, winningOutcome)`. **No money moves here.**
   The contract just records the split.
5. Winners call `claim(marketId, you)` then `withdraw()`. The tokens leave.
6. You call `withdrawFees()`. Your rake leaves, to the fee address fixed at deployment.

## 2. The money rules

- **The fee comes out of the losing pool only.** A winner never gets back less than they staked.
  If you bet 100 and win, you get at least 100 back, always.
- **The fee is capped at 5%** (`MAX_FEE_BPS = 500`) and set once, at deployment. Nobody can raise
  it afterwards — not you, not anyone. It is `immutable`, which in Solidity means it is baked into
  the deployed code and there is no function anywhere that writes to it.
- **The fee address is also fixed at deployment.** Fees can only ever go to that one address.
- **If nobody backed the winning side, the market voids** and everyone gets their stake back with
  no fee taken. Otherwise the house would collect the most from questions nobody could answer,
  which is a direct incentive to write bad markets.
- **A voided market takes no fee at all.** Everyone gets back exactly what they put in.

## 3. Who can do what

| Who | Can | Cannot |
|---|---|---|
| **Owner** (you) | change the resolver address, hand over ownership | touch a single token of escrow — there is no function that lets it |
| **Resolver** (the engine's key) | open, resolve and void markets | move money, change the fee, change the owner |
| **Fee recipient** | receive fees (fixed at deploy) | — |
| **Anyone at all** | stake, claim, withdraw, and `expire()` an abandoned market | — |

The important row is the owner one. **There is no owner function that moves escrow.** If your
owner key is stolen, the thief can point the resolver at their own key and start calling wrong
outcomes on future markets — bad, and you would need to redeploy — but they cannot drain the
contract, and money already staked in already-resolved markets is still claimable by the people
who won it. That is the whole reason owner and resolver are separate keys.

There is also **no upgrade path and no proxy**. What is deployed is what runs, forever. That
cuts off the single largest category of contract theft, which is an upgrade that quietly replaces
the payout logic.

## 4. Why claiming and withdrawing are two steps

This is the "pull payment" pattern, and it is the difference between a contract that survives
attack and one that doesn't.

The naive version sends tokens to each winner inside `resolve`. That means resolving a market
makes N external calls, and every one of those calls hands control to a stranger's code. A
malicious winner's contract can call straight back into `resolve` before it has finished
(reentrancy — how the DAO was drained in 2016), or simply revert and make the whole settlement
fail so **nobody** gets paid.

Here, `resolve` moves no money at all. It only records numbers. `claim` credits `owed[you]` — an
internal number, no external call. `withdraw` is the only place a token transfer happens, it
zeroes your balance *before* transferring, it is `nonReentrant`, and it only ever pays the caller.
Reentering it gets you a second transfer of zero.

`claimed[marketId][staker]` is set before anything else, so claiming twice on one market is
refused. There is no loop over stakers anywhere in the contract, so no number of participants can
make settlement run out of gas.

## 5. The escape hatch

Every market carries a `resolveBy` timestamp. If the resolver has not resolved it seven days past
that (`RESOLUTION_GRACE`), **anyone** can call `expire(marketId)` and the market voids — everyone
takes back exactly what they staked.

Anyone, not just participants, on purpose. The point of an escape hatch is that it does not
depend on any particular person still being around or still caring. If the server dies, if you
lose the key, if you get hit by a bus — the money still comes out. Nobody's funds can be held
hostage by an absent operator, including by you.

This is the on-chain mirror of the engine's `FINALITY_BACKSTOP_MS`: everything resolves, one way
or another, within a bounded time.

## 6. Checking it is honest, at any moment

Two view functions anyone can call for free:

- `isSolvent()` — true when the contract's actual token balance is at least what it owes
  (`totalEscrowed + totalOwed + feesAccrued`). If this ever returns false, something is wrong and
  it is visible to everyone immediately.
- `accounting()` — the four numbers behind that, so you can see exactly where the money is.

This is the same reserve invariant the Rust engine enforces off-chain, checkable on-chain by a
stranger.

## 7. Fee-on-transfer tokens

Some tokens take a cut on every transfer, so if you send 100 the contract receives 97. A contract
that credits the amount *requested* rather than the amount *received* would slowly go insolvent
and the last people to withdraw would find nothing there.

`stake` measures the contract's balance before and after the transfer and credits the difference.
USDC does not do this today, but it costs nothing to be right about it and it cannot be fixed
after deployment.

## 8. Deploying it on Base

You need three things before you start: a wallet with a little ETH on Base for gas, the USDC
address on Base, and two keys.

**The two keys matter more than anything else here.**

- **Owner key** — yours. Use it once at deployment and then put it somewhere safe and offline. It
  is never needed day to day.
- **Resolver key** — lives on the server, in `IKENGA_RESOLVER_KEY`. This one is online, which is
  exactly why it must not be the same key as the owner, and why it cannot move money.
- **Fee recipient** — where your rake lands. Make this a wallet you control and *not* the resolver
  key. It is fixed forever at deployment, so get it right.

Deployment (with Foundry, once someone with a computer has compiled and tested it):

```
forge create contracts/src/IkengaEscrow.sol:IkengaEscrow \
  --rpc-url https://mainnet.base.org \
  --constructor-args <USDC_ADDRESS> <RESOLVER_ADDRESS> <FEE_RECIPIENT_ADDRESS> 100
```

The `100` is the fee in basis points — 100 = 1%, the same rake the engine uses off-chain. The
constructor refuses anything above 500.

Do it on **Base Sepolia** first with test tokens and run a full market through it end to end
before touching mainnet.

## 9. How the engine drives it

The engine is the resolver. When on-chain settlement is switched on, the sweeper that today marks
a market resolved in the WAL also sends `resolve(marketId, winningOutcome)` from the resolver key,
and the sweeper that voids sends `voidMarket`. `marketId` is the same hash the engine already
commits to for the market terms, so the on-chain market and the off-chain one are provably the
same market — you cannot resolve one question and claim it was another.

The engine never holds the escrow key and never signs a transfer. The worst a compromised engine
can do is call a wrong outcome, which is visible on-chain to everyone and bounded by the market's
size.

## 10. What is still missing

- The Rust side of the bridge: nothing in the engine sends transactions yet. That is a separate
  piece of work and it needs an RPC endpoint the sandbox cannot reach.
- Compilation, tests, testnet, audit — see READ THIS FIRST.

## Files

- `src/IkengaEscrow.sol` — the contract.
- `test/IkengaEscrow.t.sol` — Foundry tests, organised by attack rather than by function:
  pro-rata splits, winner-never-loses, fee-from-losing-side-only, double claim, double withdraw,
  reentrancy, owner-cannot-move-escrow, fee cap, `expire()` called by a stranger, a solvency fuzz
  test, and a fee-on-transfer token. Never executed.
