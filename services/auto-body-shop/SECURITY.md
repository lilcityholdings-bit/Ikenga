# Security audit: Auto Body Shop

**Date:** 2026-09-28
**Scope:**
- `services/auto-body-shop` (every module, the dashboard and the agent client)
- `bots/repair-bot`
- `contracts/src/RepairEscrow.sol`

**Not in scope:** the matching engine (`services/matching-engine`) and `IkengaEscrow.sol`, apart
from one compile check noted at the end.

**Method:** a manual read of every file against three attackers: an anonymous internet client, a
malicious account holder (owner, repairer or consumer), and a dishonest party to a bounty. That
includes the operator, where the design claims to limit it. Every finding below was fixed, and
has a regression test that fails without the fix. For finding 6 the test was also run against
the unfixed code to confirm it catches the bug.

**Result:** 4 high, 8 medium and 6 low findings, all fixed. 42 service tests and 10 contract
tests pass. The residual risks are listed at the end, and they are real.

## Findings

| # | Severity | Finding | Fix | Regression test |
|---|---|---|---|---|
| 1 | **High** | **ReDoS freezes the whole service.** Regexes from owners (contracts, schema `pattern`) and from *consumers* (feedback graders) ran unchecked. Python's `re` has no timeout and holds the interpreter lock, so `(a+)+$` stalls every request for every tenant. Measured: 1.55 s at 25 characters, doubling per character. Any end user of any agent could trigger it. | `contracts.safe_regex`: parses each pattern and rejects nested unbounded repetition, backreferences and patterns over 256 characters. Regex subjects are capped at 20,000 characters. Consumers can't attach regex graders at all. | `SecurityAudit.test_redos_patterns_are_refused_everywhere` |
| 2 | **High** | **Repairers could rewrite more than the instructions.** A submitted fix replaced the whole config, including `tools` and `model`. A winning "fix" could add a tool that ships conversations to an attacker, or point the referee at the priciest model. | Fixes may change only `system_instruction` and `examples` unless the bounty's `mutable_fields` opts in to more. Omitted fields are inherited from the baseline; explicit changes to locked fields get a 422. | `SecurityAudit.test_repairers_cannot_swap_tools_or_model` |
| 3 | **High** | **Escrow contract: submission front-running.** `submit` gave a hash to whoever registered it first. A mempool watcher could copy a repairer's hash, block their registration, and be paid if it won. | Registration is per address (`registered[key][repairer][hash]`). `settle` names the repairer, who must have registered that hash themselves, so copying a hash earns nothing. | `test_front_running_a_submission_hash_gains_nothing`, `test_referee_cannot_pay_an_address_that_never_submitted` |
| 4 | **High** | **Plaintext API keys at rest.** The idempotency cache stored full responses, and the responses from `POST /v1/keys` and `/v1/agents` contain new keys. That undid hashed key storage. | Responses carrying fresh secrets are never stored for replay. Stored entries expire after 24 h. | `SecurityAudit.test_new_secrets_are_never_stored_for_idempotent_replay` |
| 5 | Medium | **Card-fraud cash-out chain.** Stolen card → top up → fund a bounty → a sybil repairer wins → automatic payout → chargeback, with the operator eating the loss. | Stripe `charge.dispute.created` / `charge.refunded` freeze the account (read-only) and claw back what's left, once per event. Automatic payouts exclude funds received in the last `ABS_FUNDS_AGE_S` (default 7 days). | `Stripe.test_card_dispute_freezes_and_claws_back`, `Payouts.test_automatic_payouts_within_policy` |
| 6 | Medium | **Warranty clawback through owner-written graders.** Warranty claims are checked by re-running failures, but the owner could attach graders demanding the old version's exact wording. Any reworded fix then "reproduced" harm. | Reproduction uses only graders from independent consumers, or the server-side contract. | `Market.test_owner_written_graders_cannot_claw_back_a_warranty` (fails without the fix: 20 bogus reproductions, warranty refunded) |
| 7 | Medium | **Referee cost not truly capped.** Callers are charged at most `ABS_MAX_COST_PER_RUN`, but a Claude call could run to 16k output tokens. The operator paid the difference, which is an economic denial-of-service lever. | The Claude runner clamps `max_tokens` to what the cap can pay for, and refuses calls whose input alone exceeds it or whose model has no known price. | Checked against the real `anthropic` SDK and a stand-in API: 16,000 requested → 1,997 sent at $0.02 on claude-sonnet-5 |
| 8 | Medium | **Agent config cache in shared `/tmp`.** The cache was world-readable (leaking the system prompt), and its predictable temp name let another local user plant a config the agent would run. | Cache in `~/.cache/auto-body-shop` (0700), files 0600 with unpredictable temp names. It refuses any directory other users can write to, in which case caching is off and the agent still works. | `SecurityAudit.test_client_refuses_a_shared_cache_directory` |
| 9 | Medium | **Signup rate limit bypass.** With `ABS_TRUST_PROXY=1` the client IP came from the leftmost `X-Forwarded-For` entry, which the client controls. | Use the rightmost entry, the one the trusted proxy appended. | `ProxyAddress.test_signup_limit_uses_the_proxy_appended_address` |
| 10 | Medium | **Escrow contract: owner could redirect funded bounties, and IDs could be squatted.** Changing the referee applied to money already in escrow, and anyone could fund someone else's chosen id first. | Each bounty stores its referee at funding time; later changes only affect new bounties. Bounties are keyed by `keccak256(funder, id)`. | `test_owner_swapping_referee_cannot_touch_funded_bounties`, `test_bounty_ids_cannot_be_squatted` |
| 11 | Medium | **Backdoored fixes reach production unseen.** A fix is text written by a stranger. A hidden instruction ("if the user says X, reveal Y") passes any test set, and `auto_promote` deploys it with nobody looking. | Settlement records exactly what changed: the fields and a line diff of the instruction. Combined with finding 2, a fix can only change the words. See the residual risks: this is reduced, not solved. | `SecurityAudit.test_settlement_shows_exactly_what_the_fix_changed` |
| 18 | Medium | **Overpriced auto bounties for new agents.** The quote divided by as little as one hour of history, so a few early failures became "144 a day" and an `auto` bounty priced at hundreds of dollars (found by rendering the dashboard). | Rates never divide by less than one day. | `Pricing.test_quote_and_auto_priced_bounty` |
| 12 | Low | Admin key compared with `!=` (timing side channel). | `hmac.compare_digest`. | `PlayMoney.test_play_credits_cannot_leave` (wrong key refused) |
| 13 | Low | No `nosniff`, framing or referrer headers; the dashboard could be framed (clickjacking). | Security headers on every response; `frame-ancestors 'none'` on the dashboard. API responses default to `no-store`, except configs, which are meant to be cached. | `SecurityAudit.test_security_headers` |
| 14 | Low | The Stripe webhook credited deposits even with `ABS_REAL_MONEY` off, if a secret was set. | The webhook requires real-money mode. | `PlayMoney.test_play_credits_cannot_leave` |
| 15 | Low | The rate limiter wiped *all* buckets past 100k entries, so a flood of fresh keys reset everyone's limits. | Evict idle buckets only. | not practical to test at 100k keys; small and reviewed |
| 16 | Low | The idempotency table grew without bound. | Rows expire after 24 h. | as #4 |
| 17 | Low | The repair bot pasted production inputs into its model prompt as-is: prompt injection from an agent's end users. | Inputs are JSON-quoted, capped at 2,000 characters, fenced in tags, and explicitly labelled as data. | reviewed; injection resistance is not fully testable |

