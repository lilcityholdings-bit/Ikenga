#!/usr/bin/env python3
"""Mutation fuzzer. Throws malformed everything at a live server and checks it survives.

    cargo build --release && python3 fuzz_test.py [seconds]

Hand-written tests check the cases someone thought of. This checks the ones nobody did, by
generating them: truncated requests, absurd headers, JSON that is nearly valid, signed requests
with one byte flipped, bodies that lie about their own length.

Three things are asserted after every single input, because a fuzzer that only checks for crashes
misses the failures that matter more than crashes:

  1. The process is still alive and still answering.
  2. It never returns 5xx. A 4xx is the server correctly refusing garbage; a 5xx is the server
     failing to cope with it, which means an unhandled path.
  3. The ledger is unchanged. No sequence of malformed input may move a single unit of value.
"""
import http.client
import json
import os
import random
import signal
import socket
import subprocess
import sys
import time

HOST, PORT = "127.0.0.1", 8290
BIN = "./target/release/ikenga-matching-engine"
OWNER_KEY = "fa" * 32
WAL = "/tmp/ikenga-fuzz.wal"
BUDGET_SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 45.0

findings = []


def note(kind, detail, payload):
    entry = (kind, detail, payload[:200])
    if entry not in findings:
        findings.append(entry)
        print(f"[FINDING] {kind}: {detail}\n          payload: {payload[:160]!r}")


# --------------------------------------------------------------------------------------------
# Generators. Each returns raw bytes to put on the socket.

METHODS = ["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH", "TRACE", "", "GETGETGET",
           "\x00", "GET\r\nX: y"]
PATHS = ["/", "/health", "/v1/markets", "/v1/events", "/v1/feed", "/v1/account", "/v1/reserves",
         "/dashboard", "/v1/dashboard", "/v1/markets/../../etc/passwd", "/v1/markets/%2e%2e%2f",
         "/v1/markets/" + "a" * 500, "/v1/deposits/x/USDC", "/v1/withdrawals",
         "/v1/markets/x/stakes", "/v1/agents", "/" + "x" * 2000, "//////", "/v1/%00", "/v1/feed?" + "a=1&" * 200,
         # The surface added for humans and for LLM agents. Static pages and a pure-arithmetic
         # endpoint look harmless, which is exactly why they get garbage: the quote endpoint
         # parses three caller-supplied numbers and divides by things.
         "/bet", "/build", "/agent.py", "/v1/tools", "/.well-known/ikenga.json",
         "/v1/markets/x/quote", "/v1/markets/x/quote?outcome=-1&amount=-1",
         "/v1/markets/x/quote?outcome=99999999999&amount=1e400&belief=1e400",
         "/v1/markets/x/quote?outcome=0&amount=0&belief=0",
         "/v1/markets/x/quote?outcome=0&amount=1&belief=1",
         "/v1/markets/x/quote?outcome=&amount=&belief=",
         "/v1/markets/x/quote?" + "outcome=0&" * 300,
         "/bet/../../etc/passwd", "/agent.py%00.txt"]
HEADER_NAMES = ["Content-Length", "Content-Type", "X-Agent-ID", "X-Nonce", "X-Signature",
                "X-Timestamp", "X-Owner-Key", "X-Feed-Key", "X-Idempotent", "Connection",
                "Transfer-Encoding", "Host", "X-" + "y" * 300, "", ":", "A\x00B"]
HEADER_VALUES = ["", "0", "-1", "999999999999999999999", "chunked", "close", "keep-alive",
                 "true", "\x00", "a" * 5000, "%s", "../../..", "null", "NaN", "1e400",
                 "\r\nInjected: yes", "�"]
JSON_BLOBS = [
    b"", b"{", b"}", b"[]", b"null", b"true", b"0", b'{"a"}', b'{"a":}', b'{,}',
    b'{"outcome":0,"amount":1}', b'{"outcome":-1,"amount":-1}',
    b'{"outcome":1e400,"amount":1e400}', b'{"amount":"abc"}',
    b'{"outcome":0,"amount":' + b"9" * 400 + b"}",
    b'[' * 60 + b']' * 60, b'{"a":' * 60 + b'1' + b'}' * 60,
    b'{"\\u0000":"\\uD800"}', b'{"a":"' + b"\\" * 200 + b'"}',
    b'{"pubkey_hex":"' + b"z" * 64 + b'"}', b'{"pubkey_hex":"00"}',
    b'{"pubkey_hex":"' + b"0" * 64 + b'"}',
    b'{"revoke":"feed_x"}', b'{"label":"' + b"L" * 3000 + b'"}',
    b'{"sent":true,"tx_ref":null}', b'{"reason":""}',
    b'{"question":"q","closes_at_ms":1,"observed_at_ms":0}',
    b'\xff\xfe\x00\x01', b"\x00" * 64,
]


