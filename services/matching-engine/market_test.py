#!/usr/bin/env python3
"""Pari-mutuel prediction markets, end to end.

    cargo build --release && python3 market_test.py

The properties being pinned here are the business model, not just the code:

  * A bot goes from nothing to a placed bet in two calls, with no deposit and no counterparty.
  * The house earns a rake on every resolved market where participants disagreed — that is the
    revenue, and it is collected without ever taking the other side of a bet.
  * A lone correct forecaster is never made poorer, so there is no penalty for being the first
    user. This is what makes the market launchable with one participant.
  * Money is conserved: payouts plus rake never exceed what was staked.
"""
import base64
import http.client
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

HOST, PORT = "127.0.0.1", 8093
BIN = "./target/release/ikenga-matching-engine"
OWNER_KEY = "dd" * 32
PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")

failures = []


def check(label, ok, extra=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        if extra:
            print(f"       got: {extra}")
        failures.append(label)


def generate_keypair():
    with tempfile.NamedTemporaryFile(suffix=".pem") as f:
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", f.name],
                       check=True, capture_output=True)
        priv = subprocess.run(["openssl", "pkey", "-in", f.name, "-outform", "DER"],
                              check=True, capture_output=True).stdout
        pub = subprocess.run(["openssl", "pkey", "-in", f.name, "-pubout", "-outform", "DER"],
                             check=True, capture_output=True).stdout
    return priv[16:].hex(), pub[12:].hex()


def _pem(seed_hex):
    b64 = base64.b64encode(PKCS8_PREFIX + bytes.fromhex(seed_hex)).decode()
    return ("-----BEGIN PRIVATE KEY-----\n" + b64 + "\n-----END PRIVATE KEY-----\n").encode()


def sign(seed_hex, method, path, ts, nonce, body=b""):
    payload = method.encode() + path.encode() + ts.encode() + nonce.encode() + body
    with tempfile.NamedTemporaryFile() as k, tempfile.NamedTemporaryFile() as m, tempfile.NamedTemporaryFile() as s:
        k.write(_pem(seed_hex)); k.flush()
        m.write(payload); m.flush()
        subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-inkey", k.name, "-rawin", "-in", m.name, "-out", s.name],
            check=True, capture_output=True,
        )
        return s.read().hex()


def call(method, path, seed=None, agent_id=None, body_obj=None, owner=False, _retries=6):
    """Retries on 429. A freshly registered agent sits in the New trust band at 10 req/s, and
    this test bursts well past that during setup — which is the limiter doing its job, not a
    bug. Real integrators will meet the same ceiling on their first minute."""
    for attempt in range(_retries):
        status, body = _call_once(method, path, seed, agent_id, body_obj, owner)
        if status != 429:
            return status, body
        time.sleep(0.35)
    return status, body


def _call_once(method, path, seed=None, agent_id=None, body_obj=None, owner=False):
    raw = json.dumps(body_obj).encode() if body_obj is not None else b""
    h = {}
    if raw:
        h["Content-Type"] = "application/json"
    if owner:
        h["X-Owner-Key"] = OWNER_KEY
    if seed:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        nonce = f"mkt-{time.time_ns()}"
        h.update({
            "X-Agent-ID": agent_id or "registration",
            "X-Timestamp": ts,
            "X-Nonce": nonce,
            "X-Signature": sign(seed, method, path, ts, nonce, raw),
        })
    conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
    conn.request(method, path, body=raw, headers=h)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(data)
    except Exception:
        return resp.status, data


def register():
    seed, pub = generate_keypair()
    status, reg = call("POST", "/v1/agents", seed=seed, body_obj={"pubkey_hex": pub})
    assert status == 201, f"registration failed: {status} {reg}"
    return seed, reg["agent_id"], reg


def points(seed, agent_id):
    status, acct = call("GET", "/v1/account", seed=seed, agent_id=agent_id)
    assert status == 200 and isinstance(acct, dict) and "balances" in acct, \
        f"account lookup failed: {status} {acct}"
    return next((b["balance"] for b in acct["balances"] if b["asset"] == "PTS"), 0.0)


