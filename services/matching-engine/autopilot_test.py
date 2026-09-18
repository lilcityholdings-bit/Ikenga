#!/usr/bin/env python3
"""Autopilot: the venue runs itself, end to end, with nobody touching it.

    cargo build --release && python3 autopilot_test.py

This is the difference between a program that can host a market and a venue that is open. What
is pinned here is that a server left completely alone opens markets on a live price, takes real
stakes, closes, observes, settles and pays out — and then opens the next one. No operator, no
curl, no human in any step after start.
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

HOST, PORT = "127.0.0.1", 8201
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
        "IKENGA_ROUTE_DEMO": "1",
        "IKENGA_AUTOPILOT": "BTC-USD:60,ETH-USD:60",
        "IKENGA_AUTOPILOT_INTERVAL_MS": "2000",
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






def raw_get(path, headers=None):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
    conn.request("GET", path, headers=headers or {})
    r = conn.getresponse(); d = r.read(); conn.close()
    return r.status, d


def board():
    st, d = call("GET", "/v1/markets")
    return d.get("markets", []) if isinstance(d, dict) else []


def wait_for(predicate, seconds, label):
    """Poll until predicate holds. Returns what it found, or None on timeout."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        got = predicate()
        if got:
            return got
        time.sleep(2)
    print(f"       (timed out after {seconds}s waiting for {label})")
    return None


WAL = "/tmp/ikenga-autopilot-test.wal"
if os.path.exists(WAL):
    os.remove(WAL)

proc, lines = start_server()
if not any("listening on" in l for l in lines):
    print("server failed to start:\n" + "\n".join(lines))
    sys.exit(1)

# ---------------------------------------------------------------------------------------------
print("=== 1. it opens markets on its own ===")
check("autopilot announced itself at startup",
      any("autopilot: on" in l for l in lines), lines[-3:])
check("the venue starts empty", len(board()) == 0, len(board()))

opened = wait_for(lambda: board() if len(board()) >= 2 else None, 30, "the first markets")
check("markets appear with nobody asking for them", opened is not None and len(opened) >= 2,
      len(opened) if opened else 0)

if opened:
    m = opened[0]
    check("the question names a real price threshold",
          any(c.isdigit() for c in m["question"]), m["question"])
    st, detail = call("GET", f"/v1/markets/{m['market_id']}")
    check("it settles from a price feed, not a person",
          detail["resolution"]["kind"] == "price_threshold", detail.get("resolution"))
    check("the terms are hash-committed like any other market",
          len(detail["commitment_sha256"]) == 64 and detail["commitment_intact"] is True)
    check("it closes before it is observed — no free last stake",
          detail["observed_at_ms"] > detail["closes_at_ms"],
          (detail["closes_at_ms"], detail["observed_at_ms"]))

# ---------------------------------------------------------------------------------------------
print()
print("=== 2. a real agent can bet on what it opened ===")
seed_a, agent_a, _ = register()
seed_b, agent_b, _ = register()
live = [m for m in board() if m["status"] == "Open"]
check("there is something to bet on", len(live) > 0, len(live))

target = live[0]["market_id"]
st, r = call("POST", f"/v1/markets/{target}/stakes", seed=seed_a, agent_id=agent_a,
             body_obj={"outcome": 0, "amount": 300})
check("an agent can stake on an auto-opened market", st == 200, (st, r))
st, r = call("POST", f"/v1/markets/{target}/stakes", seed=seed_b, agent_id=agent_b,
             body_obj={"outcome": 1, "amount": 200})
check("and so can someone on the other side", st == 200, (st, r))

before_a = points(seed_a, agent_a)
before_b = points(seed_b, agent_b)
check("the stakes left their balances", before_a == 700.0 and before_b == 800.0,
      (before_a, before_b))

# ---------------------------------------------------------------------------------------------
print()
print("=== 3. it settles and pays without anyone touching it ===")
settled = wait_for(
    lambda: next((m for m in board()
                  if m["market_id"] == target and m["status"] in ("Resolved", "Voided")), None),
    240, "the market to settle itself")
check("the market reached a final state on its own", settled is not None,
      next((m["status"] for m in board() if m["market_id"] == target), "gone"))

if settled:
    check("and it resolved rather than voiding", settled["status"] == "Resolved",
          settled["status"])
    after_a = points(seed_a, agent_a)
    after_b = points(seed_b, agent_b)
    paid = (after_a - before_a) + (after_b - before_b)
    check("money moved back to the participants", paid > 0, paid)
    check("the winner got more than they staked, the loser got nothing back",
          (after_a > 1000.0) != (after_b > 1000.0), (after_a, after_b))
    check("payouts never exceeded the pool", paid <= 500.0 + 1e-9, paid)

st, dash = call("GET", "/v1/dashboard", owner=True)
check("the house collected a rake with no human in the loop",
      any(r["amount"] > 0 for r in dash["revenue"]["by_asset"]), dash["revenue"])
check("nothing is sitting waiting on the operator",
      len(dash["needs_attention"]) == 0, dash["needs_attention"])

# ---------------------------------------------------------------------------------------------
print()
print("=== 4. it keeps the board stocked ===")
refilled = wait_for(lambda: [m for m in board() if m["status"] == "Open"] or None, 60,
                    "a replacement market")
check("a new market replaced the settled one", refilled is not None and len(refilled) > 0,
      len(refilled) if refilled else 0)
check("it did not stack duplicates while one was live", len(refilled or []) <= 4,
      [m["question"] for m in (refilled or [])])
check("the venue has a settled history now", dash["markets"]["resolved"] >= 1,
      dash["markets"])

stop(proc)
if os.path.exists(WAL):
    os.remove(WAL)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("THE VENUE RUNS ITSELF")
