#!/usr/bin/env python3
"""Crash-recovery test: kill -9 a running server and prove nothing was lost.

This is the test that decides whether the service can go on the internet. Unit tests can show
the WAL round-trips records; only killing a real process mid-life shows the engine actually
comes back.

Deliberately uses SIGKILL, not a graceful stop. A clean shutdown proves nothing — the failure
you actually get in production is the process dying without warning: OOM killer, hardware fault,
a platform yanking the container. If state survives kill -9, it survives the polite cases too.

Usage:  python3 crash_test.py            (starts and kills servers itself)
"""
import http.client
import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HOST, PORT = "127.0.0.1", 8099
BIN = "./target/release/ikenga-matching-engine"
WAL_DIR = "/tmp/ikenga-crash-test"
WAL = f"{WAL_DIR}/test.wal"
OWNER_KEY = "aa" * 32

failures = []

# Fixed ASN.1 prefix for an Ed25519 PKCS#8 private key wrapping a 32-byte seed (RFC 8410) — same
# constant used in src/ed25519.rs.
_PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")


def check(label, ok):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        failures.append(label)


def _seed_to_pem(seed_hex):
    der = _PKCS8_PREFIX + bytes.fromhex(seed_hex)
    b64 = base64.b64encode(der).decode()
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return ("-----BEGIN PRIVATE KEY-----\n" + "\n".join(lines) + "\n-----END PRIVATE KEY-----\n").encode()


def sign(seed_hex, method, path, ts, nonce, body):
    payload = method.encode() + path.encode() + ts.encode() + nonce.encode() + body
    with tempfile.NamedTemporaryFile() as key_f, tempfile.NamedTemporaryFile() as msg_f, tempfile.NamedTemporaryFile() as sig_f:
        key_f.write(_seed_to_pem(seed_hex)); key_f.flush()
        msg_f.write(payload); msg_f.flush()
        subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-inkey", key_f.name, "-rawin", "-in", msg_f.name, "-out", sig_f.name],
            check=True, capture_output=True,
        )
        return sig_f.read().hex()


