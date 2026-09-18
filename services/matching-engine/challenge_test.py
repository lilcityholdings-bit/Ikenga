#!/usr/bin/env python3
"""Head-to-head challenges: a bet only goes live when someone takes the other side.

    cargo build --release && python3 challenge_test.py

A pool pays winners out of the losers' money, so alone in one you get your own stake back and
nothing else. A challenge is the honest shape for that: you say what you think, you put money up,
and nothing is live until somebody disagrees enough to fund the other side.

What is pinned here is the money. An offer holds the proposer's stake from the moment it is
posted, so nothing on the board is an offer its author cannot honour. Nobody takes it, they get
every unit back and the house earns nothing. Somebody does, and both stakes are locked into an
ordinary market that settles through the same engine as everything else.
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

HOST, PORT = "127.0.0.1", 8340
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
        "IKENGA_REAL_MONEY": "1",
        "IKENGA_ROUTE_DEMO": "1",
        "IKENGA_SWEEP_INTERVAL_MS": "1000",
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










def price_rule():
    return {"kind": "price_threshold", "symbol": "BTC-USD", "comparator": "above",
            "threshold": 1.0, "if_true_outcome": 0, "if_false_outcome": 1}


def offer(seed, agent, my_stake, their_stake, closes_in_ms=120_000, expires_in_ms=None,
          my_outcome=0, question="Will BTC be above 1 dollar?"):
    now = int(time.time() * 1000)
    body = {
        "question": question,
        "my_outcome": my_outcome,
        "my_stake": my_stake,
        "their_stake": their_stake,
        "closes_at_ms": now + closes_in_ms,
        "observed_at_ms": now + closes_in_ms + 60_000,
        "dispute_window_ms": 60_000,
        "expires_at_ms": now + (expires_in_ms if expires_in_ms is not None else closes_in_ms),
        "resolution": price_rule(),
    }
    return call("POST", "/v1/challenges", seed=seed, agent_id=agent, body_obj=body)


WAL = "/tmp/ikenga-challenge-test.wal"
if os.path.exists(WAL):
    os.remove(WAL)

proc, lines = start_server()
if not any("listening on" in l for l in lines):
    print("server failed to start:\n" + "\n".join(lines))
    sys.exit(1)

seed_a, agent_a, _ = register()
seed_b, agent_b, _ = register()
seed_c, agent_c, _ = register()

# ---------------------------------------------------------------------------------------------
print("=== 1. posting an offer takes your money, so the board is real ===")
before = points(seed_a, agent_a)
st, r = offer(seed_a, agent_a, 100, 100)
check("an offer can be posted", st == 201, (st, r))
chal = r["challenge"]["challenge_id"] if st == 201 else None
after = points(seed_a, agent_a)
check("the proposer's stake is taken immediately", abs((before - after) - 100) < 1e-9,
      (before, after))
check("nothing has settled, so no market exists yet",
      r["challenge"]["market_id"] is None, r["challenge"])

st, listing = call("GET", "/v1/challenges")
check("it is listed for someone to take", any(c["challenge_id"] == chal
      for c in listing["challenges"]), listing["open"])
c0 = next(c for c in listing["challenges"] if c["challenge_id"] == chal)
check("the taker can see exactly what they must put up",
      c0["open_side"]["stake_required"] == 100.0, c0["open_side"])
check("and what the whole pot would be", c0["pot"] == 200.0, c0)

st, body = offer(seed_a, agent_a, 1e9, 100)
check("you cannot offer a bet you cannot cover", st == 402, (st, body))

# ---------------------------------------------------------------------------------------------
print()
print("=== 2. nobody can take their own bet ===")
st, body = call("POST", f"/v1/challenges/{chal}/accept", seed=seed_a, agent_id=agent_a,
                body_obj={})
check("taking your own side is refused", st == 409, (st, body))
check("and it says why", body.get("code") == "CANNOT_ACCEPT_OWN_CHALLENGE", body)

# ---------------------------------------------------------------------------------------------
print()
print("=== 3. taking the other side makes it live ===")
b_before = points(seed_b, agent_b)
st, taken = call("POST", f"/v1/challenges/{chal}/accept", seed=seed_b, agent_id=agent_b,
                 body_obj={})
check("someone can take the other side", st == 200, (st, taken))
mkt = taken.get("market_id")
check("and it becomes a real market", bool(mkt), taken)
check("the taker's stake is taken too",
      abs((b_before - points(seed_b, agent_b)) - 100) < 1e-9,
      (b_before, points(seed_b, agent_b)))

st, detail = call("GET", f"/v1/markets/{mkt}")
check("the market holds both stakes", abs(detail["total_pool"] - 200.0) < 1e-9, detail.get("total_pool"))
check("one on each side",
      detail["outcomes"][0]["pool"] == 100.0 and detail["outcomes"][1]["pool"] == 100.0,
      detail["outcomes"])
check("its terms are hash-committed like any other market",
      len(detail["commitment_sha256"]) == 64 and detail["commitment_intact"] is True)

st, body = call("POST", f"/v1/challenges/{chal}/accept", seed=seed_c, agent_id=agent_c,
                body_obj={})
check("a second taker is refused — it is already matched", st == 409, (st, body))

st, acct = call("GET", "/v1/account", seed=seed_a, agent_id=agent_a)
pos = next((p for p in acct["positions"] if p["market_id"] == mkt), None)
check("both sides show up as real positions", pos is not None and pos["total_staked"] == 100.0,
      pos)

# ---------------------------------------------------------------------------------------------
print()
print("=== 4. odds are just the two stakes ===")
st, r = offer(seed_a, agent_a, 300, 100, question="Will BTC be above 1 dollar? (3 to 1)")
odds_chal = r["challenge"]["challenge_id"]
check("a lopsided offer is allowed", st == 201, (st, r))
check("the taker is shown what they win per unit risked",
      abs(r["challenge"]["taker_wins_per_unit_risked"] - 3.0) < 1e-9,
      r["challenge"]["taker_wins_per_unit_risked"])
c_before = points(seed_c, agent_c)
st, taken2 = call("POST", f"/v1/challenges/{odds_chal}/accept", seed=seed_c, agent_id=agent_c,
                  body_obj={})
check("it can be taken at those odds", st == 200, (st, taken2))
check("the taker only risked the smaller side",
      abs((c_before - points(seed_c, agent_c)) - 100) < 1e-9, c_before - points(seed_c, agent_c))

# ---------------------------------------------------------------------------------------------
print()
print("=== 5. nobody takes it: refunded in full, house earns nothing ===")
st, dash_before = call("GET", "/v1/dashboard", owner=True)
rake_before = {r["asset"]: r["amount"] for r in dash_before["revenue"]["by_asset"]}

a_before = points(seed_a, agent_a)
st, r = offer(seed_a, agent_a, 250, 250, closes_in_ms=120_000, expires_in_ms=3_000,
              question="An offer nobody will take")
lonely = r["challenge"]["challenge_id"]
check("posting it took the money", abs((a_before - points(seed_a, agent_a)) - 250) < 1e-9,
      a_before - points(seed_a, agent_a))

time.sleep(7)  # past expiry, and several sweeper passes
check("the proposer is refunded in full",
      abs(points(seed_a, agent_a) - a_before) < 1e-9,
      (a_before, points(seed_a, agent_a)))

st, listing = call("GET", "/v1/challenges")
check("and it is off the board", not any(c["challenge_id"] == lonely
      for c in listing["challenges"]), listing["open"])

st, body = call("POST", f"/v1/challenges/{lonely}/accept", seed=seed_b, agent_id=agent_b,
                body_obj={})
check("an expired offer cannot be taken", st == 409, (st, body))

st, dash_after = call("GET", "/v1/dashboard", owner=True)
rake_after = {r["asset"]: r["amount"] for r in dash_after["revenue"]["by_asset"]}
check("the house earned nothing from an unmatched offer",
      rake_after.get("PTS", 0) == rake_before.get("PTS", 0), (rake_before, rake_after))

# ---------------------------------------------------------------------------------------------
print()
print("=== 6. pulling your own offer back ===")
a_before = points(seed_a, agent_a)
st, r = offer(seed_a, agent_a, 75, 75, question="An offer to be withdrawn")
pull = r["challenge"]["challenge_id"]
st, body = call("POST", f"/v1/challenges/{pull}/withdraw", seed=seed_b, agent_id=agent_b,
                body_obj={})
check("someone else cannot pull your offer", st == 403, (st, body))
st, body = call("POST", f"/v1/challenges/{pull}/withdraw", seed=seed_a, agent_id=agent_a,
                body_obj={})
check("the proposer can", st == 200, (st, body))
check("and gets it all back", abs(points(seed_a, agent_a) - a_before) < 1e-9,
      (a_before, points(seed_a, agent_a)))
st, body = call("POST", f"/v1/challenges/{pull}/withdraw", seed=seed_a, agent_id=agent_a,
                body_obj={})
check("withdrawing twice does not pay twice", st == 409, (st, body))

# ---------------------------------------------------------------------------------------------
print()
print("=== 7. a matched bet settles and pays like any other market ===")
st, r = offer(seed_a, agent_a, 100, 100, closes_in_ms=4_000,
              question="A challenge that will settle")
settle_chal = r["challenge"]["challenge_id"]
call("POST", f"/v1/challenges/{settle_chal}/accept", seed=seed_b, agent_id=agent_b, body_obj={})
st, ch = call("GET", "/v1/challenges")
st, acct_a = call("GET", "/v1/account", seed=seed_a, agent_id=agent_a)
smkt = next(p["market_id"] for p in acct_a["positions"]
            if p["question"] == "A challenge that will settle")

a_pre, b_pre = points(seed_a, agent_a), points(seed_b, agent_b)
deadline = time.time() + 200
while time.time() < deadline:
    st, d = call("GET", f"/v1/markets/{smkt}")
    if d["status"] in ("Resolved", "Voided"):
        break
    time.sleep(3)
check("it settled on its own", d["status"] in ("Resolved", "Voided"), d["status"])
a_post, b_post = points(seed_a, agent_a), points(seed_b, agent_b)
paid = (a_post - a_pre) + (b_post - b_pre)
check("money moved back to the two sides", paid > 0, paid)
check("payouts never exceeded the pot", paid <= 200.0 + 1e-9, paid)
if d["status"] == "Resolved":
    # Both sides can move, because the early-liquidity rebate hands a sliver of the house's rake
    # back to whoever posted the offer. Only one side is meaningfully paid; the other recovers at
    # most a fraction of a unit and is still down almost its whole stake.
    gains = sorted([a_post - a_pre, b_post - b_pre])
    check("exactly one side actually won", gains[1] > 100.0 and gains[0] < 1.0, gains)
    check("the losing side is still a losing side", gains[0] < 100.0, gains)

st, dash = call("GET", "/v1/dashboard", owner=True)
earned = {r["asset"]: r["amount"] for r in dash["revenue"]["by_asset"]}
check("and the house took its cut of the losing stake only",
      earned.get("PTS", 0) > rake_before.get("PTS", 0), earned)

stop(proc)
if os.path.exists(WAL):
    os.remove(WAL)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("CHALLENGE CHECKS PASSED")
