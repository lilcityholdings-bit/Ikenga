#!/usr/bin/env python3
"""Redeemable credits: deposit, bet, win, cash out — and the barriers around it.

    cargo build --release && python3 credits_test.py

The properties here are the ones that decide whether the platform can be trusted with money:

  * Free promotional points can NEVER be withdrawn, in any mode, by any route. Registration mints
    them freely, so if this barrier leaks the free grant becomes a money printer.
  * Credits are backed one-for-one and the reserve position is public and arithmetically checkable.
  * A withdrawal debits at request time, so the same credits cannot be staked while a payout is
    already in flight.
  * Reserves and withdrawals are durable: a restart must not resurrect a solvent ledger as an
    insolvent one, or lose a pending payout while leaving the balance debited.
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

HOST, PORT = "127.0.0.1", 8092
BIN = "./target/release/ikenga-matching-engine"
OWNER_KEY = "ee" * 32
WAL = "/tmp/ikenga-credits-test.wal"
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


def call(method, path, seed=None, agent_id=None, body_obj=None, owner=False, raw_body=None, _retries=6):
    for _ in range(_retries):
        status, body = _once(method, path, seed, agent_id, body_obj, owner, raw_body)
        if status != 429:
            return status, body
        time.sleep(0.35)
    return status, body


def _once(method, path, seed, agent_id, body_obj, owner, raw_body):
    if raw_body is not None:
        raw = raw_body
    else:
        raw = json.dumps(body_obj).encode() if body_obj is not None else b""
    h = {}
    if raw:
        h["Content-Type"] = "application/json"
    if owner:
        h["X-Owner-Key"] = OWNER_KEY
    if seed:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        nonce = f"cr-{time.time_ns()}"
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
    st, reg = call("POST", "/v1/agents", seed=seed, body_obj={"pubkey_hex": pub})
    assert st == 201, f"registration failed: {st} {reg}"
    return seed, reg["agent_id"]


def balance(seed, agent_id, asset):
    st, acct = call("GET", "/v1/account", seed=seed, agent_id=agent_id)
    assert st == 200, f"account failed: {st} {acct}"
    return next((b["balance"] for b in acct["balances"] if b["asset"] == asset), 0.0)


def start_server(extra_env=None):
    env = {
        **os.environ,
        "IKENGA_BIND": f"{HOST}:{PORT}",
        "IKENGA_OWNER_KEY": OWNER_KEY,
        "IKENGA_WAL_PATH": WAL,
        "IKENGA_REGISTRATIONS_PER_HOUR": "500",
        **(extra_env or {}),
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
    try:
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    except ProcessLookupError:
        pass


if os.path.exists(WAL):
    os.remove(WAL)

# ---------------------------------------------------------------------------------------------
print("=== 1. real money is one flag, and points still cannot leave ===")
proc, lines = start_server({"IKENGA_REAL_MONEY": "1"})
check("server starts with credits enabled", any("listening on" in l for l in lines),
      lines[-2:] if lines else lines)
check("startup states credits are live", any("REDEEMABLE BALANCES ENABLED" in l for l in lines), lines)

st, res = call("GET", "/v1/reserves")
check("reserves are public", st == 200, st)
check("real money reads as enabled", res["real_money_enabled"] is True, res)
check("an empty ledger is fully backed", res["fully_backed"] is True and res["shortfall"] == 0.0, res)

print("\n=== 3. free points still cannot be withdrawn, even with real money on ===")
seed_a, agent_a = register()
check("registration still grants points", balance(seed_a, agent_a, "PTS") == 1000.0,
      balance(seed_a, agent_a, "PTS"))
st, body = call("POST", "/v1/withdrawals", seed=seed_a, agent_id=agent_a,
                body_obj={"asset": "PTS", "amount": 500, "destination": "addr_attacker"})
check("withdrawing promotional points is refused", st == 403 and body["code"] == "NOT_REDEEMABLE",
      (st, body))
check("the refusal is about the asset, not the mode", "no cash value" in body["message"], body)

st, body = call("POST", "/v1/withdrawals", seed=seed_a, agent_id=agent_a,
                body_obj={"asset": "USDC", "amount": 1, "destination": "addr1"})
check("you cannot withdraw credits you never deposited",
      st == 400 and body["code"] == "INSUFFICIENT_BALANCE", (st, body))

print("\n=== 4. deposit, and the reserve tracks it ===")
st, credited = call("POST", f"/v1/deposits/{agent_a}/USDC", raw_body=b"500", owner=True)
check("a deposit credits the agent", st == 200 and credited["balance"] == 500.0, (st, credited))
st, res = call("GET", "/v1/reserves")
check("credits outstanding is visible", res["credits_outstanding"] == 500.0, res)
check("reserves match the deposit", res["reserves_held"] == 500.0, res)
check("still fully backed", res["fully_backed"] is True, res)

print("\n=== 5. bet real credits, win, and the payout is in credits ===")
seed_b, agent_b = register()
call("POST", f"/v1/deposits/{agent_b}/USDC", raw_body=b"500", owner=True)

now = int(time.time() * 1000)
st, mkt = call("POST", "/v1/markets", owner=True, body_obj={
    "question": "A real-money market",
    "asset": "USDC",
    "closes_at_ms": now + 1_000,
    "observed_at_ms": now + 1_100,
    "dispute_window_ms": 0,
    "resolution": {"kind": "operator_declared", "criteria": "declared by the named source",
                   "source": "example.test"},
})
check("a credit-denominated market can be opened", st == 201, (st, mkt))
mid = mkt["market_id"]

call("POST", f"/v1/markets/{mid}/stakes", seed=seed_a, agent_id=agent_a, body_obj={"outcome": 0, "amount": 100})
call("POST", f"/v1/markets/{mid}/stakes", seed=seed_b, agent_id=agent_b, body_obj={"outcome": 1, "amount": 100})

time.sleep(1.3)
call("POST", f"/v1/markets/{mid}/propose", owner=True,
     body_obj={"outcome": 0, "evidence": "the named source reported YES"})
st, settled = call("POST", f"/v1/markets/{mid}/finalize", body_obj={}, owner=True)
check("the credit market settles", st == 200, (st, settled))
# Losing pool 100, gross rake 1% = 1, winner gets 100 + 99 = 199 before the early-liquidity
# rebate hands a quarter of that rake back to the two stakers by size and by how early they were.
# The winner therefore lands a little above 599; the house keeps 0.75 of the 1.
won = balance(seed_a, agent_a, "USDC")
check("the winner is paid in credits", 400 + 199 <= won <= 400 + 199.25 + 1e-9, won)
check("the rake was taken in credits, net of the rebate",
      abs(settled["rake"] - 0.75) < 1e-9 and abs(settled["gross_rake"] - 1.0) < 1e-9,
      (settled.get("rake"), settled.get("gross_rake")))
check("the rebate is real money that left the house and reached a staker",
      settled["early_liquidity_rebate"] > 0
      and abs(settled["gross_rake"] - settled["rake"] - settled["early_liquidity_rebate"]) < 1e-9,
      settled.get("early_liquidity_rebate"))

st, res = call("GET", "/v1/reserves")
check("the ledger is still fully backed after settlement", res["fully_backed"] is True, res)

print("\n=== 6. cash out ===")
before = balance(seed_a, agent_a, "USDC")
st, wd = call("POST", "/v1/withdrawals", seed=seed_a, agent_id=agent_a,
              body_obj={"asset": "USDC", "amount": 200, "destination": "bc1qexampleaddress"})
check("a withdrawal can be requested", st == 201 and wd["status"] == "Pending", (st, wd))
check("the balance is debited immediately, not at send time",
      abs(balance(seed_a, agent_a, "USDC") - (before - 200)) < 1e-9, balance(seed_a, agent_a, "USDC"))

st, res = call("GET", "/v1/reserves")
check("the pending payout is still counted as a claim", res["withdrawals_pending"] == 200.0, res)
check("and the ledger is still solvent", res["fully_backed"] is True, res)

wid = wd["withdrawal_id"]
st, body = call("POST", f"/v1/withdrawals/{wid}/settle", body_obj={"sent": True}, owner=True)
check("marking sent without a tx reference is refused", st == 400, (st, body))

st, sent = call("POST", f"/v1/withdrawals/{wid}/settle", owner=True,
                body_obj={"sent": True, "tx_ref": "0xdeadbeef"})
check("the payout can be marked sent with a reference", st == 200 and sent["status"] == "Sent", (st, sent))
st, res = call("GET", "/v1/reserves")
check("reserves fall by the amount sent", res["reserves_held"] == 800.0, res)
check("the pending claim is cleared", res["withdrawals_pending"] == 0.0, res)
check("and the ledger remains fully backed", res["fully_backed"] is True, res)

st, body = call("POST", f"/v1/withdrawals/{wid}/settle", owner=True,
                body_obj={"sent": True, "tx_ref": "0xagain"})
check("a payout cannot be settled twice", st == 409, (st, body))

print("\n=== 7. a rejected payout is refunded in full ===")
before = balance(seed_b, agent_b, "USDC")
st, wd2 = call("POST", "/v1/withdrawals", seed=seed_b, agent_id=agent_b,
               body_obj={"asset": "USDC", "amount": 50, "destination": "bad_address"})
check("second withdrawal requested", st == 201, (st, wd2))
st, rej = call("POST", f"/v1/withdrawals/{wd2['withdrawal_id']}/settle", owner=True,
               body_obj={"sent": False, "note": "invalid destination"})
check("it can be rejected", st == 200 and rej["status"] == "Rejected", (st, rej))
check("and the agent is made whole", abs(balance(seed_b, agent_b, "USDC") - before) < 1e-9,
      (balance(seed_b, agent_b, "USDC"), before))

print("\n=== 8. deposits work in production too ===")
# Until the chain adapter exists this endpoint is the only way to fund a real deployment, so
# blocking it in production would make real money unusable. Owner-gated is the control.
stop(proc)
proc2 = subprocess.Popen(
    BIN,
    env={**os.environ, "IKENGA_BIND": f"{HOST}:{PORT}", "IKENGA_OWNER_KEY": OWNER_KEY,
         "IKENGA_WAL_PATH": WAL, "IKENGA_ENV": "production", "IKENGA_REAL_MONEY": "1"},
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)
lines = []
deadline = time.time() + 15
while time.time() < deadline:
    line = proc2.stdout.readline()
    if not line:
        break
    lines.append(line.rstrip())
    if "listening on" in line:
        break
check("production server with credits starts", any("listening on" in l for l in lines), lines[-2:])

st, dep = call("POST", f"/v1/deposits/{agent_a}/USDC", raw_body=b"250", owner=True)
check("the owner can record a deposit in production", st == 200, (st, dep))
# 500 + 500 deposited earlier, less 200 sent out, plus 250 now.
check("reserves recovered from the log and moved with the deposit",
      abs(dep["reserves_held"] - 1050.0) < 1e-9, dep)

st, body = call("POST", f"/v1/deposits/{agent_a}/USDC", raw_body=b"250")
check("without the owner key it is refused", st == 403, (st, body))

st, res = call("GET", "/v1/reserves")
check("reserves and withdrawals survived the restart — ledger still solvent",
      res["fully_backed"] is True, res)

st, body = call("POST", "/v1/withdrawals", seed=seed_a, agent_id=agent_a,
                body_obj={"asset": "PTS", "amount": 1, "destination": "x"})
check("points remain non-redeemable in production", st == 403 and body["code"] == "NOT_REDEEMABLE",
      (st, body))
stop(proc2)

if os.path.exists(WAL):
    os.remove(WAL)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CREDIT CHECKS PASSED")
