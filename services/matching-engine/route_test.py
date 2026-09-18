#!/usr/bin/env python3
"""End-to-end test for the non-custodial routing endpoint (GET /v1/route).

Starts its own servers, so this is reproducible from a clean checkout:

    cargo build --release && python3 route_test.py

Covers the three things that decide whether a router is trustworthy rather than merely working:
the fee is disclosed and adds up, the query string is actually covered by the signature (so
nobody can rewrite `amount` in flight), and a deployment with nothing to quote from says so
instead of inventing a price.
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

HOST, PORT = "127.0.0.1", 8097
BIN = "./target/release/ikenga-matching-engine"
OWNER_KEY = "bb" * 32
AGENT = "agent_A82F19"
PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")

failures = []


def check(label, ok, extra=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        if extra:
            print(f"       got: {extra}")
        failures.append(label)


def _pem(seed_hex):
    der = PKCS8_PREFIX + bytes.fromhex(seed_hex)
    b64 = base64.b64encode(der).decode()
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


def get(seed, path, signed_path=None):
    """GET `path`, signing `signed_path` (defaults to path — differ to test tampering)."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    nonce = f"route-{time.time_ns()}"
    headers = {
        "X-Agent-ID": AGENT,
        "X-Timestamp": ts,
        "X-Nonce": nonce,
        "X-Signature": sign(seed, "GET", signed_path or path, ts, nonce),
    }
    conn = http.client.HTTPConnection(HOST, PORT, timeout=10)
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(data)
    except Exception:
        return resp.status, data


def start_server(extra_env, port=PORT):
    env = {
        **os.environ,
        "IKENGA_WAL_PATH": "off",
        "IKENGA_BIND": f"{HOST}:{port}",
        "IKENGA_OWNER_KEY": OWNER_KEY,
        **extra_env,
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


def stop(proc):
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)


# ---------------------------------------------------------------------------------------------
print("=== a deployment with no liquidity sources refuses rather than inventing a price ===")
# An explicit empty string, because leaving it unset now means "use the defaults" rather than
# "use nothing". That default is deliberate: an unconfigured venue with no price sources opens
# markets, takes bets and never resolves any of them, and the symptom only appears a week later.
proc, seeds, lines = start_server({"IKENGA_ROUTE_SOURCES": ""})
seed = seeds.get(AGENT)
if not seed:
    print("failed to start server:\n" + "\n".join(lines))
    sys.exit(1)
check("startup says routing is unconfigured", any("no liquidity sources" in l for l in lines))
status, body = get(seed, "/v1/route?sell=BTC&buy=USD&amount=1.0")
check("route -> 503 with a clear code", status == 503 and body.get("code") == "NO_LIQUIDITY_SOURCES", (status, body))

# The other half of that decision: unset must NOT mean "no sources".
stop(proc)
proc2, _, lines2 = start_server({})
check("an unconfigured venue gets working defaults instead of silence",
      any("routing:" in l and "source(s)" in l for l in lines2),
      [l for l in lines2 if "routing" in l])
check("and the defaults exclude venues that do not serve the US",
      not any("binance" in l or "okx" in l for l in lines2),
      [l for l in lines2 if "routing" in l])
stop(proc2)
proc, seeds, lines = start_server({"IKENGA_ROUTE_SOURCES": ""})
seed = seeds.get(AGENT)
stop(proc)

print("\n=== with two sources, a route is priced, disclosed and metered ===")
proc, seeds, lines = start_server({"IKENGA_ROUTE_DEMO": "1", "IKENGA_ROUTE_SOURCES": ""})
seed = seeds[AGENT]
check("startup warns the demo prices are invented", any("INVENTED" in l for l in lines))

status, r = get(seed, "/v1/route?sell=BTC&buy=USD&amount=1.0&slippage_bps=50")
check("route returns 200", status == 200, (status, r))
check("picked the better of the two venues", abs(r["gross_buy_amount"] - 64_180.0) < 1e-6, r.get("gross_buy_amount"))
check("fee is 10bps of gross", abs(r["fee_amount"] - 64.18) < 1e-6, r.get("fee_amount"))
check(
    "gross == net + fee (the transparency identity)",
    abs(r["gross_buy_amount"] - (r["net_buy_amount"] + r["fee_amount"])) < 1e-9,
)
check("min_buy_amount sits below net, as a real floor", r["min_buy_amount"] < r["net_buy_amount"])
check("both demo sources used", len(r["quotes_used"]) == 2, r.get("quotes_used"))
check("response states plainly that custody is none", "own wallet" in r["custody"])
check("the call was metered for billing", r["billable_calls_this_session"] >= 1)

print("\n=== the query string is covered by the signature ===")
# Sign a 1 BTC quote, send a 1000 BTC one. If the signature only covered the bare path, this
# would succeed and any middlebox could rewrite trade sizes.
status, body = get(
    seed,
    "/v1/route?sell=BTC&buy=USD&amount=1000.0",
    signed_path="/v1/route?sell=BTC&buy=USD&amount=1.0",
)
check("rewriting amount in flight is rejected", status == 401, (status, body))

print("\n=== unsigned callers get a trial quote, not a door in the face ===")
# Deliberate: requiring signup before anyone can see whether the prices are good is a bad way to
# get a first customer, and a quote reveals nothing that isn't already public at the venues being
# quoted. The tradeoff is a per-IP hourly cap — see onboarding_test.py for the full journey.
conn = http.client.HTTPConnection(HOST, PORT, timeout=5)
conn.request("GET", "/v1/route?sell=BTC&buy=USD&amount=1.0")
unsigned = conn.getresponse()
unsigned_body = json.loads(unsigned.read())
conn.close()
check("unsigned request is served", unsigned.status == 200, unsigned.status)
check("and is flagged as a trial", unsigned_body.get("trial") is True, unsigned_body.get("trial"))
check("and is not metered against any agent", unsigned_body.get("billable_calls_this_session") == 0)

print("\n=== malformed requests ===")

status, body = get(seed, "/v1/route?sell=BTC&buy=BTC&amount=1.0")
check("same asset on both sides -> 422 BAD_PAIR", status == 422 and body["code"] == "BAD_PAIR", (status, body))
status, body = get(seed, "/v1/route?sell=BTC&buy=USD&amount=-5")
check("negative amount -> 422", status == 422, (status, body))
status, body = get(seed, "/v1/route?sell=BTC&buy=USD")
check("missing amount -> 400", status == 400, (status, body))
stop(proc)

print("\n=== production refuses to serve invented prices ===")
proc = subprocess.Popen(
    BIN,
    env={
        **os.environ,
        "IKENGA_ENV": "production",
        "IKENGA_OWNER_KEY": OWNER_KEY,
        "IKENGA_WAL_PATH": "/tmp/ikenga-route-prod-test.wal",
        "IKENGA_BIND": f"{HOST}:8096",
        "IKENGA_ROUTE_DEMO": "1",
    },
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)
out, _ = proc.communicate(timeout=15)
check("IKENGA_ROUTE_DEMO=1 is fatal in production", proc.returncode != 0, proc.returncode)
check("and says why", "worse than serving none" in out, out.strip()[:200])

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL ROUTE CHECKS PASSED")
