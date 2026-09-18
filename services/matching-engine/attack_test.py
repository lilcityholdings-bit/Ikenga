#!/usr/bin/env python3
"""Adversarial tests: each one actually tries an exploit and asserts it fails.

    cargo build --release && python3 attack_test.py

Every check here corresponds to a way a bot could take value out of the venue that it did not
earn. They are written as attacks rather than as assertions about internals, because a defence
that is only tested through the function that implements it is a defence that quietly stops
working the day someone adds a second route to the same behaviour.
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

HOST, PORT = "127.0.0.1", 8098
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






def raw_get(path, headers=None):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def deposit(agent_id, asset, amount):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
    conn.request("POST", f"/v1/deposits/{agent_id}/{asset}", body=str(amount).encode(),
                 headers={"X-Owner-Key": OWNER_KEY})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(data)
    except Exception:
        return resp.status, data


WAL = "/tmp/ikenga-attack-test.wal"
if os.path.exists(WAL):
    os.remove(WAL)
os.environ["IKENGA_REAL_MONEY"] = "1"
os.environ.pop("IKENGA_ENABLE_ORDERBOOK", None)

proc, lines = start_server()
if not any("listening on" in l for l in lines):
    print("server failed to start:\n" + "\n".join(lines))
    sys.exit(1)

seed_a, agent_a, _ = register()
seed_b, agent_b, _ = register()
seed_c, agent_c, _ = register()
seed_d, agent_d, _ = register()

# ---------------------------------------------------------------------------------------------
print("=== ATTACK 1: deposit a coin the venue can't settle, and get a free claim on USDC ===")
st, body = deposit(agent_a, "ETH", 5)
check("depositing a non-settlement coin is refused", st == 400, (st, body))
check("and the refusal names the asset that works",
      "USDC" in (body.get("message") or ""), body)
check("no phantom balance was created",
      call("GET", "/v1/account", seed=seed_a, agent_id=agent_a)[1].get("balances") is not None)
st, acct = call("GET", "/v1/account", seed=seed_a, agent_id=agent_a)
check("the agent holds no ETH",
      all(b["asset"] != "ETH" for b in acct["balances"]), acct["balances"])
st, res = call("GET", "/v1/reserves")
check("and reserves were not credited for a coin nobody holds",
      res["reserves_held"] == 0.0, res)

st, body = deposit(agent_a, "USDC", 5000)
check("depositing the settlement asset works", st == 200, (st, body))
for a, s in ((agent_b, seed_b), (agent_c, seed_c), (agent_d, seed_d)):
    deposit(a, "USDC", 5000)

# ---------------------------------------------------------------------------------------------
print()
print("=== ATTACK 2: open a market in an asset nobody can fund or be paid in ===")
now = int(time.time() * 1000)
st, body = call("POST", "/v1/markets", owner=True, body_obj={
    "question": "Will DOGE moon?", "closes_at_ms": now + 60000,
    "observed_at_ms": now + 120000, "asset": "DOGE",
    "resolution": {"kind": "operator_declared", "criteria": "x", "source": "y"},
})
check("a market in an unsupported asset is refused", st == 400, (st, body))
check("and it says the asset is not enabled",
      body.get("code") == "ASSET_NOT_ENABLED", body)

# ---------------------------------------------------------------------------------------------
print()
print("=== ATTACK 3: freeze every payout by disputing proposals you have no stake in ===")
mkt, detail = open_market("Will the reference index close above its open?", closes_in_ms=900,
                          asset="USDC", dispute_window_ms=600_000)
call("POST", f"/v1/markets/{mkt}/stakes", seed=seed_a, agent_id=agent_a,
     body_obj={"outcome": 0, "amount": 300})
call("POST", f"/v1/markets/{mkt}/stakes", seed=seed_b, agent_id=agent_b,
     body_obj={"outcome": 1, "amount": 200})
wait_for_observation(detail)
st, _ = call("POST", f"/v1/markets/{mkt}/propose", owner=True,
             body_obj={"outcome": 0, "evidence": "observed at the source"})
check("an outcome can be proposed", st == 200, st)

st, body = call("POST", f"/v1/markets/{mkt}/dispute", seed=seed_d, agent_id=agent_d,
                body_obj={"reason": "griefing, I have no position here"})
check("an outsider with no stake cannot freeze the payout", st == 409, (st, body))
check("and the refusal explains the cost principle",
      "stake" in (body.get("message") or ""), body)

st, body = call("POST", f"/v1/markets/{mkt}/dispute", seed=seed_b, agent_id=agent_b,
                body_obj={"reason": "the source disagrees"})
check("a participant CAN dispute — the defence is still intact", st == 200, (st, body))

st, body = call("POST", f"/v1/markets/{mkt}/dispute", seed=seed_b, agent_id=agent_b,
                body_obj={"reason": "and again, forever"})
check("but not twice on the same proposal", st == 409, (st, body))

st, _ = call("POST", f"/v1/markets/{mkt}/propose", owner=True,
             body_obj={"outcome": 0, "evidence": "re-proposed with a second source"})
check("the operator can re-propose after a dispute", st == 200, st)
st, body = call("POST", f"/v1/markets/{mkt}/dispute", seed=seed_b, agent_id=agent_b,
                body_obj={"reason": "still wrong"})
check("and a re-proposal gets a fresh hearing", st == 200, (st, body))
st, _ = call("POST", f"/v1/markets/{mkt}/propose", owner=True,
             body_obj={"outcome": 0, "evidence": "third time, three sources"})
st, out = call("POST", f"/v1/markets/{mkt}/finalize", body_obj={}, owner=True)
check("a market inside its dispute window still cannot be finalized early", st == 425, (st, out))

# ---------------------------------------------------------------------------------------------
print()
print("=== ATTACK 4: farm trust with free points to buy weight in the paid feed ===")
pts_mkt, pts_detail = open_market("A points market used purely to farm standing",
                                  closes_in_ms=700)
call("POST", f"/v1/markets/{pts_mkt}/stakes", seed=seed_c, agent_id=agent_c,
     body_obj={"outcome": 0, "amount": 900})
call("POST", f"/v1/markets/{pts_mkt}/stakes", seed=seed_d, agent_id=agent_d,
     body_obj={"outcome": 1, "amount": 50})
wait_for_observation(pts_detail)
settle_now(pts_mkt, outcome=1)

st, acct_d = call("GET", "/v1/account", seed=seed_d, agent_id=agent_d)
check("beating the crowd with free points still earns rate-limit standing",
      acct_d["trust_score"] > 0, acct_d["trust_score"])
check("but it buys no weight in the data product being sold",
      acct_d["forecast_trust_score"] == 0, acct_d["forecast_trust_score"])

usdc_mkt, usdc_detail = open_market("A money market where being right should count",
                                    closes_in_ms=700, asset="USDC")
call("POST", f"/v1/markets/{usdc_mkt}/stakes", seed=seed_c, agent_id=agent_c,
     body_obj={"outcome": 0, "amount": 900})
call("POST", f"/v1/markets/{usdc_mkt}/stakes", seed=seed_d, agent_id=agent_d,
     body_obj={"outcome": 1, "amount": 50})
wait_for_observation(usdc_detail)
settle_now(usdc_mkt, outcome=1)
st, acct_d = call("GET", "/v1/account", seed=seed_d, agent_id=agent_d)
check("being right with real money does earn it",
      acct_d["forecast_trust_score"] > 0, acct_d["forecast_trust_score"])

# ---------------------------------------------------------------------------------------------
print()
print("=== ATTACK 5: pad the advertised accuracy with free-money markets ===")
st, feed = call("GET", "/v1/feed")
tr = feed["track_record"]
check("points markets are kept out of the headline accuracy",
      tr["promotional_excluded"] >= 1, tr)
check("and the exclusion is published, not silent",
      "money-backed" in tr.get("baseline_note", ""), tr.get("baseline_note"))
check("every market says what it is denominated in",
      all("asset" in m for m in feed["markets"]), feed["markets"][:1])

# ---------------------------------------------------------------------------------------------
print()
print("=== ATTACK 6: buy the published consensus with one big account ===")
whale_mkt, _ = open_market("Can one account set what the crowd appears to think?",
                           closes_in_ms=600000, asset="USDC")
call("POST", f"/v1/markets/{whale_mkt}/stakes", seed=seed_a, agent_id=agent_a,
     body_obj={"outcome": 0, "amount": 3000})
for s, a in ((seed_b, agent_b), (seed_c, agent_c), (seed_d, agent_d)):
    call("POST", f"/v1/markets/{whale_mkt}/stakes", seed=s, agent_id=a,
         body_obj={"outcome": 1, "amount": 100})

st, sub = call("POST", "/v1/feed/subscribers", body_obj={"label": "attack.test"}, owner=True)
key = sub["feed_key"]
time.sleep(6)  # let the feed cache expire so this reads live
st, raw = raw_get("/v1/feed", {"X-Feed-Key": key})
paid = json.loads(raw)
wm = next(m for m in paid["markets"] if m["market_id"] == whale_mkt)
raw_share = wm["outcomes"][0]["consensus"]
weighted_share = wm["outcomes"][0]["weighted_consensus"]
check("the raw pool share is untouched — payouts stay pro rata",
      abs(raw_share - 3000 / 3300) < 1e-6, raw_share)
check("but one account cannot own the signal that is sold",
      weighted_share <= 0.2501, weighted_share)
check("and the concentration is published so a buyer can see it",
      abs(wm["top_holder_share"] - 3000 / 3300) < 1e-6, wm.get("top_holder_share"))

# ---------------------------------------------------------------------------------------------
print()
print("=== ATTACK 7: open a market that pays out before anyone can object ===")
now = int(time.time() * 1000)
body = {
    "question": "Will BTC be above 1 dollar?",
    "closes_at_ms": now + 60_000,
    "observed_at_ms": now + 120_000,
    "dispute_window_ms": 0,
    "resolution": {"kind": "price_threshold", "symbol": "BTC-USD",
                   "comparator": "above", "threshold": 1.0,
                   "if_true_outcome": 0, "if_false_outcome": 1},
}
st, resp = call("POST", "/v1/markets", seed=seed_d, agent_id=agent_d, body_obj=body)
check("an agent cannot open a market with no dispute window", st == 400, (st, resp))
check("and it says why", resp.get("code") == "DISPUTE_WINDOW_TOO_SHORT", resp)

body["dispute_window_ms"] = 600_000
body["resolution"] = {"kind": "operator_declared", "criteria": "I will decide", "source": "me"}
st, resp = call("POST", "/v1/markets", seed=seed_d, agent_id=agent_d, body_obj=body)
check("nor one it gets to resolve itself", st == 403, (st, resp))

# ---------------------------------------------------------------------------------------------
print()
print("=== ATTACK 8: bet on a race that has already been run ===")
now = int(time.time() * 1000)
st, resp = call("POST", "/v1/markets", owner=True, body_obj={
    "question": "Something that already happened",
    "closes_at_ms": now - 10_000, "observed_at_ms": now - 5_000,
    "resolution": {"kind": "operator_declared", "criteria": "x", "source": "y"},
})
check("a market that closes in the past is refused", st == 400, (st, resp))

st, resp = call("POST", "/v1/markets", owner=True, body_obj={
    "question": "A market that closes after its own answer is public",
    "closes_at_ms": now + 120_000, "observed_at_ms": now + 60_000,
    "resolution": {"kind": "operator_declared", "criteria": "x", "source": "y"},
})
check("so is one that stays open past its observation", st == 400, (st, resp))

# ---------------------------------------------------------------------------------------------
print()
print("=== ATTACK 9: cash out free promotional points ===")
st, body = call("POST", "/v1/withdrawals", seed=seed_a, agent_id=agent_a,
                body_obj={"asset": "PTS", "amount": 10, "destination": "addr1"})
check("free points can never be withdrawn, even with real money on", st == 403, (st, body))
check("and the reason is that they have no value, not that money is off",
      "no cash value" in (body.get("message") or "").lower(), body)

# ---------------------------------------------------------------------------------------------
print()
print("=== ATTACK 10: drain the float with withdrawals larger than reserves ===")
st, res = call("GET", "/v1/reserves")
check("reserves are fully backed before we start", res["fully_backed"] is True, res)
st, body = call("POST", "/v1/withdrawals", seed=seed_a, agent_id=agent_a,
                body_obj={"asset": "USDC", "amount": 1e9, "destination": "addr1"})
check("you cannot withdraw more than you hold", st in (400, 403, 409), (st, body))
st, res = call("GET", "/v1/reserves")
check("and reserves are still whole", res["fully_backed"] is True, res)

# ---------------------------------------------------------------------------------------------
print()
print("=== ATTACK 11: make the server do unbounded work from an unauthenticated socket ===")
codes = [raw_get("/v1/feed")[0] for _ in range(40)]
check("the anonymous feed is rate limited", 429 in codes, sorted(set(codes)))
st, raw = raw_get("/v1/feed", {"X-Feed-Key": key})
check("a paying subscriber is not caught by that cap", st == 200, st)

# ---------------------------------------------------------------------------------------------
print()
print("=== ATTACK 12: kill the whole process from one unauthenticated socket ===")
import socket


def shoot(payload, timeout=10):
    """Send raw bytes and return the status line, or None if the server never answered."""
    try:
        sk = socket.create_connection((HOST, PORT), timeout=timeout)
        sk.sendall(payload)
        data = sk.recv(200)
        sk.close()
        return data.split(b"\r\n")[0].decode(errors="replace")
    except Exception:
        return None


def alive():
    st, _ = call("GET", "/health")
    return st == 200


check("the server is up before we start", alive())

# A body length that no allocator will satisfy. Rust cannot catch a failed allocation — it
# aborts — so this used to be an 80-byte, no-credentials kill switch for the whole venue.
line = shoot(b"POST /v1/agents HTTP/1.1\r\nHost: x\r\nContent-Length: 1152921504606846976\r\n\r\n")
check("an absurd Content-Length is refused, not allocated", line is not None and "413" in line, line)
check("and the server is still alive", alive())

# The JSON parser is recursive, and a Rust stack overflow aborts the process. /v1/agents has to
# parse the body before it can verify anything, because the key it verifies against is IN the body.
deep = b"[" * 200000 + b"]" * 200000
line = shoot(b"POST /v1/agents HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(deep) + deep)
check("deeply nested JSON is rejected, not recursed into", line is not None and "400" in line, line)
check("and the server is still alive", alive())

line = shoot(b"GET /health HTTP/1.1\r\nHost: x\r\n" + b"X-A: b\r\n" * 10000 + b"\r\n")
check("an endless header list is refused", line is not None and "413" in line, line)
check("and the server is still alive", alive())

# A request line that never terminates. read_line would grow a String forever.
line = shoot(b"GET /" + b"a" * 100000 + b" HTTP/1.1\r\nHost: x\r\n\r\n")
check("an over-long request line is refused", line is not None, line)
check("and the server is still alive", alive())

check("a normal request still works after all of that",
      call("GET", "/v1/markets")[0] == 200)

# ---------------------------------------------------------------------------------------------
print()
print("=== ATTACK 13: poison the ledger with a number that is merely very large ===")
st, body = deposit(agent_a, "USDC", 1e308)
check("an overflowing deposit is refused", st == 400, (st, body))
check("and the message says why", "no more than" in (body.get("message") or ""), body)

st, res = call("GET", "/v1/reserves")
check("reserves still report real numbers",
      isinstance(res["credits_outstanding"], (int, float))
      and isinstance(res["reserves_held"], (int, float)), res)
check("and the solvency answer is not self-contradictory",
      (res["shortfall"] <= 1e-9) == (res["fully_backed"] is True),
      (res["shortfall"], res["fully_backed"]))

# The same ceiling has to hold on the staking path, or the overflow just moves.
mkt2, detail2 = open_market("A market to try an unpayable stake in", closes_in_ms=60_000,
                            asset="USDC", dispute_window_ms=600_000)
st, body = call("POST", f"/v1/markets/{mkt2}/stakes", seed=seed_a, agent_id=agent_a,
                body_obj={"outcome": 0, "amount": 1e308})
check("an unpayable stake is refused too", st != 200, (st, body))

st, res2 = call("GET", "/v1/reserves")
check("the ledger is unchanged after all of that",
      res2["credits_outstanding"] == res["credits_outstanding"], (res, res2))

stop(proc)
if os.path.exists(WAL):
    os.remove(WAL)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL ATTACKS REPELLED")
