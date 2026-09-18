#!/usr/bin/env python3
"""The customer journey, start to finish: discover the service, try it without signing up,
register a key you generated yourself, then use it.

    cargo build --release && python3 onboarding_test.py

The last section is the one that matters most. Before agent self-registration existed, a
production deployment seeded no agents and offered no way to create one, so it could not onboard
a single customer — every other feature was unreachable in the only mode you'd actually deploy.
That case is tested here explicitly so it can't quietly regress.
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

HOST, PORT = "127.0.0.1", 8095
PROD_PORT = 8094
BIN = "./target/release/ikenga-matching-engine"
OWNER_KEY = "cc" * 32
PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")
SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")

failures = []


def check(label, ok, extra=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        if extra:
            print(f"       got: {extra}")
        failures.append(label)


def generate_keypair():
    """What a real integrator does locally: make a keypair, keep the private half."""
    with tempfile.NamedTemporaryFile(suffix=".pem") as key_f:
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", key_f.name],
                       check=True, capture_output=True)
        priv_der = subprocess.run(["openssl", "pkey", "-in", key_f.name, "-outform", "DER"],
                                  check=True, capture_output=True).stdout
        pub_der = subprocess.run(["openssl", "pkey", "-in", key_f.name, "-pubout", "-outform", "DER"],
                                 check=True, capture_output=True).stdout
    return priv_der[16:].hex(), pub_der[12:].hex()


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


def call(method, path, port=PORT, seed=None, agent_id=None, body_obj=None, headers=None, nonce=None, ts=None):
    raw = json.dumps(body_obj).encode() if body_obj is not None else b""
    h = dict(headers or {})
    if raw:
        h["Content-Type"] = "application/json"
    if seed:
        ts = ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        nonce = nonce or f"onboard-{time.time_ns()}"
        h.update({
            "X-Agent-ID": agent_id or "registration",
            "X-Timestamp": ts,
            "X-Nonce": nonce,
            "X-Signature": sign(seed, method, path, ts, nonce, raw),
        })
    conn = http.client.HTTPConnection(HOST, port, timeout=15)
    conn.request(method, path, body=raw, headers=h)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(data)
    except Exception:
        return resp.status, data


def start_server(extra_env, port):
    env = {
        **os.environ,
        "IKENGA_BIND": f"{HOST}:{port}",
        "IKENGA_OWNER_KEY": OWNER_KEY,
        # Raised so the negative cases below (which correctly consume budget — a failed attempt
        # still costs the server work) don't trip the limiter mid-test.
        "IKENGA_REGISTRATIONS_PER_HOUR": "200",
        "IKENGA_TRIAL_ROUTES_PER_HOUR": "200",
        **extra_env,
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


# ---------------------------------------------------------------------------------------------
print("=== 1. the service explains itself to someone who has never seen it ===")
proc, lines = start_server({"IKENGA_WAL_PATH": "off", "IKENGA_ROUTE_DEMO": "1"}, PORT)
status, idx = call("GET", "/")
check("GET / returns an index without auth", status == 200, status)
check("it names the endpoints", isinstance(idx.get("endpoints"), list) and len(idx["endpoints"]) >= 8)
check("it explains how to sign", "Ed25519" in idx.get("signing", ""))
check("it says how to start", "start_here" in idx)
check("it states the custody difference", "non-custodial" in idx.get("custody", ""))
paths = [e["path"] for e in idx["endpoints"]]
check("registration is discoverable from the index", "/v1/agents" in paths, paths)

print("\n=== 2. you can try it before signing up ===")
status, trial = call("GET", "/v1/route?sell=BTC&buy=USD&amount=1.0")
check("unauthenticated route quote works", status == 200, (status, trial))
check("it is flagged as a trial", trial.get("trial") is True)
check("it tells you how to lift the limit", "POST /v1/agents" in (trial.get("trial_note") or ""))
check("it still shows the real fee", trial["fee_bps"] == 10.0)

print("\n=== 3. the quote shows what comparing venues was worth ===")
check("reports how many sources were compared", trial["sources_compared"] == 2, trial.get("sources_compared"))
check("reports the spread vs the worst usable quote", trial["spread_vs_worst_bps"] > 0, trial.get("spread_vs_worst_bps"))
# demo venues quote 64000 and 64180 -> (64180-64000)/64000 = 28.125bps
check("and the spread is arithmetically right", abs(trial["spread_vs_worst_bps"] - 28.125) < 1e-6,
      trial.get("spread_vs_worst_bps"))

print("\n=== 4. registering a key you generated yourself ===")
seed_hex, pubkey_hex = generate_keypair()
status, reg = call("POST", "/v1/agents", seed=seed_hex, body_obj={"pubkey_hex": pubkey_hex})
check("registration succeeds", status == 201, (status, reg))
agent_id = reg.get("agent_id", "")
check("an agent id is issued", agent_id.startswith("agent_"), agent_id)
check("new agents start untrusted", reg.get("trust_score") == 0 and reg.get("trust_band") == "New", reg)
check("it says the server cannot sign for you", "cannot sign for you" in reg.get("note", ""))

print("\n=== 5. the new credentials actually work ===")
status, routed = call("GET", "/v1/route?sell=BTC&buy=USD&amount=1.0", seed=seed_hex, agent_id=agent_id)
check("signed route call succeeds", status == 200, (status, routed))
check("it is no longer a trial", routed.get("trial") is False)
check("the call is metered for billing", routed.get("billable_calls_this_session") == 1, routed.get("billable_calls_this_session"))
status, acct = call("GET", "/v1/account", seed=seed_hex, agent_id=agent_id)
check("the account endpoint recognises the new agent", status == 200, (status, acct))
# Registration grants points (so a new agent can bet immediately — see docs/MARKETS.md) but
# never anything with cash value. Both halves matter: the free points are what remove the
# drop-out step in onboarding, and the absence of monetary balance is what keeps that safe.
balances = {b["asset"]: b["balance"] for b in acct.get("balances", [])}
check("it is granted points to start predicting with", balances.get("PTS") == 1000.0, balances)
check("but no money — free registration grants nothing of cash value",
      all(asset == "PTS" or amount == 0 for asset, amount in balances.items()), balances)

print("\n=== 6. registration refuses the things it should ===")
status, body = call("POST", "/v1/agents", body_obj={"pubkey_hex": pubkey_hex})
check("unsigned registration is rejected", status == 401, (status, body))

other_seed, _other_pub = generate_keypair()
status, body = call("POST", "/v1/agents", seed=other_seed, body_obj={"pubkey_hex": pubkey_hex})
check("registering someone else's public key is rejected", status == 401, (status, body))

status, body = call("POST", "/v1/agents", seed=seed_hex, body_obj={"pubkey_hex": "00" * 32})
check("the all-zero key is rejected", status == 400 and body.get("code") == "BAD_PUBKEY", (status, body))

status, body = call("POST", "/v1/agents", seed=seed_hex, body_obj={"pubkey_hex": "abcd"})
check("a wrong-length key is rejected", status == 400, (status, body))

status, body = call("POST", "/v1/agents", seed=seed_hex, body_obj={})
check("a missing key is rejected", status == 400 and body.get("code") == "MISSING_FIELD", (status, body))

# Replay: identical signed registration sent twice.
ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
nonce = "replay-registration"
s1, _ = call("POST", "/v1/agents", seed=seed_hex, body_obj={"pubkey_hex": pubkey_hex}, ts=ts, nonce=nonce)
s2, b2 = call("POST", "/v1/agents", seed=seed_hex, body_obj={"pubkey_hex": pubkey_hex}, ts=ts, nonce=nonce)
check("a captured registration cannot be replayed", s1 == 201 and s2 == 401, (s1, s2, b2))
stop(proc)

print("\n=== 7. THE ONE THAT MATTERS: a production deployment can onboard a customer ===")
wal = "/tmp/ikenga-onboarding-prod.wal"
if os.path.exists(wal):
    os.remove(wal)
proc, lines = start_server({"IKENGA_ENV": "production", "IKENGA_WAL_PATH": wal}, PROD_PORT)
check("production server starts", any("listening on" in l for l in lines), lines[-3:] if lines else lines)
check("and seeds no demo agents", not any("private_key_seed_hex" in l for l in lines))

prod_seed, prod_pub = generate_keypair()
status, reg = call("POST", "/v1/agents", port=PROD_PORT, seed=prod_seed, body_obj={"pubkey_hex": prod_pub})
check("a brand-new customer can register in production", status == 201, (status, reg))
prod_agent = reg.get("agent_id", "")
status, acct = call("GET", "/v1/account", port=PROD_PORT, seed=prod_seed, agent_id=prod_agent)
check("and immediately authenticate with their own key", status == 200, (status, acct))

# Registration must survive a restart, or every customer is silently de-registered on deploy.
stop(proc)
proc, lines = start_server({"IKENGA_ENV": "production", "IKENGA_WAL_PATH": wal}, PROD_PORT)
check("registrations are recovered on restart", any("recovered" in l for l in lines), lines[:4])
status, acct = call("GET", "/v1/account", port=PROD_PORT, seed=prod_seed, agent_id=prod_agent)
check("the customer still authenticates after a restart", status == 200, (status, acct))
stop(proc)
if os.path.exists(wal):
    os.remove(wal)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL ONBOARDING CHECKS PASSED")
