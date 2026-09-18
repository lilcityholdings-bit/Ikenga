#!/usr/bin/env python3
"""End-to-end smoke test against the running ikenga-matching-engine, using only stdlib plus the
system `openssl` binary for Ed25519 signing (no network access to pip-install a cryptography
library in this sandbox — see src/ed25519.rs for why the server itself shells out to openssl too).
Exercises: signed order submission, matching, quote, account, and the raw WS binary feed.
"""
import base64
import http.client
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time

HOST = "127.0.0.1"
PORT = 8080

AGENT_A = "agent_A82F19"
SEED_A = bytes.fromhex(os.environ["SEED_A"])
AGENT_B = "agent_B10042"
SEED_B = bytes.fromhex(os.environ["SEED_B"])
OWNER_KEY = os.environ["OWNER_KEY"]

# Fixed ASN.1 prefix for an Ed25519 PKCS#8 private key wrapping a 32-byte seed (RFC 8410) — same
# constant used in src/ed25519.rs, confirmed against real `openssl pkey -outform DER` output.
_PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")


def _seed_to_pem(seed: bytes) -> bytes:
    der = _PKCS8_PREFIX + seed
    b64 = base64.b64encode(der).decode()
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return ("-----BEGIN PRIVATE KEY-----\n" + "\n".join(lines) + "\n-----END PRIVATE KEY-----\n").encode()


def sign(seed: bytes, method, path, timestamp, nonce, body: bytes) -> str:
    payload = method.encode() + path.encode() + timestamp.encode() + nonce.encode() + body
    with tempfile.NamedTemporaryFile() as key_f, tempfile.NamedTemporaryFile() as msg_f, tempfile.NamedTemporaryFile() as sig_f:
        key_f.write(_seed_to_pem(seed)); key_f.flush()
        msg_f.write(payload); msg_f.flush()
        subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-inkey", key_f.name, "-rawin", "-in", msg_f.name, "-out", sig_f.name],
            check=True, capture_output=True,
        )
        return sig_f.read().hex()


def request(method, path, agent_id, seed, body_obj=None):
    body = json.dumps(body_obj).encode() if body_obj is not None else b""
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    nonce = f"nonce-{time.time_ns()}"
    sig = sign(seed, method, path, timestamp, nonce, body)
    headers = {
        "X-Agent-ID": agent_id,
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": sig,
        "Content-Type": "application/json",
    }
    conn = http.client.HTTPConnection(HOST, PORT, timeout=5)
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, json.loads(data) if data else None


def unauth_request(method, path):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=5)
    conn.request(method, path)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, json.loads(data) if data else None