def open_market(question, closes_in_ms=1_000, outcomes=None, dispute_window_ms=0, resolution=None,
                asset=None, observed_offset_ms=1):
    now = int(time.time() * 1000)
    body = {
        "question": question,
        "closes_at_ms": now + closes_in_ms,
        "observed_at_ms": now + closes_in_ms + observed_offset_ms,
        "dispute_window_ms": dispute_window_ms,
        "resolution": resolution or {
            "kind": "operator_declared",
            "criteria": "as reported by the named source at the observation time",
            "source": "example.test/results",
        },
    }
    if outcomes:
        body["outcomes"] = outcomes
    if asset:
        body["asset"] = asset
    status, m = call("POST", "/v1/markets", body_obj=body, owner=True)
    assert status == 201, f"market creation failed: {status} {m}"
    return m["market_id"], m


def settle_now(market_id, outcome=None, void=False, evidence="observed at the source"):
    """Propose then finalize. Markets in this test use a zero dispute window unless stated."""
    body = {"evidence": evidence}
    if void:
        body["void"] = True
    else:
        body["outcome"] = outcome
    st, prop = call("POST", f"/v1/markets/{market_id}/propose", body_obj=body, owner=True)
    assert st == 200, f"propose failed: {st} {prop}"
    return call("POST", f"/v1/markets/{market_id}/finalize", body_obj={}, owner=True)


def wait_for_observation(market):
    """Sleep until the market's committed observation time has passed."""
    remaining = market["observed_at_ms"] / 1000 - time.time()
    if remaining > 0:
        time.sleep(remaining + 0.05)


def start_server():
    env = {
        **os.environ,
        "IKENGA_BIND": f"{HOST}:{PORT}",
        "IKENGA_OWNER_KEY": OWNER_KEY,
        "IKENGA_WAL_PATH": WAL,
        "IKENGA_REGISTRATIONS_PER_HOUR": "500",
    }
    proc = subprocess.Popen(BIN, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = []
    deadline = time.time() + 15
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        lines.append(line.rstrip())
        if "listening on" in line:
            break
    return proc, lines


def stop(proc):
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)


WAL = "/tmp/ikenga-market-test.wal"
if os.path.exists(WAL):
    os.remove(WAL)

proc, lines = start_server()
if not any("listening on" in l for l in lines):
    print("server failed to start:\n" + "\n".join(lines))
    sys.exit(1)

# ---------------------------------------------------------------------------------------------
print("=== 1. nothing to first bet, with no deposit and no counterparty ===")
status, listing = call("GET", "/v1/markets")
check("markets are browsable without an account", status == 200, status)

seed_a, agent_a, reg = register()
check("registration grants points to bet with", reg["starting_points"] == 1000.0, reg.get("starting_points"))
check("the points are actually in the account", points(seed_a, agent_a) == 1000.0, points(seed_a, agent_a))

mkt, mkt_detail = open_market("Will BTC be above 70000 at the observation time?")
check("the market publishes a commitment hash", len(mkt_detail["commitment_sha256"]) == 64,
      mkt_detail.get("commitment_sha256"))
check("and the commitment matches its own terms", mkt_detail["commitment_intact"] is True)
status, staked = call("POST", f"/v1/markets/{mkt}/stakes", seed=seed_a, agent_id=agent_a,
                      body_obj={"outcome": 0, "amount": 100})
check("a brand-new bot can place a bet immediately", status == 200, (status, staked))
check("no counterparty was needed", staked.get("balance_remaining") == 900.0, staked)
check("the market now has an implied probability", staked["market_implied_probabilities"] == [1.0, 0.0],
      staked.get("market_implied_probabilities"))

print("\n=== 2. a lone correct forecaster is not punished for being first ===")
wait_for_observation(mkt_detail)
status, res = settle_now(mkt, outcome=0)
check("market resolves", status == 200, (status, res))
check("the solo winner gets exactly their stake back", res["payouts"][0]["payout"] == 100.0, res["payouts"])
check("the house takes nothing when nobody disagreed", res["rake"] == 0.0, res.get("rake"))
check("balance is whole again", points(seed_a, agent_a) == 1000.0, points(seed_a, agent_a))

