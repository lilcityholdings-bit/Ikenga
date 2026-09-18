#!/usr/bin/env python3
"""Adversarial tests for the agent-facing additions: positions, events, idempotency.

    cargo build --release && python3 agentapi_test.py

Every one of these is new attack surface, and two of them touch the authentication path. What is
pinned here is that the conveniences added for bots did not open a door: an agent cannot read
another agent's positions, the public event stream names nobody, and idempotency absorbs an
identical retry without becoming a way to replay a request the caller did not sign.
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

HOST, PORT = "127.0.0.1", 8260
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
    r = conn.getresponse(); d = r.read(); hdrs = dict(r.getheaders()); conn.close()
    return r.status, d, hdrs


def send_raw(method, path, seed, agent_id, body_obj, nonce, ts=None, idempotent=False):
    """Send a signed request with a nonce WE choose, so a retry can reuse it byte for byte."""
    raw = json.dumps(body_obj).encode()
    ts = ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sig = sign(seed, method, path, ts, nonce, raw)
    h = {"Content-Type": "application/json", "X-Agent-ID": agent_id,
         "X-Timestamp": ts, "X-Nonce": nonce, "X-Signature": sig}
    if idempotent:
        h["X-Idempotent"] = "true"
    conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
    conn.request(method, path, body=raw, headers=h)
    r = conn.getresponse(); d = r.read(); hdrs = dict((k.lower(), v) for k, v in r.getheaders())
    conn.close()
    try:
        return r.status, json.loads(d), hdrs
    except Exception:
        return r.status, d, hdrs


WAL = "/tmp/ikenga-agentapi-test.wal"
if os.path.exists(WAL):
    os.remove(WAL)

proc, lines = start_server()
if not any("listening on" in l for l in lines):
    print("server failed to start:\n" + "\n".join(lines))
    sys.exit(1)

seed_a, agent_a, _ = register()
seed_b, agent_b, _ = register()

# ---------------------------------------------------------------------------------------------
print("=== 1. an agent can finally see what it holds ===")
mkt, detail = open_market("Will the reference close above its open?", closes_in_ms=120_000)
call("POST", f"/v1/markets/{mkt}/stakes", seed=seed_a, agent_id=agent_a,
     body_obj={"outcome": 0, "amount": 300})
call("POST", f"/v1/markets/{mkt}/stakes", seed=seed_b, agent_id=agent_b,
     body_obj={"outcome": 1, "amount": 200})

st, acct = call("GET", "/v1/account", seed=seed_a, agent_id=agent_a)
check("the account view has positions at all", "positions" in acct, list(acct.keys()))
pos = acct.get("positions", [])
check("exactly the one market it staked in", len(pos) == 1, len(pos))
if pos:
    p = pos[0]
    check("it names the market and the question", p["market_id"] == mkt and p["question"], p)
    check("it shows this agent's own stake", p["outcomes"][0]["my_stake"] == 300.0, p["outcomes"])
    check("and NOT the other agent's", p["outcomes"][1]["my_stake"] == 0.0, p["outcomes"])
    check("the pool total is public anyway, so it is fine to show", p["market_pool"] == 500.0, p)
    check("unsettled means no settled value yet", p["settled_value"] is None, p)

st, acct_b = call("GET", "/v1/account", seed=seed_b, agent_id=agent_b)
pb = acct_b["positions"][0]
check("the other agent sees its own side, not agent A's",
      pb["outcomes"][1]["my_stake"] == 200.0 and pb["outcomes"][0]["my_stake"] == 0.0,
      pb["outcomes"])

blob = json.dumps(acct)
check("one agent's account never names another agent", agent_b not in blob, blob[:200])

# ---------------------------------------------------------------------------------------------
print()
print("=== 2. the event stream, and what it refuses to say ===")
st, ev = call("GET", "/v1/events")
check("the stream is readable without an account", st == 200, st)
check("opening a market produced an event",
      any(e["kind"] == "market_opened" and e["market_id"] == mkt for e in ev["events"]),
      [e["kind"] for e in ev["events"]])
check("it hands back a cursor", ev["next_cursor"] > 0, ev["next_cursor"])

blob = json.dumps(ev)
check("no agent id appears anywhere in the stream",
      agent_a not in blob and agent_b not in blob, blob[:300])
for leak in ("agent", "stake_id", "balance", "address", "pubkey", "my_stake"):
    check(f"the stream exposes no '{leak}' field", f'"{leak}' not in blob)

cursor = ev["next_cursor"]
st, again = call("GET", f"/v1/events?since={cursor}")
check("polling with the cursor returns nothing new", len(again["events"]) == 0, again["events"])
check("and does not rewind the client", again["next_cursor"] == cursor,
      (cursor, again["next_cursor"]))

wait_for_observation(detail)
settle_now(mkt, outcome=0)
st, after = call("GET", f"/v1/events?since={cursor}")
kinds = [e["kind"] for e in after["events"]]
check("settling shows up as an event", "market_resolved" in kinds, kinds)
check("the settlement event says what happened without saying who to",
      all(agent_a not in e["detail"] and agent_b not in e["detail"] for e in after["events"]),
      after["events"])

st, acct = call("GET", "/v1/account", seed=seed_a, agent_id=agent_a)
p = acct["positions"][0]
check("a settled position now shows what it was worth", p["settled_value"] is not None, p)
check("and the winner's value beats the stake", p["settled_value"] > 300.0, p["settled_value"])

# ---------------------------------------------------------------------------------------------
print()
print("=== 3. idempotency absorbs a retry without becoming a replay ===")
mkt2, detail2 = open_market("A market for testing retries", closes_in_ms=120_000)
nonce = f"retry-{time.time_ns()}"
body = {"outcome": 0, "amount": 100}

# Without opting in, a repeat is refused exactly as it always was.
plain_nonce = f"plain-{time.time_ns()}"
stp1, _, _ = send_raw("POST", f"/v1/markets/{mkt2}/stakes", seed_a, agent_a, body, plain_nonce)
stp2, rp2, hp2 = send_raw("POST", f"/v1/markets/{mkt2}/stakes", seed_a, agent_a, body, plain_nonce)
check("by default a repeat is still refused", stp1 == 200 and stp2 != 200, (stp1, stp2, rp2))
check("and nothing is replayed unless asked for", "x-idempotent-replay" not in hp2, hp2)

st1, r1, h1 = send_raw("POST", f"/v1/markets/{mkt2}/stakes", seed_a, agent_a, body, nonce,
                       idempotent=True)
check("the first attempt goes through", st1 == 200, (st1, r1))
check("and is not flagged as a replay", "x-idempotent-replay" not in h1, h1)

st2, r2, h2 = send_raw("POST", f"/v1/markets/{mkt2}/stakes", seed_a, agent_a, body, nonce,
                       idempotent=True)
check("an identical retry is absorbed rather than refused", st2 == st1, (st2, r2))
check("and says it was a replay", h2.get("x-idempotent-replay") == "true", h2)
check("the receipt confirms it already happened", r2.get("idempotent_replay") is True, r2)
check("but does NOT hand back the original body — no balance leak to a request-capturer",
      "balance_remaining" not in json.dumps(r2), r2)

st, acct = call("GET", "/v1/account", seed=seed_a, agent_id=agent_a)
p2 = next(x for x in acct["positions"] if x["market_id"] == mkt2)
check("the retry did NOT stake twice", p2["outcomes"][0]["my_stake"] == 200.0,
      p2["outcomes"])

# The security property: same nonce, DIFFERENT request must not be honoured.
st3, r3, _ = send_raw("POST", f"/v1/markets/{mkt2}/stakes", seed_a, agent_a,
                      {"outcome": 1, "amount": 999}, nonce, idempotent=True)
check("reusing the nonce for a different bet is refused", st3 != 200, (st3, r3))
st, acct = call("GET", "/v1/account", seed=seed_a, agent_id=agent_a)
p2 = next(x for x in acct["positions"] if x["market_id"] == mkt2)
check("and nothing was staked on the other outcome", p2["outcomes"][1]["my_stake"] == 0.0,
      p2["outcomes"])

# Another agent cannot pull agent A's cached answer by guessing the nonce.
st4, r4, h4 = send_raw("POST", f"/v1/markets/{mkt2}/stakes", seed_b, agent_b, body, nonce,
                       idempotent=True)
check("a different agent reusing the same nonce gets its own fresh handling",
      h4.get("x-idempotent-replay") != "true", (st4, h4))

# ---------------------------------------------------------------------------------------------
print()
print("=== 4. a bot with a broken clock is told what is wrong ===")
st, body_err, _ = send_raw("POST", f"/v1/markets/{mkt2}/stakes", seed_a, agent_a,
                           {"outcome": 0, "amount": 10}, f"skew-{time.time_ns()}",
                           ts="2020-01-01T00:00:00Z")
check("a skewed clock is rejected", st == 401, st)
msg = (body_err.get("message") or "") if isinstance(body_err, dict) else ""
check("the error names the server's own time", "Server time is" in msg, msg)
check("and says which way the client is out",
      ("behind" in msg or "ahead" in msg), msg)

# ---------------------------------------------------------------------------------------------
print()
print("=== 5. the venue keeps answering while markets are closing underneath it ===")
# Regression for a self-deadlock: the sweeper re-locked a mutex it already held the moment a
# market first crossed its closing time, hung holding it, and every request that needed markets
# hung behind it. The server stopped answering a few minutes after start, with no error anywhere.
short, short_detail = open_market("A market that closes almost immediately", closes_in_ms=3_000)
call("POST", f"/v1/markets/{short}/stakes", seed=seed_a, agent_id=agent_a,
     body_obj={"outcome": 0, "amount": 25})
time.sleep(9)  # well past the close, so the sweeper has had several passes at it

st, listing = call("GET", "/v1/markets")
check("the server still answers after a market closed under it", st == 200, st)
st, acct = call("GET", "/v1/account", seed=seed_a, agent_id=agent_a)
check("and the account endpoint, which needs the same lock, still answers", st == 200, st)

st, ev = call("GET", "/v1/events")
check("closing is announced on the stream",
      any(e["kind"] == "market_closed" and e["market_id"] == short for e in ev["events"]),
      [e["kind"] for e in ev["events"]][-6:])

# ---------------------------------------------------------------------------------------------
print()
print("=== 6. garbage signatures are refused cheaply, without collateral damage ===")
# Signature verification costs a process spawn, and it used to run before any limiting at all —
# so anyone could burn the server's whole CPU by sending well-formed requests with junk
# signatures. The budget that fixes it is keyed on (address, agent) rather than address alone,
# because keyed on address it would lock out every client behind a shared proxy.
bad_codes = []
for i in range(90):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn = http.client.HTTPConnection(HOST, PORT, timeout=10)
    conn.request("GET", "/v1/account", headers={
        "X-Agent-ID": agent_a, "X-Timestamp": ts,
        "X-Nonce": f"junk-{i}-{time.time_ns()}", "X-Signature": "ab" * 64})
    r = conn.getresponse(); rb = r.read(); conn.close()
    bad_codes.append(r.status)
    if r.status == 429:
        last_body = rb
        break

check("a run of bad signatures is eventually refused without being checked",
      429 in bad_codes, sorted(set(bad_codes)))
check("and the refusal explains itself",
      b"TOO_MANY_BAD_SIGNATURES" in last_body, last_body[:120])
check("it took a generous number of failures first, so a buggy client sees real errors",
      bad_codes.count(401) >= 30, bad_codes.count(401))

# The collateral-damage check: a DIFFERENT agent from the SAME address must be unaffected.
st, acct_b = call("GET", "/v1/account", seed=seed_b, agent_id=agent_b)
check("another agent on the same address still works",
      st == 200, (st, acct_b if st != 200 else "ok"))

stop(proc)
if os.path.exists(WAL):
    os.remove(WAL)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("AGENT API CHECKS PASSED")