def request(method, path, agent=None, secret=None, body=None, owner=False):
    raw = json.dumps(body).encode() if body is not None else b""
    headers = {"Content-Type": "application/json"}
    if agent:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        nonce = f"crash-{time.time_ns()}"
        headers.update({
            "X-Agent-ID": agent,
            "X-Timestamp": ts,
            "X-Nonce": nonce,
            "X-Signature": sign(secret, method, path, ts, nonce, raw),
        })
    if owner:
        headers["X-Owner-Key"] = OWNER_KEY
    conn = http.client.HTTPConnection(HOST, PORT, timeout=5)
    conn.request(method, path, body=raw, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(data)
    except Exception:
        return resp.status, data


def start_server():
    env = {
        **os.environ,
        "IKENGA_WAL_PATH": WAL,
        "IKENGA_BIND": f"{HOST}:{PORT}",
        "IKENGA_OWNER_KEY": OWNER_KEY,
        "IKENGA_WAL_SYNC": "always",
        # This suite exercises the order book specifically, which is off unless asked for.
        "IKENGA_ENABLE_ORDERBOOK": "1",
    }
    proc = subprocess.Popen(BIN, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    seeds, lines = {}, []
    deadline = time.time() + 15
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        lines.append(line.rstrip())
        if "private_key_seed_hex=" in line:
            agent = line.split()[2].rstrip(":")
            seeds[agent] = line.strip().split("private_key_seed_hex=")[1].split()[0]
        if "listening on" in line:
            break
    return proc, seeds, lines


def hard_kill(proc):
    """SIGKILL — no cleanup, no flush, no chance to be tidy."""
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)


# --------------------------------------------------------------------------------------------
shutil.rmtree(WAL_DIR, ignore_errors=True)
os.makedirs(WAL_DIR, exist_ok=True)

print("=== boot 1: fresh deployment ===")
proc, seeds, lines = start_server()
if not seeds:
    print("failed to start server:")
    print("\n".join(lines))
    sys.exit(1)
agent_a, agent_b = "agent_A82F19", "agent_B10042"
seed_a, seed_b = seeds[agent_a], seeds[agent_b]
check("server started with demo agents", len(seeds) == 2)

status, health = request("GET", "/health")
check("health reports durable", status == 200 and health["durable"] is True)

print("\n=== create state worth losing ===")
status, _ = request("POST", f"/dev/faucet/{agent_a}/USD", body=None, owner=True)
# faucet takes a raw number body, not JSON
conn = http.client.HTTPConnection(HOST, PORT, timeout=5)
conn.request("POST", f"/dev/faucet/{agent_a}/USD", body=b"250000", headers={"X-Owner-Key": OWNER_KEY})
conn.getresponse().read()
conn.close()

# A resting order that must still be on the book after the crash.
status, resting = request("POST", "/v1/orders", agent_b, seed_b, {
    "symbol": "BTC-USD", "side": "Sell", "order_type": "Limit", "price": 70000.0, "qty": 2.0,
})
check("resting sell accepted", status == 200 and resting["status"] == "Open")
resting_id = resting["order_id"]

# A trade that must still have moved balances and fees after the crash.
status, filled = request("POST", "/v1/orders", agent_a, seed_a, {
    "symbol": "BTC-USD", "side": "Buy", "order_type": "Limit", "price": 70000.0, "qty": 0.5,
})
check("crossing buy filled", status == 200 and filled["status"] == "Filled")

status, acct_before = request("GET", "/v1/account", agent_a, seed_a)
status, treas_before = request("GET", "/v1/treasury", owner=True)
status, quote_before = request("GET", "/v1/quote?symbol=BTC-USD")
bal_before = {b["asset"]: b["balance"] for b in acct_before["balances"]}
fees_before = {r["asset"]: r["pending_amount"] for r in treas_before["pending_fees_by_asset"]}
print(f"    balances before crash: {bal_before}")
print(f"    fees before crash:     {fees_before}")
print(f"    best ask before crash: {quote_before['best_ask']}")

print("\n=== kill -9 (no flush, no cleanup) ===")
hard_kill(proc)
check("process is dead", proc.poll() is not None)

print("\n=== boot 2: recovery ===")
proc2, seeds2, lines2 = start_server()
recovered_line = next((l for l in lines2 if "recovered" in l), "")
print(f"    {recovered_line}")

check("agent seeds survived (no new seeds printed)", len(seeds2) == 0)
check("recovery was reported at startup", "recovered" in recovered_line)

# Same credentials must still authenticate — this is what breaks clients if it regresses.
status, acct_after = request("GET", "/v1/account", agent_a, seed_a)
check("pre-crash credentials still authenticate", status == 200)

bal_after = {b["asset"]: b["balance"] for b in acct_after["balances"]}
print(f"    balances after recovery: {bal_after}")
for asset, before in bal_before.items():
    after = bal_after.get(asset, 0)
    check(f"{asset} balance restored exactly ({before} -> {after})", abs(before - after) < 1e-9)

status, treas_after = request("GET", "/v1/treasury", owner=True)
fees_after = {r["asset"]: r["pending_amount"] for r in treas_after["pending_fees_by_asset"]}
for asset, before in fees_before.items():
    after = fees_after.get(asset, 0)
    check(f"{asset} protocol fees restored ({before} -> {after})", abs(before - after) < 1e-9)

status, quote_after = request("GET", "/v1/quote?symbol=BTC-USD")
check(
    f"resting order still on the book (ask {quote_after['best_ask']})",
    quote_after["best_ask"] == quote_before["best_ask"],
)

# The partially-filled resting order must come back with its remaining size, not its full size.
status, acct_b = request("GET", "/v1/account", agent_b, seed_b)
open_ids = [o["order_id"] for o in acct_b["open_orders"]]
check("the resting order is still open for its owner", resting_id in open_ids)
remaining = next((o for o in acct_b["open_orders"] if o["order_id"] == resting_id), None)
check(
    f"partial fill preserved (filled {remaining['filled_qty'] if remaining else '?'} of 2.0)",
    remaining is not None and abs(remaining["filled_qty"] - 0.5) < 1e-9,
)

# And it must still be tradeable, not just visible.
status, second = request("POST", "/v1/orders", agent_a, seed_a, {
    "symbol": "BTC-USD", "side": "Buy", "order_type": "Limit", "price": 70000.0, "qty": 0.25,
})
check("recovered liquidity is actually tradeable", status == 200 and second["status"] == "Filled")

hard_kill(proc2)
shutil.rmtree(WAL_DIR, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CRASH-RECOVERY CHECKS PASSED")