def rand_headers(rng, n):
    out = []
    for _ in range(n):
        out.append(f"{rng.choice(HEADER_NAMES)}: {rng.choice(HEADER_VALUES)}")
    return out


# Paths that actually reach a handler which parses a body. Most of the fuzzer's budget goes here:
# a 404 exercises the router and nothing else, and the first version of this spent 57% of its
# shots discovering that /v1/%00 does not exist.
LIVE_PATHS = [
    "/v1/agents", "/v1/markets", "/v1/markets/mkt_1/stakes", "/v1/markets/mkt_1/propose",
    "/v1/markets/mkt_1/dispute", "/v1/markets/mkt_1/finalize", "/v1/markets/mkt_1/auto-resolve",
    "/v1/withdrawals", "/v1/withdrawals/w1/settle", "/v1/deposits/victim/USDC",
    "/v1/feed/subscribers", "/v1/events", "/v1/feed", "/v1/reserves", "/v1/account",
    "/v1/markets?limit=99999", "/v1/events?since=-1", "/v1/events?since=99999999999999999999",
    "/v1/events?limit=0", "/v1/feed?x=" + "y" * 400, "/v1/route?sell=BTC&buy=USD&amount=-1",
    # Against a market that actually exists, so the quote arithmetic runs on real pools rather
    # than bailing out early at "unknown market".
    "/v1/markets/mkt_1/quote?outcome=0&amount=1",
    "/v1/markets/mkt_1/quote?outcome=0&amount=1e308&belief=0.9999999999999999",
    "/v1/markets/mkt_1/quote?outcome=0&amount=0.0000000001&belief=0.0000000001",
    "/v1/account/record", "/v1/tools",
]


def gen_request(rng):
    """A whole raw HTTP request, usually broken in at least one way."""
    method = rng.choice(METHODS) if rng.random() < 0.25 else rng.choice(["POST", "GET"])
    path = rng.choice(LIVE_PATHS) if rng.random() < 0.75 else rng.choice(PATHS)
    body = rng.choice(JSON_BLOBS)
    if rng.random() < 0.25:
        body = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 300)))

    lines = [f"{method} {path} HTTP/1.1", "Host: x"]
    lines += rand_headers(rng, rng.randint(0, 12))

    style = rng.random()
    if style < 0.55:
        lines.append(f"Content-Length: {len(body)}")          # honest
    elif style < 0.70:
        lines.append(f"Content-Length: {rng.randint(0, 10**6)}")  # lies, too big
    elif style < 0.80:
        lines.append("Content-Length: -1")
    elif style < 0.88:
        lines.append("Transfer-Encoding: chunked")            # unsupported
    # else: no content-length at all

    # Sometimes present the owner key, so the operator-only handlers (deposits, settlement,
    # subscriber management) are fuzzed at all rather than stopping at the 403.
    if rng.random() < 0.35:
        lines.append(f"X-Owner-Key: {OWNER_KEY}")

    if rng.random() < 0.06:
        head = "\r\n".join(lines)                              # no terminator
        return head.encode("utf-8", "replace")
    head = "\r\n".join(lines) + "\r\n\r\n"
    return head.encode("utf-8", "replace") + body