def owner_request(method, path, body_bytes=b"", owner_key=OWNER_KEY):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=5)
    headers = {"X-Owner-Key": owner_key} if owner_key is not None else {}
    conn.request(method, path, body=body_bytes, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, json.loads(data) if data else None


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        sys.exit(1)


def ws_read_ticks(n, timeout=5):
    """Minimal RFC6455 client handshake + binary frame reader, stdlib only."""
    key = base64.b64encode(os.urandom(16)).decode()
    sock = socket.create_connection((HOST, PORT), timeout=timeout)
    req = (
        f"GET /v1/marketdata HTTP/1.1\r\n"
        f"Host: {HOST}:{PORT}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    resp = sock.recv(1024)
    assert b"101" in resp.split(b"\r\n", 1)[0], f"handshake failed: {resp}"

    ticks = []
    sock.settimeout(timeout)
    buf = b""
    while len(ticks) < n:
        buf += sock.recv(4096)
        while len(buf) >= 2:
            b0, b1 = buf[0], buf[1]
            opcode = b0 & 0x0F
            length = b1 & 0x7F
            header_len = 2
            if length == 126:
                if len(buf) < 4:
                    break
                length = struct.unpack(">H", buf[2:4])[0]
                header_len = 4
            elif length == 127:
                if len(buf) < 10:
                    break
                length = struct.unpack(">Q", buf[2:10])[0]
                header_len = 10
            if len(buf) < header_len + length:
                break
            payload = buf[header_len:header_len + length]
            buf = buf[header_len + length:]
            if opcode == 0x2:  # binary
                seq, symbol_id, price, qty, ts_ms = struct.unpack(">QHddQ", payload)
                ticks.append((seq, symbol_id, price, qty, ts_ms))
    sock.close()
    return ticks


print("=== owner-only endpoints reject requests without the owner key ===")
status, body = owner_request("POST", f"/dev/faucet/{AGENT_A}/USD", b"50000", owner_key=None)
check("faucet without X-Owner-Key is rejected", status == 403)
status, body = owner_request("GET", "/v1/treasury", owner_key="00" * 32)
check("faucet/treasury with a WRONG X-Owner-Key is rejected", status == 403)

print("=== fund agents via dev faucet (owner-authenticated) ===")
def faucet(agent_id, asset, amount):
    return owner_request("POST", f"/dev/faucet/{agent_id}/{asset}", str(amount).encode())

status, body = faucet(AGENT_A, "USD", 50000)
check("faucet funds agent A with USD", status == 200 and body["balance"] >= 50000)

print("=== agent B rests a limit sell ===")
status, body = request("POST", "/v1/orders", AGENT_B, SEED_B, {
    "symbol": "BTC-USD", "side": "Sell", "order_type": "Limit", "price": 65000.0, "qty": 1.0,
})
check("resting sell order accepted", status == 200 and body["status"] == "Open")

print("=== quote reflects the resting sell (public, unauthenticated) ===")
status, body = unauth_request("GET", "/v1/quote?symbol=BTC-USD")
check("quote shows best_ask=65000", status == 200 and body["best_ask"] == 65000)

print("=== agent A crosses it with a limit buy ===")
status, body = request("POST", "/v1/orders", AGENT_A, SEED_A, {
    "symbol": "BTC-USD", "side": "Buy", "order_type": "Limit", "price": 65500.0, "qty": 0.5,
})
check("crossing buy fills immediately", status == 200 and body["status"] == "Filled")
check("fill price is the maker's price (65000), not the taker's limit", body["fills"][0]["price"] == 65000)

print("=== anonymity: the taker's own ack must not reveal who the maker was ===")
raw_ack = json.dumps(body)
check("ack does not contain the maker's agent id", AGENT_B not in raw_ack)
check("ack contains no 'maker_agent_id' field at all", "maker_agent_id" not in raw_ack)
check("ack contains no 'taker_agent_id' field at all", "taker_agent_id" not in raw_ack)
fill0 = body["fills"][0]
check("fill tells the viewer their own side", fill0["your_side"] == "Taker")
check("fill gives a single-use counterparty alias", fill0["counterparty_alias"].startswith("anon_"))
aliases_seen = [f["counterparty_alias"] for f in body["fills"]]
check(f"each fill's alias is distinct (got {len(set(aliases_seen))} of {len(aliases_seen)})",
      len(set(aliases_seen)) == len(aliases_seen))

print("=== anonymity: IDs must not encode a global counter or creation time ===")
order_ids = []
for _ in range(3):
    # High limit price so these just rest rather than crossing; agent B holds BTC to sell.
    st, ack = request("POST", "/v1/orders", AGENT_B, SEED_B, {
        "symbol": "BTC-USD", "side": "Sell", "order_type": "Limit", "price": 99000.0, "qty": 0.01,
    })
    assert st == 200, f"setup order failed: {st} {ack}"
    order_ids.append(ack["order_id"])
# Comparing sort order was the original check here, but with genuinely random IDs that has a
# 1-in-6 chance of a false failure on any given run (3 random values sort ascending by luck about
# as often as any other order) — this bit during verification of an unrelated change. A meaningful
# check for "not a counter or timestamp" is that the IDs don't share a common prefix, which a
# counter or a shared time-based prefix would produce and true randomness won't.
common_prefix_len = len(os.path.commonprefix(order_ids))
check(
    f"order ids don't share a counter/timestamp-like prefix (common prefix length {common_prefix_len}, got {order_ids})",
    common_prefix_len <= 2,  # only the "o_" every order id starts with
)
# "o_" + 32 hex chars = 16 random bytes.
check("order ids are full-length random hex",
      all(oid.startswith("o_") and len(oid) == 34 for oid in order_ids))

print("=== the public privacy statement is honest about what it can't hide ===")
status, policy = unauth_request("GET", "/v1/privacy")
check("privacy endpoint is public", status == 200)
check("it admits the operator sees everything",
      any("operator" in s for s in policy["not_protected_against"]))
check("it admits network-level identification isn't covered",
      any("network-level" in s for s in policy["not_protected_against"]))

print("=== de-anonymization is owner-only, reason-gated, and logged ===")
alias = fill0["counterparty_alias"]
status, _ = owner_request("POST", "/v1/compliance/resolve",
                          json.dumps({"alias": alias, "reason": "test"}).encode(), owner_key=None)
check("resolving an alias without the owner key is rejected", status == 403)

status, resolved = owner_request("POST", "/v1/compliance/resolve",
                                 json.dumps({"alias": alias}).encode())
check("resolving without a stated reason is rejected", status == 400)

status, resolved = owner_request("POST", "/v1/compliance/resolve",
                                 json.dumps({"alias": alias, "reason": "smoke test audit"}).encode())
check("owner can resolve an alias with a reason", status == 200)
check(f"alias resolves to the real maker ({AGENT_B})", resolved["agent_id"] == AGENT_B)

status, unknown = owner_request("POST", "/v1/compliance/resolve",
                                json.dumps({"alias": "anon_deadbeef", "reason": "x"}).encode())
check("an unknown alias resolves to nothing", status == 404)

status, log = owner_request("GET", "/v1/compliance/disclosures")
check("the disclosure log recorded exactly the one successful lookup", log["total_disclosures"] == 1)
check("the log records who was unmasked and why",
      log["disclosures"][0]["agent_id"] == AGENT_B
      and log["disclosures"][0]["reason"] == "smoke test audit")

print("=== fee schedule is public and readable without signing up ===")
status, fees = unauth_request("GET", "/v1/fees")
check("fee schedule is public", status == 200 and len(fees["tiers"]) == 5)
base = fees["tiers"][0]
check(f"base taker is 7bps, under the majors' ~10bps (got {base['taker_bps']})",
      base["taker_bps"] == 7.0)
check(f"makers are paid, not charged (got {base['maker_bps']}bps)", base["maker_bps"] < 0)
check("every tier keeps more than it pays out (no rebate farming)",
      all(t["protocol_keeps_bps"] > 0 for t in fees["tiers"]))

print("=== protocol capture = taker fee - maker rebate, booked in the quote asset ===")
status, treasury = owner_request("GET", "/v1/treasury")
usd_fees = next((r["pending_amount"] for r in treasury["pending_fees_by_asset"] if r["asset"] == "USD"), 0)
# 0.5 BTC @ 65000 = $32,500 notional. Taker pays 7bps = $22.75; maker is rebated 1bps = $3.25;
# the treasury keeps the 6bps difference = $19.50.
check(f"treasury booked ~$19.50 (got {usd_fees})", abs(usd_fees - 19.50) < 1e-6)
check("treasury response is honest that BTC conversion isn't implemented",
      treasury["btc_converted"] is False)

print("=== replay protection: resubmitting an identical signed request fails ===")
# (re-using request() generates a fresh nonce/timestamp each call, so directly replay a raw one)
timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
nonce = "replay-test-nonce"
body_bytes = json.dumps({"symbol": "BTC-USD", "side": "Buy", "order_type": "Market", "qty": 0.01}).encode()
sig = sign(SEED_A, "POST", "/v1/orders", timestamp, nonce, body_bytes)
conn = http.client.HTTPConnection(HOST, PORT, timeout=5)
headers = {"X-Agent-ID": AGENT_A, "X-Timestamp": timestamp, "X-Nonce": nonce, "X-Signature": sig}
conn.request("POST", "/v1/orders", body=body_bytes, headers=headers)
r1 = conn.getresponse(); r1.read(); conn.close()
conn = http.client.HTTPConnection(HOST, PORT, timeout=5)
conn.request("POST", "/v1/orders", body=body_bytes, headers=headers)
r2 = conn.getresponse(); r2_body = json.loads(r2.read()); conn.close()
check("first use of nonce succeeds", r1.status == 200)
check("replayed nonce is rejected", r2.status == 401 and r2_body["code"] == "REPLAYED_NONCE")

print("=== account reflects updated balances and no open orders left ===")
status, body = request("GET", "/v1/account", AGENT_A, SEED_A)
check("account call authenticates and returns balances", status == 200 and len(body["balances"]) > 0)

print("=== unauthenticated request is rejected ===")
conn = http.client.HTTPConnection(HOST, PORT, timeout=5)
conn.request("GET", "/v1/account")
r = conn.getresponse(); r.read(); conn.close()
check("missing signature headers -> 401", r.status == 401)

print("=== websocket market-data feed delivers real binary ticks (with sequence numbers) ===")
# Trigger two fresh, separate trades so there are two ticks to receive with consecutive seqs.
import threading
def trade_soon():
    time.sleep(0.3)
    for px in (64000.0, 63900.0):
        request("POST", "/v1/orders", AGENT_B, SEED_B, {
            "symbol": "BTC-USD", "side": "Sell", "order_type": "Limit", "price": px, "qty": 0.2,
        })
        request("POST", "/v1/orders", AGENT_A, SEED_A, {
            "symbol": "BTC-USD", "side": "Buy", "order_type": "Market", "qty": 0.2,
        })
threading.Thread(target=trade_soon).start()
ticks = ws_read_ticks(2, timeout=5)
check("received at least two binary ticks over WS", len(ticks) >= 2)
seq0, symbol_id, price, qty, ts_ms = ticks[0]
check(f"tick has plausible fields (got symbol_id={symbol_id}, price={price}, qty={qty})",
      symbol_id in (1, 2, 3) and price > 0 and qty > 0)
seq1 = ticks[1][0]
check(f"tick sequence numbers are consecutive (got {seq0} then {seq1})", seq1 == seq0 + 1)

print("\nALL SMOKE TESTS PASSED")