print("\n=== 3. the house earns when participants disagree ===")
seed_b, agent_b, _ = register()
seed_c, agent_c, _ = register()
mkt2, mkt2_detail = open_market("Will ETH flip BTC this cycle?")
call("POST", f"/v1/markets/{mkt2}/stakes", seed=seed_a, agent_id=agent_a, body_obj={"outcome": 0, "amount": 100})
call("POST", f"/v1/markets/{mkt2}/stakes", seed=seed_b, agent_id=agent_b, body_obj={"outcome": 0, "amount": 300})
call("POST", f"/v1/markets/{mkt2}/stakes", seed=seed_c, agent_id=agent_c, body_obj={"outcome": 1, "amount": 400})

status, detail = call("GET", f"/v1/markets/{mkt2}")
check("the pooled forecast is visible to anyone", status == 200 and detail["total_pool"] == 800.0, detail.get("total_pool"))
probs = [o["implied_probability"] for o in detail["outcomes"]]
check("implied probabilities reflect the money", abs(probs[0] - 0.5) < 1e-9, probs)

wait_for_observation(mkt2_detail)
status, res2 = settle_now(mkt2, outcome=0)
# Losing pool 400, gross rake 1% = 4, distributable 396.
#   a: 100 + 396*(100/400) = 199 ; b: 300 + 396*(300/400) = 597
# A quarter of the gross rake is then handed back to whoever staked early, so the house keeps 3 and
# each staker's payout is nudged up by its share of the remaining 1. The pro-rata split is checked
# against the pre-rebate figures with a tolerance of that one unit — tight enough that a genuine
# error in the split still fails, loose enough that the rebate is allowed to exist.
check("the house took its 1% rake off the losing pool, less the early-liquidity rebate",
      abs(res2["gross_rake"] - 4.0) < 1e-9 and abs(res2["rake"] - 3.0) < 1e-9,
      (res2.get("gross_rake"), res2.get("rake")))
check("the rebate came out of the rake and nowhere else",
      abs(res2["gross_rake"] - res2["rake"] - res2["early_liquidity_rebate"]) < 1e-9,
      (res2["gross_rake"], res2["rake"], res2["early_liquidity_rebate"]))
payout = {p["agent_id"]: p["payout"] for p in res2["payouts"]}
check("winners split the losing pool pro rata",
      199.0 <= payout[agent_a] <= 200.0 and 597.0 <= payout[agent_b] <= 598.0, payout)
check("the loser gets back at most its share of the rebate, never its stake",
      payout.get(agent_c, 0.0) < res2["early_liquidity_rebate"] + 1e-9, payout.get(agent_c))
check("both winners came out ahead", payout[agent_a] > 100 and payout[agent_b] > 300)

total_out = sum(payout.values()) + res2["rake"]
check("money is conserved (payouts + rake <= pool)", total_out <= res2["total_pool"] + 1e-9,
      (total_out, res2["total_pool"]))
check("and every unit is accounted for, not merely under budget",
      abs(total_out - res2["total_pool"]) < 1e-6, (total_out, res2["total_pool"]))

print("\n=== 4. the rake is real revenue, visible in the treasury ===")
status, treasury = call("GET", "/v1/treasury", owner=True)
pts_fees = next((r["pending_amount"] for r in treasury["pending_fees_by_asset"] if r["asset"] == "PTS"), 0)
check("the rake the house actually kept was booked to the treasury",
      abs(pts_fees - 3.0) < 1e-9, pts_fees)

print("\n=== 5. the things that must not happen ===")
status, dup = call("POST", f"/v1/markets/{mkt2}/finalize", body_obj={}, owner=True)
check("a market cannot be settled twice", status == 409, (status, dup))

status, body = call("POST", f"/v1/markets/{mkt2}/stakes", seed=seed_a, agent_id=agent_a,
                    body_obj={"outcome": 0, "amount": 10})
check("a settled market takes no more stakes", status == 422, (status, body))

mkt3, _ = open_market("A market for the broke", closes_in_ms=120_000)
status, body = call("POST", f"/v1/markets/{mkt3}/stakes", seed=seed_c, agent_id=agent_c,
                    body_obj={"outcome": 0, "amount": 999999})