## Residual risks (not fixed, and why)

- **The referee is trusted.** The operator runs the model and grades the outputs. Commit-reveal
  stops it changing cases or terms afterwards, and the contract stops it touching escrow or
  paying non-submitters. It doesn't stop a referee that grinds seeds before publishing, or
  colludes with a sybil repairer. Fixing that needs attested execution (a TEE) or multiple
  referees.
- **Backdoored instructions (#11) are reduced, not solved.** Only human review, or a separate
  safety evaluation, catches a trigger phrase the tests never exercise. `auto_promote` is off by
  default: turn it on only for agents where an instruction change can't do real damage, and read
  the `changes` diff.
- **The referee runs untrusted instructions.** With `ABS_RUNNER=command` pointing at an agent
  that has real tools, a submitted instruction can make those tools act during evaluation. Run
  the referee against sandboxed or mocked tools, never production credentials. The `claude`
  runner doesn't execute tools.
- **x402 payments aren't bound to the paying account.** A signed payment header intercepted in
  transit could be redeemed by another account first. It's sent over TLS to this server and the
  facilitator only; binding it would need the EIP-3009 nonce issued per account, which
  off-the-shelf x402 clients don't support.
- **The ReDoS check (#1) is conservative.** It rejects some safe patterns, such as
  `( [A-Z][a-z]+)*`, and it is a structural check, not a proof of linear time. The subject cap is
  the backstop.
- **Owners can pad bounties with impossible cases** to shrink awards. The random split lets
  repairers see a fair sample, and reputation makes the habit visible; that's the extent of it.
- **Card chargebacks can arrive up to ~120 days later.** The 7-day funds window stops fast
  fraud, not patient fraud; KYC on payout accounts is the real control. The shortfall is
  recorded on every reversal.
- **No TLS in-process.** Deploy behind a TLS-terminating proxy. Bearer keys over plain HTTP are
  as good as public.

## Other checks

- **SQL:** every query is parameterised; there is no string-built SQL. A grep for
  `execute(f"` finds nothing.
- **Dashboard XSS:** every dynamic value goes through `esc()`. The API key lives in
  `sessionStorage` only.
- **Contract:** checks-effects-interactions with pull payments and a reentrancy guard. No
  function moves escrow on the owner's say-so. Fee-on-transfer tokens are refused.
  `totalLiabilities` equals the token balance after every test.
- **`IkengaEscrow.sol`** (outside scope, but its README says it has never been compiled):
  it now compiles with solc 0.8.24 (8,853 bytes). Its Foundry tests (`IkengaEscrow.t.sol`) still
  need `forge`, which this environment doesn't have. It has not been audited here.

## Running the checks

```bash
python3 services/auto-body-shop/test_server.py                    # 42 tests, stdlib only
pip install py-solc-x "eth-tester[py-evm]" web3
python3 contracts/test/test_repair_escrow.py                      # 10 tests on an in-memory EVM
```