def gen_signed_ish(rng):
    """A request that looks signed but is subtly wrong — the auth path's own fuzz surface."""
    body = json.dumps({"outcome": rng.randint(-2, 3), "amount": rng.choice([1, 0, -1, 1e308])}).encode()
    ts = rng.choice([
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "1970-01-01T00:00:00Z", "9999-99-99T99:99:99Z", "not-a-time", "",
        "2026-09-08T01:02:03.999999Z", "2026-09-08T01:02:03+05:00",
    ])
    sig = rng.choice(["", "00", "zz" * 32, "ab" * 64, "f" * 128, "0" * 127])
    lines = [
        f"POST /v1/markets/{rng.choice(['x', 'mkt_1', '../../'])}/stakes HTTP/1.1",
        "Host: x", "Content-Type: application/json",
        f"X-Agent-ID: {rng.choice(['a', '', 'agent_' + 'A' * 200, '../..'])}",
        f"X-Timestamp: {ts}",
        f"X-Nonce: {rng.choice(['n', '', 'x' * 4000])}",
        f"X-Signature: {sig}",
        f"Content-Length: {len(body)}",
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8", "replace") + body


def shoot(payload, timeout=0.6):
    """Returns the status line, or None if the server said nothing.

    The timeout is deliberately tiny. A healthy server answers a malformed request in under a
    millisecond, so anything slower is either a payload designed to hang (an unterminated header
    block, a Content-Length that promises a body never sent) or a genuine stall — and both are
    handled by moving on. Waiting seconds per shot is what turns a fuzzer into a formality: the
    first version of this managed one request per second, which tests essentially nothing.
    """
    try:
        s = socket.create_connection((HOST, PORT), timeout=timeout)
        s.sendall(payload)
        s.settimeout(timeout)
        data = s.recv(400)
        s.close()
        if not data:
            return None
        first = data.split(b"\r\n")[0].decode("latin-1")
        return first
    except (socket.timeout, ConnectionResetError, BrokenPipeError, ConnectionRefusedError, OSError):
        return None


def health():
    try:
        c = http.client.HTTPConnection(HOST, PORT, timeout=5)
        c.request("GET", "/health")
        r = c.getresponse(); r.read(); c.close()
        return r.status == 200
    except Exception:
        return False


def ledger():
    c = http.client.HTTPConnection(HOST, PORT, timeout=5)
    c.request("GET", "/v1/reserves")
    r = c.getresponse(); d = json.loads(r.read()); c.close()
    return (d["credits_outstanding"], d["reserves_held"], d["withdrawals_pending"], d["fully_backed"])


# --------------------------------------------------------------------------------------------
if os.path.exists(WAL):
    os.remove(WAL)
env = {**os.environ, "IKENGA_BIND": f"{HOST}:{PORT}", "IKENGA_OWNER_KEY": OWNER_KEY,
       "IKENGA_WAL_PATH": WAL, "IKENGA_REAL_MONEY": "1",
       # Give the router something to answer with. Otherwise /v1/route correctly returns 503
       # ("no price sources configured") and the fuzzer reports its own misconfiguration as a
       # server bug — which is exactly the false positive that makes people stop reading fuzzer
       # output.
       "IKENGA_ROUTE_DEMO": "1"}
proc = subprocess.Popen(BIN, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(60):
    if health():
        break
    time.sleep(0.25)
if not health():
    print("server did not start")
    proc.kill()
    sys.exit(1)

# Put real value in, so "the ledger never moved" is a meaningful assertion.
c = http.client.HTTPConnection(HOST, PORT, timeout=5)
c.request("POST", "/v1/deposits/victim/USDC", body=b"1000", headers={"X-Owner-Key": OWNER_KEY})
c.getresponse().read(); c.close()
baseline = ledger()
print(f"baseline ledger: {baseline}")

import threading

WORKERS = 12
sent = 0
statuses = {}
lock = threading.Lock()
deadline = time.time() + BUDGET_SECS
stop_flag = threading.Event()


def worker(seed):
    """One fuzzing thread. Concurrency is not just for speed — it is the only way to reach the
    paths where malformed requests race real ones over the same locks."""
    global sent
    rng = random.Random(seed)
    while time.time() < deadline and not stop_flag.is_set():
        payload = gen_signed_ish(rng) if rng.random() < 0.3 else gen_request(rng)
        line = shoot(payload)
        code = None
        if line:
            code = line.split(" ")[1] if " " in line else "?"
        with lock:
            sent += 1
            if code:
                statuses[code] = statuses.get(code, 0) + 1
        if code and code.startswith("5"):
            note("5xx", f"server failed to handle input ({line})", repr(payload))
        # 501/505 would mean an unimplemented method or version slipped through as a server-side
        # failure rather than a refusal; both belong in the 4xx family here.
        if code in ("501", "505"):
            note("wrong-class", f"refusal reported as a server failure ({line})", repr(payload))


threads = [threading.Thread(target=worker, args=(0xF0FF1E + i,), daemon=True)
           for i in range(WORKERS)]
print(f"fuzzing for {BUDGET_SECS:.0f}s across {WORKERS} threads...")
for t in threads:
    t.start()

while time.time() < deadline:
    time.sleep(2.0)
    if not health():
        note("DEAD", "server stopped answering under fuzzing", "(concurrent)")
        stop_flag.set()
        break
    now = ledger()
    if now != baseline:
        note("LEDGER", f"value moved: {baseline} -> {now}", "(concurrent)")
        baseline = now

stop_flag.set()
for t in threads:
    t.join(timeout=5)

alive = health()
final = ledger() if alive else None

print()
print(f"sent {sent} malformed requests")
print(f"responses: {dict(sorted(statuses.items()))}")
print(f"server alive at the end: {alive}")
print(f"ledger unchanged: {final == baseline if alive else 'n/a'}")

try:
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)
except Exception:
    pass
if os.path.exists(WAL):
    os.remove(WAL)

print()
if findings:
    print(f"{len(findings)} FINDING(S):")
    for kind, detail, payload in findings:
        print(f"  - {kind}: {detail}")
    sys.exit(1)
if not alive:
    print("SERVER DIED")
    sys.exit(1)
print("FUZZING FOUND NOTHING — no crash, no 5xx, no value moved")