check("you cannot stake points you do not have", status == 400, (status, body))

status, body = call("POST", f"/v1/markets/{mkt3}/stakes", seed=seed_a, agent_id=agent_a,
                    body_obj={"outcome": 9, "amount": 10})
check("you cannot back an outcome that does not exist", status == 422, (status, body))

status, body = call("POST", f"/v1/markets/{mkt3}/stakes", body_obj={"outcome": 0, "amount": 10})
check("unsigned stakes are rejected", status == 401, (status, body))

# Agents may open markets, but only ones they cannot influence the outcome of. Writing the
# resolution rule is picking the winner, so operator_declared stays operator-only.
now_ms_ = int(time.time() * 1000)
status, body = call("POST", "/v1/markets", seed=seed_a, agent_id=agent_a, body_obj={
    "question": "q", "closes_at_ms": now_ms_ + 1000, "observed_at_ms": now_ms_ + 2000,
    "resolution": {"kind": "operator_declared", "criteria": "c", "source": "s"}})
check("an agent cannot open a market it would resolve itself",
      status == 403 and body.get("code") == "AGENT_MARKETS_MUST_BE_MACHINE_RESOLVED", (status, body))

status, body = call("POST", "/v1/markets", body_obj={
    "question": "q", "closes_at_ms": now_ms_ + 1000, "observed_at_ms": now_ms_ + 2000,
    "resolution": {"kind": "price_threshold", "symbol": "BTC-USD", "comparator": "above",
                   "threshold": 1.0, "if_true_outcome": 0, "if_false_outcome": 1}})
check("and an unsigned caller cannot open one at all", status == 401, (status, body))

print("\n=== 6. a voided market refunds everyone in full ===")
mkt4, mkt4_detail = open_market("Unanswerable question")
call("POST", f"/v1/markets/{mkt4}/stakes", seed=seed_a, agent_id=agent_a, body_obj={"outcome": 0, "amount": 50})
call("POST", f"/v1/markets/{mkt4}/stakes", seed=seed_b, agent_id=agent_b, body_obj={"outcome": 1, "amount": 50})
before_a, before_b = points(seed_a, agent_a), points(seed_b, agent_b)
wait_for_observation(mkt4_detail)
status, res4 = settle_now(mkt4, void=True)
check("void refunds", status == 200 and res4["refunded"] is True, (status, res4))
check("and takes no rake", res4["rake"] == 0.0, res4.get("rake"))
check("both agents are made whole", points(seed_a, agent_a) == before_a + 50 and points(seed_b, agent_b) == before_b + 50,
      (points(seed_a, agent_a), before_a))

print("\n=== 7. stakes survive a crash ===")
mkt5, mkt5_detail = open_market("Does the ledger survive kill -9?", closes_in_ms=3_000)
call("POST", f"/v1/markets/{mkt5}/stakes", seed=seed_a, agent_id=agent_a, body_obj={"outcome": 0, "amount": 75})
pool_before = call("GET", f"/v1/markets/{mkt5}")[1]["total_pool"]
pts_before = points(seed_a, agent_a)
stop(proc)
proc, lines = start_server()
check("server recovered", any("recovered" in l for l in lines), lines[:3])
status, after = call("GET", f"/v1/markets/{mkt5}")
check("the market came back", status == 200 and after["total_pool"] == pool_before, (status, after.get("total_pool")))
check("the stake is still in the pool", after["outcomes"][0]["pool"] == 75.0, after["outcomes"][0])
check("the debited points stayed debited", points(seed_a, agent_a) == pts_before, points(seed_a, agent_a))

wait_for_observation(mkt5_detail)
status, res5 = settle_now(mkt5, outcome=0)
check("a recovered market still settles", status == 200 and res5["payouts"][0]["payout"] == 75.0, res5)

print("\n=== 8. the resolution attack surface ===")
# The Polymarket failure was: an asserted outcome WAS the settlement, and the assertion could be
# bought. Here an assertion only starts a clock, and any agent can stop it.
before_prop_a, before_prop_b = points(seed_a, agent_a), points(seed_b, agent_b)
mkt6, mkt6_detail = open_market("Disputable market", dispute_window_ms=60_000)
call("POST", f"/v1/markets/{mkt6}/stakes", seed=seed_a, agent_id=agent_a, body_obj={"outcome": 0, "amount": 20})
call("POST", f"/v1/markets/{mkt6}/stakes", seed=seed_b, agent_id=agent_b, body_obj={"outcome": 1, "amount": 20})
wait_for_observation(mkt6_detail)

status, early = call("POST", f"/v1/markets/{mkt6}/finalize", body_obj={}, owner=True)
check("cannot finalize before anything is proposed", status == 409, (status, early))

status, prop = call("POST", f"/v1/markets/{mkt6}/propose",
                    body_obj={"outcome": 0, "evidence": "source said YES"}, owner=True)
check("an outcome can be proposed", status == 200, (status, prop))
check("proposing pays out nothing yet", points(seed_a, agent_a) == before_prop_a - 20,
      (points(seed_a, agent_a), before_prop_a))

status, early = call("POST", f"/v1/markets/{mkt6}/finalize", body_obj={}, owner=True)
check("payout is blocked while the dispute window is open", status == 425, (status, early))

status, disp = call("POST", f"/v1/markets/{mkt6}/dispute", seed=seed_b, agent_id=agent_b,
                    body_obj={"reason": "the source says otherwise"})
check("any agent can challenge a proposal, not just the operator", status == 200, (status, disp))

status, blocked = call("POST", f"/v1/markets/{mkt6}/finalize", body_obj={}, owner=True)
check("a challenge freezes the payout", status == 409 and blocked.get("code") == "DISPUTED",
      (status, blocked))

# The safe way out of a dispute: void and refund, rather than adjudicate with money on the line.
status, voided = settle_now(mkt6, void=True, evidence="dispute unresolved — refunding")
check("a disputed market can be voided and refunded", status == 200 and voided["refunded"] is True,
      (status, voided))
check("the challenger got their stake back", points(seed_b, agent_b) == before_prop_b,
      (points(seed_b, agent_b), before_prop_b))

print("\n=== 9. evidence and timing cannot be skipped ===")
mkt7, mkt7_detail = open_market("Evidence required")
wait_for_observation(mkt7_detail)
status, body = call("POST", f"/v1/markets/{mkt7}/propose", body_obj={"outcome": 0}, owner=True)
check("a proposal without evidence is refused", status == 400, (status, body))

mkt8, mkt8_detail = open_market("Too early", closes_in_ms=120_000)
status, body = call("POST", f"/v1/markets/{mkt8}/propose",
                    body_obj={"outcome": 0, "evidence": "peeking"}, owner=True)
check("an outcome cannot be proposed before the observation time", status == 409, (status, body))

now_ms_ = int(time.time() * 1000)
status, body = call("POST", "/v1/markets", owner=True, body_obj={
    "question": "no settlement gap",
    "closes_at_ms": now_ms_ + 10_000,
    "observed_at_ms": now_ms_ + 10_000,  # same instant as close
    "resolution": {"kind": "operator_declared", "criteria": "c", "source": "s"},
})
check("a market with no settlement gap is refused at creation", status == 400, (status, body))

print("\n=== 10. free points can never be cashed out ===")
status, res = call("GET", "/v1/reserves")
check("reserves are public", status == 200, status)
check("real money is off by default", res["real_money_enabled"] is False, res)
check("points are declared non-redeemable", res["promotional_asset"]["redeemable"] is False)

status, body = call("POST", "/v1/withdrawals", seed=seed_a, agent_id=agent_a,
                    body_obj={"asset": "PTS", "amount": 10, "destination": "addr1"})
check("withdrawing promotional points is refused", status == 403, (status, body))
check("and it says why", "no cash value" in (body.get("message") or "") or
      "not redeemable" in (body.get("message") or "").lower(), body)

status, body = call("POST", "/v1/withdrawals", seed=seed_a, agent_id=agent_a,
                    body_obj={"asset": "USDC", "amount": 10, "destination": "addr1"})
check("USDC cannot be withdrawn when real money is disabled", status == 403, (status, body))

stop(proc)
if os.path.exists(WAL):
    os.remove(WAL)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL MARKET CHECKS PASSED")
